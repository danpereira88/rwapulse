"""
Chart generation for the NAV drift analysis.

Produces:
  - One per-token chart (price vs NAV + drift panel)
  - One comparison chart (mean drift ± 1σ, and drift volatility ranking)

Style: clean, minimal, suitable for institutional readers.
"""

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "reports" / "nav_drift"

# ── shared style ──────────────────────────────────────────────────────────────
STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": "#e0e0e0",
    "grid.linewidth": 0.6,
    "axes.facecolor": "#fafafa",
    "figure.facecolor": "white",
    "legend.frameon": False,
    "legend.fontsize": 8,
}

THRESHOLD_COLORS = {"25": "#aaaaaa", "50": "#cc4444"}
SOURCE_NOTE = "Sources: DefiLlama coins API (token prices), FRED DGS3MO (3M T-bill). Theoretical NAV compounds the T-bill rate daily from the first observed price."


def _apply_style():
    plt.rcParams.update(STYLE)


def _format_date_axis(ax, df):
    span_days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    if span_days > 300:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    elif span_days > 90:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    else:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")


def plot_token(
    symbol: str,
    token_meta: dict,
    df: pd.DataFrame,
    metrics: dict,
) -> Path:
    """
    Two-panel chart:
      Top: observed price vs theoretical NAV
      Bottom: drift in bps with ±25 and ±50 bps reference lines
    """
    _apply_style()
    color = token_meta.get("color", "#1f3a5f")

    fig, (ax_price, ax_drift) = plt.subplots(
        2, 1, figsize=(9, 6), sharex=True,
        gridspec_kw={"height_ratios": [2, 1], "hspace": 0.08}
    )

    # ── Top panel: price vs NAV ───────────────────────────────────────────────
    ax_price.plot(df["date"], df["price"], color=color, lw=1.5, label="Observed price", zorder=3)
    ax_price.plot(df["date"], df["nav"], color="#888888", lw=1.2, ls="--", label="Theoretical NAV (T-bill)", zorder=2)

    ax_price.set_ylabel("Price (USD)", fontsize=9)
    ax_price.legend(loc="upper left")

    # Annotate current drift
    last_drift = df["drift_bps"].iloc[-1]
    sign = "+" if last_drift >= 0 else ""
    ax_price.annotate(
        f"Latest drift: {sign}{last_drift:.0f} bps",
        xy=(df["date"].iloc[-1], df["price"].iloc[-1]),
        xytext=(-90, 10),
        textcoords="offset points",
        fontsize=8,
        color=color,
        arrowprops=dict(arrowstyle="-", color=color, lw=0.8),
    )

    title = f"{symbol} — {token_meta['name']} ({token_meta['issuer']})"
    period = f"{metrics['start_date']} → {metrics['end_date']}  |  {metrics['obs_days']} observations"
    ax_price.set_title(f"{title}\n{period}", fontsize=10, loc="left", pad=8)

    # ── Bottom panel: drift in bps ────────────────────────────────────────────
    ax_drift.axhline(0, color="#444444", lw=0.8, zorder=2)
    for threshold, tc in THRESHOLD_COLORS.items():
        ax_drift.axhline(int(threshold), color=tc, lw=0.7, ls=":", zorder=2)
        ax_drift.axhline(-int(threshold), color=tc, lw=0.7, ls=":", zorder=2)
        ax_drift.text(
            df["date"].iloc[-1], int(threshold),
            f" +{threshold}bps", va="center", fontsize=7, color=tc
        )
        ax_drift.text(
            df["date"].iloc[-1], -int(threshold),
            f" -{threshold}bps", va="center", fontsize=7, color=tc
        )

    # Fill above/below zero
    ax_drift.fill_between(df["date"], df["drift_bps"], 0,
                          where=df["drift_bps"] >= 0,
                          color=color, alpha=0.25, zorder=3)
    ax_drift.fill_between(df["date"], df["drift_bps"], 0,
                          where=df["drift_bps"] < 0,
                          color="#cc4444", alpha=0.25, zorder=3)
    ax_drift.plot(df["date"], df["drift_bps"], color=color, lw=0.8, zorder=4)

    ax_drift.set_ylabel("Drift (bps)", fontsize=9)

    # Stats annotation
    stats_txt = (
        f"Mean {metrics['mean_drift_bps']:+.0f} bps  |  "
        f"Vol {metrics['drift_vol_bps']:.0f} bps  |  "
        f"Max premium {metrics['max_premium_bps']:+.0f}  |  "
        f"Max discount {metrics['max_discount_bps']:+.0f}"
    )
    ax_drift.set_xlabel(stats_txt, fontsize=7.5, labelpad=6)

    _format_date_axis(ax_drift, df)

    # Source note
    fig.text(0.01, 0.01, SOURCE_NOTE, fontsize=6.5, color="#666666",
             va="bottom", ha="left", wrap=True)

    fig.tight_layout(rect=[0, 0.03, 1, 1])
    out = OUTPUT_DIR / f"nav_drift_{symbol.lower()}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved chart: {out}")
    return out


