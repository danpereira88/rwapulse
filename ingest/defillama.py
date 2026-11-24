"""
DefiLlama ingestion.

Tables created:
  dl_protocols         — all protocols with RWA-relevant categories (snapshot)
  dl_protocol_tvl      — historical daily TVL per protocol
  dl_stablecoins       — stablecoin metadata + current supply by chain
  dl_stablecoin_history — daily circulating supply per stablecoin
  dl_yield_pools       — yield pools, filtered for RWA-adjacent
  dl_chains            — TVL breakdown by chain
  dl_protocol_fees     — protocol fee/revenue data where available
"""

import time
import logging
from pathlib import Path
from datetime import datetime, timezone
import sys

from typing import Optional

import requests
import pandas as pd

# Allow running as a script from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection, append_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE = "https://api.llama.fi"
STABLECOINS_BASE = "https://stablecoins.llama.fi"
YIELDS_BASE = "https://yields.llama.fi"

# Categories DefiLlama uses that are RWA-relevant
RWA_CATEGORIES = {
    "RWA",
    "RWA Lending",
    "Treasury",
    "Treasury Bills",
    "Tokenized Bitcoin",  # bridges toward real assets
    "Real World Assets",
    "Undercollateralized Lending",  # Maple, Goldfinch, Clearpool live here
    "Stablecoins",
}

# Keywords for yield pool filtering
RWA_POOL_KEYWORDS = {
    "buidl", "benji", "ousg", "usdy", "usyc", "ustb", "ondo", "maple",
    "centrifuge", "goldfinch", "clearpool", "paxg", "xaut", "tbill",
    "treasury", "t-bill", "rwa", "real world", "backed", "superstate",
    "hashnote", "franklin",
}


