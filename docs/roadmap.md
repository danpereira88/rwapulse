# RWA Intelligence — Build Roadmap

Project: `rwa-data-exploration` → RWA Intelligence product.
Goal: monthly intelligence brief with issuer scorecards, built on a three-layer data stack.

---

## Layer architecture

Three layers, each buildable and queryable independently.

```
Layer 3 — On-chain activity          (mint/burn flows, holder concentration, transfer counts)
    ↑
Layer 2 — Static reference data      (protocol profiles, composite scores, legal/wrapper metadata)
    ↑
Layer 1 — TVL and market data        (DefiLlama TVL, CoinGecko prices, DuckDB pipeline)
```

Dashboard reads from Supabase. All analytical work stays in DuckDB.

---

### Layer 1 — TVL and market data ✅ Built

DefiLlama TVL time-series, CoinGecko prices, stablecoin history. DuckDB at `data/rwa.duckdb`. One-way sync to Supabase via `sync/sync_to_supabase.py`. Run with `python run_all.py`.

**What's in it:** 110,838 rows of protocol TVL across 180 slugs (2020–present). Daily token price snapshots. Stablecoin supply history. Full data capability audit at `reports/data_capability_audit.md`.

**Known exclusions from Supabase sync:** `sky-rwa` (governance wind-down distorts concentration), `blockchain-capital` (static $965M placeholder), `ondo-global-markets` (tokenised stocks, structurally distinct). Full exclusion rationale in `sync/README.md`.

---

### Layer 2 — Static reference data ✅ Built

AI-augmented protocol profiles in `protocol_profiles` Supabase table. One profile per product (or platform). Populated by `sync/gather_profile.py` using Claude with web search, then written to Supabase via `profile_io.write_profile()`.

**Current state:** Five anchor profiles complete and written to Supabase: BlackRock BUIDL (composite 4.4, Tier 2), Ondo USDY (3.4, Tier 4), Circle USYC (3.4, Tier 4), Ondo OUSG (3.4, Tier 4), Maple Finance (3.3, Tier 4), Centrifuge (3.3, Tier 4). Framework calibration produces meaningful differentiation across structurally different products. Composite computation is deterministic via SCORE_WEIGHTS. Cost per profile run: $0.19 after cache warm-up.

**Schema:** ~55 columns + 4 JSONB columns. Three analytical layers (L1 Underlying, L2 Wrapper, L3 On-chain), composite score, metadata. Full schema in `sync/PROFILES.md`. Field semantics and platform-vs-product notes in `docs/methodology.md`.

**Scoring framework:** L-scores (1–5) assigned by gather agent. Composite computed deterministically by the script from `SCORE_WEIGHTS` × L-scores. Asset-class weights: MMF (30/40/30), Tokenised Treasuries (30/40/30), Private Credit (35/35/30). Tier thresholds: 4.5–5.0 Tier 1 → below 3.0 Tier 5.

**Cost:** ~$0.55–0.65 per profile on first run (cold cache). ~$0.19 on re-run (warm cache, tokens cached from prior system prompt).

---

### Layer 3 — On-chain activity 🔨 Planned

Per-protocol on-chain enrichment: mint/burn flows, holder concentration (top-10 pct), transfer counts, active address growth. Joins to Layer 2 profile rows via `defillama_slug`.

**Design:** Read from public EVM RPC (no paid node required for v1). Etherscan API for holder counts and transfer volume. Results written to a new Supabase table `protocol_onchain_activity` keyed by (slug, date).

**v1 scope:** Ethereum-only on-chain reads. The DefiLlama TVL data is multichain (consolidated across all chains a protocol deploys to), and profile chains_deployed arrays correctly capture multichain footprint. But on-chain activity ingestion (transfer counts, mint/burn flows, holder concentration) in v1 reads from Ethereum only. Multichain EVM expansion (Arbitrum, Polygon, Base, Avalanche) is a planned v2 follow-on. Solana coverage is a v3 expansion given its non-EVM infrastructure differs structurally. Caveat for the methodology page: USDY specifically suffers from Ethereum-only coverage given its 10-chain growth strategy.

**Not started.** Do not begin until Layer 2 is validated against reader feedback.

---

## Decision log

- **DuckDB over Postgres for local analytics** (project start). Single-file, zero-config, fast for columnar aggregations. Supabase is for the dashboard read layer only; all analysis runs locally.

