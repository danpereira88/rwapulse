"""
Verify what's in Supabase after a sync run.

Prints: row counts, date ranges, and last synced_at per dashboard table.

Usage:
    python -m sync.verify_sync
"""

import os
import sys
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

TABLES = [
    ("dashboard_protocol_daily_tvl",  "date"),
    ("dashboard_protocol_metadata",   "snapshot_date"),
    ("dashboard_token_snapshots",     "snapshot_date"),
    ("dashboard_stablecoin_history",  "date"),
]


def _session() -> requests.Session:
    if not SUPA_URL:
        print("ERROR: SUPABASE_URL not set in .env", file=sys.stderr)
        sys.exit(1)
    if not SUPA_KEY:
        print("ERROR: SUPABASE_SERVICE_ROLE_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    s = requests.Session()
    s.headers.update({
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
    })
    return s


def _row_count(s: requests.Session, table: str) -> Optional[int]:
    """Return row count using Prefer: count=exact."""
    resp = s.get(
        f"{SUPA_URL}/rest/v1/{table}",
        params={"select": "*"},
        headers={"Prefer": "count=exact", "Range": "0-0"},
    )
    cr = resp.headers.get("Content-Range", "")
    if "/" in cr:
        try:
            return int(cr.split("/")[1])
        except ValueError:
            pass
    return None


def _one_val(s: requests.Session, table: str, col: str, order: str) -> Optional[str]:
    """Fetch the min or max value of a column."""
    resp = s.get(
        f"{SUPA_URL}/rest/v1/{table}",
        params={"select": col, "order": f"{col}.{order}", "limit": "1"},
    )
    if not resp.ok:
        return None
    data = resp.json()
    if data and data[0].get(col):
        return str(data[0][col])
    return None


def main():
    s = _session()

    print(f"\n{'Table':<40} {'Rows':>8}  {'Date range':<32}  Last synced_at")
    print("-" * 105)

    for table, date_col in TABLES:
        try:
            n      = _row_count(s, table)
            min_d  = _one_val(s, table, date_col, "asc")
            max_d  = _one_val(s, table, date_col, "desc")
            last_s = _one_val(s, table, "synced_at", "desc")

            n_str      = f"{n:,}" if n is not None else "?"
            date_range = f"{min_d} → {max_d}" if min_d else "— (empty)"
            sync_str   = last_s[:19].replace("T", " ") + " UTC" if last_s else "—"
            print(f"{table:<40} {n_str:>8}  {date_range:<32}  {sync_str}")
        except Exception as e:
            print(f"{table:<40}  ERROR: {e}")

    # Protocol / date distinct counts for TVL table
    print()
    try:
        # Fetch slugs to count distinct
        resp = _one_val(s, "dashboard_protocol_daily_tvl", "slug", "asc")
        # Simple approach: get count from Content-Range for protocols
        r = s.get(
            f"{SUPA_URL}/rest/v1/dashboard_protocol_daily_tvl",
            params={"select": "slug", "limit": "0"},
            headers={"Prefer": "count=exact", "Range": "0-0"},
        )
        total_tvl_rows = None
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            total_tvl_rows = int(cr.split("/")[1])

        if total_tvl_rows is not None:
            print(f"  dashboard_protocol_daily_tvl: {total_tvl_rows:,} total rows")
    except Exception:
        pass

    # Categories in metadata
    try:
        resp = s.get(
            f"{SUPA_URL}/rest/v1/dashboard_protocol_metadata",
            params={"select": "category"},
        )
        if resp.ok:
            rows = resp.json()
            from collections import Counter
            cats = Counter(r.get("category") for r in rows)
            top5 = cats.most_common(5)
            print(f"\n  Top categories in dashboard_protocol_metadata:")
            for cat, n in top5:
                print(f"    {(cat or '(null)'):<32} {n} protocols")
    except Exception:
        pass

    print()


if __name__ == "__main__":
    main()
