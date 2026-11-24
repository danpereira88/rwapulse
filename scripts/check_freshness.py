"""
Daily freshness check + Supabase keep-alive.

Queries DuckDB for the most recent data dates across key tables, computes
staleness, and upserts a row to the Supabase pipeline_health table.

That upsert is the keep-alive: it's a real Postgres WRITE that resets the
Supabase free-tier 7-day inactivity pause timer.

Thresholds:
    ok      — staleness ≤ 9 days (next weekly run is due within 2 days)
    warning — staleness 10–14 days (weekly run is overdue; alert)
    error   — staleness > 14 days (critically stale; alert)

Email alerts are sent if GMAIL_SMTP_USER and GMAIL_SMTP_APP_PASSWORD are set
in .env. Alerts are skipped (but logged) if SMTP is not configured.

Usage:
    python scripts/check_freshness.py           # normal daily run
    python scripts/check_freshness.py --dry-run # compute but don't write to Supabase
"""

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import duckdb
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("check_freshness")

# ── Configuration ──────────────────────────────────────────────────────────────

DB_PATH  = ROOT / "data" / "rwa.duckdb"
SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

GMAIL_USER     = os.getenv("GMAIL_SMTP_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_SMTP_APP_PASSWORD", "")
ALERT_TO       = os.getenv("ALERT_EMAIL_TO", GMAIL_USER)  # default: send to self

# Staleness thresholds (days since last DuckDB update)
THRESHOLD_WARNING = 9   # pipeline is overdue; alert
THRESHOLD_ERROR   = 14  # critically stale; urgent alert

RUN_TYPE = "daily_freshness"


# ── DuckDB freshness queries ───────────────────────────────────────────────────

def _query_freshness() -> dict:
    """
    Return a dict of {source_name: max_date_str} for key DuckDB tables.
    Uses the most conservative (oldest) date as the overall staleness anchor.
    """
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        results = {}

        rows = {
            "dl_protocol_tvl":       "SELECT MAX(date) FROM dl_protocol_tvl",
            "dl_stablecoin_history": "SELECT MAX(date) FROM dl_stablecoin_history",
            "cg_rwa_tokens":         "SELECT MAX(snapshot_date) FROM cg_rwa_tokens WHERE snapshot_date != 'pre-append'",
        }

        for name, sql in rows.items():
            val = con.execute(sql).fetchone()[0]
            results[name] = str(val) if val else None

        return results
    finally:
        con.close()


def _compute_staleness(freshness: dict) -> tuple[int, str]:
    """
    Return (staleness_days, most_stale_source).
    staleness_days = days since the oldest source was updated.
    """
    today = date.today()
    worst_days = 0
    worst_source = "unknown"

    for source, max_date_str in freshness.items():
        if not max_date_str:
            continue
        try:
            max_date = date.fromisoformat(max_date_str)
            days = (today - max_date).days
            if days > worst_days:
                worst_days = days
                worst_source = source
        except ValueError:
            pass

    return worst_days, worst_source


# ── Supabase keep-alive write ──────────────────────────────────────────────────

