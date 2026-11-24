-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 004: Expand eligible_investors enum
--
-- Problem: the original five-value CHECK constraint was US-centric.
-- USYC (Reg S / non-US persons) and other offshore products have no valid slot.
-- This migration expands the constraint to nine values covering the global
-- tokenisation market without breaking existing rows.
--
-- Steps:
--   1. Normalise existing space-based values to underscore convention
--      ('qualified purchaser' → 'qualified_purchaser', etc.)
--   2. Drop the old inline CHECK constraint
--   3. Add a new named CHECK constraint with nine values
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Step 1: Drop the old CHECK constraint first ──────────────────────────────
-- Must drop BEFORE the UPDATE so the old constraint doesn't reject the new
-- underscore values ('qualified_purchaser' is not in the original space-based set).
-- The IF EXISTS guard makes this idempotent.

ALTER TABLE protocol_profiles
    DROP CONSTRAINT IF EXISTS protocol_profiles_l2_eligible_investors_check;

-- ── Step 2: Normalise existing row values ─────────────────────────────────────
-- Now safe to rewrite — no constraint active.
-- (Only two values need renaming; 'retail', 'accredited', 'restricted' stay.)

UPDATE protocol_profiles
SET l2_eligible_investors = 'qualified_purchaser'
WHERE l2_eligible_investors = 'qualified purchaser';

UPDATE protocol_profiles
SET l2_eligible_investors = 'qualified_institutional_buyer'
WHERE l2_eligible_investors = 'qualified institutional buyer';

-- ── Step 3: Add expanded CHECK constraint ────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD CONSTRAINT protocol_profiles_l2_eligible_investors_check
    CHECK (l2_eligible_investors IS NULL OR l2_eligible_investors IN (
        'retail',
        'accredited',
        'qualified_purchaser',
        'qualified_institutional_buyer',
        'non_us_persons_reg_s',
        'non_us_persons_offshore',
        'professional_investor',
        'restricted',
        'mixed'
    ));

-- ── Step 4: Smoke-test queries (run after migration to verify) ────────────────
-- Run these individually in the SQL editor to verify:

-- 4a. Confirm constraint exists and lists nine values:
-- SELECT constraint_name, check_clause
-- FROM information_schema.check_constraints
-- WHERE constraint_name = 'protocol_profiles_l2_eligible_investors_check';

-- 4b. Confirm BUIDL row was normalised correctly:
-- SELECT defillama_slug, l2_eligible_investors
-- FROM protocol_profiles
-- WHERE defillama_slug = 'blackrock-buidl';
-- Expected: 'qualified_purchaser'

-- 4c. Confirm new value passes:
-- INSERT INTO protocol_profiles (product_name, l2_eligible_investors)
-- VALUES ('_smoke_test', 'non_us_persons_reg_s');
-- SELECT l2_eligible_investors FROM protocol_profiles WHERE product_name = '_smoke_test';
-- DELETE FROM protocol_profiles WHERE product_name = '_smoke_test';

-- 4d. Confirm invalid value fails:
-- INSERT INTO protocol_profiles (product_name, l2_eligible_investors)
-- VALUES ('_should_fail', 'invalid_value');
-- Expected: ERROR: new row for relation "protocol_profiles" violates check constraint
-- ─────────────────────────────────────────────────────────────────────────────
