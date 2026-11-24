"""
Chain sweep — every tokenised real-world asset CoinGecko lists on a chain,
with supply read on-chain. Covers Solana and Ethereum.

The main on-chain reader (ingest/rwa_supply.py) covers the RWA Pulse universe:
the top of CoinGecko's RWA categories, looked up coin by coin. That misses
most tokenised stocks and the long tail on any one chain. The sweep instead:

  1. pages through every CoinGecko tokenised-asset category (/coins/markets:
     id, name, price, market cap in one call per 250 coins);
  2. maps each coin to its mint on the chain (/coins/list?include_platform=true,
     one call for every coin CoinGecko lists);
  3. reads each mint's supply from the chain (Solana getTokenSupply) × price.

Free sources only, no per-coin lookups, so a full Solana sweep is ~30 CoinGecko
calls plus one RPC call per mint.

What counts: a token in a specific tokenised-asset category (stocks, ETFs,
treasuries, MMFs, credit, gold, commodities…), or one in CoinGecko's broad RWA
category that RWA Pulse's universe rules classify as an asset (not "other").
Governance/platform tokens and stablecoins are excluded by the same rules as
the main universe (ingest/rwa_universe.py).

Table (appended per snapshot_date):
  chain_sweep — platform, coingecko_id, name, symbol, address, asset_class,
                issuer, categories, supply, issuer_held, circulating, price_usd,
                minted_value_usd, value_usd (circulating × price), cg_mcap_usd, status

Minted vs circulating: issuers often pre-mint inventory into a wallet they
control. Balances held by a mint's own authority wallets are subtracted, so
value_usd is what's held by everyone else.

Usage:
  python ingest/chain_sweep.py               # sweep Solana and write
  python ingest/chain_sweep.py --dry-run     # print, write nothing
"""

import argparse
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection  # noqa: E402
from ingest.rwa_supply import cg_get, jsonrpc, _rpcs, SOLANA_RPCS, CG_KEY  # noqa: E402
from ingest.rwa_universe import classify, EXCLUDE, CURATED  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# CoinGecko category -> asset class. Checked in this order: the first category a
# coin belongs to decides its class. None = generic (class from the name rules).
CATEGORY_CLASS = [
    ("tokenized-money-market-fund-mmfs", "treasuries"),
    ("tokenized-treasuries", "treasuries"),
    ("tokenized-t-bills", "treasuries"),
    ("tokenized-non-us-government-securities", "treasuries"),
    ("tokenized-private-credit", "credit"),
    ("tokenized-credit", "credit"),
    ("tokenized-stock", "stocks"),
    ("xstocks-ecosystem", "stocks"),
    ("tokenized-exchange-traded-funds-etfs", "stocks"),
    ("tokenized-exchange-traded-product-etps", "stocks"),
    ("tokenized-closed-end-funds-cefs", "stocks"),
    ("tokenized-pre-ipo-stocks", "stocks"),
    ("prestocks-ecosystem", "stocks"),
    ("remora-markets-tokenized-rstocks", "stocks"),
    ("backpack-securities-ecosystem", "stocks"),
    ("tokenized-gold", "commodities"),
    ("tokenized-silver", "commodities"),
    ("tokenized-commodities", "commodities"),
    ("ondo-tokenized-assets", None),
    ("securitize-ecosystem", None),
    ("tokenized-products", None),
    ("real-world-assets-rwa", None),
]
STABLECOIN_CATEGORIES = ("stablecoins",)
# In the universe's exclusion list only because the headline totals count them
# through DefiLlama; a chain view needs them. Kept in step with
# rwa_universe.CHAIN_ONLY and ui/insights.CHAIN_ONLY_META.
CHAIN_ONLY_CLASS = {"onyc": ("credit", "OnRe")}
MIN_MCAP = 10_000           # stop paging a category below this market cap
PAGE_PAUSE = 2.5 if CG_KEY else 6.0
RPC_PAUSE = 0.6     # between batched requests: free endpoints throttle bursts

