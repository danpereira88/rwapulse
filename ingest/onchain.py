"""
On-chain ingestion via public Ethereum (and Polygon) RPC.

Uses public endpoints (Cloudflare, Ankr) — no API key required.
Falls back to ETHEREUM_RPC_URL from .env if set.

Tables created:
  onchain_token_supply   — current totalSupply + decimals per contract (snapshot)
  onchain_transfer_flows — aggregated daily net flows from recent Transfer events
  onchain_transfer_events — raw Transfer events (last ~2000 blocks per token)
"""

import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
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

# Public RPC endpoints — tried in order
ETH_RPCS = [
    os.getenv("ETHEREUM_RPC_URL", ""),
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum.publicnode.com",
]
ETH_RPCS = [r for r in ETH_RPCS if r]  # remove empty

POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]

# Verified contract addresses (confirmed against Etherscan, May 2026)
TOKENS = [
    {
        "symbol": "BUIDL",
        "name": "BlackRock USD Institutional Digital Liquidity Fund",
        "chain": "ethereum",
        "address": "0x7712c34205737192402172409a8F7ccef8aA2AEc",
        "asset_class": "tokenized_treasury",
        "issuer": "BlackRock / Securitize",
    },
    {
        "symbol": "USDY",
        "name": "Ondo US Dollar Yield",
        "chain": "ethereum",
        "address": "0x96F6eF951840721AdBF46Ac996b59E0235CB985C",
        "asset_class": "tokenized_treasury",
        "issuer": "Ondo Finance",
    },
    {
        "symbol": "OUSG",
        "name": "Ondo Short-Term US Government Bond Fund",
        "chain": "ethereum",
        "address": "0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92",
        "asset_class": "tokenized_treasury",
        "issuer": "Ondo Finance",
    },
    {
        "symbol": "USYC",
        "name": "US Yield Coin (Hashnote)",
        "chain": "ethereum",
        "address": "0x136471a34f6ef19fE571EFFC1CA711fdb8E49f2b",
        "asset_class": "tokenized_treasury",
        "issuer": "Hashnote",
    },
    {
        "symbol": "USTB",
        "name": "Superstate Short Duration US Government Securities Fund",
        "chain": "ethereum",
        "address": "0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e",
        "asset_class": "tokenized_treasury",
        "issuer": "Superstate",
    },
    {
        "symbol": "BENJI",
        "name": "Franklin OnChain US Government Money Fund (Ethereum)",
        "chain": "ethereum",
        "address": "0x3DDc84940Ab509C11B20B76B466933f40b750dc9",
        "asset_class": "tokenized_treasury",
        "issuer": "Franklin Templeton",
    },
    {
        "symbol": "BENJI_POLYGON",
        "name": "Franklin OnChain US Government Money Fund (Polygon)",
        "chain": "polygon",
        "address": "0x408a634b8a8f0de729b48574a3a7ec3fe820b00a",
        "asset_class": "tokenized_treasury",
        "issuer": "Franklin Templeton",
    },
    {
        "symbol": "PAXG",
        "name": "PAX Gold",
        "chain": "ethereum",
        "address": "0x45804880De22913dAFE09f4980848ECE6EcbAf78",
        "asset_class": "tokenized_gold",
        "issuer": "Paxos",
    },
    {
        "symbol": "XAUT",
        "name": "Tether Gold",
        "chain": "ethereum",
        "address": "0x68749665FF8D2d112Fa859AA293F07A622782F38",
        "asset_class": "tokenized_gold",
        "issuer": "Tether",
    },
]

# ERC-20 function selectors
TOTAL_SUPPLY_SIG = "0x18160ddd"
DECIMALS_SIG = "0x313ce567"
SYMBOL_SIG = "0x95d89b41"

