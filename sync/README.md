# Supabase Sync Layer

One-way push from the local DuckDB (`data/rwa.duckdb`) to a Supabase Postgres instance. The dashboard reads from Supabase; all analytical work stays in DuckDB.

## Setup

### 1. Create a Supabase project

Go to [supabase.com](https://supabase.com), create a project. Free tier is enough for this data volume.

### 2. Get your API credentials

Supabase dashboard → **Settings** → **API**:

- **Project URL** — looks like `https://<ref>.supabase.co`
- **service_role** key (under *Project API keys*) — starts with `sb_secret_…`

> The sync uses the REST API — no direct Postgres password needed.

### 3. Add to .env

```bash
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=sb_secret_...   # Settings → API → service_role key
```

### 4. Install dependencies

```bash
pip install -r requirements.txt   # requests is already included
```

### 5. Create tables in Supabase

The sync uses the REST API — DDL must be applied manually once:

1. Supabase dashboard → **SQL Editor** → **New Query**
2. Paste the contents of `sync/schema.sql`
3. Click **Run**

---

## Running the sync

### Manual run (all tables)

```bash
cd /path/to/rwa-data-exploration
python -m sync.sync_to_supabase
```

### Sync a single table

```bash
python -m sync.sync_to_supabase --table protocol_tvl
python -m sync.sync_to_supabase --table protocol_metadata
python -m sync.sync_to_supabase --table token_snapshots
python -m sync.sync_to_supabase --table stablecoin_history
```

### Full pipeline (ingest + sync)

```bash
python run_all_with_sync.py
```

This runs `run_all.py` first. If ingestion succeeds, sync runs. If ingestion fails, sync is skipped — no partial data pushed.

Skip the slow on-chain step:

```bash
python run_all_with_sync.py --skip-onchain
```

---

## Verifying a sync run

```bash
python -m sync.verify_sync
```

Prints row counts, date ranges, and last `synced_at` per table. Run this after every sync to confirm what was pushed.

---

## What's synced (and what isn't)

**Synced:**

| Supabase table | Source (DuckDB) | Notes |
|---|---|---|
| `dashboard_protocol_daily_tvl` | `dl_protocol_tvl` + `dl_protocols` | 2023-01-01 onwards; category/chain enriched |
| `dashboard_protocol_metadata` | `dl_protocols` | Latest snapshot only; full upsert each run |
| `dashboard_token_snapshots` | `cg_rwa_tokens` | Accumulates daily; incremental push |
| `dashboard_stablecoin_history` | `dl_stablecoin_history` | 2023-01-01 onwards; incremental push |

**Not synced:**

| What | Why |
|---|---|
| `sky-rwa` | Governance wind-down; distorts concentration metrics |
| `blockchain-capital` | Flat $965M every day since Sep 2025 — static placeholder |
| `ondo-global-markets` | Tokenised stocks; structurally distinct from yield-bearing RWA |
| `pre-append` snapshot rows | Unknown vintage |
| `dl_protocol_tvl` rows before 2023-01-01 | Too old for dashboard; keeps Supabase lean |
| `2026-05-14` TVL rows | Duplicate ingestion day artifact |
| Subgraph tables (Maple, Centrifuge, Goldfinch) | Not in v1 dashboard scope |
| `onchain_transfer_flows` | Not in v1 dashboard scope |

Gold tokens (Tether Gold, Paxos Gold, etc.) **are** synced — they're tagged via `category` so the dashboard can filter them in or out per view.

---

## Recovering from a failed sync

The sync is idempotent. If it fails mid-run:

1. Check the logs for which table failed and why.
2. Fix the issue (usually a credentials problem or schema mismatch).
3. Re-run `python -m sync.sync_to_supabase` — it will pick up where it left off. Incremental tables skip dates already in Supabase; metadata does a full upsert with no duplicates.

## Development: wipe and restart

If you change the schema and need a clean slate:

```bash
python -m sync.wipe_supabase --confirm   # prints DROP + CREATE SQL
```

Copy the printed SQL, paste it into the Supabase **SQL Editor**, and run it.
Then repopulate:

```bash
python -m sync.sync_to_supabase
```

> DDL (DROP TABLE, CREATE TABLE) can't be run via the REST API — it must go through
> the SQL Editor or a direct Postgres connection. `wipe_supabase.py` generates the
> SQL for you so it's one copy-paste.
