"""Solana vs Ethereum / EVM usage benchmark.

A dated snapshot from public dashboards, used on the Solana tab. These figures
are NOT refreshed by the weekly pipeline: rwa.xyz and DefiLlama's RWA chain
metrics have no free API. Update by hand (new `as_of`) when re-checking.

Definitions
- value: "distributed" RWA value on the network, stablecoins excluded (rwa.xyz).
  Distributed = the token can move between wallets on that chain; "represented"
  (records mirrored on-chain) is left out.
- holders: wallets holding at least one RWA on that network (rwa.xyz). A person
  with wallets on several chains is counted on each.
- defi_share: DeFi active TVL / active AUM (DefiLlama RWA dashboard).
- inactive_share: (on-chain AUM - active AUM) / on-chain AUM (DefiLlama).
- change_30d: 30-day change in active AUM (DefiLlama); mixes flows and price.
"""

AS_OF = "2026-11-24"

SOURCES = [
    ["rwa.xyz – Networks", "https://app.rwa.xyz/networks"],
    ["DefiLlama – RWA dashboard", "https://defillama.com/rwa"],
]

# Totals across all 40 networks rwa.xyz tracks (distributed, ex-stablecoins).
TOTAL_VALUE = 41_210_000_000
TOTAL_HOLDERS = 5_912_472

# name, family, distributed value (USD), RWA holders
NETWORKS = [
    ["Ethereum", "EVM (L1)", 14_415_072_454, 298_183],
    ["BNB Chain", "EVM (L1)", 6_307_743_445, 2_248_842],
    ["Solana", "Solana", 5_193_696_544, 1_085_759],
    ["Stellar", "Other", 4_861_497_105, 26_567],
    ["Avalanche", "EVM (L1)", 1_326_705_566, 10_110],
    ["Arbitrum", "EVM (L2)", 1_046_875_737, 14_352],
    ["Base", "EVM (L2)", 755_163_440, 198_279],
    ["Polygon", "EVM", 433_926_137, 22_784],
    ["Robinhood Chain", "EVM (L2)", 328_102_355, 1_712_830],
]

# DefiLlama RWA dashboard, chain pages.
DEFI = {
    "Solana":    {"defi_share": 34.5, "inactive_share": 29.0, "change_30d": -22.4},
    "Ethereum":  {"defi_share": 15.5, "inactive_share": 10.7, "change_30d": -15.6},
    "Arbitrum":  {"defi_share": 23.0},
    "Avalanche": {"defi_share": 30.0},
    "Base":      {"defi_share": 29.0, "change_30d": 108.0},
    "BNB Chain": {"defi_share": 0.6},
}

# Product-level evidence on Solana (DefiLlama RWA dashboard).
EVIDENCE = [
    ["OnRe", "DeFi-native credit", "89% of value used in DeFi"],
    ["Huma", "DeFi-native credit", "91% of value used in DeFi"],
    ["Hastra", "DeFi-native credit", "84% of value used in DeFi"],
    ["Kamino Lend", "Lending venue", "$461.8M of RWA collateral"],
    ["xStocks", "Tokenised stocks", "$525M on Solana, 11.7% in DeFi, +$74M in 7 days"],
    ["BlackRock BUIDL", "Institutional MMF", "0.65% of Solana supply in DeFi, −12.5% in 7 days"],
]


def benchmark():
    rows = []
    for name, family, value, holders in NETWORKS:
        d = DEFI.get(name, {})
        rows.append({
            "name": name, "family": family, "value": value, "holders": holders,
            "value_share": value / TOTAL_VALUE * 100,
            "holder_share": holders / TOTAL_HOLDERS * 100,
            "per_holder": value / holders if holders else None,
            "defi_share": d.get("defi_share"), "inactive_share": d.get("inactive_share"),
            "change_30d": d.get("change_30d"),
        })
    by = {r["name"]: r for r in rows}
    sol, eth = by["Solana"], by["Ethereum"]
    return {
        "as_of": AS_OF, "sources": SOURCES, "networks": rows, "evidence": EVIDENCE,
        "total_value": TOTAL_VALUE, "total_holders": TOTAL_HOLDERS,
        "holders_x": sol["holders"] / eth["holders"],
        "value_x": eth["value"] / sol["value"],
        "ticket_x": eth["per_holder"] / sol["per_holder"],
        "solana": sol, "ethereum": eth,
    }
