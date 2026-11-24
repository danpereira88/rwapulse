-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 001: Protocol profiles schema
-- RWA Intelligence Dashboard
--
-- Design: one table (protocol_profiles) — all seven sections, 1:1 per protocol.
-- JSONB columns handle confidence flags, citations, and asset-class-specific extras.
-- Soft FK to dashboard_protocol_daily_tvl.slug via defillama_slug.
--
-- Apply via:
--   Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS protocol_profiles (

    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- ── Section 1: Identity ───────────────────────────────────────────────────
    -- defillama_slug is the join key to dashboard_protocol_daily_tvl.slug
    defillama_slug                  TEXT        UNIQUE,
    product_name                    TEXT        NOT NULL,
    common_name                     TEXT,
    issuer_name                     TEXT,
    tokenisation_platform           TEXT,
    launch_date                     DATE,
    asset_class                     TEXT        CHECK (asset_class IN (
                                        'MMF',
                                        'Tokenised Treasuries',
                                        'Private Credit',
                                        'Structured Credit',
                                        'Commodity',
                                        'Equity',
                                        'Other'
                                    )),
    -- Editorial tier: Anchor = major well-documented, Tracked = active thinner data,
    -- Reference = in TVL data but not deeply profiled
    tier                            TEXT        CHECK (tier IN ('Anchor', 'Tracked', 'Reference')),
    -- Array of chain names, e.g. ARRAY['Ethereum','Base','Arbitrum']
    chains                          TEXT[],

    -- ── Section 2: Editorial summary ─────────────────────────────────────────
    -- 80-120 word paragraph summarising the product
    editorial_summary               TEXT,

    -- ── Section 3: Layer 1 — The Underlying ──────────────────────────────────
    -- Narrative or JSON-structured description of portfolio holdings
    l1_holdings_composition         TEXT,
    -- Weighted average maturity of holdings in days
    l1_wam_days                     INTEGER,
    -- Annualised yield; stored as decimal e.g. 5.2300 = 5.23%
    l1_yield_current                NUMERIC(6,4),
    l1_yield_benchmark              NUMERIC(6,4),
    -- Spread over benchmark in basis points (positive = above benchmark)
    l1_yield_gap_bps                INTEGER,
    l1_fund_administrator           TEXT,
    l1_auditor                      TEXT,
    l1_score                        SMALLINT    CHECK (l1_score BETWEEN 1 AND 5),
    l1_score_rationale              TEXT,

    -- ── Section 4: Layer 2 — The Wrapper ─────────────────────────────────────
    -- e.g. 'Delaware LP', 'Cayman Fund', 'Irish UCITS'
    l2_legal_structure              TEXT,
    -- e.g. 'Reg D 506(c)', 'Reg S', '3(c)(7)'
    l2_securities_classification    TEXT,
    -- Registered legal name of the issuing entity
    l2_issuer_legal_entity          TEXT,
    l2_custodian                    TEXT,
    -- Transfer agent or token issuer responsible for on-chain registry
    l2_token_transfer_agent         TEXT,
    l2_nav_cadence                  TEXT        CHECK (l2_nav_cadence IN (
                                        'intraday', 'daily', 'weekly', 'monthly', 'irregular'
                                    )),
    l2_attestation_cadence          TEXT        CHECK (l2_attestation_cadence IN (
                                        'daily', 'weekly', 'monthly', 'quarterly', 'none'
                                    )),
    l2_attestor                     TEXT,
    l2_audit_cadence                TEXT        CHECK (l2_audit_cadence IN (
                                        'quarterly', 'annual', 'none'
                                    )),
    -- Description of how redemptions work (timing, mechanics, minimums)
    l2_redemption_mechanics         TEXT,
    -- Minimum investment in USD (integer, e.g. 5000000 for $5M)
    l2_subscription_minimum_usd     BIGINT,
    l2_eligible_investors           TEXT        CHECK (l2_eligible_investors IN (
                                        'retail',
                                        'accredited',
                                        'qualified purchaser',
                                        'qualified institutional buyer',
                                        'restricted'
                                    )),
    l2_transfer_restrictions        TEXT,
    -- TRUE = confirmed bankruptcy remote. FALSE = confirmed not. NULL = unclear/unassessed.
    l2_bankruptcy_remote            BOOLEAN,
    l2_score                        SMALLINT    CHECK (l2_score BETWEEN 1 AND 5),
    l2_score_rationale              TEXT,

    -- ── Section 5: Layer 3 — On-chain ────────────────────────────────────────
    -- Primary (canonical) contract address
    l3_primary_contract             TEXT,
    -- JSON array of {chain, address} for multi-chain deployments
    -- e.g. [{"chain":"Ethereum","address":"0x..."},{"chain":"Base","address":"0x..."}]
    l3_multichain_contracts         JSONB,
    l3_smart_contract_auditor       TEXT,
    -- Nullable: pulled from live data (Etherscan) on each pipeline run
    l3_holder_count                 INTEGER,
    -- % of supply held by top 10 addresses; nullable
    l3_top10_holder_pct             NUMERIC(5,2),
    -- How NAV is reported or priced on-chain (oracle, push feed, etc.)
    l3_oracle_mechanism             TEXT,
    -- DeFi protocols where this token is accepted as collateral/depositable
    l3_composability                TEXT[],
    l3_secondary_market_depth       TEXT        CHECK (l3_secondary_market_depth IN (
                                        'none', 'thin', 'moderate', 'deep'
                                    )),
    l3_score                        SMALLINT    CHECK (l3_score BETWEEN 1 AND 5),
    l3_score_rationale              TEXT,

    -- ── Section 6: Composite framing ─────────────────────────────────────────
    -- Weighted average of L1/L2/L3 scores; weights applied by agent, stored here
    composite_score                 NUMERIC(4,2),
    composite_tier                  TEXT        CHECK (composite_tier IN (
                                        'Tier 1', 'Tier 2', 'Tier 3', 'Tier 4', 'Tier 5'
                                    )),
    -- 80-120 word editorial paragraph contextualising the composite score
    composite_framing               TEXT,

    -- ── Cross-cutting: confidence, citations, asset-specific extras ───────────
    -- AI-assigned confidence per field. Keys = column names, values = 'high'|'medium'|'low'
    -- Only populated for fields where confidence was assessed. E.g.:
    -- {"l1_yield_current": "high", "l2_custodian": "medium", "l3_holder_count": "low"}
    field_confidence                JSONB,

    -- Source URLs per field. Array of {field, url} objects. E.g.:
    -- [{"field":"l1_yield_current","url":"https://..."},{"field":"l2_custodian","url":"https://..."}]
    source_citations                JSONB,

    -- Asset-class-specific fields not present in the main schema.
    -- Structure is intentionally flexible and varies by asset_class:
    --   Private Credit:  {"underwriter":"...","loan_duration_days":180,"default_rate_pct":0.3}
    --   Commodity:       {"vault_location":"...","vault_operator":"...","physical_redemption":true}
    --   Equity:          {"exchange":"NYSE","isin":"US..."}
    asset_specific                  JSONB,

    -- ── Section 7: Metadata ───────────────────────────────────────────────────
    last_verified_date              DATE,
    verified_by                     TEXT,
    next_review_date                DATE,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Table + column comments ───────────────────────────────────────────────────

COMMENT ON TABLE protocol_profiles IS
    'Structured profiles for tokenised RWA protocols. One row per protocol. '
    'Seven-section structure: Identity, Editorial, L1 (Underlying), L2 (Wrapper), '
    'L3 (On-chain), Composite, Metadata. Source of truth for protocol profile pages.';

COMMENT ON COLUMN protocol_profiles.defillama_slug IS
    'Soft FK to dashboard_protocol_daily_tvl.slug. Enables joining TVL history at query time. '
    'NULL for protocols not tracked by DefiLlama.';

COMMENT ON COLUMN protocol_profiles.field_confidence IS
    'AI-assigned confidence level per field. Keys are column names, values are '
    '''high'', ''medium'', or ''low''. Sparse — only populated for assessed fields.';

COMMENT ON COLUMN protocol_profiles.source_citations IS
    'Source URLs per field. Array of {field, url} objects. One URL per field '
    'citation; a field may appear multiple times if multiple sources exist.';

COMMENT ON COLUMN protocol_profiles.asset_specific IS
    'Overflow for asset-class-specific fields. Structure varies by asset_class. '
    'Do not use for fields that apply to multiple asset classes — promote those to columns.';

COMMENT ON COLUMN protocol_profiles.composite_score IS
    'Weighted average of l1_score, l2_score, l3_score. Weights are applied by the '
    'agent scoring workflow (not computed by the DB) and vary by asset_class.';

COMMENT ON COLUMN protocol_profiles.l2_bankruptcy_remote IS
    'TRUE = confirmed bankruptcy-remote structure. FALSE = confirmed not isolated. '
    'NULL = not assessed or ambiguous.';

COMMENT ON COLUMN protocol_profiles.l3_multichain_contracts IS
    'JSON array: [{"chain":"Ethereum","address":"0x..."},{"chain":"Base","address":"0x..."}]. '
    'Includes all chains; l3_primary_contract is the canonical Ethereum address.';

COMMENT ON COLUMN protocol_profiles.l3_composability IS
    'Array of DeFi protocol names where this token is accepted as collateral or '
    'can be deposited. E.g. ARRAY[''Aave'',''Morpho'',''Spark''].';

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_pp_asset_class    ON protocol_profiles (asset_class);
CREATE INDEX IF NOT EXISTS idx_pp_tier           ON protocol_profiles (tier);
CREATE INDEX IF NOT EXISTS idx_pp_comp_tier      ON protocol_profiles (composite_tier);
CREATE INDEX IF NOT EXISTS idx_pp_last_verified  ON protocol_profiles (last_verified_date);
CREATE INDEX IF NOT EXISTS idx_pp_updated_at     ON protocol_profiles (updated_at DESC);

-- ── Auto-update updated_at on every write ────────────────────────────────────

CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protocol_profiles_updated_at
    BEFORE UPDATE ON protocol_profiles
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
