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

## 6a. Chain sweep: full coverage on 15 chains

The main on-chain reader covers the RWA Pulse universe. The chain views instead read **every tokenised asset CoinGecko lists on the chain** (`ingest/chain_sweep.py`, weekly, all chains in one pass). Chains swept: Solana; the EVM chains Ethereum, BNB Chain, Polygon, Arbitrum, Base, Optimism, Avalanche, Mantle, Plume and Etherlink; the XRP Ledger, Stellar, Aptos and Sui. Every chain goes through the same rules below; only the supply call differs.

1. **Which tokens.** Every coin in CoinGecko's tokenised-asset categories (stocks, ETFs, pre-IPO, treasuries, MMFs, credit, gold, silver, commodities, Ondo and Securitize ecosystems, and the broad RWA category) that has a token on the chain. A coin only in the broad RWA category must also match an asset pattern (treasury, credit, gold, stock…), which keeps out platform and governance tokens; the universe's exclusion list applies too.
2. **Supply.** EVM chains: `totalSupply` and `decimals`, batched `eth_call`. XRP Ledger: issued currencies from the issuer's `gateway_balances` obligations; Multi-Purpose Tokens from the issuance's `OutstandingAmount` and `AssetScale`. Stellar: Horizon's asset record (balances plus claimable balances, liquidity pools and Soroban contracts). Aptos and Sui: the chain's total-supply view. Solana: read from the mint account (`getMultipleAccounts`, 100 mints a call). Token-2022 extensions are applied: the scaled-UI multiplier used for stock splits and dividends (xStocks and others; multipliers range from 0.1× to 10×) and continuously compounding interest-bearing notes (e.g. Etherfuse).
3. **Minted vs circulating.** Issuers often pre-mint inventory into a wallet they control — some xStocks keep over 90% of their supply there. That inventory is subtracted, so the figure is what's held by everyone else. On Solana, the wallets checked are the mint's own authorities (mint, freeze, multiplier, metadata authorities, permanent delegate). ERC-20s have no such fields, so on Ethereum the checks are the contract's `owner()`, the issuer role wallets many RWA tokens expose (`minter`, `burner`, `pauser`, `multiplierUpdater` — xStocks keep inventory with the burner), the token contract itself and any known issuer treasuries. Issuers using other role schemes can be missed, which the market-cap cross-check partly catches. On the XRP Ledger and Stellar the issuing account can't hold its own token, so supply is already net of the issuer, but a separate distribution wallet still counts. On Aptos and Sui no netting is done yet: the figure is total supply. Inventory an issuer keeps elsewhere can't be told apart from real holders and is still counted. Each chain's netting is shown next to its figures, so chains are compared knowing this.
4. **Value.** Circulating supply × CoinGecko price. For assets the main reader also covers, its reading is used instead (it knows the asset's other chains).
5. **Cross-check.** A token whose circulating value on one chain is more than 1.5× CoinGecko's market cap for the whole asset — or, for stocks and commodities, over $1M with no market cap at all — is shown but not counted ("sources disagree"). Treasury, MMF and credit funds with no market cap are taken at face value: they mint on subscription rather than pre-minting. Tokens counted through DefiLlama (OnRe) are exempt.

Assets CoinGecko doesn't list on a chain are still missing; this matters most on chains where issuers list late (XRP Ledger, Stellar, Aptos, Sui), so their figures are a floor. A failed sweep never blocks the weekly publish; the page shows the sweep's date.

## 6b. Solana vs Ethereum benchmark

The Solana tab compares distribution across chains. Two parts:

- **Same asset, both chains** — from our own weekly on-chain reads: for each asset read on both Solana and Ethereum, the value on each. Refreshed weekly.
- **Holders, average holding, DeFi use, inactive supply** — a dated snapshot from rwa.xyz (distributed value excluding stablecoins, wallets holding an RWA) and DefiLlama's RWA dashboard (DeFi active TVL / active AUM; on-chain AUM not counted as active). Neither has a free API, so these numbers live in `ui/chain_benchmark.py` with an `as_of` date and are updated by hand. Holder counts count a person once per chain and can be inflated by airdrops or empty wallets.

## 7. Publishing safeguards

The weekly job publishes only if every pipeline step succeeded and the new snapshot passes `scripts/check_snapshot.py`: total non-zero, within ±25% of the last published snapshot, no more than 30% of value without a source, and data date not going backwards.

## Changelog

- **v2.2 — 28 Nov 2026.** Chain sweep extended from Solana and Ethereum to 15 chains (all EVM chains read, XRP Ledger incl. Multi-Purpose Tokens, Stellar, Aptos, Sui), same inclusion and cross-check rules on every chain.
- **v2.1 — 24 Nov 2026.** Solana and Ethereum tabs: full chain sweeps with Token-2022 multipliers and issuer inventory netted out; Solana vs Ethereum benchmark.
- **v2 — 24 Nov 2026.** Asset-level universe grouped by issuer; on-chain supply as a primary source; like-for-like changes; guarded watch rules. Replaces the protocol-level method after DefiLlama moved RWA assets to its paid API.
- **v1 — May 2026.** Protocol-level TVL from DefiLlama plus AI-assisted protocol profiles (L1/L2/L3 scores) — see [methodology-v1-profiles.md](methodology-v1-profiles.md).
