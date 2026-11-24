# RWA Intelligence — Profile Methodology

This document describes the conventions, scoring framework, and semantic notes for the `protocol_profiles` dataset. It is written for analysts consuming the data and for engineers maintaining the gather workflow.

---

## Scoring framework

Each profile is scored across three analytical layers. Layer scores are integers 1–5 assigned by the AI gather agent based on documented evidence quality.

| Layer | Description | What 1 means | What 5 means |
|---|---|---|---|
| L1 — Underlying | Asset quality, holdings transparency, auditor | No documentation, opaque composition | Institutional-grade with Big Four audit and full attestation |
| L2 — Wrapper | Legal structure, compliance, operational risk | No legal structure, no investor protections | Bankruptcy-remote SPV, regulated custodian, third-party audit |
| L3 — On-chain | Contract security, composability, secondary market | Unaudited, illiquid, no DeFi integrations | Multi-audited, deep liquidity, broad DeFi composability |

### Composite score

The composite score is computed **deterministically by the script** from L-scores and asset-class weights. The AI agent's composite is informational only and is overridden.

| Asset class | L1 weight | L2 weight | L3 weight |
|---|---|---|---|
| MMF | 30% | 40% | 30% |
| Tokenised Treasuries | 30% | 40% | 30% |
| Private Credit | 35% | 35% | 30% |
| Structured Credit | 35% | 35% | 30% |
| Commodity | 25% | 35% | 40% |
| Equity | 25% | 35% | 40% |
| Other | 33% | 34% | 33% |

Tier thresholds: 4.5–5.0 = Tier 1, 4.0–4.4 = Tier 2, 3.5–3.9 = Tier 3, 3.0–3.4 = Tier 4, below 3.0 = Tier 5.

---

## Platform-level vs. product-level profiles

Most profiles in this dataset are **product-level** — they describe a single tokenized product (BUIDL, USYC, OUSG, USDY, Maple). For these profiles, fields like `l3_primary_contract` and `l3_holder_count` describe the product directly.

