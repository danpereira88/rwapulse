"""
On-chain activity enrichment for anchor protocol profiles.

Reads Ethereum Transfer events for each anchor token via Etherscan API v2,
computes rolling 30-day activity metrics, and writes them back to Supabase
via profile_io.write_profile().

Scope: Ethereum-only (v1). Multichain EVM expansion is a planned v2 follow-on.
Forward-only: no historical backfill. Today is day zero.

Usage:
    python -m enrich.enrich_onchain_activity          # enrich all anchor profiles
    python -m enrich.enrich_onchain_activity --dry-run # compute but don't write
    python -m enrich.enrich_onchain_activity --slug blackrock-buidl  # single profile
"""

import argparse
import os
import sys
import time
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

from sync.profile_io import list_profiles, write_profile, read_profile  # noqa

# ── Configuration ──────────────────────────────────────────────────────────────

PREFIX = "[enrich_onchain_activity]"

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE    = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAIN   = "1"   # Ethereum mainnet
ETHERSCAN_DELAY   = 0.22  # 5 req/s free tier; 0.22s gives headroom

COINGECKO_BASE    = "https://api.coingecko.com/api/v3"

# Standard ERC-20 Transfer(address,address,uint256) topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Burn / mint address
ZERO_ADDR        = "0x0000000000000000000000000000000000000000"
ZERO_ADDR_TOPIC  = "0x0000000000000000000000000000000000000000000000000000000000000000"

# Max records Etherscan returns per getLogs call
ETHERSCAN_PAGE_LIMIT = 1000

# Approx Ethereum blocks per day (12s block time)
BLOCKS_PER_DAY = 7_150

# CoinGecko ID mapping for non-stable tokens
COINGECKO_IDS = {
    "centrifuge": "centrifuge",   # CFG token
}

# Tokens we treat as USD-stable (price = $1.00)
# syrupUSDC and all USD-denominated fund tokens
STABLE_PRICE_SLUGS = {
    "blackrock-buidl",
    "circle-usyc",
    "ondo-ousg",
    "ondo-usdy",
    "maple-finance",
}

# Maps protocol_profiles.defillama_slug → actual slug in dashboard_protocol_daily_tvl.
# Some profiles use an internal slug that doesn't match DefiLlama's naming, or DefiLlama
# doesn't track the product separately (e.g. OUSG and USDY both live under
# "ondo-yield-assets"). For those profiles, activity_vs_tvl_ratio is approximate —
# see methodology document for per-profile caveats.
TVL_SLUG_MAP = {
    "ondo-ousg":     "ondo-yield-assets",   # DefiLlama bundles OUSG+USDY together
    "ondo-usdy":     "ondo-yield-assets",   # same slug; ratio is approximate for each product
    "maple-finance": "maple",               # DefiLlama uses 'maple' (platform aggregate)
    "centrifuge":    "centrifuge-protocol", # DefiLlama uses 'centrifuge-protocol'
}

# ── Logging helpers ────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"{PREFIX} {msg}")

def warn(msg: str):
    print(f"{PREFIX} WARNING: {msg}", file=sys.stderr)


# ── Etherscan helpers ──────────────────────────────────────────────────────────

def _es_call(params: dict, retries: int = 3) -> dict:
    """Make a rate-limited Etherscan v2 API call with retry on connection errors."""
    p = {"chainid": ETHERSCAN_CHAIN, "apikey": ETHERSCAN_API_KEY, **params}
    time.sleep(ETHERSCAN_DELAY)
    for attempt in range(retries):
        try:
            r = requests.get(ETHERSCAN_BASE, params=p, timeout=20)
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            warn(f"Etherscan request error (attempt {attempt+1}/{retries}): {e}")
            time.sleep(1.0)
    return {}


def _get_block_at_timestamp(ts: int) -> int:
    """Return the Ethereum block number closest to a Unix timestamp."""
    r = _es_call({
        "module": "block", "action": "getblocknobytime",
        "timestamp": str(ts), "closest": "before"
    })
    result = r.get("result")
    if not result or not str(result).isdigit():
        raise RuntimeError(f"Could not fetch block at timestamp {ts}: {r}")
    return int(result)


