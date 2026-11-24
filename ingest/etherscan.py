"""
Etherscan ingestion (free tier API key required).

Get a free key at https://etherscan.io/apis (no credit card, instant)
Then set ETHERSCAN_API_KEY=your_key in .env

Note: holder count and tokeninfo require Etherscan Pro. Free tier gives
      contract verification status and recent transactions.

Tables created:
  es_token_info        — token metadata from on-chain + contract source (free tier)
  es_recent_txns       — recent ERC-20 transfer transactions per contract
  es_contract_info     — contract verification status, name, compiler, proxy detection
"""

import time
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
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

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet

ETHEREUM_TOKENS = [
    {"symbol": "BUIDL",  "address": "0x7712c34205737192402172409a8F7ccef8aA2AEc"},
    {"symbol": "USDY",   "address": "0x96F6eF951840721AdBF46Ac996b59E0235CB985C"},
    {"symbol": "OUSG",   "address": "0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92"},
    {"symbol": "USYC",   "address": "0x136471a34f6ef19fE571EFFC1CA711fdb8E49f2b"},
    {"symbol": "USTB",   "address": "0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e"},
    {"symbol": "BENJI",  "address": "0x3DDc84940Ab509C11B20B76B466933f40b750dc9"},
    {"symbol": "PAXG",   "address": "0x45804880De22913dAFE09f4980848ECE6EcbAf78"},
    {"symbol": "XAUT",   "address": "0x68749665FF8D2d112Fa859AA293F07A622782F38"},
]

_EMPTY_COLS = {
    "es_token_info":    ["symbol","address","contract_name","compiler_version","is_proxy",
                         "implementation","license_type","ingested_at"],
    "es_recent_txns":   ["symbol","address","block_number","tx_hash","from_address",
                         "to_address","value_raw","token_decimal","timestamp","date",
                         "gas_used","ingested_at"],
    "es_contract_info": ["symbol","address","is_verified","contract_name","compiler_version",
                         "license_type","proxy","implementation","ingested_at"],
}


def _api(params: dict, retries: int = 3):
    params["apikey"] = ETHERSCAN_API_KEY
    params["chainid"] = CHAIN_ID
    for attempt in range(retries):
        try:
            r = requests.get(ETHERSCAN_BASE, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            # Treat rate-limit as retryable
            result_str = str(data.get("result", ""))
            if data.get("status") == "0" and "rate limit" in result_str.lower():
                log.warning("Etherscan rate limit — sleeping 5s")
                time.sleep(5)
                continue
            return data
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _empty(con, table: str):
    """Ensure table exists with correct schema without wiping existing history."""
    df = pd.DataFrame(columns=_EMPTY_COLS[table])
    append_snapshot(con, table, df)


def ingest_token_info(con) -> int:
    """
    Uses getsourcecode to get contract name, compiler, proxy detection, and license.
    tokeninfo (holder count, description, website) requires Etherscan Pro — skipped.
    """
    if not ETHERSCAN_API_KEY:
        log.warning("Etherscan skipped: ETHERSCAN_API_KEY not set in .env")
        _empty(con, "es_token_info")
        return 0

    rows = []
    for token in ETHEREUM_TOKENS:
        address = token["address"]
        try:
            resp = _api({
                "module": "contract",
                "action": "getsourcecode",
                "address": address,
            })
            result = resp.get("result", [])
            info = result[0] if isinstance(result, list) and result else {}

            abi = info.get("ABI", "")
            is_verified = bool(abi and abi != "Contract source code not verified")

            rows.append({
                "symbol": token["symbol"],
                "address": address.lower(),
                "contract_name": info.get("ContractName"),
                "compiler_version": info.get("CompilerVersion"),
                "optimization_used": info.get("OptimizationUsed"),
                "evm_version": info.get("EVMVersion"),
                "license_type": info.get("LicenseType"),
                "is_verified": is_verified,
                "is_proxy": info.get("Proxy") == "1",
                "implementation": info.get("Implementation") or None,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
            })
            log.info(f"  {token['symbol']}: verified={is_verified}, name={info.get('ContractName')}")
            time.sleep(0.25)
        except Exception as e:
            log.warning(f"Token info failed for {token['symbol']}: {e}")

    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=_EMPTY_COLS["es_token_info"])
    n = append_snapshot(con, "es_token_info", df)
    log.info(f"es_token_info: {n} rows appended")
    return len(df)