def fetch_categories(categories=None, min_mcap: float = MIN_MCAP) -> dict:
    """{coingecko_id: {name, symbol, price, mcap, categories:[...]}} across categories."""
    coins = {}
    for cat in categories or [c for c, _ in CATEGORY_CLASS] + list(STABLECOIN_CATEGORIES):
        n = 0
        for page in range(1, 41):
            rows = cg_get("/coins/markets", params={"vs_currency": "usd", "category": cat,
                                                     "order": "market_cap_desc", "per_page": 250, "page": page})
            time.sleep(PAGE_PAUSE)
            if not rows:
                break
            for x in rows:
                c = coins.setdefault(x["id"], {"name": x.get("name"), "symbol": x.get("symbol"),
                                               "price": x.get("current_price"), "mcap": x.get("market_cap"),
                                               "categories": []})
                c["categories"].append(cat)
                n += 1
            if len(rows) < 250 or (rows[-1].get("market_cap") or 0) < min_mcap:
                break
        log.info(f"  {cat}: {n} coins")
    return coins


def asset_class(cg_id: str, coin: dict):
    """(asset_class, issuer) or (None, None) if the token doesn't count."""
    cats = coin["categories"]
    if cg_id in CHAIN_ONLY_CLASS:   # counted via DefiLlama in totals, but chain views need them
        return CHAIN_ONLY_CLASS[cg_id]
    if cg_id in EXCLUDE:
        return None, None
    specific = next((cls for cat, cls in CATEGORY_CLASS if cls and cat in cats), None)
    if any(c in cats for c in STABLECOIN_CATEGORIES) and specific not in ("treasuries",) and cg_id not in CURATED:
        return None, None
    # Name/issuer rules from the main universe; pretend broad-RWA membership so
    # classify() doesn't reject coins that only sit in a specific category.
    cls, issuer = classify(cg_id, coin.get("name") or "", coin.get("symbol") or "",
                           ",".join(cats + ["real-world-assets-rwa"]))
    if cls is None:
        return None, None
    if specific:
        cls = specific
    elif cls == "other" and cg_id not in CURATED:
        return None, None  # broad-RWA only and no asset pattern: likely a platform token
    return cls, issuer


SECONDS_PER_YEAR = 60 * 60 * 24 * 365.24  # as in spl-token-2022's interest-bearing extension


def solana_ui_multiplier(info: dict, now: float) -> float:
    """Token-2022 mints can show holders a UI amount that differs from the raw
    supply; CoinGecko prices the UI unit, so value = raw supply × this factor.
      scaledUiAmountConfig  — stock splits and dividends (xStocks and others)
      interestBearingConfig — continuously compounding notes (e.g. Etherfuse)"""
    m = 1.0
    for e in info.get("extensions") or []:
        st = e.get("state") or {}
        if e.get("extension") == "scaledUiAmountConfig":
            eff = st.get("newMultiplierEffectiveTimestamp") or 0
            m *= float(st.get("newMultiplier") if now >= eff else st.get("multiplier") or 1)
        elif e.get("extension") == "interestBearingConfig":
            init, last = st.get("initializationTimestamp") or 0, st.get("lastUpdateTimestamp") or 0
            pre, cur = st.get("preUpdateAverageRate") or 0, st.get("currentRate") or 0
            m *= math.exp((pre * (last - init) + cur * (now - last)) / SECONDS_PER_YEAR / 10_000)
    return m


TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")   # Token-2022


