"""
Enrichment orchestrator — runs on-chain activity enrichment for anchor profiles.

Designed to run daily after run_all.py completes the main ingestion pipeline.

Usage:
    python run_enrichment.py                  # enrich all anchor profiles
    python run_enrichment.py --dry-run        # compute but don't write
    python run_enrichment.py --slug blackrock-buidl  # single profile
"""

import argparse
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="RWA Intelligence enrichment orchestrator")
    parser.add_argument("--slug", help="Enrich only this profile", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Starting enrichment pipeline")
    log.info("=" * 60)

    from sync.availability import supabase_available
    ok, why = supabase_available()
    if not ok:
        log.warning(f"Enrichment skipped: it reads profiles from Supabase, which is unavailable — {why}")
        return

    t0 = time.time()
    try:
        from enrich.enrich_onchain_activity import run as enrich_run
        slugs = [args.slug] if args.slug else None
        result = enrich_run(slugs=slugs, dry_run=args.dry_run)
        elapsed = time.time() - t0

        log.info("=" * 60)
        log.info("ENRICHMENT SUMMARY")
        log.info("=" * 60)
        if result.get("status") == "ok":
            log.info(f"  ✓ on-chain activity: {result.get('enriched')} profiles enriched "
                     f"in {result.get('elapsed')}s")
            if result.get("errors"):
                log.warning(f"  ⚠ {result.get('errors')} profile(s) had errors")
        else:
            log.error(f"  ✗ enrichment failed: {result.get('reason')}")

    except Exception as e:
        elapsed = time.time() - t0
        log.error(f"Enrichment pipeline failed after {elapsed:.1f}s: {e}")
        raise


if __name__ == "__main__":
    main()