- **Free API sources only for v1** (project start). DefiLlama, CoinGecko (public endpoints), public RPC, The Graph, Etherscan free tier. Avoids API cost commitments before product-market fit is established.

- **One-way sync, no ORM** (project start). `sync_to_supabase.py` pushes from DuckDB → Supabase. No bidirectional sync. No abstraction layer. Simple, auditable, debuggable.

- **Supabase DDL via SQL Editor only** (Layer 2 build). PostgREST REST API cannot execute DDL. All schema changes are written as numbered migration files (`migrations/0XX_*.sql`) and applied manually via the Supabase SQL Editor. This keeps a full migration history without requiring a migration runner.

- **AI gather agent at temperature=1** (Layer 2 build). Required for the web_search tool. Prompt caching splits system prompt (static, cached) from user message (per-profile, not cached). Cache warm-up costs ~$0.48; subsequent re-runs ~$0.19.

- **Two rows for Ondo (USDY and OUSG separately)** (Layer 2 build). USDY and OUSG are legally distinct entities with different issuer structures, regulatory frameworks, and chain footprints. Separate rows reflect genuine structural differences rather than treating them as one "Ondo" profile.

- **Platform-level profile for Centrifuge** (Layer 2 build). Centrifuge hosts multiple pools with different underlying assets, SPV structures, and risk profiles. A single platform-level row captures the infrastructure layer; individual pools can be added as tracked profiles if needed.

- **Framework calibration confirmed across five anchor profiles** (May 2026). Composite spread is 1.1 points across Tiers 2-4. Ordering matches institutional intuition (BUIDL highest reflecting BlackRock institutional wrapper; Centrifuge lowest reflecting asset-agnostic platform structure with no consolidated attestation). Composite computation made deterministic via `SCORE_WEIGHTS` × L-scores in the script (previously delegated to agent, which caused temperature-driven variance). Two profiles received composite corrections during the post-milestone cleanup pass (USYC 3.3→3.4, Maple 3.35→3.30); no tier changes resulted. Framework will not be recalibrated until profile count exceeds 15 or reader feedback specifically requests it.

---

## Open questions to resolve later

- **Subgraph data coverage.** The Graph subgraphs for Maple, Centrifuge, and Goldfinch are wired up in `ingest/subgraphs.py` but not yet in the dashboard scope. Pool-level loan data (outstanding principal, default rate, WAL) would significantly strengthen the private credit profiles' L1 scores. Resolve before Layer 3 if possible.

- **CoinGecko API key.** Price history requires a paid CoinGecko key beyond the public endpoint's rate limit. Currently only current prices are reliable. Decide whether historical price data is worth the cost before building any price-return analytics.

- **`ondo-global-markets` exclusion.** Ondo's tokenised equities product is currently excluded from the TVL sync (structurally distinct). If the product scales, a separate asset_class entry or a parallel table may be warranted.

- **Private credit benchmark for yield gap.** Maple and Centrifuge's `l1_yield_gap_bps` currently uses 3-month T-bill as the benchmark. For private credit, SOFR+spread or the Cliffwater Direct Lending Index (DLI) would be more appropriate. Resolve before publishing yield comparisons that include private credit products.

- **L-score variance across re-gathers at temperature=1.** The deterministic composite fix in `gather_profile.py` solves the arithmetic layer of reproducibility — same L-scores now produce the same composite regardless of run. But the L-scores themselves vary across runs because the agent's judgment is non-deterministic at temperature=1 (Centrifuge's L3 was 3 on dry-run, 4 on write with identical structured inputs). This means a profile's tier could shift between gather runs even when nothing about the underlying product has changed, which is a credibility problem for an intelligence product. Three resolution options:
  - *Accept the variance:* Frame scores as "as-of-verified-date" snapshots that can legitimately update as the agent's view evolves. Honest, but undersells the framework's stability claim.
  - *Lock scores on re-runs:* If a profile already has L-scores, leave them alone unless an explicit `--rescore` flag is passed. The structured fields update on re-run; the scores don't.
  - *Separate deterministic scoring step:* The agent populates structured fields; a rule-based or low-temperature scoring function computes L-scores from the populated fields using an explicit rubric.

  Decision deferred. Re-evaluate before profile count exceeds 15, or earlier if a reader specifically asks why a score changed between two visits.

---

_Last updated: 2026-05-25_
