"""
Exploratory SQL queries against the local DuckDB database.

Run this interactively:
    python explore/queries.py

Or import specific queries in a notebook / REPL:
    from explore.queries import run_query
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection


def run_query(sql: str, label: str = "") -> None:
    con = get_connection()
    if label:
        print(f"\n{'─'*60}")
        print(f"  {label}")
        print(f"{'─'*60}")
    result = con.execute(sql).fetchdf()
    print(result.to_string(index=False))
    con.close()
    return result


QUERIES = {
    "top_rwa_protocols_by_tvl": """
        SELECT name, category, ROUND(tvl_usd / 1e6, 2) AS tvl_m_usd,
               change_1d_pct, change_7d_pct, change_1m_pct, chains
        FROM dl_protocols
        WHERE tvl_usd IS NOT NULL
        ORDER BY tvl_usd DESC
        LIMIT 25
    """,

    "tvl_by_category": """
        SELECT category, COUNT(*) AS num_protocols,
               ROUND(SUM(tvl_usd) / 1e6, 2) AS total_tvl_m_usd,
               ROUND(AVG(tvl_usd) / 1e6, 2) AS avg_tvl_m_usd
        FROM dl_protocols
        WHERE tvl_usd IS NOT NULL AND tvl_usd > 0
        GROUP BY category
        ORDER BY total_tvl_m_usd DESC
    """,

    "treasury_tvl_trend_top5": """
        WITH top5 AS (
            SELECT slug FROM dl_protocols
            WHERE category IN ('RWA', 'Treasury', 'Treasury Bills', 'Real World Assets')
            ORDER BY tvl_usd DESC NULLS LAST LIMIT 5
        )
        SELECT t.slug, t.date,
               ROUND(t.tvl_usd / 1e6, 3) AS tvl_m_usd
        FROM dl_protocol_tvl t
        JOIN top5 ON t.slug = top5.slug
        WHERE t.date >= (CURRENT_DATE - INTERVAL 90 DAY)::VARCHAR
        ORDER BY t.slug, t.date
    """,

    "chain_tvl_distribution": """
        SELECT chain, ROUND(tvl_usd / 1e6, 2) AS tvl_m_usd,
               change_1d_pct, change_7d_pct
        FROM dl_chains
        WHERE tvl_usd IS NOT NULL AND tvl_usd > 0
        ORDER BY tvl_usd DESC
        LIMIT 20
    """,

    "yield_pools_rwa": """
        SELECT project, symbol, chain, tvl_usd, apy, apy_base, apy_reward, apy_mean_30d
        FROM dl_yield_pools
        WHERE tvl_usd IS NOT NULL AND tvl_usd > 0
        ORDER BY tvl_usd DESC
        LIMIT 30
    """,

    "onchain_token_supply": """
        SELECT symbol, chain, ROUND(total_supply_adjusted, 2) AS supply,
               decimals, issuer, asset_class, address
        FROM onchain_token_supply
        ORDER BY total_supply_adjusted DESC NULLS LAST
    """,

    "transfer_flows_30d": """
        SELECT symbol, chain,
               ROUND(total_minted, 2) AS minted_30d,
               ROUND(total_burned, 2) AS burned_30d,
               ROUND(net_flow, 2) AS net_flow_30d,
               transfer_count,
               unique_senders, unique_receivers
        FROM onchain_transfer_flows
        ORDER BY ABS(net_flow) DESC
    """,

    "coingecko_rwa_market_caps": """
        SELECT display_name, symbol, asset_class,
               ROUND(market_cap_usd / 1e6, 2) AS market_cap_m_usd,
               ROUND(current_price_usd, 4) AS price_usd,
               ROUND(total_volume_24h_usd / 1e6, 2) AS volume_24h_m_usd,
               price_change_pct_24h, price_change_pct_30d,
               circulating_supply
        FROM cg_rwa_tokens
        WHERE market_cap_usd IS NOT NULL
        ORDER BY market_cap_usd DESC
    """,

    "coingecko_category_breakdown": """
        SELECT category_id, COUNT(*) AS num_tokens,
               ROUND(SUM(market_cap_usd) / 1e6, 2) AS total_market_cap_m_usd
        FROM cg_rwa_category
        GROUP BY category_id
        ORDER BY total_market_cap_m_usd DESC
    """,

    "stablecoin_rwa_history_recent": """
        SELECT name, symbol, date,
               ROUND(total_circulating_usd / 1e6, 2) AS circulating_m_usd
        FROM dl_stablecoin_history
        WHERE date >= (CURRENT_DATE - INTERVAL 90 DAY)::VARCHAR
        ORDER BY name, date DESC
        LIMIT 100
    """,

    "tables_in_db": """
        SELECT table_name, estimated_size AS rows
        FROM duckdb_tables()
        ORDER BY table_name
    """,
}


def main():
    queries_to_run = [
        ("Tables in database", QUERIES["tables_in_db"]),
        ("Top RWA protocols by TVL", QUERIES["top_rwa_protocols_by_tvl"]),
        ("TVL by category", QUERIES["tvl_by_category"]),
        ("On-chain token supply", QUERIES["onchain_token_supply"]),
        ("30-day transfer flows", QUERIES["transfer_flows_30d"]),
        ("CoinGecko market caps", QUERIES["coingecko_rwa_market_caps"]),
        ("Yield pools (RWA-adjacent)", QUERIES["yield_pools_rwa"]),
    ]
    for label, sql in queries_to_run:
        try:
            run_query(sql, label)
        except Exception as e:
            print(f"  Query failed ({label}): {e}")


if __name__ == "__main__":
    main()