A small number of profiles are **platform-level** — they describe a tokenization platform that hosts multiple products (Centrifuge being the clearest example; Ondo's profile sits between the two, with USDY and OUSG as separate product-level entries). For platform-level profiles:

- `l3_primary_contract` typically refers to the platform's governance or representative token, not a pool/vault contract
- `l3_holder_count` typically refers to the governance token's holder count, not product investor count
- Yield fields (`l1_yield_current`, `l1_yield_benchmark`, `l1_yield_gap_bps`) may be NULL because no single platform-wide yield applies
- `l1_holdings_composition` may describe the range of asset classes rather than a specific composition
- `l2_eligible_investors` reflects the most restrictive baseline across pools, not a product-specific eligibility

When comparing across profiles, these semantic differences should be noted. Cross-profile comparisons of holder count or primary contract metadata are not always like-for-like between platform-level and product-level entries.

**Current platform-level profiles:** Centrifuge

**Current product-level profiles:** BUIDL, USYC, OUSG, USDY, Maple Finance

---

## Launch date convention

`launch_date` is set to the **regulatory launch date** — the Form D filing date for US-structured products, or the equivalent first legal offering date for offshore products. This is preferred over:

- On-chain contract deployment date (earlier, but pre-commercialisation)
- First public announcement date
- First TVL appearance on DefiLlama

Where the on-chain deployment date differs materially from the regulatory launch date, both are captured in `asset_specific` JSONB under the keys `on_chain_deployment_date` and `regulatory_launch_date`.

---

## Chain naming convention

Canonical chain names used across all profiles:

| Chain | Canonical name | Deprecated aliases |
|---|---|---|
| Ethereum mainnet | Ethereum | ETH, ETH Mainnet |
| BNB Smart Chain | BNB Chain | BSC, Binance Smart Chain |
| XRP Ledger | XRP Ledger | XRPL |
| Arbitrum One | Arbitrum | Arbitrum One |
| Optimism | Optimism | OP Mainnet |
| Hyperliquid EVM | HyperEVM | Hyperliquid EVM |

All others use the form from the [DefiLlama chain list](https://api.llama.fi/chains).

---

## Field confidence levels

Each populated field may carry a confidence tag in `field_confidence`:

| Level | Meaning |
|---|---|
| `high` | Sourced from a primary document (fund prospectus, SEC filing, official smart contract, on-chain data) |
| `medium` | Sourced from official marketing/documentation with no independent corroboration |
| `low` | Sourced from a secondary or aggregator source; should be verified before reliance |

Fields with no confidence entry are NULL (not applicable or no credible source found). A confidence entry is only present when a field is populated.

---

## Activity tier rubric

Activity tier captures how actively a token is used relative to its structural design. The rubric differs by token type because restricted tokens (whitelist-enforced) measure activity primarily through issuance/redemption events, while permissionless tokens measure it through aggregate transfer activity.

**For transfer-restricted tokens (whitelist-only access):**

| Tier | Mint + burn events in 30 days |
|---|---|
| dormant | 0 |
| low | 1–10 |
| moderate | 11–50 |
| high | 51–200 |
| very_high | >200 |

**For permissionless tokens:**

| Tier | Transfer events in 30 days |
|---|---|
| dormant | ≤10 |
| low | 11–100 |
| moderate | 101–1,000 |
| high | 1,001–10,000 |
| very_high | >10,000 |

Thresholds were calibrated against the May 2026 anchor set (BUIDL, USYC, OUSG, USDY, syrupUSDC, CFG) after the first live run produced a four-orders-of-magnitude range (47–37,419 transfers). The original four-tier rubric assigned all six anchors to 'high'; the calibrated five-tier rubric produces meaningful differentiation. Thresholds may be recalibrated as the tracked product set expands beyond 15 profiles.

---

## Data layer boundaries

Profile data is populated by two distinct workflows that write to disjoint field sets:

**Gather workflow** (`sync/gather_profile.py`)
- Populates Identity fields, Layer 1/2/3 analytical fields, editorial summaries, framework scores, source citations, and confidence flags
- Runs on demand per protocol (typically quarterly per profile)
- Uses AI-augmented research with web search and primary-source citation

**Enrichment workflow** (`enrich/enrich_onchain_activity.py`)
- Populates activity fields (`transfer_count_30d`, `mint_count_30d`, `activity_tier`, `activity_vs_tvl_ratio`, etc.) and the `l3_holder_count` field
- Runs daily for all anchor profiles
- Reads Ethereum on-chain data via Etherscan API and direct RPC

The two workflows never modify each other's fields. The enrichment workflow does not touch Identity, analytical, or editorial sections. The gather workflow does not touch activity metrics. This separation keeps each workflow's outputs traceable to its inputs and prevents one workflow from silently corrupting data populated by the other.

---

## activity_vs_tvl_ratio limitations

For some profiles, `activity_vs_tvl_ratio` is computed against an aggregator-level TVL figure rather than a product-specific one, due to how DefiLlama categorizes the underlying products:

- **OUSG and USDY** both share TVL from the `ondo-yield-assets` slug. DefiLlama does not track them separately. The individual product ratios are therefore approximate — each is inflated because the denominator is shared. The OUSG ratio will appear very low (institutional issuance activity is small relative to combined TVL) and the USDY ratio will appear elevated (active DeFi token against a combined denominator).
- **Maple's** TVL covers the `maple` slug, which is platform-aggregate (syrupUSDC plus legacy lending pools). The activity metrics measure syrupUSDC specifically, so the ratio under-represents how active syrupUSDC capital actually is.

These limitations reflect DefiLlama's aggregation granularity rather than measurement error. For more precise per-product TVL, future work will compute total supply directly from on-chain reads. For BUIDL, USYC, and Centrifuge, where the DefiLlama slug matches the product directly, the ratio is product-specific.

---

## Source conflict vs. identity discrepancy

Two JSONB columns track data quality issues:

**`identity_discrepancies`** — contradictions between the gather agent's research and the canonical Identity section fields (`product_name`, `issuer_name`, `tokenisation_platform`, `launch_date`, `asset_class`, `chains_deployed`). These require a human to update the seed row. The agent is instructed not to incorporate contradictions into prose.

**`source_conflicts`** — disagreements between two credible third-party sources on a non-Identity field (e.g. auditor name, custodian, holder count). These require human review to determine which source is authoritative; neither value is written to the primary field.
