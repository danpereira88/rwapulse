"""
Multichain RWA token supply × price — replaces DefiLlama protocol TVL for the
products DefiLlama moved to its paid RWA API (from 2026-09-20).

Free sources only:
  • CoinGecko /coins/{id} (no key needed): each token's contract address on
    every chain it lives on, its decimals, and its current USD price/NAV.
  • Public JSON-RPC (EVM chains) and Solana RPC: totalSupply read straight from
    the token contracts.
  • Stellar Horizon: issued amount for Stellar assets.

Tables (appended per snapshot_date):
  rwa_contracts   — slug, coingecko_id, platform, address, decimals (latest seen)
  rwa_supply      — one row per token per chain: supply, price, value, status
  rwa_product_aum — one row per product: summed value, coverage, CoinGecko
                    market cap for comparison

Usage:
  python ingest/rwa_supply.py              # run and write
  python ingest/rwa_supply.py --dry-run    # fetch and print, write nothing
RPC overrides: set RPC_<PLATFORM> in .env, e.g. RPC_ETHEREUM, RPC_BINANCE_SMART_CHAIN.
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection, append_snapshot  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CG_BASE = "https://api.coingecko.com/api/v3"
CG_KEY = os.getenv("COINGECKO_API_KEY", "")

# DefiLlama protocol slug -> CoinGecko ids that make up that product.
# Slugs match dl_protocols so the dashboard can join on them.
PRODUCTS = {
    "blackrock-buidl": ["blackrock-usd-institutional-digital-liquidity-fund"],
    "circle-usyc": ["hashnote-usyc"],
    "ondo-yield-assets": ["ousg", "ondo-us-dollar-yield"],
    "spiko": ["spiko-us-t-bills-money-market-fund", "eutbl", "spiko-amundi-overnight-swap-fund-eur",
              "safo", "spiko-uk-t-bills-money-market-fund", "spiko-amundi-overnight-swap-fund-gbp",
              "spiko-amundi-overnight-swap-fund-chf"],
    "anemoy-capital": ["janus-henderson-anemoy-aaa-clo-fund"],
    "wisdomtree": ["wisdomtree-treasury-money-market-digital-fund"],
    "openeden-tbill": ["openeden-tbill"],
    "invesco-ustb": ["superstate-short-duration-us-government-securities-fund-ustb"],
    "tether-gold": ["tether-gold"],
    "paxos-gold": ["pax-gold"],
    # Not a DefiLlama protocol slug: tracked for reference, not added to totals
    "_franklin-benji": ["franklin-templeton-benji"],
}

# CoinGecko platform id -> public RPC endpoints (tried in order).
EVM_RPCS = {
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "https://rpc.ankr.com/eth"],
    "binance-smart-chain": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnb.org"],
    "polygon-pos": ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"],
    "arbitrum-one": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"],
    "base": ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
    "optimistic-ethereum": ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"],
    "avalanche": ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"],
    "mantle": ["https://mantle-rpc.publicnode.com", "https://rpc.mantle.xyz"],
    "etherlink": ["https://node.mainnet.etherlink.com"],
}
SOLANA_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
STELLAR_HORIZON = "https://horizon.stellar.org"

TOTAL_SUPPLY = "0x18160ddd"
DECIMALS = "0x313ce567"

session = requests.Session()


def _rpcs(platform: str, defaults: list) -> list:
    override = os.getenv("RPC_" + platform.upper().replace("-", "_"), "").strip()
    return ([override] if override else []) + defaults


def cg_get(path: str, params: Optional[dict] = None, retries: int = 4):
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    for attempt in range(retries):
        r = session.get(f"{CG_BASE}{path}", params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            log.warning("CoinGecko rate limit — waiting 60s")
            time.sleep(60)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"CoinGecko kept rate-limiting {path}")


def jsonrpc(urls: list, method: str, params: list):
    last = None
    for url in urls:
        try:
            r = session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                last = j["error"]
                continue
            return j["result"]
        except Exception as e:  # try the next endpoint
            last = e
    raise RuntimeError(f"{method} failed on all endpoints: {last}")


def evm_supply(platform: str, address: str, decimals: Optional[int]) -> float:
    urls = _rpcs(platform, EVM_RPCS[platform])
    raw = jsonrpc(urls, "eth_call", [{"to": address, "data": TOTAL_SUPPLY}, "latest"])
    if decimals is None:
        decimals = int(jsonrpc(urls, "eth_call", [{"to": address, "data": DECIMALS}, "latest"]), 16)
    return int(raw, 16) / 10 ** decimals


def solana_supply(mint: str) -> float:
    res = jsonrpc(_rpcs("solana", SOLANA_RPCS), "getTokenSupply", [mint])
    v = res["value"]
    return int(v["amount"]) / 10 ** int(v["decimals"])


def stellar_supply(address: str) -> float:
    # CoinGecko lists Stellar assets as CODE-ISSUER (or CODE:ISSUER)
    for sep in ("-", ":"):
        if sep in address:
            code, issuer = address.split(sep, 1)
            break
    else:
        raise ValueError(f"unrecognised Stellar asset id {address!r}")
    r = session.get(f"{STELLAR_HORIZON}/assets", params={"asset_code": code, "asset_issuer": issuer}, timeout=20)
    r.raise_for_status()
    recs = r.json()["_embedded"]["records"]
    if not recs:
        raise ValueError("asset not found on Horizon")
    rec = recs[0]
    # 'amount' covers authorised holders; add claimable balances and pool shares when present
    total = float(rec.get("amount") or 0)
    total += float(rec.get("claimable_balances_amount") or 0)
    total += float(rec.get("liquidity_pools_amount") or 0)
    total += float(rec.get("contracts_amount") or 0)
    return total


def read_supply(platform: str, address: str, decimals: Optional[int]) -> tuple[Optional[float], str]:
    try:
        if platform in EVM_RPCS:
            return evm_supply(platform, address, decimals), "ok"
        if platform == "solana":
            return solana_supply(address), "ok"
        if platform == "stellar":
            if address.startswith("C") and len(address) == 56 and "-" not in address and ":" not in address:
                # Soroban contract id — Horizon can't look these up by contract
                return None, "unsupported (Stellar Soroban contract id)"
            return stellar_supply(address), "ok"
        return None, "unsupported chain"
    except Exception as e:
        return None, f"error: {str(e)[:120]}"


def fetch_token(cg_id: str) -> Optional[dict]:
    d = cg_get(f"/coins/{cg_id}", params={
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "false", "developer_data": "false", "sparkline": "false"})
    if not d:
        return None
    md = d.get("market_data") or {}
    platforms = []
    for plat, info in (d.get("detail_platforms") or {}).items():
        addr = (info or {}).get("contract_address")
        if plat and addr:
            platforms.append({"platform": plat, "address": addr.strip(), "decimals": (info or {}).get("decimal_place")})
    return {"coingecko_id": cg_id, "symbol": (d.get("symbol") or "").upper(), "name": d.get("name"),
            "price": (md.get("current_price") or {}).get("usd"),
            "cg_mcap": (md.get("market_cap") or {}).get("usd"), "platforms": platforms}


DEFAULT_PAUSE = 2.5 if CG_KEY else 13.0  # free tier without a key allows only a few calls a minute


def fetch_prices(ids: list) -> dict:
    """One batched call: {coingecko_id: (price, market_cap)}."""
    d = cg_get("/simple/price", params={"ids": ",".join(ids), "vs_currencies": "usd",
                                        "include_market_cap": "true"}) or {}
    return {k: (v.get("usd"), v.get("usd_market_cap")) for k, v in d.items()}


def collect(products: dict = PRODUCTS, pause: float = DEFAULT_PAUSE,
            cached: Optional[dict] = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """cached: {coingecko_id: [platform dicts]} from rwa_contracts. Tokens in it
    skip the per-coin lookup; their prices come from one batched call."""
    now = datetime.now(timezone.utc).isoformat()
    contracts, supply = [], []
    cached = cached or {}
    all_ids = [c for ids in products.values() for c in ids]
    prices = {}
    if any(c in cached for c in all_ids):
        try:
            prices = fetch_prices([c for c in all_ids if c in cached])
        except Exception as e:
            log.warning(f"Batched price lookup failed, falling back to per-coin lookups — {e}")
            cached = {}
    for slug, ids in products.items():
        for cg_id in ids:
            if cg_id in cached and cg_id in prices:
                p, mc = prices[cg_id]
                t = {"coingecko_id": cg_id, "symbol": "", "name": None, "price": p, "cg_mcap": mc,
                     "platforms": cached[cg_id]}
            else:
                try:
                    t = fetch_token(cg_id)
                except Exception as e:
                    log.warning(f"{cg_id}: CoinGecko lookup failed — {e}")
                    t = None
                time.sleep(pause)  # free CoinGecko tier: stay under the rate limit
            if not t:
                supply.append({"slug": slug, "coingecko_id": cg_id, "platform": None, "address": None,
                               "supply": None, "price_usd": None, "value_usd": None, "cg_mcap_usd": None,
                               "status": "coingecko lookup failed", "ingested_at": now})
                continue
            if not t["platforms"]:
                log.warning(f"{cg_id}: CoinGecko lists no contract addresses")
            for p in t["platforms"]:
                contracts.append({"slug": slug, "coingecko_id": cg_id, **p, "ingested_at": now})
                amt, status = read_supply(p["platform"], p["address"], p["decimals"])
                supply.append({"slug": slug, "coingecko_id": cg_id, "symbol": t["symbol"],
                               "platform": p["platform"], "address": p["address"], "supply": amt,
                               "price_usd": t["price"],
                               "value_usd": amt * t["price"] if amt is not None and t["price"] else None,
                               "cg_mcap_usd": t["cg_mcap"], "status": status, "ingested_at": now})
                log.info(f"  {cg_id} on {p['platform']}: {status}" + (f", {amt:,.0f}" if amt is not None else ""))

    sup = pd.DataFrame(supply)
    aum = []
    for slug, g in sup.groupby("slug"):
        rows = g[g["platform"].notna()]
        ok = rows[rows["status"] == "ok"]
        cg_mcap = g.drop_duplicates("coingecko_id")["cg_mcap_usd"].fillna(0).sum()
        val = ok["value_usd"].fillna(0).sum()
        lookup_failed = bool((g["platform"].isna()).any())
        aum.append({"slug": slug, "value_usd": val if len(ok) else None,
                    "chains_ok": len(ok), "chains_total": len(rows),
                    "complete": bool(len(rows) and len(ok) == len(rows) and not lookup_failed),
                    "cg_mcap_usd": cg_mcap or None,
                    "vs_coingecko": (val / cg_mcap) if cg_mcap and len(ok) else None,
                    "ingested_at": now})
    return pd.DataFrame(contracts), sup, pd.DataFrame(aum)


def load_cached_contracts(max_age_days: int = 30) -> dict:
    """Contract addresses from rwa_contracts, if refreshed within max_age_days."""
    try:
        con = get_connection()
    except Exception:
        return {}
    try:
        if not con.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_name = 'rwa_contracts'").fetchone()[0]:
            return {}
        rows = con.execute(f"""SELECT coingecko_id, platform, address, decimals FROM rwa_contracts
                               WHERE TRY_CAST(ingested_at AS TIMESTAMPTZ) >= now() - INTERVAL {int(max_age_days)} DAY""").fetchall()
    finally:
        con.close()
    out = {}
    for cg_id, plat, addr, dec in rows:
        out.setdefault(cg_id, []).append({"platform": plat, "address": addr,
                                          "decimals": int(dec) if dec is not None else None})
    return out


def run(dry_run: bool = False, refresh_contracts: bool = False) -> dict:
    cached = {} if refresh_contracts else load_cached_contracts()
    if cached:
        log.info(f"Using cached contract addresses for {len(cached)} tokens (refresh with --refresh-contracts)")
    contracts, sup, aum = collect(cached=cached)
    if dry_run:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(aum.to_string(index=False))
        return {"products": len(aum), "dry_run": True}
    con = get_connection()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS rwa_contracts
                       (slug VARCHAR, coingecko_id VARCHAR, platform VARCHAR, address VARCHAR,
                        decimals BIGINT, ingested_at VARCHAR)""")
        if not contracts.empty:
            con.execute("""DELETE FROM rwa_contracts USING contracts
                           WHERE rwa_contracts.coingecko_id = contracts.coingecko_id
                             AND rwa_contracts.platform = contracts.platform""")
            con.execute("INSERT INTO rwa_contracts SELECT slug, coingecko_id, platform, address, "
                        "TRY_CAST(decimals AS BIGINT), ingested_at FROM contracts")
        n1 = append_snapshot(con, "rwa_supply", sup)
        n2 = append_snapshot(con, "rwa_product_aum", aum)
    finally:
        con.close()
    bad = aum[(aum["vs_coingecko"].notna()) & ((aum["vs_coingecko"] > 1.5) | (aum["vs_coingecko"] < 0.5))]
    for r in bad.itertuples():
        log.warning(f"{r.slug}: on-chain value is {r.vs_coingecko:.2f}× CoinGecko market cap — check for "
                    "bridged duplicates or missing chains in rwa_supply")
    log.info(f"rwa_supply: {n1} rows, rwa_product_aum: {n2} rows ({date.today().isoformat()})")
    return {"supply_rows": n1, "products": n2,
            "complete": int(aum["complete"].sum()) if not aum.empty else 0}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-contracts", action="store_true", help="re-fetch contract addresses from CoinGecko")
    a = ap.parse_args()
    print(run(dry_run=a.dry_run, refresh_contracts=a.refresh_contracts))