# ── Associated token accounts (PDA derivation, no extra dependencies) ──
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def b58encode(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def _on_curve(b: bytes) -> bool:
    """Is this 32-byte string a valid ed25519 point? (RFC 8032 decompression)"""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return not (b[31] >> 7)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def associated_token_address(owner: str, mint: str, token_program: str) -> str:
    import hashlib
    seeds = [b58decode(owner), b58decode(token_program), b58decode(mint)]
    prog = b58decode(_ATA_PROGRAM)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def _authorities(info: dict) -> set:
    """Wallets that control a mint: mint/freeze authority and every extension
    authority (scaled-UI, interest rate, metadata, permanent delegate…)."""
    a = {info.get("mintAuthority"), info.get("freezeAuthority")}
    for e in info.get("extensions") or []:
        for k, v in (e.get("state") or {}).items():
            if isinstance(v, str) and len(v) > 30 and (k.lower().endswith("authority") or k in ("delegate",)):
                a.add(v)
    return {x for x in a if x}


def read_solana_many(mints: list, batch: int = 100) -> dict:
    """{mint: {"supply", "issuer_held", "error"}}.

    supply      — UI supply from the mint account (one getMultipleAccounts call
                  per 100 mints), with Token-2022 multipliers applied.
    issuer_held — what the mint's own authority wallets hold. Issuers often
                  pre-mint inventory into their own wallet (xStocks keep 90%+ of
                  some tokens there); that isn't held by anyone else yet, so it's
                  reported but not counted as circulating. One
                  getTokenAccountsByOwner call per authority wallet and token
                  program covers every mint it holds."""
    out, now, info_by_mint, mult = {}, time.time(), {}, {}
    for i in range(0, len(mints), batch):
        chunk = mints[i:i + batch]
        try:
            res = jsonrpc(_rpcs("solana", SOLANA_RPCS), "getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
        except Exception as e:
            out.update({m: {"supply": None, "issuer_held": None, "error": f"read failed: {str(e)[:100]}"} for m in chunk})
            continue
        for mint, acc in zip(chunk, res["value"]):
            data = (acc or {}).get("data")
            info = ((data.get("parsed") or {}).get("info") if isinstance(data, dict) else None)
            if not info or info.get("supply") is None:
                out[mint] = {"supply": None, "issuer_held": None,
                             "error": "account not found" if acc is None else "not a mint"}
                continue
            mult[mint] = solana_ui_multiplier(info, now)
            raw = int(info["supply"]) / 10 ** int(info["decimals"])
            info_by_mint[mint] = {**info, "_program": acc.get("owner")}
            out[mint] = {"supply": raw * mult[mint], "issuer_held": 0.0, "error": None}
        time.sleep(RPC_PAUSE)

    # Issuer inventory: balances of each mint held by that mint's authorities
    auths = {m: _authorities(i) for m, i in info_by_mint.items()}
    holdings = {}   # (owner, mint) -> raw UI amount
    failed = set()
    for owner in sorted(set().union(*auths.values()) if auths else set()):
        # All-or-nothing per wallet: if either token program's listing fails
        # (after retries), drop what was read for this wallet and fall back to
        # its associated accounts below — never both, or balances double up.
        found, ok = {}, True
        for prog in TOKEN_PROGRAMS:
            res = None
            for attempt in range(4):
                try:
                    res = jsonrpc(_rpcs("solana", SOLANA_RPCS), "getTokenAccountsByOwner",
                                  [owner, {"programId": prog}, {"encoding": "jsonParsed"}])
                    break
                except Exception as e:
                    last = e
                    time.sleep(2 * (attempt + 1))
            if res is None:
                ok = False
                log.warning(f"  holdings of {owner[:8]}… not listed — {str(last)[:120]}; using its associated accounts")
                break
            for acc in res.get("value") or []:
                ti = acc["account"]["data"]["parsed"]["info"]
                amt = ti.get("tokenAmount") or {}
                key = (owner, ti.get("mint"))
                found[key] = found.get(key, 0.0) + int(amt.get("amount") or 0) / 10 ** int(amt.get("decimals") or 0)
            time.sleep(RPC_PAUSE)
        if ok:
            holdings.update(found)
        else:
            failed.add(owner)
    # Fallback for wallets the RPC wouldn't list (method unavailable, too many
    # accounts): read their associated token account for each mint directly.
    if failed:
        pairs = [(o, m, info_by_mint[m].get("_program") or TOKEN_PROGRAMS[0])
                 for m, owners in auths.items() for o in owners if o in failed]
        atas = [associated_token_address(o, m, p) for o, m, p in pairs]
        for i in range(0, len(atas), batch):
            try:
                res = jsonrpc(_rpcs("solana", SOLANA_RPCS), "getMultipleAccounts",
                              [atas[i:i + batch], {"encoding": "jsonParsed"}])
            except Exception as e:
                log.warning(f"  associated-account reads failed — {str(e)[:80]}")
                continue
            for (o, m, _), acc in zip(pairs[i:i + batch], res["value"]):
                if acc:
                    amt = acc["data"]["parsed"]["info"]["tokenAmount"]
                    holdings[(o, m)] = holdings.get((o, m), 0.0) + int(amt["amount"]) / 10 ** int(amt["decimals"])
            time.sleep(RPC_PAUSE)
        failed = set()   # ATA reads cover the main account; other accounts of those wallets are missed
    for m, owners in auths.items():
        if owners & failed:
            out[m]["issuer_held"] = None   # unknown: don't pretend it's zero
            continue
        held = sum(holdings.get((o, m), 0.0) for o in owners) * mult.get(m, 1.0)
        if held > out[m]["supply"] * 1.001:     # can't hold more than exists: a read went wrong
            out[m]["issuer_held"] = None
            out[m]["error"] = None
            log.warning(f"  {m[:8]}…: issuer holdings ({held:,.0f}) exceed supply ({out[m]['supply']:,.0f}) — not used")
            continue
        out[m]["issuer_held"] = min(held, out[m]["supply"])
    return out


# ── EVM chains ──
# ERC-20s have no standard "authority" fields. Issuer inventory is looked for
# in the contract's owner(), the token contract itself, the issuer role getters
# many RWA tokens expose (minter, burner, pauser, multiplierUpdater — Backed's
# xStocks keep pre-minted inventory with the burner, e.g. 99.96% of TBLL xStock
# on 24 Nov 2026), plus any known issuer treasuries below.
# totalSupply/balanceOf are already in display units (xStocks' multiplier is
# applied inside the contract: balanceOf = sharesOf × multiplier), so no
# adjustment is needed on EVM.
SEL_TOTAL_SUPPLY, SEL_DECIMALS, SEL_OWNER, SEL_BALANCE_OF = "0x18160ddd", "0x313ce567", "0x8da5cb5b", "0x70a08231"
SEL_ROLES = {"minter()": "0x07546172", "burner()": "0x27810b6e", "pauser()": "0x9fd0506d",
             "multiplierUpdater()": "0x0194b09b"}   # keccak-256 selectors
ISSUER_TREASURIES = {   # platform -> extra wallets known to hold unsold issuer inventory
    "ethereum": [],
}
EVM_BATCH = 15          # tokens per batched JSON-RPC request (7 calls each, ~105 per request)


# Keyless public endpoints that accept batched eth_call. Tried in rotation with
# backoff: free endpoints throttle bursts, so one failure isn't final.
SWEEP_EVM_RPCS = {
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org", "https://1rpc.io/eth",
                 "https://eth.llamarpc.com"],
}
EVM_ATTEMPTS = 8


def evm_batch(platform: str, calls: list) -> list:
    """calls: [(to, data)] -> [hex result or None] in one batched request.
    Rotates through the chain's endpoints with backoff; a reverted call (no
    owner(), no minter()…) comes back as None, not as a failure."""
    from ingest.rwa_supply import session, EVM_RPCS
    urls = _rpcs(platform, SWEEP_EVM_RPCS.get(platform) or EVM_RPCS[platform])
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
               for i, (to, data) in enumerate(calls)]
    last = None
    for attempt in range(EVM_ATTEMPTS):
        url = urls[attempt % len(urls)]
        try:
            r = session.post(url, json=payload, timeout=40)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            res = r.json()
            if not isinstance(res, list):      # whole batch refused (rate limit, size limit, auth)
                raise RuntimeError(str(res)[:300])
            by_id = {x.get("id"): x.get("result") for x in res}
            return [by_id.get(i) for i in range(len(calls))]
        except Exception as e:
            last = f"{url}: {e}"
            time.sleep(min(2 * (attempt + 1), 10))
    raise RuntimeError(f"batched eth_call failed after {EVM_ATTEMPTS} attempts — last: {last}"[:400])


