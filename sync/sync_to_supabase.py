"""
DuckDB → Supabase sync.

One-way, idempotent, fail-loud.
Uses the Supabase REST API (PostgREST) — no direct Postgres connection needed.

Usage:
    python -m sync.sync_to_supabase          # sync all tables
    python -m sync.sync_to_supabase --table protocol_tvl

Tables synced:
    dashboard_protocol_daily_tvl   ← dl_protocol_tvl + dl_protocols
    dashboard_protocol_metadata    ← dl_protocols (latest snapshot)
    dashboard_token_snapshots      ← cg_rwa_tokens (real snapshots)
    dashboard_stablecoin_history   ← dl_stablecoin_history

Exclusions applied throughout:
    - sky-rwa          (governance wind-down)
    - blockchain-capital (static flat reporting)
    - ondo-global-markets (tokenised stocks)
    - snapshot_date = 'pre-append' (unknown vintage)
    - date 2026-05-14 in dl_protocol_tvl (duplicate ingestion day)
    - dl_protocol_tvl rows before 2023-01-01

Commodities (gold tokens) ARE included — tagged via category so the
dashboard can filter them. They're needed for the gold sidebar.

Prerequisites:
    Tables must exist in Supabase before the first sync.
    Run sync/schema.sql in the Supabase SQL Editor if tables are missing:
      Dashboard → SQL Editor → New Query → paste schema.sql → Run
"""

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import requests
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

DB_PATH  = ROOT / "data" / "rwa.duckdb"
SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Protocols excluded from all sync operations
EXCLUDED_SLUGS = frozenset({
    "sky-rwa",             # governance wind-down; distorts concentration metrics
    "blockchain-capital",  # flat $965M every day since Sep 2025 — static placeholder
    "ondo-global-markets", # tokenised stocks; structurally distinct from yield-bearing RWA
})

TVL_START_DATE   = "2023-01-01"
TVL_EXCLUDE_DATE = "2026-05-14"   # duplicate ingestion artifact — exclude this specific date

BATCH_SIZE = 500  # rows per REST batch (safe for Supabase's 1 MB request limit)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Connection helpers ────────────────────────────────────────────────────────

def duck_conn():
    return duckdb.connect(str(DB_PATH), read_only=True)


def supa_session() -> requests.Session:
    """Build a requests.Session pre-loaded with Supabase auth headers."""
    if not SUPA_URL:
        raise RuntimeError("SUPABASE_URL is not set. Add it to .env (see .env.example).")
    if not SUPA_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not set. Add it to .env.")
    s = requests.Session()
    s.headers.update({
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type":  "application/json",
    })
    return s


# ── Schema guard ──────────────────────────────────────────────────────────────

DASHBOARD_TABLES = [
    "dashboard_protocol_daily_tvl",
    "dashboard_protocol_metadata",
    "dashboard_token_snapshots",
    "dashboard_stablecoin_history",
]


def check_tables_exist(session: requests.Session) -> bool:
    """
    Verify all dashboard tables exist in Supabase.
    Returns True if all exist. On any missing table, logs clear instructions and returns False.
    """
    missing = []
    for table in DASHBOARD_TABLES:
        resp = session.get(f"{SUPA_URL}/rest/v1/{table}", params={"limit": "0"})
        # PostgREST returns 200 (empty array) for valid tables; 404/400 for unknown relations
        if resp.status_code not in (200,):
            missing.append(table)

    if missing:
        schema_path = Path(__file__).parent / "schema.sql"
        log.error(
            "Tables missing in Supabase: %s\n"
            "\n"
            "  Fix: open the Supabase SQL Editor and run the schema:\n"
            "    1. Go to https://supabase.com → your project → SQL Editor\n"
            "    2. Click 'New Query'\n"
            "    3. Paste the contents of:  %s\n"
            "    4. Click Run\n"
            "  Then re-run the sync.\n",
            missing,
            schema_path,
        )
        return False
    return True


# ── Utilities ─────────────────────────────────────────────────────────────────

