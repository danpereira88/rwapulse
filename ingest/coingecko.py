"""
CoinGecko ingestion (free tier, no API key, ~30 req/min).

Tables created:
  cg_rwa_tokens        — metadata + current market data for known RWA tokens
  cg_token_history     — daily OHLCV + market cap for each token (90 days free)
  cg_rwa_category      — category-level aggregates if CoinGecko has an RWA category
"""

import time
import logging
from pathlib import Path
from typing import Optional
import sys

import requests
import pandas as pd
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection, append_snapshot

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE = "https://api.coingecko.com/api/v3"
# Free "Demo" API key from https://www.coingecko.com/en/api (no credit card required)
# Required for historical /market_chart endpoint; current /coins/markets works without
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

# Known RWA token CoinGecko IDs — verified as of mid-2025
# Format: (coingecko_id, display_name, asset_class)
RWA_TOKENS = [
    # Tokenized Treasuries
    ("blackrock-usd-institutional-digital-liquidity-fund", "BUIDL", "tokenized_treasury"),
    ("ondo-us-dollar-yield", "USDY", "tokenized_treasury"),
    ("ondo-finance", "ONDO", "tokenized_treasury"),  # governance token
    ("hashnote-usyc", "USYC", "tokenized_treasury"),
    ("superstate-short-duration-us-government-securities-fund", "USTB", "tokenized_treasury"),
    ("franklin-onchain-us-government-money-fund", "BENJI", "tokenized_treasury"),
    ("openeden-tbill", "TBILL", "tokenized_treasury"),
    ("backed-ib01-0-3month-ustreasury-bond", "bIB01", "tokenized_treasury"),
    # Tokenized Gold / Commodities
    ("pax-gold", "PAXG", "tokenized_gold"),
    ("tether-gold", "XAUT", "tokenized_gold"),
    ("cache-gold", "CGT", "tokenized_gold"),
    # Private Credit / Lending protocols
    ("maple", "MPL", "private_credit"),
    ("centrifuge", "CFG", "private_credit"),
    ("goldfinch", "GFI", "private_credit"),
    ("clearpool", "CPOOL", "private_credit"),
    # Stablecoins with RWA backing
    ("usual-usd", "USD0", "rwa_stablecoin"),
    ("mountain-protocol-usdm", "USDM", "rwa_stablecoin"),
    # Broader RWA infrastructure
    ("realtoken-ecosystem-governance", "REG", "rwa_infrastructure"),
    ("propbase", "PROPS", "rwa_infrastructure"),
]


