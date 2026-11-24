"""
Full pipeline: ingest → sync.

Runs the complete DuckDB ingestion (run_all.py) and, only on success,
pushes the curated subset to Supabase (sync/sync_to_supabase.py).

If ingestion fails, sync is skipped — no partial data pushed to Supabase.

Usage:
    python run_all_with_sync.py
    python run_all_with_sync.py --skip-onchain   # passed through to run_all.py
    python run_all_with_sync.py --skip-sync       # ingest only, skip Supabase push
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser(description="Ingest + sync pipeline")
    parser.add_argument("--skip-onchain", action="store_true",
                        help="Skip on-chain ingestion step (passed to run_all.py)")
    parser.add_argument("--skip-sync", action="store_true",
                        help="Run ingestion only; skip Supabase sync")
    args = parser.parse_args()

    # ── Step 1: ingestion ──────────────────────────────────────────────────────
    ingest_cmd = [sys.executable, str(ROOT / "run_all.py")]
    if args.skip_onchain:
        ingest_cmd.append("--skip-onchain")

    log.info("=" * 60)
    log.info("Step 1: DuckDB ingestion")
    log.info("=" * 60)

    result = subprocess.run(ingest_cmd, cwd=ROOT)

    if result.returncode != 0:
        log.error(f"Ingestion failed (exit {result.returncode}) — sync skipped")
        sys.exit(result.returncode)

    log.info("Ingestion complete")

    # ── Step 2: sync ───────────────────────────────────────────────────────────
    if args.skip_sync:
        log.info("--skip-sync: Supabase sync skipped")
        sys.exit(0)

    from sync.availability import supabase_available
    ok, why = supabase_available()
    if not ok:
        log.warning(f"Supabase sync skipped: {why}. Local DuckDB is up to date.")
        sys.exit(0)

    log.info("=" * 60)
    log.info("Step 2: Supabase sync")
    log.info("=" * 60)

    from sync.sync_to_supabase import main as run_sync
    exit_code = run_sync()

    if exit_code != 0:
        log.error("Sync failed — see errors above")
        sys.exit(exit_code)

    log.info("Pipeline complete — ingestion + sync successful")
    sys.exit(0)


if __name__ == "__main__":
    main()