def plot_comparison(all_metrics: list[dict], token_meta: dict) -> Path:
    """
    Two-panel comparison across tokens:
      Left: Mean drift ± 1σ (horizontal bar)
      Right: Drift volatility ranking
    """
    _apply_style()

    # Sort by absolute mean drift for the left panel
    sorted_m = sorted(all_metrics, key=lambda x: abs(x["mean_drift_bps"]))
    symbols = [m["symbol"] for m in sorted_m]
    means = [m["mean_drift_bps"] for m in sorted_m]
    vols = [m["drift_vol_bps"] for m in sorted_m]
    colors = [token_meta[s]["color"] for s in symbols]
    errors = vols  # ±1σ error bars

    fig, (ax_l, ax_r) = plt.subplots(1, 2, figsize=(10, 4))

    # ── Left: mean drift ──────────────────────────────────────────────────────
    y_pos = np.arange(len(symbols))
    bars = ax_l.barh(y_pos, means, xerr=errors, color=colors, alpha=0.85,
                     error_kw={"elinewidth": 1.2, "capsize": 4, "capthick": 1.2},
                     height=0.55, zorder=3)
    ax_l.axvline(0, color="#333333", lw=1.0, zorder=4)
    ax_l.axvline(25, color=THRESHOLD_COLORS["25"], lw=0.7, ls=":", zorder=2)
    ax_l.axvline(-25, color=THRESHOLD_COLORS["25"], lw=0.7, ls=":", zorder=2)
    ax_l.axvline(50, color=THRESHOLD_COLORS["50"], lw=0.7, ls=":", zorder=2)
    ax_l.axvline(-50, color=THRESHOLD_COLORS["50"], lw=0.7, ls=":", zorder=2)
    ax_l.set_yticks(y_pos)
    ax_l.set_yticklabels(symbols, fontsize=10)
    ax_l.set_xlabel("Mean drift (bps)  ± 1σ", fontsize=9)
    ax_l.set_title("Mean NAV drift by token", fontsize=10, loc="left")

    # Label each bar
    for i, (mean, vol) in enumerate(zip(means, vols)):
        sign = "+" if mean >= 0 else ""
        ax_l.text(mean + (5 if mean >= 0 else -5), i,
                  f"{sign}{mean:.0f}", va="center",
                  ha="left" if mean >= 0 else "right", fontsize=8)

    # ── Right: drift volatility ───────────────────────────────────────────────
    # Sort by volatility descending
    sorted_v = sorted(all_metrics, key=lambda x: x["drift_vol_bps"], reverse=True)
    sym_v = [m["symbol"] for m in sorted_v]
    vol_v = [m["drift_vol_bps"] for m in sorted_v]
    col_v = [token_meta[s]["color"] for s in sym_v]
    y_pos2 = np.arange(len(sym_v))

    ax_r.barh(y_pos2, vol_v, color=col_v, alpha=0.85, height=0.55, zorder=3)
    ax_r.set_yticks(y_pos2)
    ax_r.set_yticklabels(sym_v, fontsize=10)
    ax_r.set_xlabel("Drift volatility — 1σ (bps)", fontsize=9)
    ax_r.set_title("NAV drift volatility by token", fontsize=10, loc="left")

    for i, v in enumerate(vol_v):
        ax_r.text(v + 0.5, i, f"{v:.0f}", va="center", ha="left", fontsize=8)

    fig.suptitle(
        "Tokenised US Treasury NAV Drift — Cross-token comparison",
        fontsize=11, x=0.01, ha="left", y=1.02
    )
    fig.text(0.01, -0.04, SOURCE_NOTE, fontsize=6.5, color="#666666")

    fig.tight_layout()
    out = OUTPUT_DIR / "nav_drift_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved comparison chart: {out}")
    return out
