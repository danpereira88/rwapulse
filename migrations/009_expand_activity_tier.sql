-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 009: Expand activity_tier CHECK constraint to include 'very_high'
--
-- Context: The original four-value rubric (dormant/low/moderate/high) was
-- first-pass. After the first live enrichment run (May 2026), the full data
-- range spans four orders of magnitude (47–37,419 transfers). A fifth tier
-- 'very_high' was added to produce meaningful differentiation.
--
-- New set: dormant | low | moderate | high | very_high
--
-- Approach: PostgreSQL does not support ALTER CONSTRAINT to change the allowed
-- values, so we drop the existing constraint and recreate it with the new set.
-- The inline constraint added in migration 006 was auto-named by Postgres;
-- the canonical name is protocol_profiles_activity_tier_check.
-- Use IF EXISTS to keep the migration idempotent.
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Drop the old four-value constraint ────────────────────────────────────────

ALTER TABLE protocol_profiles
    DROP CONSTRAINT IF EXISTS protocol_profiles_activity_tier_check;

-- ── Recreate with five values ──────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD CONSTRAINT protocol_profiles_activity_tier_check
        CHECK (activity_tier IN ('dormant', 'low', 'moderate', 'high', 'very_high'));

-- ── Smoke-test queries (run after migration to verify) ────────────────────────

-- Confirm constraint exists and includes very_high:
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid = 'protocol_profiles'::regclass
--   AND conname LIKE '%activity_tier%';
-- Expected: 1 row — constraint def should list 'dormant', 'low', 'moderate', 'high', 'very_high'

-- Confirm old values still accepted:
-- UPDATE protocol_profiles SET activity_tier = 'high'
-- WHERE defillama_slug = 'blackrock-buidl';
-- Expected: UPDATE 1

-- Confirm new value accepted:
-- UPDATE protocol_profiles SET activity_tier = 'very_high'
-- WHERE defillama_slug = 'blackrock-buidl';
-- Expected: UPDATE 1

-- Confirm rejected values still fail:
-- UPDATE protocol_profiles SET activity_tier = 'invalid'
-- WHERE defillama_slug = 'blackrock-buidl';
-- Expected: ERROR - violates check constraint
-- ─────────────────────────────────────────────────────────────────────────────