# Transfer event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class RPCClient:
    """Minimal JSON-RPC client with failover across multiple endpoints."""

    def __init__(self, rpcs: list[str]):
        self.rpcs = rpcs
        self._current = 0

    def call(self, method: str, params: list, retries: int = 3):
        for attempt in range(retries * len(self.rpcs)):
            rpc = self.rpcs[self._current % len(self.rpcs)]
            try:
                resp = requests.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": method, "params": params,
                }, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise ValueError(data["error"])
                return data.get("result")
            except Exception as e:
                log.debug(f"RPC call failed on {rpc}: {e}")
                self._current += 1
                time.sleep(0.5)
        raise RuntimeError(f"All RPC endpoints failed for {method}")

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def get_logs(self, address: str, topics: list, from_block: str, to_block: str = "latest"):
        return self.call("eth_getLogs", [{
            "address": address,
            "topics": topics,
            "fromBlock": from_block,
            "toBlock": to_block,
        }])

    def get_block_number(self) -> int:
        result = self.call("eth_blockNumber", [])
        return int(result, 16)


def _decode_uint256(hex_val: Optional[str]) -> Optional[int]:
    if not hex_val or hex_val == "0x":
        return None
    return int(hex_val, 16)


def ingest_token_supply(con) -> int:
    eth_client = RPCClient(ETH_RPCS)
    poly_client = RPCClient(POLYGON_RPCS)

    rows = []
    for token in TOKENS:
        client = poly_client if token["chain"] == "polygon" else eth_client
        address = token["address"]
        try:
            raw_supply = client.eth_call(address, TOTAL_SUPPLY_SIG)
            raw_decimals = client.eth_call(address, DECIMALS_SIG)

            supply_raw = _decode_uint256(raw_supply)
            decimals = _decode_uint256(raw_decimals) or 18

            supply_adjusted = (supply_raw / (10 ** decimals)) if supply_raw is not None else None

            rows.append({
                "symbol": token["symbol"],
                "name": token["name"],
                "chain": token["chain"],
                "address": address.lower(),
                "asset_class": token["asset_class"],
                "issuer": token["issuer"],
                "total_supply_raw": supply_raw,
                "decimals": decimals,
                "total_supply_adjusted": supply_adjusted,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
            })
            log.info(f"  {token['symbol']} ({token['chain']}): supply={supply_adjusted:,.2f}" if supply_adjusted else f"  {token['symbol']}: no supply")
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"Supply read failed for {token['symbol']}: {e}")
            rows.append({
                "symbol": token["symbol"],
                "name": token["name"],
                "chain": token["chain"],
                "address": address.lower(),
                "asset_class": token["asset_class"],
                "issuer": token["issuer"],
                "total_supply_raw": None,
                "decimals": None,
                "total_supply_adjusted": None,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
            })

    df = pd.DataFrame(rows)
    n = append_snapshot(con, "onchain_token_supply", df)
    log.info(f"onchain_token_supply: {n} rows appended")
    return len(df)


