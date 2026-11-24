-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 006: Add on-chain activity enrichment fields
--
-- Adds all Layer 3 activity metrics to protocol_profiles.
-- Populated daily by enrich/enrich_onchain_activity.py from Etherscan API.
-- Ethereum-only in v1; multichain expansion is a planned v2 follow-on.
--
-- Forward-only: no historical backfill. Day zero is when the script first runs;
-- rolling 30-day windows are fully populated after 30 days of daily runs.
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Transfer activity ─────────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS transfer_count_30d       INTEGER,
    ADD COLUMN IF NOT EXISTS transfer_volume_usd_30d  NUMERIC,
    ADD COLUMN IF NOT EXISTS mint_count_30d            INTEGER,
    ADD COLUMN IF NOT EXISTS burn_count_30d            INTEGER,
    ADD COLUMN IF NOT EXISTS mint_volume_usd_30d       NUMERIC,
    ADD COLUMN IF NOT EXISTS burn_volume_usd_30d       NUMERIC,
    ADD COLUMN IF NOT EXISTS net_flow_usd_30d          NUMERIC;

-- ── Participant counts ────────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS unique_senders_30d        INTEGER,
    ADD COLUMN IF NOT EXISTS unique_receivers_30d      INTEGER,
    ADD COLUMN IF NOT EXISTS active_holder_count       INTEGER,
    ADD COLUMN IF NOT EXISTS holder_count_change_30d   INTEGER;

-- ── Activity tier and ratios ──────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS activity_tier             TEXT
        CHECK (activity_tier IN ('high', 'moderate', 'low', 'dormant')),
    ADD COLUMN IF NOT EXISTS activity_vs_tvl_ratio     NUMERIC;

-- ── Token mechanics ───────────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS transfer_restricted       BOOLEAN;

-- ── Enrichment metadata ───────────────────────────────────────────────────────

ALTER TABLE protocol_profiles
    ADD COLUMN IF NOT EXISTS activity_data_as_of       DATE,
    ADD COLUMN IF NOT EXISTS activity_data_source      TEXT;

-- ── Column documentation ──────────────────────────────────────────────────────

COMMENT ON COLUMN protocol_profiles.transfer_count_30d IS
    'Total ERC-20 Transfer events on Ethereum in the rolling 30-day window.';

COMMENT ON COLUMN protocol_profiles.transfer_volume_usd_30d IS
    'Sum of transferred token amounts × token price (USD) over 30 days. '
    'USD-stable tokens priced at 1.00; CFG priced via CoinGecko spot.';

COMMENT ON COLUMN protocol_profiles.mint_count_30d IS
    'Transfer events where from = 0x000...000 in rolling 30d (subscriptions/issuances).';

COMMENT ON COLUMN protocol_profiles.burn_count_30d IS
    'Transfer events where to = 0x000...000 in rolling 30d (redemptions).';

COMMENT ON COLUMN protocol_profiles.activity_tier IS
    'Activity classification computed by enrich_onchain_activity.py. '
    'Rubric differs for restricted vs permissionless tokens. '
    'Restricted (BUIDL/USYC/OUSG): mint+burn count drives tier. '
    'Permissionless (USDY/syrupUSDC/CFG): transfer_count + unique_senders drives tier. '
    'Thresholds are first-pass estimates; expect recalibration after 30-60 days of data.';

COMMENT ON COLUMN protocol_profiles.transfer_restricted IS
    'True if token transfers are gated by on-chain whitelist (KYC/accredited investor). '
    'Drives activity_tier rubric selection. Inferred from on-chain behavior on first run, '
    'then frozen unless explicitly overridden.';

COMMENT ON COLUMN protocol_profiles.activity_data_as_of IS
    'Date the activity metrics were last computed. Updated every successful run.';

COMMENT ON COLUMN protocol_profiles.activity_data_source IS
    'Data source(s) used for activity enrichment (e.g. "Etherscan API v2 + CoinGecko").';

-- ── Smoke-test queries (run after migration to verify) ────────────────────────

-- Confirm columns exist:
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'protocol_profiles'
--   AND column_name IN (
--     'transfer_count_30d', 'mint_count_30d', 'activity_tier',
--     'transfer_restricted', 'activity_data_as_of', 'activity_data_source'
--   )
-- ORDER BY column_name;
-- Expected: 6 rows

-- Confirm activity_tier constraint works:
-- UPDATE protocol_profiles SET activity_tier = 'invalid' WHERE defillama_slug = 'blackrock-buidl';
-- Expected: ERROR - violates check constraint
-- ─────────────────────────────────────────────────────────────────────────────
