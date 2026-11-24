"""
Protocol GraphQL ingestion.

Sources:
  - Maple Finance: public API at https://api.maple.finance/v2/graphql (no key needed)
  - Centrifuge: public API at https://api.centrifuge.io (no key needed)
  - Goldfinch: The Graph decentralized network (requires THE_GRAPH_API_KEY)

Tables created:
  sg_maple_pools        — Maple Finance v2 lending pool metadata
  sg_maple_loans        — Loan-level data from Maple (amount, status, borrower)
  sg_centrifuge_pools   — Centrifuge pool metadata and NAV
  sg_centrifuge_assets  — Individual assets / tranches
  sg_goldfinch_pools    — Goldfinch senior/junior pool data
  sg_goldfinch_tranched_pools — Goldfinch tranched pool data
"""

import time
import logging
from pathlib import Path
from typing import Optional
import sys

import requests
import pandas as pd
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection, append_snapshot

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY", "")
GRAPH_GATEWAY = "https://gateway.thegraph.com"
GRAPH_GATEWAY_ARB = "https://gateway-arbitrum.network.thegraph.com"

# Subgraph IDs on The Graph decentralized network
# Verified against The Graph Explorer, May 2026
SUBGRAPHS = {
    "maple_ethereum": {
        "id": "5H97eNhy9fVHcqRXZtCV2UxHG2DbzcFA7yth1TaVZ45x",
        "chain": "mainnet",
        "description": "Maple Finance v1 Ethereum",
    },
    "goldfinch_v2": {
        "id": "G9N1RFta3jbpPNmeGxSJoMVBZUJeG1jiSxUfYG29UQHj",
        "chain": "arbitrum-one",
        "description": "Goldfinch V2 (Arbitrum)",
    },
}


def _subgraph_url(subgraph_key: str) -> Optional[str]:
    """Build the GraphQL endpoint URL for a given subgraph."""
    if not GRAPH_API_KEY:
        return None
    sg = SUBGRAPHS.get(subgraph_key, {})
    sg_id = sg.get("id")
    if not sg_id:
        return None
    chain = sg.get("chain", "mainnet")
    # The Graph gateway: key goes in Authorization header, not URL path
    if chain == "arbitrum-one":
        return f"{GRAPH_GATEWAY_ARB}/api/subgraphs/id/{sg_id}"
    return f"{GRAPH_GATEWAY}/api/subgraphs/id/{sg_id}"


def _graphql(url: str, query: str, variables: Optional[dict] = None, retries: int = 3):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    headers = {}
    if GRAPH_API_KEY:
        headers["Authorization"] = f"Bearer {GRAPH_API_KEY}"
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise ValueError(f"GraphQL errors: {data['errors']}")
            return data.get("data", {})
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning(f"GraphQL retry {attempt+1}/{retries}: {e}")
            time.sleep(2 ** attempt)


def _paginate(url: str, query_template: str, entity: str, page_size: int = 1000) -> list:
    """Fetch all records using skip-based pagination."""
    all_records = []
    skip = 0
    while True:
        query = query_template.format(first=page_size, skip=skip)
        data = _graphql(url, query)
        records = data.get(entity, [])
        if not records:
            break
        all_records.extend(records)
        if len(records) < page_size:
            break
        skip += page_size
        time.sleep(0.3)
    return all_records


# ─────────────────────────────────────────────
# Maple Finance — public API (no key needed)
# https://api.maple.finance/v2/graphql
# ─────────────────────────────────────────────

MAPLE_API = "https://api.maple.finance/v2/graphql"

MAPLE_POOLS_QUERY = """
{
  poolV2S {
    id name openToPublic apy tvl liquidityCap principalOut numActiveLoans
    totalAssets unrealizedLosses issuanceRate delegateManagementFeeRate
    asset { id symbol decimals }
    delegate { id }
  }
}
"""

MAPLE_LOANS_QUERY = """
{
  loans(first: 1000) {
    id principalOwed interestPaid paymentsRemaining defaultSuffered
    amountFunded nextPaymentDue collateralRequired
    borrower { id }
    collateralAsset { id symbol }
  }
}
"""


