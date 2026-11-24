"""
Print the SQL needed to wipe and recreate all dashboard_* tables in Supabase.

DESTRUCTIVE — the printed SQL drops all dashboard tables.

Usage:
    python -m sync.wipe_supabase --confirm

This prints the DROP + CREATE SQL to stdout. To apply it:
  1. Copy the SQL printed below (or run this script and pipe to clipboard)
  2. Open your Supabase project → SQL Editor → New Query
  3. Paste and click Run
  4. Then run:  python -m sync.sync_to_supabase

Why not execute it automatically?
  DDL (DROP TABLE, CREATE TABLE) cannot be run via the Supabase REST API.
  It must be applied through the SQL Editor or a direct Postgres connection.
"""

import sys
from pathlib import Path

TABLES = [
    "dashboard_protocol_daily_tvl",
    "dashboard_protocol_metadata",
    "dashboard_token_snapshots",
    "dashboard_stablecoin_history",
]


def main():
    if "--confirm" not in sys.argv:
        print(
            "ERROR: This will DROP and recreate all dashboard_* tables in Supabase.\n"
            "Re-run with --confirm to see the SQL:\n\n"
            "    python -m sync.wipe_supabase --confirm\n"
        )
        sys.exit(1)

    schema_path = Path(__file__).parent / "schema.sql"
    schema_ddl = schema_path.read_text()

    drop_sql = "\n".join(
        f"DROP TABLE IF EXISTS {t} CASCADE;" for t in TABLES
    )

    print("=" * 70)
    print("Run the following SQL in your Supabase SQL Editor:")
    print("  Dashboard → SQL Editor → New Query → paste → Run")
    print("=" * 70)
    print()
    print("-- Step 1: Drop existing tables")
    print(drop_sql)
    print()
    print("-- Step 2: Recreate from schema")
    print(schema_ddl)
    print()
    print("=" * 70)
    print("After running the SQL, repopulate with:")
    print("  python -m sync.sync_to_supabase")
    print("=" * 70)


if __name__ == "__main__":
    main()
