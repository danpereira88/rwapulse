-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 003: Split conflated fields
--
-- Adds two columns to protocol_profiles that were previously conflated into
-- single existing fields:
--
--   nav_oracle_cadence  — on-chain NAV reporting frequency via oracle feeds
--                         (Chronicle, RedStone, Chainlink, etc.)
--                         Distinct from l2_attestation_cadence, which tracks
--                         periodic third-party attestations by audit/admin firms.
--
--   defi_collateral_use — DeFi lending protocols accepting the token as collateral
--                         (Aave, Morpho, Maker, Compound, Spark, etc.)
--                         Distinct from l3_composability, which tracks B2B reserve
--                         usage by other tokenisation platforms (Ondo, Ethena, Frax).
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS nav_oracle_cadence TEXT,
    ADD COLUMN IF NOT EXISTS defi_collateral_use JSONB;

-- ── Comments ──────────────────────────────────────────────────────────────────

COMMENT ON COLUMN protocol_profiles.nav_oracle_cadence IS
    'How often NAV is reported on-chain via oracle feeds (e.g. Chronicle, RedStone, '
    'Chainlink). Distinct from l2_attestation_cadence, which tracks periodic '
    'third-party institutional attestations by audit firms or administrators. '
    'Example values: ''continuous'', ''hourly'', ''daily'', ''none''.';

COMMENT ON COLUMN protocol_profiles.defi_collateral_use IS
    'Array of DeFi lending protocols that accept this token as collateral for '
    'borrowing (e.g. ["Aave v3", "Morpho", "Maker"]). Distinct from l3_composability, '
    'which tracks B2B reserve adoption by other tokenisation platforms. '
    'For transfer-restricted tokens (whitelist-only), DeFi collateral use is typically '
    'rare or absent due to whitelisting constraints.';

COMMENT ON COLUMN protocol_profiles.l3_composability IS
    'Array of OTHER tokenisation platforms or stablecoin issuers that hold this token '
    'as a balance-sheet reserve (e.g. Ondo Finance using BUIDL as OUSG reserves, '
    'Ethena holding BUIDL in its reserve mix). This is B2B institutional adoption — '
    'one platform trusting another''s product as cash-equivalent. '
    'Do NOT include DeFi lending protocols here — those go in defi_collateral_use.';