def _get(url: str, params: Optional[dict] = None, retries: int = 3):
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                wait = 60
                log.warning(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise RuntimeError(
                    "CoinGecko 401: Historical endpoints require a free Demo API key. "
                    "Sign up at https://www.coingecko.com/en/api and set COINGECKO_API_KEY in .env"
                )
            r.raise_for_status()
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning(f"Retry {attempt+1}/{retries} for {url}: {e}")
            time.sleep(3 ** attempt)


def ingest_token_metadata(con) -> int:
    """Pull current market data for all known RWA tokens, 50 at a time."""
    ids = [t[0] for t in RWA_TOKENS]
    all_rows = []

    # CoinGecko /coins/markets allows batch of up to 250 IDs
    chunk_size = 50
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            data = _get(f"{BASE}/coins/markets", params={
                "vs_currency": "usd",
                "ids": ",".join(chunk),
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": False,
                "price_change_percentage": "1h,24h,7d,30d",
            })
            for coin in data:
                cg_id = coin.get("id")
                # Look up our metadata
                token_meta = next((t for t in RWA_TOKENS if t[0] == cg_id), (cg_id, cg_id, "unknown"))
                all_rows.append({
                    "coingecko_id": cg_id,
                    "symbol": coin.get("symbol", "").upper(),
                    "name": coin.get("name"),
                    "display_name": token_meta[1],
                    "asset_class": token_meta[2],
                    "current_price_usd": coin.get("current_price"),
                    "market_cap_usd": coin.get("market_cap"),
                    "market_cap_rank": coin.get("market_cap_rank"),
                    "fully_diluted_valuation_usd": coin.get("fully_diluted_valuation"),
                    "total_volume_24h_usd": coin.get("total_volume"),
                    "high_24h_usd": coin.get("high_24h"),
                    "low_24h_usd": coin.get("low_24h"),
                    "price_change_24h_usd": coin.get("price_change_24h"),
                    "price_change_pct_1h": coin.get("price_change_percentage_1h_in_currency"),
                    "price_change_pct_24h": coin.get("price_change_percentage_24h_in_currency"),
                    "price_change_pct_7d": coin.get("price_change_percentage_7d_in_currency"),
                    "price_change_pct_30d": coin.get("price_change_percentage_30d_in_currency"),
                    "circulating_supply": coin.get("circulating_supply"),
                    "total_supply": coin.get("total_supply"),
                    "max_supply": coin.get("max_supply"),
                    "ath_usd": coin.get("ath"),
                    "ath_date": coin.get("ath_date"),
                    "atl_usd": coin.get("atl"),
                    "atl_date": coin.get("atl_date"),
                    "last_updated": coin.get("last_updated"),
                    "ingested_at": pd.Timestamp.utcnow().isoformat(),
                })
            time.sleep(2)  # stay under 30 req/min
        except Exception as e:
            log.warning(f"Token metadata chunk {i} failed: {e}")

    df = pd.DataFrame(all_rows)
    n = append_snapshot(con, "cg_rwa_tokens", df)
    log.info(f"cg_rwa_tokens: {n} rows appended ({len(df)} of {len(ids)} tokens found)")
    return len(df)


def ingest_token_history(con) -> int:
    """Pull daily price history for each RWA token. Requires COINGECKO_API_KEY (free Demo tier)."""
    if not COINGECKO_API_KEY:
        log.warning(
            "COINGECKO_API_KEY not set — skipping historical price data.\n"
            "Get a free Demo key at https://www.coingecko.com/en/api then add to .env"
        )
        df = pd.DataFrame(columns=["coingecko_id","date","price_usd","market_cap_usd","volume_24h_usd"])
        con.execute("DROP TABLE IF EXISTS cg_token_history")
        con.execute("CREATE TABLE cg_token_history AS SELECT * FROM df")
        return 0

    all_rows = []
    found_tokens = []
    try:
        found_tokens = con.execute("SELECT DISTINCT coingecko_id FROM cg_rwa_tokens").fetchdf()["coingecko_id"].tolist()
    except Exception:
        found_tokens = [t[0] for t in RWA_TOKENS]

    for cg_id in found_tokens:
        try:
            # /coins/{id}/market_chart — daily granularity for >90 days, hourly for <90
            data = _get(f"{BASE}/coins/{cg_id}/market_chart", params={
                "vs_currency": "usd",
                "days": "max",  # free tier gives up to 365 days daily
                "interval": "daily",
            })
            prices = data.get("prices", [])
            market_caps = {str(int(ts)): v for ts, v in data.get("market_caps", [])}
            volumes = {str(int(ts)): v for ts, v in data.get("total_volumes", [])}

            for ts, price in prices:
                ts_key = str(int(ts))
                from datetime import datetime, timezone
                date_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                all_rows.append({
                    "coingecko_id": cg_id,
                    "date": date_str,
                    "price_usd": price,
                    "market_cap_usd": market_caps.get(ts_key),
                    "volume_24h_usd": volumes.get(ts_key),
                })
            log.info(f"  {cg_id}: {len(prices)} daily points")
            time.sleep(2)
        except Exception as e:
            log.warning(f"History failed for {cg_id}: {e}")

    df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=["coingecko_id", "date", "price_usd", "market_cap_usd", "volume_24h_usd"]
    )
    con.execute("DROP TABLE IF EXISTS cg_token_history")
    con.execute("CREATE TABLE cg_token_history AS SELECT * FROM df")
    log.info(f"cg_token_history: {len(df)} rows")
    return len(df)


def ingest_rwa_category(con) -> int:
    """Pull CoinGecko category data for RWA-related categories."""
    all_rows = []
    rwa_category_ids = [
        "real-world-assets-rwa",
        "tokenized-gold",
        "tokenized-us-treasuries",
        "asset-backed-tokens",
    ]

    for cat_id in rwa_category_ids:
        try:
            data = _get(f"{BASE}/coins/markets", params={
                "vs_currency": "usd",
                "category": cat_id,
                "order": "market_cap_desc",
                "per_page": 100,
                "page": 1,
                "sparkline": False,
            })
            for coin in data:
                all_rows.append({
                    "category_id": cat_id,
                    "coingecko_id": coin.get("id"),
                    "symbol": coin.get("symbol", "").upper(),
                    "name": coin.get("name"),
                    "market_cap_usd": coin.get("market_cap"),
                    "current_price_usd": coin.get("current_price"),
                    "total_volume_24h_usd": coin.get("total_volume"),
                    "price_change_pct_24h": coin.get("price_change_percentage_24h"),
                    "circulating_supply": coin.get("circulating_supply"),
                    "ingested_at": pd.Timestamp.utcnow().isoformat(),
                })
            log.info(f"  category {cat_id}: {len(data)} tokens")
            time.sleep(2)
        except Exception as e:
            log.warning(f"Category {cat_id} failed: {e}")

    df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=["category_id","coingecko_id","symbol","name","market_cap_usd",
                 "current_price_usd","total_volume_24h_usd","price_change_pct_24h",
                 "circulating_supply","ingested_at"]
    )
    n = append_snapshot(con, "cg_rwa_category", df)
    log.info(f"cg_rwa_category: {n} rows appended")
    return len(df)


def run():
    con = get_connection()
    results = {}

    try:
        results["rwa_tokens"] = ingest_token_metadata(con)
    except Exception as e:
        log.error(f"token metadata failed: {e}")

    try:
        results["token_history"] = ingest_token_history(con)
    except Exception as e:
        log.error(f"token history failed: {e}")

    try:
        results["rwa_category"] = ingest_rwa_category(con)
    except Exception as e:
        log.error(f"rwa category failed: {e}")

    con.close()
    log.info(f"CoinGecko ingestion complete: {results}")
    return results


if __name__ == "__main__":
    run()