def _uint(h):
    try:
        return int(h, 16) if h and h != "0x" else None
    except (TypeError, ValueError):
        return None


def _addr(h):
    if not h or len(h) < 66:
        return None
    a = "0x" + h[-40:]
    return None if int(a, 16) == 0 else a


def read_evm_many(platform: str, addresses: list) -> dict:
    """{address: {"supply", "issuer_held", "error"}} via batched eth_call:
    totalSupply, decimals and owner() per token, then balanceOf for the owner,
    the contract itself and known issuer treasuries."""
    out, meta = {}, {}
    for i in range(0, len(addresses), EVM_BATCH):
        chunk = addresses[i:i + EVM_BATCH]
        sels = (SEL_TOTAL_SUPPLY, SEL_DECIMALS, SEL_OWNER, *SEL_ROLES.values())
        calls = [(a, sel) for a in chunk for sel in sels]
        try:
            res = evm_batch(platform, calls)
        except Exception as e:
            out.update({a: {"supply": None, "issuer_held": None, "error": f"read failed: {str(e)[:300]}"} for a in chunk})
            continue
        n = len(sels)
        for k, a in enumerate(chunk):
            ts, dec = res[n * k], _uint(res[n * k + 1])
            wallets = {_addr(res[n * k + j]) for j in range(2, n)} - {None}
            if _uint(ts) is None or dec is None or dec > 36:
                out[a] = {"supply": None, "issuer_held": None, "error": "not an ERC-20 (no totalSupply/decimals)"}
                continue
            meta[a] = (dec, wallets)
            out[a] = {"supply": _uint(ts) / 10 ** dec, "issuer_held": 0.0, "error": None}
        time.sleep(RPC_PAUSE)
    # Issuer inventory
    pairs = []
    for a, (dec, wallets) in meta.items():
        for h in {*wallets, a.lower(), *ISSUER_TREASURIES.get(platform, [])}:
            pairs.append((a, h))
    for i in range(0, len(pairs), EVM_BATCH * 3):
        chunk = pairs[i:i + EVM_BATCH * 3]
        try:
            res = evm_batch(platform, [(a, SEL_BALANCE_OF + h[2:].lower().rjust(64, "0")) for a, h in chunk])
        except Exception as e:
            for a, _ in chunk:
                out[a]["issuer_held"] = None
            log.warning(f"  issuer balances not read — {str(e)[:80]}")
            continue
        for (a, h), r in zip(chunk, res):
            v = _uint(r)
            if out[a]["issuer_held"] is not None and v is not None:
                out[a]["issuer_held"] += v / 10 ** meta[a][0]
        time.sleep(RPC_PAUSE)
    return out