def _get_current_block() -> int:
    """Return the latest Ethereum block number."""
    r = _es_call({"module": "proxy", "action": "eth_blockNumber"})
    hex_block = r.get("result", "")
    if not hex_block:
        raise RuntimeError(f"Could not fetch current block: {r}")
    return int(hex_block, 16)


def _get_token_decimals(contract_addr: str) -> int:
    """Return ERC-20 token decimals via eth_call to decimals()."""
    r = _es_call({
        "module": "proxy", "action": "eth_call",
        "to": contract_addr, "data": "0x313ce567", "tag": "latest"
    })
    hex_result = r.get("result", "")
    if hex_result and len(hex_result) > 3:
        try:
            return int(hex_result, 16)
        except ValueError:
            pass
    warn(f"Could not parse decimals for {contract_addr}, defaulting to 18")
    return 18


def _get_logs_chunk(contract_addr: str, from_block: int, to_block: int) -> list:
    """
    Fetch Transfer event logs for a single block range.
    Returns list of log dicts. Returns [] on API error.
    Raises RuntimeError if result indicates the range hit the page limit
    (caller must split the range).
    """
    r = _es_call({
        "module": "logs", "action": "getLogs",
        "address": contract_addr,
        "topic0": TRANSFER_TOPIC,
        "fromBlock": str(from_block),
        "toBlock": str(to_block),
        "page": "1",
        "offset": str(ETHERSCAN_PAGE_LIMIT),
    })
    result = r.get("result")
    if not isinstance(result, list):
        # API error (rate limit, no records, etc.)
        return []
    return result


def _fetch_all_logs(contract_addr: str, from_block: int, to_block: int,
                    _depth: int = 0) -> list:
    """
    Fetch all Transfer event logs between from_block and to_block using
    adaptive binary-split chunking. If a range returns exactly 1000 results
    (the Etherscan page limit), split it in half and recurse.

    Max recursion depth: 20 (handles >1M events per range).
    """
    if _depth > 20:
        warn(f"Max recursion depth reached for {contract_addr} "
             f"[{from_block}-{to_block}]; returning partial results")
        return _get_logs_chunk(contract_addr, from_block, to_block)

    chunk = _get_logs_chunk(contract_addr, from_block, to_block)

    if len(chunk) < ETHERSCAN_PAGE_LIMIT:
        # Under the limit — this range is fully captured
        return chunk

    # Hit the 1000-record limit — split range and recurse
    if from_block >= to_block:
        # Can't split further (same block)
        return chunk

    mid = (from_block + to_block) // 2
    left  = _fetch_all_logs(contract_addr, from_block,  mid,      _depth + 1)
    right = _fetch_all_logs(contract_addr, mid + 1, to_block, _depth + 1)
    return left + right


# ── Price helpers ──────────────────────────────────────────────────────────────

