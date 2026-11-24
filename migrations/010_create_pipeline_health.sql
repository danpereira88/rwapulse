-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 010: Create pipeline_health table
--
-- One row per job type (run_type is the primary key). Upserted on every
-- automated run, so it serves dual purpose:
--   1. Operational visibility — last-run timestamp and status per job.
--   2. Supabase keep-alive — each upsert is a real Postgres WRITE that
--      resets the free-tier 7-day inactivity pause timer.
--
-- Run types:
--   daily_freshness  — written by scripts/check_freshness.py, every day at 07:00
--   weekly_full      — written by scripts/weekly_pipeline.sh, every Monday 06:00
--
-- Apply via: Supabase SQL Editor → New Query → paste → Run
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pipeline_health (
    run_type    TEXT PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL,
    status      TEXT        NOT NULL
                CHECK (status IN ('ok', 'warning', 'error')),
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE pipeline_health IS
    'One row per automated job type. Upserted on every run. '
    'Acts as a Supabase keep-alive (write resets the 7-day free-tier pause timer). '
    'Populated exclusively by the automation layer (scripts/); '
    'never touched by the gather or enrichment workflows.';

COMMENT ON COLUMN pipeline_health.run_type IS
    'Job identifier: daily_freshness | weekly_full';

COMMENT ON COLUMN pipeline_health.run_at IS
    'UTC timestamp of the most recent run of this job type.';

COMMENT ON COLUMN pipeline_health.status IS
    'ok = data fresh and pipeline healthy. '
    'warning = data stale (>9 days) but within tolerable range. '
    'error = pipeline failed or data critically stale (>14 days).';

COMMENT ON COLUMN pipeline_health.details IS
    'Freeform JSONB payload. For daily_freshness: staleness metrics per source table. '
    'For weekly_full: duration, step results, exit code.';

-- ── Smoke-test (run after migration) ──────────────────────────────────────────

-- Confirm table exists:
-- SELECT * FROM pipeline_health;
-- Expected: 0 rows (empty until first automated run)

-- Confirm upsert works:
-- INSERT INTO pipeline_health (run_type, run_at, status, details)
-- VALUES ('test', now(), 'ok', '{"test": true}')
-- ON CONFLICT (run_type) DO UPDATE SET run_at = EXCLUDED.run_at, status = EXCLUDED.status, details = EXCLUDED.details;
-- Expected: INSERT 0 1
-- DELETE FROM pipeline_health WHERE run_type = 'test';
-- ─────────────────────────────────────────────────────────────────────────────
