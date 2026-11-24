"""
Fetch and cache:
  1. 3-month T-bill daily yields from FRED (DGS3MO series)
  2. Token price history from DefiLlama coins API

Both are free, no API key required.
Results are cached as CSV in reports/nav_drift/cache/ to avoid re-fetching.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent.parent / "reports" / "nav_drift" / "cache"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO"
LLAMA_BASE = "https://coins.llama.fi/chart"

# Fetch window: 2024-01-01 through today
FETCH_START_TS = 1704067200  # 2024-01-01 00:00 UTC
CHUNK_DAYS = 490             # DefiLlama caps at 500 points per request


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.csv"


def fetch_tbill_rates(force: bool = False) -> pd.DataFrame:
    """
    Return a DataFrame with columns [date, rate_pct] for the 3M T-bill.
    rate_pct is the annualised yield in percent (e.g. 5.25).
    Missing observations (weekends/holidays) are forward-filled.
    """
    cache = _cache_path("tbill_dgs3mo")
    if cache.exists() and not force:
        log.info("Loading T-bill rates from cache")
        df = pd.read_csv(cache, parse_dates=["date"])
        return df

    log.info("Fetching 3M T-bill rates from FRED...")
    r = requests.get(FRED_URL, timeout=30)
    r.raise_for_status()

    lines = r.text.strip().splitlines()
    rows = []
    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) != 2:
            continue
        date_str, val_str = parts
        if val_str.strip() == ".":
            rows.append({"date": pd.to_datetime(date_str), "rate_pct": None})
        else:
            try:
                rows.append({"date": pd.to_datetime(date_str), "rate_pct": float(val_str)})
            except ValueError:
                continue

    df = pd.DataFrame(rows)
    df = df.sort_values("date").reset_index(drop=True)
    # Forward-fill missing (weekends, holidays) — use last known rate
    df["rate_pct"] = df["rate_pct"].ffill()
    df = df.dropna(subset=["date"])

    df.to_csv(cache, index=False)
    log.info(f"T-bill: {len(df)} observations, {df['date'].min().date()} → {df['date'].max().date()}")
    return df


def _fetch_llama_chunk(address: str, start_ts: int, span: int) -> list[dict]:
    """Fetch one chunk of price history from DefiLlama coins API."""
    url = f"{LLAMA_BASE}/{address}"
    params = {"start": start_ts, "span": span, "period": "1d"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    coins = data.get("coins", {})
    if not coins:
        return []
    key = list(coins.keys())[0]
    return coins[key].get("prices", [])


def fetch_token_prices(symbol: str, address: str, force: bool = False) -> pd.DataFrame:
    """
    Return a DataFrame with columns [date, price] for the given token,
    fetched from the DefiLlama coins API in up to two chunks.
    """
    cache = _cache_path(f"prices_{symbol.lower()}")
    if cache.exists() and not force:
        log.info(f"Loading {symbol} prices from cache")
        df = pd.read_csv(cache, parse_dates=["date"])
        return df

    log.info(f"Fetching {symbol} prices from DefiLlama...")
    all_points = []

    # Chunk 1: FETCH_START_TS → FETCH_START_TS + CHUNK_DAYS days
    chunk1 = _fetch_llama_chunk(address, FETCH_START_TS, CHUNK_DAYS)
    all_points.extend(chunk1)
    time.sleep(0.5)

    # Chunk 2: from end of chunk 1 → today
    if chunk1:
        last_ts = chunk1[-1]["timestamp"]
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        remaining_days = max(1, (now_ts - last_ts) // 86400 + 2)
        if remaining_days > 5:
            chunk2 = _fetch_llama_chunk(address, last_ts, min(remaining_days, CHUNK_DAYS))
            all_points.extend(chunk2)
            time.sleep(0.5)

    if not all_points:
        log.warning(f"{symbol}: no price data returned")
        return pd.DataFrame(columns=["date", "price"])

    df = pd.DataFrame([
        {
            "date": datetime.fromtimestamp(p["timestamp"], tz=timezone.utc).date(),
            "price": p["price"],
        }
        for p in all_points
    ])
    df["date"] = pd.to_datetime(df["date"])
    # Deduplicate (keep last observation per date)
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last").reset_index(drop=True)

    df.to_csv(cache, index=False)
    log.info(
        f"{symbol}: {len(df)} daily obs, "
        f"{df['date'].min().date()} → {df['date'].max().date()}, "
        f"price {df['price'].min():.4f}–{df['price'].max():.4f}"
    )
    return df
