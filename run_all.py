"""
Orchestrator — runs all ingestion steps and then generates the inventory report.

Usage:
    python run_all.py                    # run everything
    python run_all.py --skip-onchain     # skip slow on-chain step
    python run_all.py --only defillama   # run only one source
    python run_all.py --skip-enrichment  # skip on-chain activity enrichment
"""

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STEPS = [
    ("defillama",  "ingest.defillama",  "DefiLlama protocols, TVL, stablecoins, yields"),
    ("coingecko",  "ingest.coingecko",  "CoinGecko prices and market caps"),
    ("onchain",    "ingest.onchain",    "On-chain totalSupply and Transfer events"),
    ("subgraphs",  "ingest.subgraphs",  "Maple, Centrifuge, Goldfinch subgraphs"),
    ("etherscan",  "ingest.etherscan",  "Etherscan holder counts and contract info"),
    ("rwa_supply", "ingest.rwa_supply", "Multichain RWA token supply × price (replaces DefiLlama TVL for big products)"),
    ("chain_sweep", "ingest.chain_sweep", "Every tokenised asset CoinGecko lists on 15 chains, supply read on-chain"),
]


def run_step(module_path: str, label: str) -> dict:
    import importlib
    log.info(f"\n{'='*60}")
    log.info(f"Starting: {label}")
    log.info(f"{'='*60}")
    t0 = time.time()
    try:
        mod = importlib.import_module(module_path)
        result = mod.run()
        elapsed = time.time() - t0
        log.info(f"Finished: {label} in {elapsed:.1f}s — {result}")
        return {"status": "ok", "result": result, "elapsed": elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        log.error(f"FAILED: {label} after {elapsed:.1f}s — {e}")
        return {"status": "error", "error": str(e), "elapsed": elapsed}


def main():
    parser = argparse.ArgumentParser(description="RWA data ingestion orchestrator")
    parser.add_argument("--only", help="Run only this source (defillama/coingecko/onchain/subgraphs/etherscan/rwa_supply)")
    parser.add_argument("--skip-onchain", action="store_true", help="Skip on-chain step (can be slow)")
    parser.add_argument("--skip-inventory", action="store_true", help="Skip inventory report generation")
    parser.add_argument("--skip-enrichment", action="store_true", help="Skip on-chain activity enrichment step")
    args = parser.parse_args()

    summary = {}
    for name, module, label in STEPS:
        if args.only and args.only != name:
            continue
        if args.skip_onchain and name == "onchain":
            log.info(f"Skipping {label} (--skip-onchain)")
            continue
        summary[name] = run_step(module, label)

    # Run on-chain activity enrichment
    if not args.skip_enrichment and not args.only:
        from sync.availability import supabase_available
        ok, why = supabase_available()
        if not ok:
            log.warning(f"Skipping on-chain activity enrichment: Supabase unavailable — {why}")
            args.skip_enrichment = True
    if not args.skip_enrichment and not args.only:
        log.info("\n" + "="*60)
        log.info("Starting: on-chain activity enrichment")
        log.info("="*60)
        t0_enrich = time.time()
        try:
            from enrich.enrich_onchain_activity import run as enrich_run
            enrich_result = enrich_run()
            elapsed_enrich = time.time() - t0_enrich
            summary["enrichment"] = {
                "status": enrich_result.get("status"),
                "elapsed": round(elapsed_enrich, 1),
                "enriched": enrich_result.get("enriched"),
            }
            log.info(f"Finished: on-chain activity enrichment in {elapsed_enrich:.1f}s — "
                     f"{enrich_result.get('enriched')} profiles enriched")
        except Exception as e:
            elapsed_enrich = time.time() - t0_enrich
            log.error(f"FAILED: on-chain activity enrichment after {elapsed_enrich:.1f}s — {e}")
            summary["enrichment"] = {"status": "error", "error": str(e)}

    # Generate inventory report
    if not args.skip_inventory:
        log.info("\n" + "="*60)
        log.info("Generating inventory report...")
        log.info("="*60)
        try:
            from explore import inventory
            inventory.run()
        except Exception as e:
            log.error(f"Inventory report failed: {e}")

    # Print summary
    log.info("\n" + "="*60)
    log.info("INGESTION SUMMARY")
    log.info("="*60)
    for name, result in summary.items():
        status = result.get("status", "unknown")
        elapsed = result.get("elapsed", 0)
        if status == "ok":
            extra = f" ({result['enriched']} enriched)" if "enriched" in result else ""
            log.info(f"  ✓ {name}: {elapsed:.1f}s{extra}")
        else:
            log.info(f"  ✗ {name}: FAILED — {result.get('error', '')}")

    report_path = Path("reports/inventory.md")
    if report_path.exists():
        log.info(f"\nInventory report: {report_path.resolve()}")

    # Report failure honestly so the weekly job doesn't publish a half-built snapshot
    failed = [n for n, r in summary.items() if r.get("status") == "error"]
    if failed:
        log.error(f"Steps failed: {', '.join(failed)} — exiting with status 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
