"""
Protocol profile read/write utility.

Thin wrapper around the Supabase REST API for the protocol_profiles table.
No ORM. No abstraction layers. Three functions.

Usage:
    from sync.profile_io import read_profile, write_profile, list_profiles

    profile = read_profile("blackrock-buidl")
    write_profile({"defillama_slug": "blackrock-buidl", "product_name": "...", ...})
    profiles = list_profiles(asset_class="MMF")
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "protocol_profiles"


# ── Session ────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    if not SUPA_URL:
        raise RuntimeError("SUPABASE_URL not set in .env")
    if not SUPA_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY not set in .env")
    s = requests.Session()
    s.headers.update({
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type":  "application/json",
    })
    return s


# ── Public API ─────────────────────────────────────────────────────────────────

def read_profile(protocol_slug: str) -> Optional[dict]:
    """
    Return a single profile as a dict, or None if not found.

    Args:
        protocol_slug: the defillama_slug value, e.g. "blackrock-buidl"

    Returns:
        Full profile dict with all columns, or None.
    """
    s = _session()
    resp = s.get(
        f"{SUPA_URL}/rest/v1/{TABLE}",
        params={
            "defillama_slug": f"eq.{protocol_slug}",
            "limit": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def write_profile(profile: dict) -> dict:
    """
    Insert or update a profile. Returns the saved row.

    Behaviour:
    - If the row already exists (matched by defillama_slug): PATCH (partial update).
      Only the fields present in the dict are updated; absent fields are untouched.
      This is safe for NOT NULL columns — you can update a single column without
      supplying product_name or other required fields.
    - If the row does not exist yet: POST (full insert). product_name is required.

    Python lists are sent as JSON arrays (Postgres TEXT[] and JSONB both accept this).
    Python dicts are sent as JSON objects (JSONB columns).
    None values are sent as JSON null.

    Args:
        profile: dict with any subset of protocol_profiles columns.
                 Must include 'defillama_slug' for updates; also 'product_name'
                 for fresh inserts.

    Returns:
        The saved row as a dict.
    """
    s = _session()

    # Strip DB-managed keys
    payload = {k: v for k, v in profile.items() if k not in ("id", "created_at", "updated_at")}
    slug = payload.get("defillama_slug")

    if slug:
        # Check whether the row already exists
        exists_resp = s.get(
            f"{SUPA_URL}/rest/v1/{TABLE}",
            params={"defillama_slug": f"eq.{slug}", "select": "defillama_slug", "limit": "1"},
        )
        row_exists = exists_resp.ok and bool(exists_resp.json())

        if row_exists:
            # PATCH — partial update; only supplied fields are touched
            resp = s.patch(
                f"{SUPA_URL}/rest/v1/{TABLE}",
                params={"defillama_slug": f"eq.{slug}"},
                data=json.dumps(payload),
                headers={"Prefer": "return=representation"},
            )
        else:
            # POST — full insert; product_name must be present
            resp = s.post(
                f"{SUPA_URL}/rest/v1/{TABLE}",
                data=json.dumps(payload),
                headers={"Prefer": "return=representation"},
            )
    else:
        # No slug — plain insert
        resp = s.post(
            f"{SUPA_URL}/rest/v1/{TABLE}",
            data=json.dumps(payload),
            headers={"Prefer": "return=representation"},
        )

    if not resp.ok:
        raise RuntimeError(
            f"write_profile failed [{resp.status_code}]: {resp.text[:600]}"
        )

    rows = resp.json()
    return rows[0] if rows else {}


def list_profiles(
    tier: Optional[str] = None,
    asset_class: Optional[str] = None,
    order_by: str = "updated_at",
    descending: bool = True,
) -> list:
    """
    Return a list of profiles, optionally filtered.

    Args:
        tier:        Filter by tier value ('Anchor', 'Tracked', 'Reference').
        asset_class: Filter by asset class ('MMF', 'Tokenised Treasuries', etc.).
        order_by:    Column to sort by (default: 'updated_at').
        descending:  Sort direction (default: True).

    Returns:
        List of profile dicts. Empty list if no matches.
    """
    s = _session()

    params: dict = {
        "order": f"{order_by}.{'desc' if descending else 'asc'}",
    }
    if tier:
        params["tier"] = f"eq.{tier}"
    if asset_class:
        params["asset_class"] = f"eq.{asset_class}"

    resp = s.get(f"{SUPA_URL}/rest/v1/{TABLE}", params=params)
    resp.raise_for_status()
    return resp.json()


# ── CLI convenience ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "read" and len(sys.argv) > 2:
        result = read_profile(sys.argv[2])
        if result:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"No profile found for slug: {sys.argv[2]}")

    elif cmd == "list":
        tier       = sys.argv[2] if len(sys.argv) > 2 else None
        asset_cls  = sys.argv[3] if len(sys.argv) > 3 else None
        profiles   = list_profiles(tier=tier, asset_class=asset_cls)
        for p in profiles:
            slug    = p.get("defillama_slug", "—")
            name    = p.get("product_name", "—")
            ac      = p.get("asset_class", "—")
            t       = p.get("tier", "—")
            updated = (p.get("updated_at") or "")[:10]
            print(f"  {slug:<30} {name:<45} {ac:<25} {t:<10} {updated}")
        print(f"\n  {len(profiles)} profile(s)")

    else:
        print("Usage:")
        print("  python -m sync.profile_io list [tier] [asset_class]")
        print("  python -m sync.profile_io read <defillama-slug>")