def ingest_transfer_events(con) -> tuple[int, int]:
    """
    Pull Transfer events for the last ~30 days worth of blocks (~200k blocks on Ethereum).
    Public RPCs typically limit eth_getLogs to ~2000 blocks per request, so we chunk.
    """
    eth_client = RPCClient(ETH_RPCS)
    poly_client = RPCClient(POLYGON_RPCS)

    BLOCKS_PER_DAY_ETH = 7200     # ~12s block time
    BLOCKS_PER_DAY_POLY = 43200   # ~2s block time
    LOOKBACK_DAYS = 30
    CHUNK_SIZE = 1999  # stay under the 2000 block limit common on public nodes

    all_events = []

    for token in TOKENS:
        client = poly_client if token["chain"] == "polygon" else eth_client
        blocks_per_day = BLOCKS_PER_DAY_POLY if token["chain"] == "polygon" else BLOCKS_PER_DAY_ETH
        address = token["address"]

        try:
            current_block = client.get_block_number()
            from_block = current_block - (LOOKBACK_DAYS * blocks_per_day)

            log.info(f"  Fetching Transfer events for {token['symbol']} ({token['chain']}) blocks {from_block}–{current_block}")

            chunk_start = from_block
            token_events = 0
            while chunk_start <= current_block:
                chunk_end = min(chunk_start + CHUNK_SIZE, current_block)
                try:
                    logs = client.get_logs(
                        address=address,
                        topics=[TRANSFER_TOPIC],
                        from_block=hex(chunk_start),
                        to_block=hex(chunk_end),
                    )
                    if logs:
                        for log_entry in logs:
                            tx_hash = log_entry.get("transactionHash", "")
                            block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                            # Topics: [Transfer sig, from_addr (padded), to_addr (padded)]
                            topics = log_entry.get("topics", [])
                            from_addr = ("0x" + topics[1][-40:]) if len(topics) > 1 else None
                            to_addr = ("0x" + topics[2][-40:]) if len(topics) > 2 else None
                            # Data field is the amount
                            data = log_entry.get("data", "0x")
                            amount_raw = _decode_uint256(data) if data and data != "0x" else 0
                            all_events.append({
                                "symbol": token["symbol"],
                                "chain": token["chain"],
                                "address": address.lower(),
                                "block_number": block_num,
                                "tx_hash": tx_hash,
                                "from_address": from_addr,
                                "to_address": to_addr,
                                "amount_raw": amount_raw,
                            })
                            token_events += 1
                except Exception as e:
                    log.warning(f"getLogs chunk {chunk_start}-{chunk_end} failed for {token['symbol']}: {e}")
                chunk_start = chunk_end + 1
                time.sleep(0.3)

            log.info(f"  {token['symbol']}: {token_events} transfer events")
        except Exception as e:
            log.warning(f"Transfer events failed for {token['symbol']}: {e}")

    df_events = pd.DataFrame(all_events) if all_events else pd.DataFrame(
        columns=["symbol","chain","address","block_number","tx_hash",
                 "from_address","to_address","amount_raw"]
    )
    con.execute("DROP TABLE IF EXISTS onchain_transfer_events")
    con.execute("CREATE TABLE onchain_transfer_events AS SELECT * FROM df_events")

    # Aggregate supply/demand flows: mints (from=0x0), burns (to=0x0), transfers
    if not df_events.empty:
        # Join decimals
        decimals_map = {}
        try:
            dec_df = con.execute("SELECT symbol, decimals FROM onchain_token_supply").fetchdf()
            decimals_map = dict(zip(dec_df["symbol"], dec_df["decimals"]))
        except Exception:
            pass

        flow_rows = []
        NULL_ADDR = "0x" + "0" * 40
        for symbol, group in df_events.groupby("symbol"):
            dec = decimals_map.get(symbol, 18) or 18
            mints = group[group["from_address"] == NULL_ADDR]["amount_raw"].sum() / (10 ** dec)
            burns = group[group["to_address"] == NULL_ADDR]["amount_raw"].sum() / (10 ** dec)
            transfers = len(group)
            unique_senders = group["from_address"].nunique()
            unique_receivers = group["to_address"].nunique()
            flow_rows.append({
                "symbol": symbol,
                "chain": group["chain"].iloc[0],
                "lookback_days": LOOKBACK_DAYS,
                "total_minted": mints,
                "total_burned": burns,
                "net_flow": mints - burns,
                "transfer_count": transfers,
                "unique_senders": unique_senders,
                "unique_receivers": unique_receivers,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
            })
        df_flows = pd.DataFrame(flow_rows)
    else:
        df_flows = pd.DataFrame(
            columns=["symbol","chain","lookback_days","total_minted","total_burned",
                     "net_flow","transfer_count","unique_senders","unique_receivers","ingested_at"]
        )

    append_snapshot(con, "onchain_transfer_flows", df_flows)

    log.info(f"onchain_transfer_events: {len(df_events)} rows, onchain_transfer_flows: {len(df_flows)} rows")
    return len(df_events), len(df_flows)


def run():
    con = get_connection()
    results = {}

    log.info("Reading token supply...")
    try:
        results["token_supply"] = ingest_token_supply(con)
    except Exception as e:
        log.error(f"token_supply failed: {e}")

    log.info("Reading Transfer events (30-day lookback)...")
    try:
        n_events, n_flows = ingest_transfer_events(con)
        results["transfer_events"] = n_events
        results["transfer_flows"] = n_flows
    except Exception as e:
        log.error(f"transfer_events failed: {e}")

    con.close()
    log.info(f"On-chain ingestion complete: {results}")
    return results


if __name__ == "__main__":
    run()