def _max_date_in_supa(session: requests.Session, table: str, date_col: str = "date") -> str:
    """Return the max date already in a Supabase table, or TVL_START_DATE if empty."""
    resp = session.get(
        f"{SUPA_URL}/rest/v1/{table}",
        params={"select": date_col, "order": f"{date_col}.desc", "limit": "1"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data and data[0].get(date_col):
        return str(data[0][date_col])
    return TVL_START_DATE


def _clean(v):
    """
    Normalise a value for JSON serialisation:
      - None / pd.NA / NaN / inf → None
      - pandas Timestamps → ISO date string
      - date/datetime objects → isoformat string
      - everything else → unchanged
    """
    if v is None:
        return None
    # Catches pd.NA, pd.NaT, float('nan'), numpy NaN, etc.
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and math.isinf(v):
        return None
    # pandas Timestamp has both .date() and .isoformat()
    if hasattr(v, "date") and callable(getattr(v, "date")):
        return v.date().isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _upsert_batched(session: requests.Session, table: str, records: list[dict]) -> int:
    """
    Upsert records into a Supabase table via the REST API, in batches.
    Stamps synced_at on every row. Returns total rows processed.
    """
    if not records:
        log.info("  %s: 0 rows to push (already up to date)", table)
        return 0

    now_str = datetime.now(timezone.utc).isoformat()
    total = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = [dict(rec, synced_at=now_str) for rec in records[i : i + BATCH_SIZE]]
        resp = session.post(
            f"{SUPA_URL}/rest/v1/{table}",
            data=json.dumps(batch),
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if not resp.ok:
            raise RuntimeError(
                f"Upsert failed for {table} [{resp.status_code}]: {resp.text[:600]}"
            )
        total += len(batch)

    log.info("  %s: %d rows upserted", table, total)
    return total


# ── Sync functions ─────────────────────────────────────────────────────────────

def sync_protocol_daily_tvl(duck, session) -> int:
    """
    dl_protocol_tvl (joined to dl_protocols for category/chains)
    → dashboard_protocol_daily_tvl

    Incremental: only pushes rows with date > max date already in Supabase.
    """
    log.info("Syncing dashboard_protocol_daily_tvl ...")

    max_date = _max_date_in_supa(session, "dashboard_protocol_daily_tvl")
    today    = date.today().isoformat()
    log.info("  Incremental: %s → %s (excluding %s)", max_date, today, TVL_EXCLUDE_DATE)

    excluded = ", ".join(f"'{s}'" for s in EXCLUDED_SLUGS)

    df = duck.execute(f"""
        SELECT
            t.slug,
            ANY_VALUE(t.name)                   AS protocol_name,
            t.date,
            MAX(t.tvl_usd)                      AS tvl_usd,
            ANY_VALUE(p.category)               AS category,
            ANY_VALUE(p.chains)                 AS chains
        FROM dl_protocol_tvl t
        LEFT JOIN (
            SELECT slug, category, chains
            FROM dl_protocols
            WHERE snapshot_date = (
                SELECT MAX(snapshot_date)
                FROM dl_protocols
                WHERE snapshot_date != 'pre-append'
            )
        ) p ON t.slug = p.slug
        WHERE t.slug NOT IN ({excluded})
          AND t.date >= '{TVL_START_DATE}'
          AND t.date <= '{today}'
          AND t.date != '{TVL_EXCLUDE_DATE}'
          AND t.date > '{max_date}'
        GROUP BY t.slug, t.date
        ORDER BY t.date, t.slug
    """).fetchdf()

    records = [
        {
            "slug":          row.slug,
            "protocol_name": row.protocol_name,
            "date":          _clean(row.date),
            "tvl_usd":       _clean(row.tvl_usd),
            "category":      _clean(row.category),
            "chains":        _clean(row.chains),
        }
        for row in df.itertuples(index=False)
    ]
    return _upsert_batched(session, "dashboard_protocol_daily_tvl", records)


def sync_protocol_metadata(duck, session) -> int:
    """
    dl_protocols (latest real snapshot)
    → dashboard_protocol_metadata

    Full upsert on every run — metadata table is small (~190 rows) and always
    reflects the freshest snapshot.
    """
    log.info("Syncing dashboard_protocol_metadata ...")

    excluded = ", ".join(f"'{s}'" for s in EXCLUDED_SLUGS)

    df = duck.execute(f"""
        SELECT
            slug, name, category, chains, tvl_usd,
            change_1d_pct, change_7d_pct, change_1m_pct,
            description, url, logo, twitter, snapshot_date
        FROM dl_protocols
        WHERE snapshot_date = (
            SELECT MAX(snapshot_date) FROM dl_protocols
            WHERE snapshot_date != 'pre-append'
        )
        AND slug NOT IN ({excluded})
        ORDER BY slug
    """).fetchdf()

    records = [
        {
            "slug":          row.slug,
            "name":          _clean(row.name),
            "category":      _clean(row.category),
            "chains":        _clean(row.chains),
            "tvl_usd":       _clean(row.tvl_usd),
            "change_1d_pct": _clean(row.change_1d_pct),
            "change_7d_pct": _clean(row.change_7d_pct),
            "change_1m_pct": _clean(row.change_1m_pct),
            "description":   _clean(row.description),
            "url":           _clean(row.url),
            "logo":          _clean(row.logo),
            "twitter":       _clean(row.twitter),
            "snapshot_date": str(row.snapshot_date),
        }
        for row in df.itertuples(index=False)
    ]
    return _upsert_batched(session, "dashboard_protocol_metadata", records)


def sync_token_snapshots(duck, session) -> int:
    """
    cg_rwa_tokens (real snapshot_dates only, not pre-append)
    → dashboard_token_snapshots

    Incremental: only pushes snapshot_dates not yet in Supabase.
    """
    log.info("Syncing dashboard_token_snapshots ...")

    max_date = _max_date_in_supa(session, "dashboard_token_snapshots", date_col="snapshot_date")
    log.info("  Incremental from %s", max_date)

    df = duck.execute(f"""
        SELECT
            coingecko_id, symbol, name, snapshot_date,
            current_price_usd, market_cap_usd,
            price_change_pct_24h, price_change_pct_7d, price_change_pct_30d,
            circulating_supply
        FROM cg_rwa_tokens
        WHERE snapshot_date != 'pre-append'
          AND snapshot_date > '{max_date}'
        ORDER BY snapshot_date, coingecko_id
    """).fetchdf()

    records = [
        {
            "coingecko_id":         row.coingecko_id,
            "symbol":               _clean(row.symbol),
            "name":                 _clean(row.name),
            "snapshot_date":        str(row.snapshot_date),
            "current_price_usd":    _clean(row.current_price_usd),
            "market_cap_usd":       _clean(row.market_cap_usd),
            "price_change_pct_24h": _clean(row.price_change_pct_24h),
            "price_change_pct_7d":  _clean(row.price_change_pct_7d),
            "price_change_pct_30d": _clean(row.price_change_pct_30d),
            "circulating_supply":   _clean(row.circulating_supply),
        }
        for row in df.itertuples(index=False)
    ]
    return _upsert_batched(session, "dashboard_token_snapshots", records)


def sync_stablecoin_history(duck, session) -> int:
    """
    dl_stablecoin_history (all stablecoins, 2023-01-01 onwards)
    → dashboard_stablecoin_history

    Incremental: only pushes dates not yet in Supabase.
    """
    log.info("Syncing dashboard_stablecoin_history ...")

    max_date = _max_date_in_supa(session, "dashboard_stablecoin_history")
    log.info("  Incremental from %s", max_date)

    df = duck.execute(f"""
        SELECT
            id AS stablecoin_id, name, symbol, date, total_circulating_usd
        FROM dl_stablecoin_history
        WHERE date >= '{TVL_START_DATE}'
          AND date > '{max_date}'
        ORDER BY date, id
    """).fetchdf()

    records = [
        {
            "stablecoin_id":        row.stablecoin_id,
            "name":                 _clean(row.name),
            "symbol":               _clean(row.symbol),
            "date":                 _clean(row.date),
            "total_circulating_usd": _clean(row.total_circulating_usd),
        }
        for row in df.itertuples(index=False)
    ]
    return _upsert_batched(session, "dashboard_stablecoin_history", records)


# ── Orchestrator ───────────────────────────────────────────────────────────────

SYNC_FUNCTIONS = {
    "protocol_tvl":      sync_protocol_daily_tvl,
    "protocol_metadata": sync_protocol_metadata,
    "token_snapshots":   sync_token_snapshots,
    "stablecoin_history": sync_stablecoin_history,
}


def main(tables=None) -> int:
    """
    Run the sync. Returns 0 on full success, 1 if any table failed.

    Args:
        tables: list of table keys to sync (default: all).
                Keys must be from SYNC_FUNCTIONS.
    """
    targets = tables or list(SYNC_FUNCTIONS.keys())
    log.info("Starting sync — tables: %s", ", ".join(targets))

    try:
        duck = duck_conn()
    except Exception as e:
        log.error("Failed to open DuckDB: %s", e)
        return 1

    try:
        session = supa_session()
    except RuntimeError as e:
        log.error(str(e))
        duck.close()
        return 1

    if not check_tables_exist(session):
        duck.close()
        return 1

    failed = []
    for key in targets:
        fn = SYNC_FUNCTIONS.get(key)
        if fn is None:
            log.error("Unknown table key: %s. Valid keys: %s", key, list(SYNC_FUNCTIONS))
            failed.append(key)
            continue
        try:
            fn(duck, session)
        except Exception as e:
            log.error("FAILED syncing %s: %s", key, e)
            failed.append(key)

    duck.close()

    if failed:
        log.error("Sync completed with failures: %s", failed)
        return 1

    log.info("Sync complete — all tables successful")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync DuckDB → Supabase (REST API)")
    parser.add_argument(
        "--table",
        choices=list(SYNC_FUNCTIONS.keys()),
        help="Sync a single table (default: all)",
    )
    args = parser.parse_args()
    sys.exit(main([args.table] if args.table else None))