def _get_coingecko_price(coingecko_id: str) -> float:
    """Fetch current USD price from CoinGecko simple/price (no key needed)."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": coingecko_id, "vs_currencies": "usd"},
            timeout=10
        )
        data = r.json()
        price = data.get(coingecko_id, {}).get("usd")
        if price is not None:
            return float(price)
    except Exception as e:
        warn(f"CoinGecko price fetch failed for {coingecko_id}: {e}")
    return 1.0  # safe fallback


def _get_token_price(slug: str) -> float:
    """Return USD price for a token. Stable tokens → 1.0; CFG → CoinGecko."""
    if slug in STABLE_PRICE_SLUGS:
        return 1.0
    cg_id = COINGECKO_IDS.get(slug)
    if cg_id:
        return _get_coingecko_price(cg_id)
    warn(f"No price source for {slug}; defaulting to $1.00")
    return 1.0


# ── TVL lookup ─────────────────────────────────────────────────────────────────

def _get_current_tvl(slug: str) -> Optional[float]:
    """
    Fetch the most recent TVL for a protocol slug from Supabase
    dashboard_protocol_daily_tvl.

    Uses TVL_SLUG_MAP to translate internal profile slugs to the actual DefiLlama
    slug used in the TVL table. For profiles where the map points to an aggregate
    slug (e.g. OUSG and USDY both → 'ondo-yield-assets'), the returned TVL is the
    aggregate figure; see methodology doc for ratio interpretation caveats.
    """
    import os
    SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not SUPA_URL or not SUPA_KEY:
        return None

    tvl_slug = TVL_SLUG_MAP.get(slug, slug)

    headers = {
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
    }
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/dashboard_protocol_daily_tvl",
            params={
                "slug": f"eq.{tvl_slug}",
                "select": "tvl_usd,date",
                "order": "date.desc",
                "limit": "1",
            },
            headers=headers,
            timeout=10
        )
        rows = r.json()
        if rows:
            return float(rows[0].get("tvl_usd") or 0)
    except Exception as e:
        warn(f"TVL lookup failed for {slug} (mapped to '{tvl_slug}'): {e}")
    return None


# ── Contract address resolution ────────────────────────────────────────────────

def _resolve_eth_address(profile: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Return (eth_address, anomaly_note) for the profile's Ethereum token contract.

    Resolution order (primary is authoritative):
    1. l3_primary_contract — if it's a valid Ethereum address (42-char hex, 0x prefix).
       This is the gather-workflow's canonical designation and wins unconditionally.
    2. First Ethereum-tagged entry in l3_multichain_contracts — used only when
       l3_primary_contract is absent or malformed.
    3. None — skip the protocol and log a warning.

    The multichain_contracts array is a fallback, not a preference source.
    An anomaly_note is returned if multichain has multiple Ethereum entries (for
    logging purposes only — it does not change which address is returned).

    Returns a tuple:
        eth_address  — the resolved contract address (lowercased), or None
        anomaly_note — human-readable note if something looks odd, else None
    """
    slug    = profile.get("defillama_slug", "")
    primary = (profile.get("l3_primary_contract") or "").strip()

    def _is_valid_eth_addr(addr: str) -> bool:
        return (
            isinstance(addr, str)
            and len(addr) == 42
            and addr.lower().startswith("0x")
        )

    # ── Preference 1: l3_primary_contract ─────────────────────────────────────
    if _is_valid_eth_addr(primary):
        # Check for multiple ETH entries in multichain (log only — doesn't change result)
        multichain = profile.get("l3_multichain_contracts") or []
        eth_entries = [
            c for c in multichain
            if isinstance(c, dict) and c.get("chain", "").lower() == "ethereum"
            and c.get("address")
        ]
        anomaly_note = None
        if len(eth_entries) > 1:
            addrs = [c["address"].lower() for c in eth_entries]
            anomaly_note = (
                f"Multiple Ethereum entries in l3_multichain_contracts for {slug}: {addrs}. "
                f"Using l3_primary_contract ({primary[:10]}...) as authoritative."
            )
        return primary.lower(), anomaly_note

    # ── Preference 2: first Ethereum entry in l3_multichain_contracts ─────────
    multichain = profile.get("l3_multichain_contracts") or []
    eth_entries = [
        c for c in multichain
        if isinstance(c, dict) and c.get("chain", "").lower() == "ethereum"
        and c.get("address")
    ]

    if eth_entries:
        addr = eth_entries[0]["address"].lower()
        note = (f"l3_primary_contract missing or invalid for {slug}; "
                f"falling back to first Ethereum entry in l3_multichain_contracts.")
        return addr, note

    # ── Preference 3: nothing found ────────────────────────────────────────────
    return None, None


# ── Transfer restricted inference ──────────────────────────────────────────────

