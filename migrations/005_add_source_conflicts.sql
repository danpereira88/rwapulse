-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 005: Add source_conflicts column
--
-- The USYC gather run surfaced an auditor conflict (Cohen and Company per
-- Hashnote docs vs Valaston International per RWA.xyz). The agent routed this
-- into identity_discrepancies, which was a workaround — the auditor is not an
-- Identity field. This column provides a proper home for multi-source conflicts
-- on analytical fields.
--
-- Distinct from identity_discrepancies, which tracks contradictions between
-- agent research and the canonical Identity section (product_name, issuer,
-- chains_deployed, etc.).
--
-- source_conflicts tracks disagreements between two credible third-party
-- sources on the value of a non-Identity field, where human review is needed
-- to resolve which source is authoritative.
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS source_conflicts JSONB;

COMMENT ON COLUMN protocol_profiles.source_conflicts IS
    'Array of source conflicts where multiple credible sources disagree on a '
    'non-Identity field value. Structure per entry: '
    '{field, value_a, source_a, value_b, source_b, note}. '
    'Identity-section conflicts (product_name, issuer, chains, etc.) continue '
    'to use the identity_discrepancies column.';
-- ─────────────────────────────────────────────────────────────────────────────
