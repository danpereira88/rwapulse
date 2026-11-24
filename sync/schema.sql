-- Supabase schema for the RWA intelligence dashboard
-- Source of truth: DuckDB at data/rwa.duckdb
-- This is a presentation cache — never write to this directly.
-- Apply with: psql $SUPABASE_DB_URL -f sync/schema.sql

-- ── 1. Protocol daily TVL ─────────────────────────────────────────────────────
-- Source: dl_protocol_tvl joined to dl_protocols for category/chain metadata.
-- Exclusions: sky-rwa, blockchain-capital, ondo-global-markets, pre-2023, 2026-05-14.
-- Commodities (gold tokens) included — tagged via category for dashboard filtering.

CREATE TABLE IF NOT EXISTS dashboard_protocol_daily_tvl (
    slug            TEXT        NOT NULL,
    protocol_name   TEXT        NOT NULL,
    date            DATE        NOT NULL,
    tvl_usd         NUMERIC,
    category        TEXT,       -- NULL if protocol not in dl_protocols metadata
    chains          TEXT,       -- comma-separated primary chains
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slug, date)
);

CREATE INDEX IF NOT EXISTS idx_pdtvl_date     ON dashboard_protocol_daily_tvl (date);
CREATE INDEX IF NOT EXISTS idx_pdtvl_cat_date ON dashboard_protocol_daily_tvl (category, date);


-- ── 2. Protocol metadata ──────────────────────────────────────────────────────
-- Source: dl_protocols — latest real snapshot only (not pre-append).
-- One row per protocol. Replaced on each sync with the freshest metadata.

CREATE TABLE IF NOT EXISTS dashboard_protocol_metadata (
    slug            TEXT        NOT NULL PRIMARY KEY,
    name            TEXT,
    category        TEXT,
    chains          TEXT,
    tvl_usd         NUMERIC,
    change_1d_pct   NUMERIC,
    change_7d_pct   NUMERIC,
    change_1m_pct   NUMERIC,
    description     TEXT,
    url             TEXT,
    logo            TEXT,
    twitter         TEXT,
    snapshot_date   DATE        NOT NULL,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pmeta_category ON dashboard_protocol_metadata (category);


-- ── 3. Token snapshots ────────────────────────────────────────────────────────
-- Source: cg_rwa_tokens — real snapshot_dates only (not pre-append).
-- Accumulates one row per token per day as the daily ingest runs.

CREATE TABLE IF NOT EXISTS dashboard_token_snapshots (
    coingecko_id            TEXT        NOT NULL,
    symbol                  TEXT,
    name                    TEXT,
    snapshot_date           DATE        NOT NULL,
    current_price_usd       NUMERIC,
    market_cap_usd          NUMERIC,
    price_change_pct_24h    NUMERIC,
    price_change_pct_7d     NUMERIC,
    price_change_pct_30d    NUMERIC,
    circulating_supply      NUMERIC,
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (coingecko_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_toksnap_date ON dashboard_token_snapshots (snapshot_date);


-- ── 4. Stablecoin history ─────────────────────────────────────────────────────
-- Source: dl_stablecoin_history — all stablecoins, 2023-01-01 onwards.
-- Used for context charts ("tokenised cash in stablecoin context").

CREATE TABLE IF NOT EXISTS dashboard_stablecoin_history (
    stablecoin_id           TEXT        NOT NULL,
    name                    TEXT,
    symbol                  TEXT,
    date                    DATE        NOT NULL,
    total_circulating_usd   NUMERIC,
    synced_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stablecoin_id, date)
);

CREATE INDEX IF NOT EXISTS idx_stable_date ON dashboard_stablecoin_history (date);