def _get(url: str, params: Optional[dict] = None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning(f"Retry {attempt+1}/{retries} for {url}: {e}")
            time.sleep(2 ** attempt)


def ingest_protocols(con) -> int:
    log.info("Fetching all protocols...")
    data = _get(f"{BASE}/protocols")

    rows = []
    for p in data:
        cat = p.get("category", "") or ""
        # Include if category matches OR if name/slug hints at RWA
        is_rwa = cat in RWA_CATEGORIES
        name_lower = (p.get("name", "") + p.get("slug", "")).lower()
        has_rwa_name = any(k in name_lower for k in RWA_POOL_KEYWORDS)
        if not (is_rwa or has_rwa_name):
            continue

        rows.append({
            "slug": p.get("slug"),
            "name": p.get("name"),
            "category": cat,
            "chains": ",".join(p.get("chains", [])),
            "tvl_usd": p.get("tvl"),
            "change_1d_pct": p.get("change_1d"),
            "change_7d_pct": p.get("change_7d"),
            "change_1m_pct": p.get("change_1m"),
            "mcap": p.get("mcap"),
            "fdv": p.get("fdv"),
            "logo": p.get("logo"),
            "url": p.get("url"),
            "description": p.get("description"),
            "twitter": p.get("twitter"),
            "audits": p.get("audits"),
            "audit_links": ",".join(p.get("audit_links") or []),
            "gecko_id": p.get("gecko_id"),
            "cmcId": p.get("cmcId"),
            "ingested_at": pd.Timestamp.utcnow().isoformat(),
        })

    df = pd.DataFrame(rows)
    n = append_snapshot(con, "dl_protocols", df)
    log.info(f"dl_protocols: {n} rows appended")
    return len(df)


def ingest_protocol_tvl(con) -> int:
    slugs = con.execute("SELECT DISTINCT slug, name FROM dl_protocols").fetchdf()
    all_rows = []
    failed = []

    for _, row in slugs.iterrows():
        slug = row["slug"]
        try:
            data = _get(f"{BASE}/protocol/{slug}")
            tvl_data = data.get("tvl", [])  # list of {date, totalLiquidityUSD}
            for entry in tvl_data:
                ts = entry.get("date")
                try:
                    date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    date_str = str(ts)
                all_rows.append({
                    "slug": slug,
                    "name": row["name"],
                    "date": date_str,
                    "tvl_usd": entry.get("totalLiquidityUSD"),
                })
            time.sleep(0.3)  # be polite
        except Exception as e:
            log.warning(f"TVL fetch failed for {slug}: {e}")
            failed.append(slug)

    if failed:
        log.warning(f"Failed TVL fetches: {failed}")

    df = pd.DataFrame(all_rows, columns=["slug", "name", "date", "tvl_usd"])
    # DefiLlama returns two points for the latest day (daily close + live reading);
    # keep the last one so (slug, date) is unique.
    df = df.drop_duplicates(subset=["slug", "date"], keep="last")

    # Merge, never replace. Until 2026-11-24 this table was dropped and rebuilt on
    # every run, so when DefiLlama stopped returning history for a protocol
    # (BUIDL, USYC, Ondo, Spiko... from 2026-09-20) its whole history was deleted.
    # Now: rows we fetched overwrite matching (slug, date) rows; everything else stays.
    con.execute("""CREATE TABLE IF NOT EXISTS dl_protocol_tvl
                   (slug VARCHAR, name VARCHAR, date VARCHAR, tvl_usd DOUBLE)""")
    before = con.execute("SELECT count(*) FROM dl_protocol_tvl").fetchone()[0]
    had_history = {r[0] for r in con.execute("SELECT DISTINCT slug FROM dl_protocol_tvl").fetchall()}
    if not df.empty:
        con.execute("""DELETE FROM dl_protocol_tvl USING df
                       WHERE dl_protocol_tvl.slug = df.slug AND dl_protocol_tvl.date = df.date""")
        con.execute("INSERT INTO dl_protocol_tvl SELECT slug, name, date, tvl_usd FROM df")
    # Collapse duplicates left by older runs (same slug + date twice)
    con.execute("""CREATE OR REPLACE TABLE dl_protocol_tvl AS
                   SELECT slug, any_value(name) AS name, date, max(tvl_usd) AS tvl_usd
                   FROM dl_protocol_tvl GROUP BY slug, date""")
    after = con.execute("SELECT count(*) FROM dl_protocol_tvl").fetchone()[0]

    returned = set(df["slug"].unique()) if not df.empty else set()
    tried = set(slugs["slug"])
    went_quiet = sorted(((had_history & tried) - returned) - set(failed))
    if went_quiet:
        log.warning(f"DefiLlama returned no TVL history for {len(went_quiet)} protocols that "
                    f"had history before (kept existing rows): {went_quiet[:20]}")
    log.info(f"dl_protocol_tvl: fetched {len(df)} rows across {len(returned)} protocols; "
             f"table {before} → {after} rows")
    return len(df)


def ingest_stablecoins(con) -> tuple[int, int]:
    log.info("Fetching stablecoins...")
    data = _get(f"{STABLECOINS_BASE}/stablecoins?includePrices=true")
    pegged_assets = data.get("peggedAssets", [])

    meta_rows = []
    history_rows = []

    # RWA-relevant stablecoin names / symbols
    rwa_stable_keywords = {
        "usdy", "ousg", "usyc", "ustb", "buidl", "benji", "ondo", "hashnote",
        "superstate", "franklin", "backed", "maple", "centrifuge",
        "tbill", "treasury", "fobxx", "usdm", "stbt", "usd+",
    }

    for asset in pegged_assets:
        name_lower = (asset.get("name", "") + asset.get("symbol", "")).lower()
        peg_type = asset.get("pegType", "")
        # Include USD-pegged + yield-bearing, or any with RWA name
        is_rwa = any(k in name_lower for k in rwa_stable_keywords)
        # Also capture all USD stablecoins for market context
        is_usd = "usd" in peg_type.lower() or "dollar" in peg_type.lower()

        if not (is_rwa or is_usd):
            continue

        chains_data = asset.get("chainCirculating", {})
        total_supply = asset.get("circulating", {}).get("peggedUSD", 0)
        circ_prev_day = asset.get("circulatingPrevDay", {}).get("peggedUSD", 0)
        circ_prev_week = asset.get("circulatingPrevWeek", {}).get("peggedUSD", 0)
        circ_prev_month = asset.get("circulatingPrevMonth", {}).get("peggedUSD", 0)

        if chains_data:
            for chain, chain_info in chains_data.items():
                circ = chain_info.get("current", {})
                meta_rows.append({
                    "id": asset.get("id"),
                    "name": asset.get("name"),
                    "symbol": asset.get("symbol"),
                    "peg_type": peg_type,
                    "peg_mechanism": asset.get("pegMechanism"),
                    "chain": chain,
                    "chain_supply_usd": circ.get("peggedUSD", 0),
                    "total_supply_usd": total_supply,
                    "total_prev_day_usd": circ_prev_day,
                    "total_prev_week_usd": circ_prev_week,
                    "total_prev_month_usd": circ_prev_month,
                    "price": asset.get("price"),
                    "gecko_id": asset.get("gecko_id"),
                    "ingested_at": pd.Timestamp.utcnow().isoformat(),
                })
        else:
            # Asset has no chain breakdown — store as single row with total
            meta_rows.append({
                "id": asset.get("id"),
                "name": asset.get("name"),
                "symbol": asset.get("symbol"),
                "peg_type": peg_type,
                "peg_mechanism": asset.get("pegMechanism"),
                "chain": "unknown",
                "chain_supply_usd": total_supply,
                "total_supply_usd": total_supply,
                "total_prev_day_usd": circ_prev_day,
                "total_prev_week_usd": circ_prev_week,
                "total_prev_month_usd": circ_prev_month,
                "price": asset.get("price"),
                "gecko_id": asset.get("gecko_id"),
                "ingested_at": pd.Timestamp.utcnow().isoformat(),
            })

        # Fetch history for RWA-specific stablecoins
        if is_rwa:
            asset_id = asset.get("id")
            try:
                hist = _get(f"{STABLECOINS_BASE}/stablecoincharts/all?stablecoin={asset_id}")
                for entry in hist:
                    ts = entry.get("date")
                    try:
                        date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
                    except Exception:
                        date_str = str(ts)
                    history_rows.append({
                        "id": asset_id,
                        "name": asset.get("name"),
                        "symbol": asset.get("symbol"),
                        "date": date_str,
                        "total_circulating_usd": entry.get("totalCirculating", {}).get("peggedUSD", 0),
                        "total_unreleased_usd": entry.get("totalUnreleased", {}).get("peggedUSD", 0),
                    })
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"Stablecoin history failed for {asset.get('name')}: {e}")

    df_meta = pd.DataFrame(meta_rows) if meta_rows else pd.DataFrame(
        columns=["id","name","symbol","peg_type","peg_mechanism","chain",
                 "chain_supply_usd","total_supply_usd","total_prev_day_usd",
                 "total_prev_week_usd","total_prev_month_usd","price","gecko_id","ingested_at"]
    )
    df_hist = pd.DataFrame(history_rows) if history_rows else pd.DataFrame(
        columns=["id","name","symbol","date","total_circulating_usd","total_unreleased_usd"]
    )

    append_snapshot(con, "dl_stablecoins", df_meta)

    # dl_stablecoin_history already has a date column from the API — keep as full-replace
    # so we always have the complete history without duplicates
    con.execute("DROP TABLE IF EXISTS dl_stablecoin_history")
    con.execute("CREATE TABLE dl_stablecoin_history AS SELECT * FROM df_hist")

    log.info(f"dl_stablecoins: {len(df_meta)} rows, dl_stablecoin_history: {len(df_hist)} rows")
    return len(df_meta), len(df_hist)