READERS = {"solana": read_solana_many, "ethereum": lambda addrs: read_evm_many("ethereum", addrs)}
SWEEP_PLATFORMS = ("solana", "ethereum")
MAX_FAILED_SHARE = 0.10   # a chain sweep with more unread tokens than this isn't saved


def fetch_all_platform_addresses(platforms) -> dict:
    """{platform: {coingecko_id: address}} from one /coins/list call."""
    rows = cg_get("/coins/list", params={"include_platform": "true"}) or []
    out = {p: {} for p in platforms}
    for r in rows:
        for p in platforms:
            a = (r.get("platforms") or {}).get(p)
            if a:
                out[p][r["id"]] = a
    return out


def sweep(platforms=SWEEP_PLATFORMS) -> pd.DataFrame:
    """One pass over CoinGecko's categories, then every chain in `platforms`."""
    now = datetime.now(timezone.utc).isoformat()
    log.info(f"Sweeping CoinGecko tokenised-asset categories for {', '.join(platforms)}")
    coins = fetch_categories()
    addrs_by_platform = fetch_all_platform_addresses(platforms)
    frames = []
    for platform in platforms:
        addrs = addrs_by_platform[platform]
        keep = {}
        for cg_id, coin in coins.items():
            if cg_id in addrs:
                cls, issuer = asset_class(cg_id, coin)
                if cls is not None:
                    keep[cg_id] = (cls, issuer)
        try:
            reads = READERS[platform]([addrs[c] for c in keep])
        except Exception as e:
            log.warning(f"{platform}: sweep reads failed — {e}")
            continue
        rows = []
        for cg_id, (cls, issuer) in keep.items():
            coin, addr = coins[cg_id], addrs[cg_id]
            rd = reads.get(addr) or {"supply": None, "issuer_held": None, "error": "not read"}
            amt, held, err = rd["supply"], rd["issuer_held"], rd["error"]
            circ = max(amt - held, 0.0) if amt is not None and held is not None else None
            status = err or ("ok" if held is not None else "ok, issuer holdings not read")
            price = coin.get("price")
            rows.append({"platform": platform, "coingecko_id": cg_id, "name": coin.get("name"),
                         "symbol": coin.get("symbol"), "address": addr, "asset_class": cls, "issuer": issuer,
                         "categories": ",".join(coin["categories"]), "supply": amt, "issuer_held": held,
                         "circulating": circ, "price_usd": price,
                         "minted_value_usd": amt * price if amt is not None and price else None,
                         # value = circulating × price; minted when issuer holdings couldn't be read
                         "value_usd": (circ if circ is not None else amt) * price if amt is not None and price else None,
                         "cg_mcap_usd": coin.get("mcap"),
                         "status": status if price else ("no price" if status.startswith("ok") else status),
                         "ingested_at": now})
        df = pd.DataFrame(rows)
        if not df.empty:
            ok = df[df["status"].str.startswith("ok")]
            # Unreadable tokens (RPC failures) and unread issuer wallets both
            # bias the totals; past the threshold, keep last week's sweep.
            bad = (~df["status"].str.startswith("ok")) & (df["status"] != "no price") \
                | (df["status"] == "ok, issuer holdings not read")
            share = bad.mean()
            log.info(f"{platform}: {len(df)} RWA tokens, {len(ok)} read, ${ok['value_usd'].sum() / 1e9:.2f}B "
                     f"circulating (${ok['minted_value_usd'].sum() / 1e9:.2f}B minted), {share:.0%} incomplete")
            if share > MAX_FAILED_SHARE:
                log.warning(f"{platform}: {share:.0%} of tokens incomplete (limit {MAX_FAILED_SHARE:.0%}) — "
                            f"not saving this chain's sweep; its page keeps the last complete one. "
                            f"First error: {df.loc[bad, 'status'].iloc[0][:300]}")
                continue
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run(dry_run: bool = False, platforms=SWEEP_PLATFORMS) -> dict:
    """Never raises: a failed sweep must not block the weekly publish. The chain
    pages show the sweep's date, so a stale sweep stays visible. All chains are
    written in one snapshot (snapshots are idempotent per table and date)."""
    try:
        df = sweep(platforms)
    except Exception as e:
        log.warning(f"Chain sweep failed, keeping the last sweep — {e}")
        return {"warning": f"sweep failed: {e}"}
    if dry_run:
        with pd.option_context("display.width", 200, "display.max_rows", 80):
            print(df.sort_values("value_usd", ascending=False).head(60)
                  [["platform", "coingecko_id", "asset_class", "issuer", "value_usd", "status"]].to_string(index=False))
        return {"tokens": len(df), "dry_run": True}
    if df.empty or not df["status"].str.startswith("ok").any():
        log.warning("Chain sweep read nothing, keeping the last sweep")
        return {"warning": "nothing read"}
    con = get_connection()
    try:
        n = save_sweep(con, df)
    finally:
        con.close()
    return {"tokens": len(df), "rows_written": n, "platforms": sorted(df["platform"].unique())}


def save_sweep(con, df: pd.DataFrame, snapshot_date: str = None) -> int:
    """Write this run's chains for today, replacing an earlier run of the same
    chains on the same day (append_snapshot would skip a same-day re-run)."""
    from datetime import date as _date
    df = df.copy()
    df["snapshot_date"] = snapshot_date or _date.today().isoformat()
    exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'chain_sweep'").fetchone()[0]
    if not exists:
        con.execute("CREATE TABLE chain_sweep AS SELECT * FROM df")
        return len(df)
    platforms = sorted(df["platform"].unique())
    con.execute(f"DELETE FROM chain_sweep WHERE snapshot_date = ? AND platform IN ({', '.join('?' * len(platforms))})",
                [df["snapshot_date"].iloc[0], *platforms])
    con.execute("INSERT INTO chain_sweep BY NAME SELECT * FROM df")
    return len(df)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--platform", action="append", choices=sorted(READERS),
                    help="chain to sweep (repeatable); default: all")
    a = ap.parse_args()
    print(run(dry_run=a.dry_run, platforms=tuple(a.platform) if a.platform else SWEEP_PLATFORMS))