def ingest_recent_txns(con) -> int:
    if not ETHERSCAN_API_KEY:
        log.warning("Etherscan txns skipped: no API key")
        _empty(con, "es_recent_txns")
        return 0

    all_rows = []
    for token in ETHEREUM_TOKENS:
        address = token["address"]
        try:
            resp = _api({
                "module": "account",
                "action": "tokentx",
                "contractaddress": address,
                "page": 1,
                "offset": 200,
                "sort": "desc",
            })
            txns = resp.get("result", [])
            if not isinstance(txns, list):
                log.warning(f"  {token['symbol']}: unexpected response: {str(txns)[:100]}")
                continue
            for tx in txns:
                ts = int(tx.get("timeStamp", 0))
                all_rows.append({
                    "symbol": token["symbol"],
                    "address": address.lower(),
                    "block_number": int(tx.get("blockNumber", 0)),
                    "tx_hash": tx.get("hash"),
                    "from_address": tx.get("from"),
                    "to_address": tx.get("to"),
                    "value_raw": tx.get("value"),
                    "token_decimal": tx.get("tokenDecimal"),
                    "timestamp": ts,
                    "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None,
                    "gas_used": tx.get("gasUsed"),
                    "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
                })
            log.info(f"  {token['symbol']}: {len(txns)} recent txns")
            time.sleep(0.25)
        except Exception as e:
            log.warning(f"Txns failed for {token['symbol']}: {e}")

    df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame(columns=_EMPTY_COLS["es_recent_txns"])
    con.execute("DROP TABLE IF EXISTS es_recent_txns")
    con.execute("CREATE TABLE es_recent_txns AS SELECT * FROM df")
    log.info(f"es_recent_txns: {len(df)} rows")
    return len(df)


def ingest_contract_info(con) -> int:
    """Alias that calls ingest_token_info data into es_contract_info too for backwards compat."""
    if not ETHERSCAN_API_KEY:
        log.warning("Etherscan contract info skipped: no API key")
        _empty(con, "es_contract_info")
        return 0

    rows = []
    for token in ETHEREUM_TOKENS:
        address = token["address"]
        try:
            resp = _api({
                "module": "contract",
                "action": "getsourcecode",
                "address": address,
            })
            result = resp.get("result", [])
            info = result[0] if isinstance(result, list) and result else {}
            abi = info.get("ABI", "")
            is_verified = bool(abi and abi != "Contract source code not verified")

            rows.append({
                "symbol": token["symbol"],
                "address": address.lower(),
                "is_verified": is_verified,
                "contract_name": info.get("ContractName"),
                "compiler_version": info.get("CompilerVersion"),
                "optimization_used": info.get("OptimizationUsed"),
                "evm_version": info.get("EVMVersion"),
                "license_type": info.get("LicenseType"),
                "proxy": info.get("Proxy") == "1",
                "implementation": info.get("Implementation") or None,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
            })
            log.info(f"  {token['symbol']}: verified={is_verified}")
            time.sleep(0.25)
        except Exception as e:
            log.warning(f"Contract info failed for {token['symbol']}: {e}")

    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=_EMPTY_COLS["es_contract_info"])
    n = append_snapshot(con, "es_contract_info", df)
    log.info(f"es_contract_info: {n} rows appended")
    return len(df)


def run():
    if not ETHERSCAN_API_KEY:
        log.warning(
            "ETHERSCAN_API_KEY not set — all Etherscan tables will be empty.\n"
            "Get a free key at https://etherscan.io/apis then add to .env"
        )

    con = get_connection()
    results = {}

    try:
        results["token_info"] = ingest_token_info(con)
    except Exception as e:
        log.error(f"token_info failed: {e}")

    try:
        results["recent_txns"] = ingest_recent_txns(con)
    except Exception as e:
        log.error(f"recent_txns failed: {e}")

    try:
        results["contract_info"] = ingest_contract_info(con)
    except Exception as e:
        log.error(f"contract_info failed: {e}")

    con.close()
    log.info(f"Etherscan ingestion complete: {results}")
    return results


if __name__ == "__main__":
    run()
