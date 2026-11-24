"""
Source configuration for RWA Opportunity Intelligence.

Each source dict is consumed by opportunity_intel.py.
Add new sources here; the pipeline picks them up automatically.

Forum base_url must be a live Discourse instance serving /latest.json.
GitHub repo must be the org/repo slug (verified via API).
"""

# ── Private credit sources ─────────────────────────────────────────────────────
# Used for the post-origination servicing / workout / recovery hypothesis.

PRIVATE_CREDIT = {
    "discourse": [
        {
            "name":     "centrifuge",
            "base_url": "https://gov.centrifuge.io",
            "cap":      40,
            # Standard /latest.json pull; no category override needed.
        },
        {
            "name":     "goldfinch",
            "base_url": "https://gov.goldfinch.finance",
            "cap":      40,
        },
        {
            "name":     "sky",
            # forum.sky.money and forum.makerdao.com both 301 → forum.skyeco.com
            "base_url": "https://forum.skyeco.com",
            "cap":      40,   # cap on /latest.json pull
            # Sky-specific: also pull from the legacy RWA category and run
            # targeted searches. Controlled by the extra keys below.
            "rwa_category": {
                "path":  "/c/legacy/real-world-finance/33.json",
                "cap":   28,  # category only has 28 topics; pull all
            },
            "rwa_searches": [
                # Each query returns up to 50 results; we cap at search_cap each.
                {"query": "New Silver restructuring vault",  "cap": 8},
                {"query": "collateral offboarding RWA",      "cap": 8},
                {"query": "RWA counterparty default",        "cap": 8},
                {"query": "RWA monitoring risk vault",       "cap": 6},
                {"query": "arranger MIP65 CLYPS offboarding","cap": 6},
            ],
        },
        # community.maple.finance returns 403 for all unauthenticated requests.
        # Covered via GitHub issues only.
    ],

    "github": [
        {
            "name":  "maple",
            "repo":  "maple-labs/maple-core-v2",
            "cap":   50,
        },
        {
            "name":  "centrifuge",
            "repo":  "centrifuge/centrifuge-chain",
            "cap":   50,
        },
        {
            "name":  "goldfinch",
            "repo":  "goldfinch-eng/mono",
            "cap":   50,
        },
    ],
}