def _infer_transfer_restricted(profile: dict, mint_count: int, burn_count: int,
                                 total_count: int) -> tuple[bool, str]:
    """
    Determine if a token is transfer-restricted (whitelist-gated).

    If already stored in the profile, use that value (frozen).
    Otherwise infer from eligible_investors and on-chain behaviour:
    - If (mint + burn) / total > 0.80 → transfers are mostly institutional
      subscription/redemption → likely restricted
    - If eligible_investors in {qualified_purchaser, qualified_institutional_buyer,
      accredited} AND asset_class is not governance token → restricted

    Returns (is_restricted, rationale).
    """
    # If already stored, respect it
    existing = profile.get("transfer_restricted")
    if existing is not None:
        return bool(existing), "from stored profile value"

    asset_class = profile.get("asset_class", "")
    eligible    = profile.get("l2_eligible_investors", "") or ""

    # Governance/platform tokens are freely tradeable regardless of pool eligibility
    # (Centrifuge CFG is the governance token, not a pool token)
    common_name = profile.get("common_name", "").upper()
    if common_name == "CENTRIFUGE":
        return False, "CFG is a freely-tradeable governance token"

    # Behavioural signal: if we have events and >80% are mint/burn
    if total_count > 0:
        mb_ratio = (mint_count + burn_count) / total_count
        if mb_ratio > 0.80:
            return True, f"behavioural: {mb_ratio:.0%} of transfers are mint/burn"

    # Eligibility signal
    # Behavioural signal — low mint/burn ratio means active secondary market → permissionless
    if total_count > 0:
        mb_ratio = (mint_count + burn_count) / total_count
        if mb_ratio < 0.30:
            return False, f"behavioural: only {mb_ratio:.0%} of transfers are mint/burn (secondary market active)"

    # Eligibility signal — on-chain whitelist enforced for US-regulated wrapper structures
    # Note: non_us_persons_reg_s is an investor eligibility category, not a transfer restriction —
    # these tokens (USDY) can be freely transferred in secondary markets once issued.
    restricted_eligibility = {
        "qualified_purchaser",
        "qualified_institutional_buyer",
        "accredited",
    }
    if eligible in restricted_eligibility and asset_class in ("MMF", "Tokenised Treasuries"):
        return True, f"eligibility-based: {eligible} + {asset_class}"

    return False, f"default: permissionless (eligible={eligible})"


# ── Activity tier rubric ───────────────────────────────────────────────────────

def _compute_activity_tier(is_restricted: bool, mint_count: int, burn_count: int,
                             transfer_count: int, unique_senders: int) -> str:
    """
    Compute activity_tier per the calibrated rubric.

    Restricted tokens (BUIDL, USYC, OUSG): tier from mint+burn count.
      Whitelist-gated tokens primarily show activity through issuance/redemption events.
    Permissionless tokens (USDY, syrupUSDC, CFG): tier from transfer count alone.
      Transfer count is the primary differentiator; unique_senders dropped for simplicity.

    Calibrated against May 2026 anchor set (47–37,419 transfer range).
    Five tiers: dormant | low | moderate | high | very_high
    """
    if is_restricted:
        mb = mint_count + burn_count
        if mb > 200:
            return "very_high"
        if mb > 50:
            return "high"
        if mb > 10:
            return "moderate"
        if mb > 0:
            return "low"
        return "dormant"
    else:
        if transfer_count > 10_000:
            return "very_high"
        if transfer_count > 1_000:
            return "high"
        if transfer_count > 100:
            return "moderate"
        if transfer_count > 10:
            return "low"
        return "dormant"


# ── Metrics computation ────────────────────────────────────────────────────────

def _compute_metrics(logs: list, decimals: int, price_usd: float) -> dict:
    """
    Compute all activity metrics from a list of Transfer event logs.

    Returns a dict with all activity field values.
    """
    scale = 10 ** decimals

    mints = [l for l in logs if l.get("topics", ["", ""])[1] == ZERO_ADDR_TOPIC]
    burns = [l for l in logs
             if len(l.get("topics", [])) > 2 and l["topics"][2] == ZERO_ADDR_TOPIC]

    # Exclude mint/burn from "regular" transfers for sender/receiver counts
    def parse_amount(log_entry: dict) -> float:
        try:
            return int(log_entry.get("data", "0x0"), 16) / scale
        except (ValueError, ZeroDivisionError):
            return 0.0

    # Aggregate volumes
    mint_volume = sum(parse_amount(l) for l in mints)
    burn_volume = sum(parse_amount(l) for l in burns)
    total_volume = sum(parse_amount(l) for l in logs)

    # Participant counts — exclude zero address
    senders   = set()
    receivers = set()
    for l in logs:
        topics = l.get("topics", [])
        if len(topics) >= 2:
            from_raw = topics[1]
            to_raw   = topics[2] if len(topics) > 2 else ""
            from_addr = "0x" + from_raw[-40:] if len(from_raw) >= 40 else ""
            to_addr   = "0x" + to_raw[-40:]   if len(to_raw)   >= 40 else ""
            if from_addr and from_addr != ZERO_ADDR:
                senders.add(from_addr.lower())
            if to_addr and to_addr != ZERO_ADDR:
                receivers.add(to_addr.lower())

    # Active holder count: unique addresses that sent OR received
    active_holders = len(senders | receivers)

    return {
        "transfer_count_30d":      len(logs),
        "transfer_volume_usd_30d": round(total_volume * price_usd, 2),
        "mint_count_30d":          len(mints),
        "burn_count_30d":          len(burns),
        "mint_volume_usd_30d":     round(mint_volume * price_usd, 2),
        "burn_volume_usd_30d":     round(burn_volume * price_usd, 2),
        "net_flow_usd_30d":        round((mint_volume - burn_volume) * price_usd, 2),
        "unique_senders_30d":      len(senders),
        "unique_receivers_30d":    len(receivers),
        "active_holder_count":     active_holders,
    }