def ingest_maple(con) -> tuple[int, int]:
    log.info("Fetching Maple v2 pools via public API...")
    try:
        r = requests.post(MAPLE_API, json={"query": MAPLE_POOLS_QUERY}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise ValueError(f"Maple API errors: {data['errors']}")
        pools = data.get("data", {}).get("poolV2S", [])

        pool_rows = []
        for p in pools:
            asset = p.get("asset") or {}
            decimals = int(asset.get("decimals") or 6)
            scale = 10 ** decimals

            def to_float(raw):
                try:
                    return float(raw) / scale if raw else None
                except Exception:
                    return None

            pool_rows.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "asset_symbol": asset.get("symbol"),
                "asset_decimals": decimals,
                "open_to_public": p.get("openToPublic"),
                "apy": p.get("apy"),
                "tvl": to_float(p.get("tvl")),
                "liquidity_cap": to_float(p.get("liquidityCap")),
                "principal_out": to_float(p.get("principalOut")),
                "num_active_loans": p.get("numActiveLoans"),
                "total_assets": to_float(p.get("totalAssets")),
                "unrealized_losses": to_float(p.get("unrealizedLosses")),
                "delegate_mgmt_fee_rate": p.get("delegateManagementFeeRate"),
                "delegate_id": (p.get("delegate") or {}).get("id"),
                "ingested_at": pd.Timestamp.utcnow().isoformat(),
            })

        log.info(f"Fetching Maple loans...")
        r2 = requests.post(MAPLE_API, json={"query": MAPLE_LOANS_QUERY}, timeout=30)
        r2.raise_for_status()
        data2 = r2.json()
        if "errors" in data2:
            raise ValueError(f"Maple loan errors: {data2['errors']}")
        loans = data2.get("data", {}).get("loans", [])

        loan_rows = []
        for l in loans:
            ca = l.get("collateralAsset") or {}
            loan_rows.append({
                "id": l.get("id"),
                "borrower": (l.get("borrower") or {}).get("id"),
                "collateral_asset": ca.get("symbol"),
                "principal_owed_raw": l.get("principalOwed"),
                "interest_paid_raw": l.get("interestPaid"),
                "payments_remaining": l.get("paymentsRemaining"),
                "default_suffered_raw": l.get("defaultSuffered"),
                "amount_funded_raw": l.get("amountFunded"),
                "next_payment_due": l.get("nextPaymentDue"),
                "collateral_required_raw": l.get("collateralRequired"),
                "ingested_at": pd.Timestamp.utcnow().isoformat(),
            })

        df_pools = pd.DataFrame(pool_rows) if pool_rows else pd.DataFrame(columns=["id","name","ingested_at"])
        df_loans = pd.DataFrame(loan_rows) if loan_rows else pd.DataFrame(columns=["id","borrower","ingested_at"])

        n_pools = append_snapshot(con, "sg_maple_pools", df_pools)
        n_loans = append_snapshot(con, "sg_maple_loans", df_loans)

        log.info(f"sg_maple_pools: {n_pools} rows appended, sg_maple_loans: {n_loans} rows appended")
        return len(df_pools), len(df_loans)

    except Exception as e:
        log.warning(f"Maple ingestion failed: {e} — existing history preserved")
        return 0, 0


# ─────────────────────────────────────────────
# Centrifuge — public GraphQL API (no key needed)
# https://api.centrifuge.io
# ─────────────────────────────────────────────

CENTRIFUGE_API = "https://api.centrifuge.io"

CENTRIFUGE_POOLS_QUERY = """
{
  pools(limit: 100) {
    items {
      id centrifugeId name isActive currency decimals createdAt
      tokens {
        items {
          id name symbol totalIssuance tokenPrice tokenPriceComputedAt
          decimals
        }
      }
    }
    totalCount
  }
}
"""

CENTRIFUGE_SNAPSHOTS_QUERY = """
{
  poolSnapshots(
    limit: 200
    orderBy: "timestamp"
    orderDirection: "DESC"
  ) {
    items {
      id timestamp blockNumber pool { id centrifugeId name }
    }
    totalCount
  }
}
"""