def ingest_yield_pools(con) -> int:
    log.info("Fetching yield pools...")
    data = _get(f"{YIELDS_BASE}/pools")
    pools = data.get("data", [])

    rows = []
    for p in pools:
        project = (p.get("project") or "").lower()
        symbol = (p.get("symbol") or "").lower()
        pool_id = (p.get("pool") or "").lower()
        # Filter for RWA-adjacent pools
        match = any(k in project or k in symbol or k in pool_id for k in RWA_POOL_KEYWORDS)
        if not match:
            continue
        rows.append({
            "pool_id": p.get("pool"),
            "chain": p.get("chain"),
            "project": p.get("project"),
            "symbol": p.get("symbol"),
            "tvl_usd": p.get("tvlUsd"),
            "apy": p.get("apy"),
            "apy_base": p.get("apyBase"),
            "apy_reward": p.get("apyReward"),
            "apy_mean_30d": p.get("apyMean30d"),
            "il_risk": p.get("ilRisk"),
            "stablecoin": p.get("stablecoin"),
            "exposure": p.get("exposure"),
            "predictions_betas": str(p.get("predictions", {}).get("betas")),
            "ingested_at": pd.Timestamp.utcnow().isoformat(),
        })

    df = pd.DataFrame(rows)
    n = append_snapshot(con, "dl_yield_pools", df)
    log.info(f"dl_yield_pools: {n} rows appended")
    return len(df)


