"""
Core drift computation for price-accumulating tokenised Treasury tokens.

Theoretical NAV methodology
───────────────────────────
For a price-accumulating token, the balance stays constant and the price
rises as yield accrues.  The theoretical NAV at day t is:

    nav_t = anchor_price × ∏_{i=0}^{t-1} (1 + r_i / 365)

where r_i is the 3-month T-bill annualised yield (decimal) on day i, and
anchor_price is the observed price on the first day of the analysis window.

Drift is then:
    drift_bps_t = (price_t − nav_t) / nav_t × 10,000

Important caveat: for restricted tokens (USDY, OUSG, USYC, USTB) the
DefiLlama "price" is the oracle-reported NAV, not a secondary market price.
These tokens have limited or no open secondary markets.  Drift therefore
measures how the fund's realised yield diverges from the T-bill benchmark,
plus any oracle staleness artefacts — not secondary market friction per se.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MIN_DAYS = 60  # drop tokens with fewer observations


def _merge_and_align(
    prices: pd.DataFrame,
    tbill: pd.DataFrame,
) -> Optional[pd.DataFrame]:
    """
    Inner-join prices to T-bill rates on date.
    Forward-fill T-bill on days the FRED series has no observation.
    """
    # Build a full daily date range covering the price series
    date_range = pd.date_range(prices["date"].min(), prices["date"].max(), freq="D")
    grid = pd.DataFrame({"date": date_range})

    # Bring in T-bill, forward-fill across the grid
    tb = tbill[["date", "rate_pct"]].copy()
    merged = grid.merge(tb, on="date", how="left")
    merged["rate_pct"] = merged["rate_pct"].ffill().bfill()

    # Join prices
    merged = merged.merge(prices[["date", "price"]], on="date", how="left")
    # Forward-fill price gaps (oracle may not update every day)
    merged["price"] = merged["price"].ffill()
    merged = merged.dropna(subset=["price", "rate_pct"])
    return merged.reset_index(drop=True)


def compute_nav_drift(
    symbol: str,
    prices: pd.DataFrame,
    tbill: pd.DataFrame,
) -> Optional[pd.DataFrame]:
    """
    Return a daily DataFrame with columns:
      date, price, nav, drift_bps, rate_pct
    Returns None if insufficient data.
    """
    if len(prices) < MIN_DAYS:
        log.warning(f"{symbol}: only {len(prices)} obs — skipping (min {MIN_DAYS})")
        return None

    df = _merge_and_align(prices, tbill)
    if df is None or len(df) < MIN_DAYS:
        log.warning(f"{symbol}: insufficient aligned data — skipping")
        return None

    anchor = df["price"].iloc[0]

    # Daily compounding factor from T-bill rate (annualised %)
    # We use the rate from day i-1 to grow from nav_{i-1} to nav_i
    daily_factor = 1.0 + df["rate_pct"] / 100.0 / 365.0
    # Shift so day 0 contributes to day 1's growth
    shifted_factor = daily_factor.shift(1).fillna(1.0)
    cum_factor = shifted_factor.cumprod()

    df["nav"] = anchor * cum_factor
    df["drift_bps"] = (df["price"] - df["nav"]) / df["nav"] * 10_000

    log.info(
        f"{symbol}: {len(df)} days, drift mean={df['drift_bps'].mean():.1f} bps, "
        f"vol={df['drift_bps'].std():.1f} bps"
    )
    return df


def compute_metrics(symbol: str, df: pd.DataFrame) -> dict:
    """
    Summarise the drift series into scalar metrics used in the report.
    """
    drift = df["drift_bps"]
    price = df["price"]

    # Annualised realised yield from price appreciation
    days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    realised_yield_pct = (
        (price.iloc[-1] / price.iloc[0]) ** (365.0 / max(days, 1)) - 1
    ) * 100

    # Average T-bill yield over same period
    avg_tbill_pct = df["rate_pct"].mean()

    # Yield gap: positive = outperformed T-bill
    yield_gap_bps = (realised_yield_pct - avg_tbill_pct) * 100

    # Oracle staleness: fraction of days where price did not change vs prior day
    price_unchanged = (price.diff().abs() < 1e-8).sum()
    stale_pct = price_unchanged / len(price) * 100

    # Autocorrelation of drift at lag 1 (persistence)
    drift_autocorr = drift.autocorr(lag=1)

    return {
        "symbol": symbol,
        "obs_days": len(df),
        "start_date": df["date"].iloc[0].strftime("%Y-%m-%d"),
        "end_date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "anchor_price": df["price"].iloc[0],
        "end_price": df["price"].iloc[-1],
        "mean_drift_bps": round(drift.mean(), 1),
        "drift_vol_bps": round(drift.std(), 1),
        "max_premium_bps": round(drift.max(), 1),
        "max_discount_bps": round(drift.min(), 1),
        "realised_yield_pct": round(realised_yield_pct, 2),
        "avg_tbill_pct": round(avg_tbill_pct, 2),
        "yield_gap_bps": round(yield_gap_bps, 1),
        "stale_pct": round(stale_pct, 1),
        "drift_autocorr": round(drift_autocorr, 3) if not np.isnan(drift_autocorr) else None,
        # Days where drift exceeded thresholds
        "days_beyond_25bps": int((drift.abs() > 25).sum()),
        "days_beyond_50bps": int((drift.abs() > 50).sum()),
    }