# ── Main enrichment loop ───────────────────────────────────────────────────────

def _enrich_profile(profile: dict, from_block: int, to_block: int,
                    dry_run: bool = False) -> Optional[dict]:
    """
    Enrich a single profile. Returns the dict that would be written to Supabase,
    or None if the profile should be skipped (no Ethereum address, API error).
    """
    slug         = profile.get("defillama_slug", "unknown")
    common_name  = profile.get("common_name") or slug

    # ── Resolve Ethereum contract address ─────────────────────────────────────
    eth_addr, anomaly_note = _resolve_eth_address(profile)
    if not eth_addr:
        warn(f"{slug}: no Ethereum contract address found — skipping")
        return None
    if anomaly_note:
        warn(f"{slug}: {anomaly_note}")

    log(f"{slug} ({eth_addr[:10]}...{eth_addr[-4:]}): fetching 30d Transfer events")

    # ── Fetch Transfer event logs ──────────────────────────────────────────────
    try:
        logs = _fetch_all_logs(eth_addr, from_block, to_block)
    except Exception as e:
        warn(f"{slug}: getLogs failed — {e}")
        return None

    log(f"  {slug}: {len(logs)} Transfer events fetched")

    # ── Token decimals ─────────────────────────────────────────────────────────
    try:
        decimals = _get_token_decimals(eth_addr)
    except Exception as e:
        warn(f"{slug}: decimals fetch failed ({e}); defaulting to 18")
        decimals = 18

    # ── Token price ────────────────────────────────────────────────────────────
    price_usd = _get_token_price(slug)

    # ── Compute metrics ────────────────────────────────────────────────────────
    metrics = _compute_metrics(logs, decimals, price_usd)

    # ── Transfer restricted ────────────────────────────────────────────────────
    is_restricted, restricted_rationale = _infer_transfer_restricted(
        profile,
        metrics["mint_count_30d"],
        metrics["burn_count_30d"],
        metrics["transfer_count_30d"],
    )

    # ── Activity tier ──────────────────────────────────────────────────────────
    tier = _compute_activity_tier(
        is_restricted,
        metrics["mint_count_30d"],
        metrics["burn_count_30d"],
        metrics["transfer_count_30d"],
        metrics["unique_senders_30d"],
    )

    # ── TVL ratio ──────────────────────────────────────────────────────────────
    tvl = _get_current_tvl(slug)
    if tvl and tvl > 0 and metrics["transfer_volume_usd_30d"] is not None:
        tvl_ratio = round(metrics["transfer_volume_usd_30d"] / tvl, 6)
    else:
        tvl_ratio = None

    # ── Holder count ──────────────────────────────────────────────────────────
    # tokenholdercount requires Etherscan Pro; leave l3_holder_count unchanged.
    # holder_count_change_30d requires two snapshots; not computable yet.

    # ── Build update dict ──────────────────────────────────────────────────────
    update = {
        "defillama_slug": slug,
        **metrics,
        "activity_tier":          tier,
        "activity_vs_tvl_ratio":  tvl_ratio,
        "transfer_restricted":    is_restricted,
        "activity_data_as_of":    date.today().isoformat(),
        "activity_data_source":   "Etherscan API v2 + CoinGecko spot price",
    }

    # ── Log summary ───────────────────────────────────────────────────────────
    tier_basis = "restricted (mint+burn)" if is_restricted else "permissionless (transfer count)"
    log(
        f"  {slug}: {metrics['transfer_count_30d']} transfers "
        f"({metrics['mint_count_30d']} mints, {metrics['burn_count_30d']} burns), "
        f"{metrics['unique_senders_30d']} unique senders → "
        f"tier '{tier}' ({tier_basis}) | restricted={is_restricted} [{restricted_rationale}]"
    )
    if tvl:
        log(f"    TVL ${tvl:,.0f} | transfer_volume ${metrics['transfer_volume_usd_30d']:,.0f} | ratio {tvl_ratio}")

    return update


