# Protocol Profiles

Structured deep-dive profiles for tokenised RWA protocols. One row per protocol in `protocol_profiles`, with seven sections covering identity, editorial summary, three analytical layers (underlying, wrapper, on-chain), composite scoring, and metadata.

---

## Schema in brief

One table: **`protocol_profiles`** (~55 columns + 4 JSONB columns).

| Section | Key columns |
|---|---|
| Identity | `defillama_slug`, `product_name`, `asset_class`, `tier`, `chains` |
| Editorial | `editorial_summary` |
| L1 — Underlying | `l1_yield_current`, `l1_wam_days`, `l1_auditor`, `l1_score` |
| L2 — Wrapper | `l2_legal_structure`, `l2_nav_cadence`, `l2_eligible_investors`, `l2_score` |
| L3 — On-chain | `l3_primary_contract`, `l3_holder_count`, `l3_composability`, `l3_score` |
| Composite | `composite_score`, `composite_tier`, `composite_framing` |
| Metadata | `last_verified_date`, `verified_by`, `next_review_date` |

**JSONB columns** (flexible, no schema change needed to extend):

| Column | What it holds |
|---|---|
| `field_confidence` | `{"l1_yield_current": "high", "l2_custodian": "medium"}` |
| `source_citations` | `[{"field": "l1_yield_current", "url": "https://..."}]` |
| `asset_specific` | Asset-class extras (vault location for commodities, underwriter for private credit, etc.) |
| `l3_multichain_contracts` | `[{"chain": "Ethereum", "address": "0x..."}]` |

`defillama_slug` is a soft FK to `dashboard_protocol_daily_tvl.slug` — it enables joining live TVL data to a profile page at query time.

---

## Reading and writing profiles

```python
from sync.profile_io import read_profile, write_profile, list_profiles

# Read one profile
profile = read_profile("blackrock-buidl")   # returns dict or None

# Write / upsert a profile (uses defillama_slug as conflict key)
write_profile({
    "defillama_slug":   "blackrock-buidl",
    "product_name":     "BlackRock USD Institutional Digital Liquidity Fund",
    "asset_class":      "MMF",
    "tier":             "Anchor",
    "chains":           ["Ethereum", "Base", "Arbitrum"],
    "l1_yield_current": 5.23,
    "field_confidence": {"l1_yield_current": "high"},
    "source_citations": [{"field": "l1_yield_current", "url": "https://..."}],
})

# List all profiles
all_profiles = list_profiles()

# Filter
mmf_profiles    = list_profiles(asset_class="MMF")
anchor_profiles = list_profiles(tier="Anchor")
```

From the command line:

```bash
python -m sync.profile_io list
python -m sync.profile_io list Anchor
python -m sync.profile_io list Anchor MMF
python -m sync.profile_io read blackrock-buidl
```

---

## Current data

One seed row exists for `blackrock-buidl` with the Identity section populated and all other fields NULL. This is a test stub — it verifies the schema accepts the row and the read/write functions work. Do not treat it as verified data.

Full profile content is populated by the AI gathering workflow (separate task).

---

## Extending the schema

**Adding a column that applies to all asset classes:** add a column to `protocol_profiles` via migration (e.g. `migrations/002_add_column.sql`) and update `profile_io.py` if needed. Existing rows get NULL for the new column.

**Adding fields for a specific asset class only:** put them in `asset_specific` JSONB. No migration needed. Document the expected structure in a comment or this README.

```python
write_profile({
    "defillama_slug": "some-private-credit-protocol",
    "asset_class":    "Private Credit",
    "asset_specific": {
        "underwriter":         "Credora",
        "loan_duration_days":  180,
        "default_rate_pct":    0.3,
    },
})
```

**Adding a new asset class enum value:** run `ALTER TABLE protocol_profiles DROP CONSTRAINT ...; ALTER TABLE protocol_profiles ADD CONSTRAINT ...` with the updated CHECK constraint.

---

## Migration

Schema applied via `migrations/001_create_profile_schema.sql`.

To wipe and recreate the profile table:

```sql
-- Run in Supabase SQL Editor
DROP TABLE IF EXISTS protocol_profiles CASCADE;
```

Then re-apply `migrations/001_create_profile_schema.sql`.

The existing `dashboard_*` tables are unaffected — profiles are entirely additive.