def ingest_chains(con) -> int:
    log.info("Fetching chain TVL...")
    data = _get(f"{BASE}/v2/chains")
    rows = []
    for c in data:
        rows.append({
            "chain": c.get("name"),
            "gecko_id": c.get("gecko_id"),
            "tvl_usd": c.get("tvl"),
            "change_1d_pct": c.get("change_1d"),
            "change_7d_pct": c.get("change_7d"),
            "ingested_at": pd.Timestamp.utcnow().isoformat(),
        })
    df = pd.DataFrame(rows)
    n = append_snapshot(con, "dl_chains", df)
    log.info(f"dl_chains: {n} rows appended")
    return len(df)


def ingest_protocol_fees(con) -> int:
    """Pull fees/revenue overview — available for protocols that report them."""
    log.info("Fetching protocol fees overview...")
    try:
        data = _get(f"{BASE}/overview/fees?excludeTotalDataChartBreakdown=true&excludeTotalDataChart=true")
        protocols = data.get("protocols", [])
        rows = []
        rwa_slugs = set(con.execute("SELECT DISTINCT slug FROM dl_protocols").fetchdf()["slug"].tolist())
        for p in protocols:
            slug = p.get("slug") or p.get("name", "").lower().replace(" ", "-")
            if slug not in rwa_slugs:
                # also check by name similarity
                name_lower = p.get("name", "").lower()
                if not any(k in name_lower for k in RWA_POOL_KEYWORDS):
                    continue
            rows.append({
                "slug": slug,
                "name": p.get("name"),
                "total_24h_usd": p.get("total24h"),
                "total_48h_usd": p.get("total48hto24h"),
                "total_7d_usd": p.get("total7d"),
                "total_30d_usd": p.get("total30d"),
                "total_alltime_usd": p.get("totalAllTime"),
                "revenue_24h_usd": p.get("revenue24h"),
                "revenue_30d_usd": p.get("revenue30d"),
                "ingested_at": pd.Timestamp.utcnow().isoformat(),
            })
        df = pd.DataFrame(rows)
        n = append_snapshot(con, "dl_protocol_fees", df)
        log.info(f"dl_protocol_fees: {n} rows appended")
        return len(df)
    except Exception as e:
        log.warning(f"Fees endpoint failed: {e}")
        return 0


def run():
    con = get_connection()
    results = {}

    try:
        results["protocols"] = ingest_protocols(con)
    except Exception as e:
        log.error(f"protocols failed: {e}")

    try:
        results["protocol_tvl"] = ingest_protocol_tvl(con)
    except Exception as e:
        log.error(f"protocol_tvl failed: {e}")

    try:
        n_meta, n_hist = ingest_stablecoins(con)
        results["stablecoins"] = n_meta
        results["stablecoin_history"] = n_hist
    except Exception as e:
        log.error(f"stablecoins failed: {e}")

    try:
        results["yield_pools"] = ingest_yield_pools(con)
    except Exception as e:
        log.error(f"yield_pools failed: {e}")

    try:
        results["chains"] = ingest_chains(con)
    except Exception as e:
        log.error(f"chains failed: {e}")

    try:
        results["protocol_fees"] = ingest_protocol_fees(con)
    except Exception as e:
        log.error(f"protocol_fees failed: {e}")

    con.close()
    log.info(f"DefiLlama ingestion complete: {results}")
    return results


if __name__ == "__main__":
    run()