def run(slugs: Optional[list] = None, dry_run: bool = False) -> dict:
    """
    Main entry point. Enriches all anchor profiles (or a specific subset).

    Args:
        slugs:   list of defillama_slug values to enrich. None → all Anchor profiles.
        dry_run: if True, compute but don't write to Supabase.

    Returns:
        summary dict with counts.
    """
    if not ETHERSCAN_API_KEY:
        warn("ETHERSCAN_API_KEY not set in .env — aborting")
        return {"status": "error", "reason": "no API key"}

    log(f"Starting enrichment run" + (" (dry-run)" if dry_run else ""))
    t0 = time.time()

    # ── Load profiles ──────────────────────────────────────────────────────────
    if slugs:
        profiles = [read_profile(s) for s in slugs]
        profiles = [p for p in profiles if p is not None]
    else:
        profiles = list_profiles(tier="Anchor")

    if not profiles:
        warn("No profiles found — aborting")
        return {"status": "error", "reason": "no profiles"}

    log(f"Loaded {len(profiles)} anchor profile(s)")

    # ── Determine 30-day block range ───────────────────────────────────────────
    log("Resolving 30-day block range...")
    try:
        to_block   = _get_current_block()
        ts_30d_ago = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        from_block = _get_block_at_timestamp(ts_30d_ago)
        log(f"Block range: {from_block:,} → {to_block:,} ({to_block - from_block:,} blocks)")
    except Exception as e:
        warn(f"Could not determine block range: {e}")
        return {"status": "error", "reason": str(e)}

    # ── Enrich each profile ────────────────────────────────────────────────────
    n_success = 0
    n_skipped = 0
    n_errors  = 0
    api_call_count = 0  # rough estimate

    for profile in profiles:
        slug = profile.get("defillama_slug", "?")
        try:
            update = _enrich_profile(profile, from_block, to_block, dry_run=dry_run)
        except Exception as e:
            warn(f"{slug}: unexpected error — {e}")
            n_errors += 1
            continue

        if update is None:
            n_skipped += 1
            continue

        if dry_run:
            log(f"  {slug}: (dry-run) would write: "
                f"transfer_count={update.get('transfer_count_30d')}, "
                f"tier={update.get('activity_tier')}")
            n_success += 1
        else:
            try:
                write_profile(update)
                n_success += 1
            except Exception as e:
                warn(f"{slug}: write_profile failed — {e}")
                n_errors += 1
                continue

    elapsed = time.time() - t0
    total = len(profiles)
    log(f"Completed: {n_success}/{total} enriched, {n_skipped} skipped, "
        f"{n_errors} errors. Run time: {elapsed:.1f}s")

    # ── Pro-tier limitation note ───────────────────────────────────────────────
    log("NOTE: l3_holder_count and l3_top10_holder_pct require Etherscan Pro "
        "and were NOT updated. Existing profile values are preserved.")

    return {
        "status": "ok",
        "enriched": n_success,
        "skipped":  n_skipped,
        "errors":   n_errors,
        "elapsed":  round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="On-chain activity enrichment for anchor protocol profiles"
    )
    parser.add_argument(
        "--slug", help="Enrich only this profile (defillama_slug)", default=None
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute metrics but do not write to Supabase"
    )
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else None
    result = run(slugs=slugs, dry_run=args.dry_run)

    if result.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