def ingest_centrifuge(con) -> tuple[int, int]:
    log.info("Fetching Centrifuge pools via public API...")
    try:
        r = requests.post(CENTRIFUGE_API, json={"query": CENTRIFUGE_POOLS_QUERY}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise ValueError(f"Centrifuge API errors: {data['errors']}")

        pools = data.get("data", {}).get("pools", {}).get("items", [])
        pool_rows = []
        token_rows = []

        for p in pools:
            pool_rows.append({
                "id": p.get("id"),
                "centrifuge_id": p.get("centrifugeId"),
                "name": p.get("name"),
                "is_active": p.get("isActive"),
                "currency": p.get("currency"),
                "decimals": p.get("decimals"),
                "created_at": p.get("createdAt"),
                "ingested_at": pd.Timestamp.utcnow().isoformat(),
            })
            for t in (p.get("tokens") or {}).get("items", []):
                # tokenPrice is in ray units (1e27), convert to human
                raw_price = t.get("tokenPrice")
                try:
                    price_float = float(raw_price) / 1e18 if raw_price else None
                except Exception:
                    price_float = None
                raw_issuance = t.get("totalIssuance")
                try:
                    dec = int(t.get("decimals") or 18)
                    issuance_float = float(raw_issuance) / (10 ** dec) if raw_issuance else None
                except Exception:
                    issuance_float = None
                token_rows.append({
                    "pool_id": p.get("id"),
                    "pool_name": p.get("name"),
                    "token_id": t.get("id"),
                    "name": t.get("name"),
                    "symbol": t.get("symbol"),
                    "decimals": t.get("decimals"),
                    "total_issuance_raw": raw_issuance,
                    "total_issuance": issuance_float,
                    "token_price_raw": raw_price,
                    "token_price": price_float,
                    "token_price_at": t.get("tokenPriceComputedAt"),
                    "ingested_at": pd.Timestamp.utcnow().isoformat(),
                })

        df_pools = pd.DataFrame(pool_rows) if pool_rows else pd.DataFrame(columns=["id","name","ingested_at"])
        df_tokens = pd.DataFrame(token_rows) if token_rows else pd.DataFrame(columns=["pool_id","symbol","ingested_at"])

        n_pools = append_snapshot(con, "sg_centrifuge_pools", df_pools)
        n_tokens = append_snapshot(con, "sg_centrifuge_assets", df_tokens)

        log.info(f"sg_centrifuge_pools: {n_pools} rows appended, sg_centrifuge_assets: {n_tokens} rows appended")
        return len(df_pools), len(df_tokens)

    except Exception as e:
        log.warning(f"Centrifuge ingestion failed: {e} — existing history preserved")
        return 0, 0


# ─────────────────────────────────────────────
# Goldfinch
# ─────────────────────────────────────────────

GOLDFINCH_POOLS_QUERY = """
{{
  seniorPools(first: {first}, skip: {skip}) {{
    id
    assets
    sharePrice
    totalPoolTokens
    totalLoansOutstanding
    totalWritedowns
    totalSupply
    estimatedTotalInterest
    estimatedApy
    defaultRate
  }}
}}
"""

GOLDFINCH_TRANCHED_POOLS_QUERY = """
{{
  tranchedPools(first: {first}, skip: {skip}) {{
    id
    estimatedSeniorPoolContribution
    juniorFeePercent
    reserveFeePercent
    estimatedLeverageRatio
    isPaused
    backerDeposits
    totalDeposited
    numBackers
    balance
    totalPrincipalPaid
    principalAmountRepaid
    interestAmountRepaid
    totalDebt
    estimatedTotalAssets
    remainingCapacity
    creditLineAddress
  }}
}}
"""


def ingest_goldfinch(con) -> tuple[int, int]:
    url_key = "goldfinch_v2"
    gf_url = _subgraph_url(url_key)

    if not gf_url:
        log.warning("Goldfinch skipped: THE_GRAPH_API_KEY not set in .env")
        _create_empty_tables(con, {
            "sg_goldfinch_pools": ["id","assets","share_price","total_loans_outstanding",
                                   "total_writedowns","estimated_apy","default_rate","ingested_at"],
            "sg_goldfinch_tranched_pools": ["id","total_deposited","num_backers","total_debt",
                                            "balance","total_principal_paid","ingested_at"],
        })
        return 0, 0

    senior_pools = []
    tranched_pools = []

    try:
        log.info("Fetching Goldfinch senior pools...")
        senior_pools = _paginate(gf_url, GOLDFINCH_POOLS_QUERY, "seniorPools")
    except Exception as e:
        log.warning(f"Goldfinch senior pools failed: {e}")

    try:
        log.info("Fetching Goldfinch tranched pools...")
        tranched_pools = _paginate(gf_url, GOLDFINCH_TRANCHED_POOLS_QUERY, "tranchedPools")
    except Exception as e:
        log.warning(f"Goldfinch tranched pools failed: {e}")

    sp_rows = [{
        "id": p.get("id"),
        "assets": p.get("assets"),
        "share_price": p.get("sharePrice"),
        "total_pool_tokens": p.get("totalPoolTokens"),
        "total_loans_outstanding": p.get("totalLoansOutstanding"),
        "total_writedowns": p.get("totalWritedowns"),
        "total_supply": p.get("totalSupply"),
        "estimated_apy": p.get("estimatedApy"),
        "default_rate": p.get("defaultRate"),
        "estimated_total_interest": p.get("estimatedTotalInterest"),
        "ingested_at": pd.Timestamp.utcnow().isoformat(),
    } for p in senior_pools]

    tp_rows = [{
        "id": p.get("id"),
        "total_deposited": p.get("totalDeposited"),
        "num_backers": p.get("numBackers"),
        "balance": p.get("balance"),
        "total_debt": p.get("totalDebt"),
        "total_principal_paid": p.get("totalPrincipalPaid"),
        "principal_amount_repaid": p.get("principalAmountRepaid"),
        "interest_amount_repaid": p.get("interestAmountRepaid"),
        "estimated_total_assets": p.get("estimatedTotalAssets"),
        "remaining_capacity": p.get("remainingCapacity"),
        "is_paused": p.get("isPaused"),
        "backer_deposits": p.get("backerDeposits"),
        "num_backers": p.get("numBackers"),
        "junior_fee_pct": p.get("juniorFeePercent"),
        "reserve_fee_pct": p.get("reserveFeePercent"),
        "ingested_at": pd.Timestamp.utcnow().isoformat(),
    } for p in tranched_pools]

    df_sp = pd.DataFrame(sp_rows) if sp_rows else pd.DataFrame(columns=["id","ingested_at"])
    df_tp = pd.DataFrame(tp_rows) if tp_rows else pd.DataFrame(columns=["id","ingested_at"])

    n_sp = append_snapshot(con, "sg_goldfinch_pools", df_sp)
    n_tp = append_snapshot(con, "sg_goldfinch_tranched_pools", df_tp)

    log.info(f"sg_goldfinch_pools: {n_sp} rows appended, sg_goldfinch_tranched_pools: {n_tp} rows appended")
    return len(df_sp), len(df_tp)


def run():
    if not GRAPH_API_KEY:
        log.warning(
            "THE_GRAPH_API_KEY not set. Subgraph ingestion will create empty tables.\n"
            "Get a free key at https://thegraph.com/studio/apikeys/ (100K queries/month free)\n"
            "Then add to .env: THE_GRAPH_API_KEY=your_key_here"
        )

    con = get_connection()
    results = {}

    try:
        n_pools, n_loans = ingest_maple(con)
        results["maple_pools"] = n_pools
        results["maple_loans"] = n_loans
    except Exception as e:
        log.error(f"Maple ingestion failed: {e}")

    try:
        n_pools, n_assets = ingest_centrifuge(con)
        results["centrifuge_pools"] = n_pools
        results["centrifuge_assets"] = n_assets
    except Exception as e:
        log.error(f"Centrifuge ingestion failed: {e}")

    try:
        n_sp, n_tp = ingest_goldfinch(con)
        results["goldfinch_senior_pools"] = n_sp
        results["goldfinch_tranched_pools"] = n_tp
    except Exception as e:
        log.error(f"Goldfinch ingestion failed: {e}")

    con.close()
    log.info(f"Subgraph ingestion complete: {results}")
    return results


if __name__ == "__main__":
    run()
