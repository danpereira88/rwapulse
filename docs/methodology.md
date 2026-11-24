# RWA Pulse — Methodology (v2, 24 Nov 2026)

How every number on the site is built. The code that implements each rule is
named so you can check it. Previous version: [v1 — protocol profiles](methodology-v1-profiles.md).

## 1. What counts: the asset universe

Asset-level, grouped by issuer (`ingest/rwa_universe.py`):

1. Every token in CoinGecko's *Real World Assets* and *Tokenized Gold* categories (top 250 by market cap each week),
2. **minus** governance and infrastructure tokens (LINK, XLM, ONDO, SYRUP…), wrapped crypto, plain stablecoins, and Figure HELOC (see §6),
3. **plus** a curated list of institutional funds CoinGecko lists outside those categories (e.g. Franklin BENJI, WisdomTree WTGXX, OpenEden TBILL),
4. **plus** DefiLlama protocols that still report TVL and have no token on CoinGecko (Maple, Re, Huma, OnRe, R25, USD AI, RealT).

Each asset gets one **asset class** by rule: *Treasuries & MMFs*, *Private & structured credit*, *Gold & commodities*, *Tokenised stocks & ETFs*, *Other*.
**Yield-bearing** = Treasuries & MMFs + credit. Gold, other RWAs and stocks are reported separately.

## 2. What each asset is worth

For each asset on each weekly snapshot, in order (`ui/insights.py`, `universe()`):

| Source | Used when |
|---|---|
| **On-chain supply × price** (`ingest/rwa_supply.py`) | every chain CoinGecko lists for the token was read **and** the total is within 0.75–1.5× of CoinGecko's market cap |
| **On-chain, partial** | some chains read, and CoinGecko has no market cap at all |
| **CoinGecko market cap** | otherwise, when CoinGecko has a figure |
| **DefiLlama protocol TVL** | issuers with no token (Maple etc.), and history for curated funds before on-chain reads began |

On-chain reads cover EVM chains (Ethereum, BNB, Polygon, Arbitrum, Base, Optimism, Avalanche, Mantle, Etherlink, Plume), Solana, Stellar, XRP Ledger, Aptos and Sui. Price/NAV comes from CoinGecko.
When a complete on-chain read disagrees with CoinGecko by more than the band, CoinGecko is used and the row is marked **sources disagree**.

## 3. Changes are like-for-like

Any change, growth multiple or drawdown compares **only assets valued at both dates from the same source**. A source switch (e.g. CoinGecko → on-chain) or an asset entering/leaving coverage therefore can't show up as a market move. Each like-for-like figure states what share of today's value it covers.

The weekly *level* charts are not like-for-like and say so; the dashed line holds assets that lost their source at their last known value.

## 4. Concentration

Top-5 share and HHI (sum of squared % shares) across **issuers**, on the yield-bearing universe.

## 5. Watch rules (written before the data)

| Watch | Fires when | Guard |
|---|---|---|
| 01 Non-Treasury breakout | an asset class other than Treasuries > 5% of yield-bearing value **and** ≥ 5× like-for-like since the first snapshot | — |
| 02 Ethereum below 70% | Ethereum's share of value read on-chain < 70% | needs ≥ 90% of value read on-chain over ≥ 2 weekly reads; otherwise "not enough data" |
| 03 An anchor issuer halves | a top-10 issuer is down ≥ 50% like-for-like vs any earlier snapshot | comparison must cover ≥ 80% of the issuer's value; "sources disagree" issuers excluded |

## 6. Known limits

- **Figure HELOC (~$29B on Provenance)** is excluded by this methodology as loan records rather than a freely transferable token. DefiLlama and rwa.xyz list it, so headline totals here run lower than theirs by that amount.
- **Fund-of-fund holdings are not yet netted out** (e.g. OUSG holds BUIDL), so some value is counted twice. Bridged tokens can also be double-counted where a bridge escrows on one chain and mints on another.
- History starts 15 May 2026 (weekly). Figures from the May 2026 study used a protocol-level method and are shown only as a labelled reference, not compared with live numbers.
- CoinGecko market caps for permissioned funds rely on issuer-reported supply and NAV.

## 7. Publishing safeguards

The weekly job publishes only if every pipeline step succeeded and the new snapshot passes `scripts/check_snapshot.py`: total non-zero, within ±25% of the last published snapshot, no more than 30% of value without a source, and data date not going backwards.

## Changelog

- **v2 — 24 Nov 2026.** Asset-level universe grouped by issuer; on-chain supply as a primary source; like-for-like changes; guarded watch rules. Replaces the protocol-level method after DefiLlama moved RWA assets to its paid API.
- **v1 — May 2026.** Protocol-level TVL from DefiLlama plus AI-assisted protocol profiles (L1/L2/L3 scores) — see [methodology-v1-profiles.md](methodology-v1-profiles.md).