def _write_health(status: str, details: dict, dry_run: bool = False) -> bool:
    """
    Upsert a row to pipeline_health in Supabase.

    This is the keep-alive: a real Postgres WRITE that resets the 7-day
    free-tier inactivity timer.

    Returns True on success, False on failure.
    """
    if dry_run:
        log.info("[dry-run] Would upsert pipeline_health: run_type=%s status=%s", RUN_TYPE, status)
        log.info("[dry-run] details: %s", json.dumps(details, indent=2))
        return True

    if not SUPA_URL or not SUPA_KEY:
        log.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — cannot write pipeline_health")
        return False

    payload = [{
        "run_type": RUN_TYPE,
        "run_at":   datetime.now(timezone.utc).isoformat(),
        "status":   status,
        "details":  details,
    }]

    try:
        resp = requests.post(
            f"{SUPA_URL}/rest/v1/pipeline_health",
            data=json.dumps(payload),
            headers={
                "apikey":        SUPA_KEY,
                "Authorization": f"Bearer {SUPA_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
            timeout=15,
        )
        if resp.ok:
            log.info("pipeline_health upserted: run_type=%s status=%s (keep-alive written)", RUN_TYPE, status)
            return True
        else:
            log.error("pipeline_health upsert failed [%d]: %s", resp.status_code, resp.text[:300])
            return False
    except Exception as e:
        log.error("pipeline_health upsert error: %s", e)
        return False


# ── Email alert ────────────────────────────────────────────────────────────────

def _send_alert(staleness_days: int, worst_source: str, status: str, details: dict):
    """
    Send a Gmail SMTP alert. Skipped silently if SMTP is not configured.
    """
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.warning("SMTP not configured (GMAIL_SMTP_USER / GMAIL_SMTP_APP_PASSWORD not set) "
                    "— alert not sent. Set these in .env to enable email alerts.")
        return

    subject = f"[RWA Pulse] Pipeline {status.upper()}: data {staleness_days}d stale"
    body_lines = [
        f"RWA Pulse freshness check — status: {status.upper()}",
        "",
        f"  Most stale source : {worst_source}",
        f"  Staleness          : {staleness_days} days",
        f"  Warning threshold  : {THRESHOLD_WARNING} days",
        f"  Error threshold    : {THRESHOLD_ERROR} days",
        "",
        "Source breakdown:",
    ]
    for src, max_date in details.get("sources", {}).items():
        body_lines.append(f"  {src:<35} last updated: {max_date or 'UNKNOWN'}")

    body_lines += [
        "",
        "Action required: run the weekly pipeline manually or check for errors.",
        "  cd ~/rwa-data-exploration && source .venv/bin/activate",
        "  python run_all_with_sync.py",
        "  python run_enrichment.py",
    ]
    body = "\n".join(body_lines)

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_TO

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, ALERT_TO, msg.as_string())

        log.info("Alert email sent to %s", ALERT_TO)
    except Exception as e:
        log.error("Failed to send alert email: %s", e)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily freshness check + Supabase keep-alive")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute staleness but don't write to Supabase or send alerts")
    args = parser.parse_args()

    log.info("=== RWA Pulse freshness check ===")
    today = date.today().isoformat()
    log.info("Date: %s", today)

    # ── Query DuckDB ──────────────────────────────────────────────────────────
    try:
        freshness = _query_freshness()
    except Exception as e:
        log.error("DuckDB freshness query failed: %s", e)
        _write_health("error", {"error": str(e), "stage": "duckdb_query"}, dry_run=args.dry_run)
        sys.exit(1)

    log.info("DuckDB max dates:")
    for src, max_date in freshness.items():
        log.info("  %-35s %s", src, max_date or "NULL")

    # ── Compute staleness ─────────────────────────────────────────────────────
    staleness_days, worst_source = _compute_staleness(freshness)
    log.info("Staleness: %d days (worst: %s)", staleness_days, worst_source)

    if staleness_days <= THRESHOLD_WARNING:
        status = "ok"
    elif staleness_days <= THRESHOLD_ERROR:
        status = "warning"
    else:
        status = "error"

    log.info("Status: %s", status)

    details = {
        "check_date":     today,
        "staleness_days": staleness_days,
        "worst_source":   worst_source,
        "threshold_warn": THRESHOLD_WARNING,
        "threshold_error": THRESHOLD_ERROR,
        "sources":        freshness,
    }

    # ── Write to Supabase (keep-alive) ────────────────────────────────────────
    write_ok = _write_health(status, details, dry_run=args.dry_run)

    if not write_ok and not args.dry_run:
        log.error("pipeline_health write failed — keep-alive may not have reached Supabase")

    # ── Alert if stale ────────────────────────────────────────────────────────
    if status != "ok" and not args.dry_run:
        log.warning("Data is stale (%d days) — sending alert", staleness_days)
        _send_alert(staleness_days, worst_source, status, details)
    elif status != "ok" and args.dry_run:
        log.info("[dry-run] Would send %s alert for %d-day staleness", status, staleness_days)

    log.info("=== Freshness check complete ===")
    sys.exit(0 if status == "ok" else 1)


if __name__ == "__main__":
    main()
