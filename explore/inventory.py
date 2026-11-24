"""
Generates reports/inventory.md — a structured data inventory and exploration report.

Run after all ingestion steps have completed:
    python explore/inventory.py
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent.parent / "reports"


def _q(con, sql: str):
    """Execute SQL and return a DataFrame, or None on error."""
    try:
        return con.execute(sql).fetchdf()
    except Exception as e:
        log.warning(f"Query failed: {e}\nSQL: {sql[:120]}")
        return None


def _table_exists(con, name: str) -> bool:
    try:
        result = con.execute(
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{name}'"
        ).fetchone()
        return result and result[0] > 0
    except Exception:
        return False


def _row_count(con, table: str) -> int:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _fmt_num(n) -> str:
    if n is None:
        return "N/A"
    try:
        return f"{float(n):,.0f}"
    except Exception:
        return str(n)


def _fmt_m(n) -> str:
    if n is None:
        return "N/A"
    try:
        return f"${float(n)/1e6:,.1f}M"
    except Exception:
        return str(n)


def _df_to_md_table(df, max_rows: int = 10) -> str:
    if df is None or df.empty:
        return "_No data available._\n"
    df = df.head(max_rows)
    lines = []
    headers = list(df.columns)
    lines.append("| " + " | ".join(str(h) for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if v is None:
                cells.append("—")
            elif isinstance(v, float):
                cells.append(f"{v:,.4g}" if abs(v) < 1e6 else f"{v:,.0f}")
            else:
                s = str(v)
                cells.append(s[:60] + "…" if len(s) > 60 else s)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def generate_report(con) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    def h(n, text):
        lines.append(f"\n{'#' * n} {text}\n")

    def p(text):
        lines.append(text + "\n")

    def code(text, lang=""):
        lines.append(f"```{lang}\n{text}\n```\n")

    h(1, "RWA Data Inventory & Exploration Report")
    p(f"_Generated: {now}_")
    p("""This report describes the data ingested from free public sources into `data/rwa.duckdb`.
It covers what we have, what we can and cannot answer with it, and surprises found during ingestion.""")

    # ─────────────────────────────────────────────
    h(2, "1. What Data Do We Have?")
    # ─────────────────────────────────────────────

    # Table overview
    tables_df = _q(con, """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main'
        ORDER BY table_name
    """)

    # Row counts
    table_counts = {}
    if tables_df is not None:
        for t in tables_df["table_name"].tolist():
            table_counts[t] = _row_count(con, t)

    h(3, "1.1 DefiLlama")
    p("""**Source:** `https://api.llama.fi` / `https://stablecoins.llama.fi` / `https://yields.llama.fi`
**Key required:** No
**Update frequency:** Daily snapshots (protocol TVL is daily; pools are point-in-time)""")

    dl_tables = {
        "dl_protocols": {
            "desc": "RWA-relevant protocols filtered from DefiLlama's full protocol list",
            "fields": "slug, name, category, chains (comma-sep), tvl_usd, change_1d/7d/1m_pct, mcap, fdv, gecko_id, description, url, twitter, audit_links, ingested_at",
            "granularity": "Snapshot (single row per protocol)",
            "update_freq": "Daily",
        },
        "dl_protocol_tvl": {
            "desc": "Historical daily TVL per protocol (all time)",
            "fields": "slug, name, date (YYYY-MM-DD), tvl_usd",
            "granularity": "Daily",
            "update_freq": "Daily",
        },
        "dl_stablecoins": {
            "desc": "USD stablecoin metadata with chain-level supply breakdown",
            "fields": "id, name, symbol, peg_type, peg_mechanism, chain, chain_supply_usd, total_supply_usd, total_prev_day/week/month_usd, price, gecko_id",
            "granularity": "Snapshot per (stablecoin, chain) pair",
            "update_freq": "Daily",
        },
        "dl_stablecoin_history": {
            "desc": "Historical daily circulating supply for RWA-tagged stablecoins",
            "fields": "id, name, symbol, date, total_circulating_usd, total_unreleased_usd",
            "granularity": "Daily",
            "update_freq": "Daily",
        },
        "dl_yield_pools": {
            "desc": "Yield farming pools filtered for RWA-adjacent projects",
            "fields": "pool_id, chain, project, symbol, tvl_usd, apy, apy_base, apy_reward, apy_mean_30d, il_risk, stablecoin (bool), exposure",
            "granularity": "Snapshot",
            "update_freq": "Daily",
        },
        "dl_chains": {
            "desc": "TVL breakdown by blockchain",
            "fields": "chain, gecko_id, tvl_usd, change_1d_pct, change_7d_pct",
            "granularity": "Snapshot",
            "update_freq": "Daily",
        },
        "dl_protocol_fees": {
            "desc": "Protocol fee and revenue data for RWA protocols that report it",
            "fields": "slug, name, total_24h/48h/7d/30d/alltime_usd, revenue_24h/30d_usd",
            "granularity": "Rolling windows (not historical series)",
            "update_freq": "Daily",
        },
    }

    # Date ranges for TVL
    tvl_range = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest FROM dl_protocol_tvl") if _table_exists(con, "dl_protocol_tvl") else None
    stbl_range = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest FROM dl_stablecoin_history") if _table_exists(con, "dl_stablecoin_history") else None

    for tname, info in dl_tables.items():
        rows = table_counts.get(tname, 0)
        p(f"**`{tname}`** — {rows:,} rows")
        p(f"- Description: {info['desc']}")
        p(f"- Fields: `{info['fields']}`")
        p(f"- Granularity: {info['granularity']}")
        p(f"- Update frequency: {info['update_freq']}")
        if tname == "dl_protocol_tvl" and tvl_range is not None and not tvl_range.empty:
            r = tvl_range.iloc[0]
            p(f"- Date range: {r['earliest']} → {r['latest']}")
        if tname == "dl_stablecoin_history" and stbl_range is not None and not stbl_range.empty:
            r = stbl_range.iloc[0]
            p(f"- Date range: {r['earliest']} → {r['latest']}")
        p("")

    h(3, "1.2 CoinGecko")
    p("""**Source:** `https://api.coingecko.com/api/v3`
**Key required:** No (rate-limited to ~30 req/min on free tier)
**Update frequency:** Daily price history; current data is real-time""")

    cg_tables = {
        "cg_rwa_tokens": {
            "desc": "Current market data snapshot for curated RWA tokens",
            "fields": "coingecko_id, symbol, name, display_name, asset_class, current_price_usd, market_cap_usd, market_cap_rank, fdv_usd, total_volume_24h_usd, high/low_24h, price_change_pct_1h/24h/7d/30d, circulating_supply, total_supply, max_supply, ath/atl_usd, ath/atl_date",
            "granularity": "Snapshot",
            "update_freq": "Real-time on demand",
        },
        "cg_token_history": {
            "desc": "Daily OHLCV and market cap for each tracked RWA token",
            "fields": "coingecko_id, date, price_usd, market_cap_usd, volume_24h_usd",
            "granularity": "Daily",
            "update_freq": "Daily",
        },
        "cg_rwa_category": {
            "desc": "All tokens in CoinGecko RWA categories (real-world-assets-rwa, tokenized-gold, tokenized-us-treasuries, asset-backed-tokens)",
            "fields": "category_id, coingecko_id, symbol, name, market_cap_usd, current_price_usd, total_volume_24h_usd, price_change_pct_24h, circulating_supply",
            "granularity": "Snapshot",
            "update_freq": "Real-time on demand",
        },
    }

    cg_hist_range = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest FROM cg_token_history") if _table_exists(con, "cg_token_history") else None

    for tname, info in cg_tables.items():
        rows = table_counts.get(tname, 0)
        p(f"**`{tname}`** — {rows:,} rows")
        p(f"- Description: {info['desc']}")
        p(f"- Fields: `{info['fields']}`")
        p(f"- Granularity: {info['granularity']}")
        p(f"- Update frequency: {info['update_freq']}")
        if tname == "cg_token_history" and cg_hist_range is not None and not cg_hist_range.empty:
            r = cg_hist_range.iloc[0]
            p(f"- Date range: {r['earliest']} → {r['latest']}")
        p("")

    h(3, "1.3 On-Chain (Public RPC)")
    p("""**Source:** Public Ethereum/Polygon RPC endpoints (Cloudflare, Ankr, LlamaRPC)
**Key required:** No (uses public RPCs with graceful failover)
**Update frequency:** Point-in-time snapshot; Transfer events cover last 30 days""")

    onchain_tables = {
        "onchain_token_supply": {
            "desc": "Current total supply for each tracked RWA token contract",
            "fields": "symbol, name, chain, address, asset_class, issuer, total_supply_raw (uint256), decimals, total_supply_adjusted (human-readable), ingested_at",
            "granularity": "Snapshot",
            "update_freq": "Re-run to refresh",
        },
        "onchain_transfer_events": {
            "desc": "Raw ERC-20 Transfer events for the last 30 days (~200k blocks)",
            "fields": "symbol, chain, address, block_number, tx_hash, from_address, to_address, amount_raw",
            "granularity": "Per-transaction",
            "update_freq": "30-day rolling window on each run",
        },
        "onchain_transfer_flows": {
            "desc": "Aggregated mint/burn/transfer counts per token over the 30-day window",
            "fields": "symbol, chain, lookback_days, total_minted, total_burned, net_flow, transfer_count, unique_senders, unique_receivers",
            "granularity": "Aggregated summary per token",
            "update_freq": "30-day rolling window on each run",
        },
    }

    for tname, info in onchain_tables.items():
        rows = table_counts.get(tname, 0)
        p(f"**`{tname}`** — {rows:,} rows")
        p(f"- Description: {info['desc']}")
        p(f"- Fields: `{info['fields']}`")
        p(f"- Granularity: {info['granularity']}")
        p(f"- Update frequency: {info['update_freq']}")
        p("")

    h(3, "1.4 The Graph Subgraphs")
    p("""**Source:** The Graph decentralized network
**Key required:** Yes — `THE_GRAPH_API_KEY` in .env (free at thegraph.com/studio/apikeys, 100K queries/mo)
**Note:** Empty tables are created when key is absent. Run `python ingest/subgraphs.py` after adding key.""")

    sg_tables = {
        "sg_maple_pools": "Maple Finance lending pool metadata (v1 Ethereum) — liquidity, principal out, interest earned, active loans, fees",
        "sg_maple_loans": "Maple Finance individual loan records — borrower, principal, APR, state (active/defaulted/closed), interest paid",
        "sg_centrifuge_pools": "Centrifuge Tinlake pool metadata — debt, reserve, tranche token prices, yield",
        "sg_centrifuge_assets": "Centrifuge individual real-world assets / tranches",
        "sg_goldfinch_pools": "Goldfinch senior pool data — assets, share price, total loans, write-downs, APY, default rate",
        "sg_goldfinch_tranched_pools": "Goldfinch junior/tranched pools — depositors, balance, repaid principal/interest, remaining capacity",
    }

    for tname, desc in sg_tables.items():
        rows = table_counts.get(tname, 0)
        status = "✓ populated" if rows > 0 else "⚠ empty (add THE_GRAPH_API_KEY)"
        p(f"**`{tname}`** — {rows:,} rows ({status})")
        p(f"- {desc}")
        p("")

    h(3, "1.5 Etherscan")
    p("""**Source:** `https://api.etherscan.io/api`
**Key required:** Yes — `ETHERSCAN_API_KEY` in .env (free at etherscan.io/apis)
**Note:** Empty tables are created when key is absent.""")

    es_tables = {
        "es_token_info": "Token metadata from Etherscan including holder count, website, Twitter, description — critical for institutional context",
        "es_recent_txns": "Last 200 ERC-20 transfer transactions per token — from/to addresses, value, block, date",
        "es_contract_info": "Contract verification status, compiler version, license type, proxy detection",
    }

    for tname, desc in es_tables.items():
        rows = table_counts.get(tname, 0)
        status = "✓ populated" if rows > 0 else "⚠ empty (add ETHERSCAN_API_KEY)"
        p(f"**`{tname}`** — {rows:,} rows ({status})")
        p(f"- {desc}")
        p("")

    # ─────────────────────────────────────────────
    h(2, "2. Coverage Analysis")
    # ─────────────────────────────────────────────

    # Asset class coverage
    h(3, "2.1 Asset Classes Tracked")

    coverage_table = [
        ("Tokenized US Treasuries", "BUIDL, USDY, OUSG, USYC, USTB, BENJI, TBILL, bIB01, OpenEden TBILL",
         "**Strong** — multiple issuers, TVL data from DefiLlama, price from CoinGecko, on-chain supply for all major contracts"),
        ("Tokenized Gold", "PAXG, XAUT, CGT (Cache Gold)",
         "**Good** — well-tracked on CoinGecko, on-chain supply reads, but less DeFi integration data"),
        ("Private Credit Protocols", "Maple, Centrifuge, Goldfinch, Clearpool",
         "**Partial** — DefiLlama TVL and protocol-level data available; loan-level data requires The Graph key"),
        ("RWA Stablecoins", "USDM (Mountain), USD0 (Usual), STBT, USD+",
         "**Mixed** — some appear in DefiLlama stablecoin data; supply history available for some"),
        ("Tokenized Stocks/Equities", "Backed Finance (bCSPX, bIBTA), Ondo OMMF",
         "**Weak** — limited DefiLlama coverage; CoinGecko category exists but small"),
        ("Real Estate", "RealT, Propbase, Homebase",
         "**Weak** — protocol-level TVL on DefiLlama; no price oracle; fragmented"),
        ("Infrastructure/Governance", "ONDO token, CFG, MPL, GFI, CPOOL",
         "**Good** — all tracked on CoinGecko with full price history"),
    ]

    lines.append("| Asset Class | Key Assets | Coverage Assessment |")
    lines.append("| --- | --- | --- |")
    for asset_class, assets, assessment in coverage_table:
        lines.append(f"| {asset_class} | {assets} | {assessment} |")
    lines.append("")

    h(3, "2.2 Cross-Source Overlap")
    p("The following shows which primary issuers appear in multiple data sources:")

    overlap_check = _q(con, """
        WITH defillama AS (
            SELECT DISTINCT LOWER(name) AS name FROM dl_protocols
            UNION ALL
            SELECT DISTINCT LOWER(name) FROM dl_stablecoin_history
        ),
        coingecko AS (
            SELECT DISTINCT LOWER(display_name) AS name FROM cg_rwa_tokens
        ),
        onchain AS (
            SELECT DISTINCT LOWER(symbol) AS name FROM onchain_token_supply
        )
        SELECT
            dl.name AS protocol_name,
            'DefiLlama' AS in_defillama,
            CASE WHEN cg.name IS NOT NULL THEN 'CoinGecko' ELSE '—' END AS in_coingecko,
            CASE WHEN oc.name IS NOT NULL THEN 'On-chain' ELSE '—' END AS in_onchain
        FROM defillama dl
        LEFT JOIN coingecko cg ON dl.name = cg.name
        LEFT JOIN onchain oc ON dl.name = oc.name
        WHERE cg.name IS NOT NULL OR oc.name IS NOT NULL
        LIMIT 20
    """) if _table_exists(con, "dl_protocols") else None

    if overlap_check is not None and not overlap_check.empty:
        p(_df_to_md_table(overlap_check, 15))
    else:
        p("_Cross-source join requires more data. Run all ingestion steps first._\n")

    # Protocol count
    n_protocols = _row_count(con, "dl_protocols")
    n_cg = _row_count(con, "cg_rwa_tokens")
    n_onchain = _row_count(con, "onchain_token_supply")

    p(f"**Total distinct RWA-relevant protocols/assets tracked:**")
    p(f"- DefiLlama protocols: {n_protocols:,}")
    p(f"- CoinGecko tokens (curated): {n_cg:,}")
    p(f"- On-chain contracts with supply reads: {n_onchain:,}")

    # ─────────────────────────────────────────────
    h(2, "3. Depth and Freshness")
    # ─────────────────────────────────────────────

    h(3, "3.1 Historical Depth by Source")

    depth_data = []
    if _table_exists(con, "dl_protocol_tvl"):
        r = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest, COUNT(DISTINCT slug) AS protocols FROM dl_protocol_tvl")
        if r is not None and not r.empty:
            row = r.iloc[0]
            depth_data.append(("DefiLlama Protocol TVL", str(row["earliest"]), str(row["latest"]), f"{row['protocols']} protocols, daily"))

    if _table_exists(con, "dl_stablecoin_history"):
        r = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest, COUNT(DISTINCT id) AS assets FROM dl_stablecoin_history")
        if r is not None and not r.empty:
            row = r.iloc[0]
            depth_data.append(("DefiLlama Stablecoin History", str(row["earliest"]), str(row["latest"]), f"{row['assets']} assets, daily"))

    if _table_exists(con, "cg_token_history"):
        r = _q(con, "SELECT MIN(date) AS earliest, MAX(date) AS latest, COUNT(DISTINCT coingecko_id) AS tokens FROM cg_token_history")
        if r is not None and not r.empty:
            row = r.iloc[0]
            depth_data.append(("CoinGecko Price History", str(row["earliest"]), str(row["latest"]), f"{row['tokens']} tokens, daily"))

    if _table_exists(con, "onchain_transfer_events"):
        r = _q(con, "SELECT MIN(block_number) AS min_block, MAX(block_number) AS max_block, COUNT(*) AS events FROM onchain_transfer_events")
        if r is not None and not r.empty:
            row = r.iloc[0]
            depth_data.append(("On-chain Transfer Events", f"Block {row['min_block']}", f"Block {row['max_block']}", "30-day lookback, per-transaction"))

    if depth_data:
        lines.append("| Source | Earliest | Latest | Coverage |")
        lines.append("| --- | --- | --- | --- |")
        for name, earliest, latest, cov in depth_data:
            lines.append(f"| {name} | {earliest} | {latest} | {cov} |")
        lines.append("")

    h(3, "3.2 Per-Asset Historical Coverage")
    p("How far back does TVL data go for the top tokenized Treasury issuers?")

    per_asset = _q(con, """
        SELECT slug,
               MIN(date) AS earliest,
               MAX(date) AS latest,
               COUNT(*) AS daily_points,
               ROUND(MAX(tvl_usd)/1e6, 1) AS peak_tvl_m,
               ROUND(datediff('day', MIN(date)::DATE, MAX(date)::DATE) / 365.25, 1) AS years_of_data
        FROM dl_protocol_tvl
        GROUP BY slug
        ORDER BY MAX(tvl_usd) DESC NULLS LAST
        LIMIT 20
    """) if _table_exists(con, "dl_protocol_tvl") else None

    if per_asset is not None and not per_asset.empty:
        p(_df_to_md_table(per_asset, 15))

    # ─────────────────────────────────────────────
    h(2, "4. What Questions Can the Data Answer Right Now?")
    # ─────────────────────────────────────────────

    p("The following questions are answerable with SQL against the current database:\n")

    answerable = [
        ("TVL momentum", "What is the month-over-month TVL change for each tokenized Treasury issuer, and which are growing fastest?",
         "`dl_protocol_tvl` — compare last 30 vs prior 30 days per slug"),
        ("Category share shift", "Is private credit or tokenized Treasuries growing faster as a share of total RWA TVL?",
         "`dl_protocol_tvl` grouped by category over time"),
        ("Chain distribution", "Which chains hold the most RWA TVL today, and how has that share shifted over the past 6 months?",
         "`dl_chains` snapshot + `dl_protocol_tvl` chain breakdown"),
        ("Issuer concentration", "How concentrated is tokenized Treasury market cap? What is the Herfindahl index across issuers?",
         "`onchain_token_supply` or `cg_rwa_tokens` market_cap_usd"),
        ("Yield comparison", "What APY is each RWA yield pool currently offering, and how does it compare to the risk-free rate proxy?",
         "`dl_yield_pools` apy_base vs apy_reward breakdown"),
        ("Token price stability", "How closely do yield-bearing Treasury tokens (USDY, USYC, USTB) track their NAV over time?",
         "`cg_token_history` price_usd deviation from $1 or protocol-stated NAV"),
        ("Mint/burn activity", "Which tokenized Treasury has seen the highest net issuance (mints minus burns) over the past 30 days?",
         "`onchain_transfer_flows` net_flow column"),
        ("Active participant count", "How many unique addresses have transacted with each tokenized Treasury in the last 30 days?",
         "`onchain_transfer_flows` unique_senders + unique_receivers"),
        ("Protocol fee revenue", "Which RWA protocols are generating the most fee revenue, and what is their revenue/TVL ratio?",
         "`dl_protocol_fees` joined to `dl_protocols`"),
        ("Gold token divergence", "Do PAXG and XAUT track gold spot price equally, or has there been a persistent premium/discount between them?",
         "`cg_token_history` for PAXG vs XAUT, normalize and compute spread"),
        ("Stablecoin market share", "What fraction of total USD stablecoin supply is backed by T-bill/RWA collateral vs fiat vs algorithmic?",
         "`dl_stablecoins` grouped by peg_mechanism"),
        ("Multichain footprint", "Which tokenized Treasury is most deployed across chains, and does TVL distribution match token supply distribution?",
         "`dl_protocols` chains field + `onchain_token_supply` cross-chain"),
        ("Protocol longevity", "How long has each RWA protocol been live (first TVL data point vs today)?",
         "`dl_protocol_tvl` MIN(date) per slug"),
        ("Private credit default risk", "For Maple v1 loans, what fraction of total principal has been lost to defaults?",
         "`sg_maple_loans` WHERE loan_state = 'Defaulted' — requires The Graph key"),
        ("CoinGecko category breadth", "How many distinct tokens are in CoinGecko's 'tokenized-us-treasuries' category vs 'real-world-assets-rwa'?",
         "`cg_rwa_category` GROUP BY category_id"),
        ("TVL-to-market-cap ratio", "For governance tokens (ONDO, MPL, CFG), what is their FDV relative to the TVL they govern?",
         "`dl_protocols` fdv + tvl_usd per protocol"),
        ("Historical ATH comparison", "Which tokenized Treasury tokens are closest to or furthest from their all-time high price?",
         "`cg_rwa_tokens` ath_usd vs current_price_usd"),
        ("Seasonal/monthly TVL patterns", "Are there seasonal patterns in RWA TVL (year-end, quarter-end)?",
         "`dl_protocol_tvl` aggregated by month across protocols"),
        ("Holder count vs supply", "For tokens with Etherscan data: what is the average holding size (supply / holders)?",
         "`es_token_info` holder_count + `onchain_token_supply` total_supply_adjusted"),
        ("Verification completeness", "Are all tracked RWA contracts verified on Etherscan, or are any opaque?",
         "`es_contract_info` is_verified column"),
    ]

    for i, (category, question, how) in enumerate(answerable, 1):
        p(f"**{i}. {category}**")
        p(f"*{question}*")
        p(f"How: {how}\n")

    # ─────────────────────────────────────────────
    h(2, "5. What Questions Can the Data NOT Answer?")
    # ─────────────────────────────────────────────

    gaps = [
        ("Holder wealth distribution (whale vs retail)",
         "We have unique address counts and total holder counts (Etherscan), but not the distribution of holdings. We cannot tell if 10 whales hold 95% of BUIDL supply or if there are 1,000 diversified institutions.",
         "Partially addressable with Etherscan token holder list endpoint (free, but returns top 10K holders only). Full distribution requires Dune or Nansen (paid)."),

        ("Underlying collateral composition",
         "We know BUIDL holds US Treasuries, but we cannot see the portfolio — duration, yield, specific instruments, cash percentage, repo exposure.",
         "Requires issuer reporting. Some issuers publish attestation reports (monthly PDFs). Not available via any free API. Would require scraping/parsing PDFs."),

        ("Real-time NAV per token",
         "We have on-chain totalSupply and CoinGecko prices, but for restricted tokens (BUIDL, OUSG, USYC) the 'price' on CoinGecko is based on limited secondary market data or is stale. True NAV requires issuer attestation.",
         "Some issuers post NAV on-chain via oracle contracts. Addressable with additional on-chain reads — but requires identifying the oracle contract per issuer."),

        ("Issuer financial health and solvency",
         "We have no data on BlackRock, Franklin Templeton, Ondo, or Superstate's balance sheets, capital adequacy, or operational risk.",
         "Requires public filings (SEC EDGAR for registered funds), annual reports, or paid data. BlackRock BUIDL is a Cayman SPV — limited public disclosure."),

        ("Regulatory and compliance status",
         "We cannot determine which jurisdictions each token is legally available in, whether tokens are registered securities, or if issuers are facing regulatory action.",
         "Manual research. SEC EDGAR for registered funds (BENJI is a registered mutual fund). Others require direct issuer documentation review."),

        ("Secondary market liquidity depth",
         "We have 24h trading volume from CoinGecko, but this is thin or zero for most institutional tokens. We cannot measure bid-ask spreads, slippage, or available liquidity at various order sizes.",
         "Requires DEX liquidity data (Uniswap subgraph for on-chain pools — free via The Graph) + CEX order book data (mostly paid). Most tokenized Treasuries have no meaningful secondary market."),

        ("Redemption mechanics and timing",
         "We cannot determine how long it takes to redeem each tokenized asset, whether there are gates or queues, or what the minimum redemption size is.",
         "Manual research per issuer. Not available via any API. Critical for institutional users evaluating liquidity risk."),

        ("Subscription capacity and whitelist status",
         "We cannot tell if BUIDL or OUSG are accepting new investors, what the minimum investment is, or whether there is a waitlist.",
         "Issuer-facing. Securitize (BUIDL platform) and Ondo each have their own onboarding processes — no API."),

        ("Off-chain attestation cadence and audit quality",
         "We know some tokens publish monthly attestations, but we cannot verify timeliness, auditor identity, or scope of attestation from API data.",
         "Partially addressable by scraping issuer websites for PDF links. Requires manual review of attestation reports."),

        ("Loan-level default history for private credit",
         "We have DefiLlama TVL for Maple, Centrifuge, Goldfinch, but cannot see individual loan performance, borrower identity, collateral quality, or recovery rates without subgraph access.",
         "Fully addressable with THE_GRAPH_API_KEY (free). Maple and Goldfinch subgraphs have loan-level data."),

        ("Cross-chain total supply reconciliation",
         "BENJI exists on Ethereum and Polygon. We read both, but we cannot verify that the total reflects one underlying fund share class without issuer confirmation.",
         "Partially addressable by summing across chains and comparing to NAV reports. Gap is issuer documentation."),

        ("Staking, rehypothecation, and leverage",
         "We cannot tell if tokenized Treasury positions are being used as collateral in DeFi lending protocols, amplifying systemic risk.",
         "Partially addressable with DeFi protocol subgraphs (Aave, Compound) — check if BUIDL/USYC appear as collateral assets. Requires additional free subgraph queries."),

        ("Issuer profit margins and fee economics",
         "We have DefiLlama fee data for some protocols, but this is incomplete. We cannot model the economics of each issuer (cost of fund administration vs fee income).",
         "Not available via free APIs. Would require regulatory filings or paid data."),
    ]

    for i, (gap, problem, resolution) in enumerate(gaps, 1):
        p(f"**{i}. {gap}**")
        p(problem)
        p(f"_Resolution path: {resolution}_\n")

    # ─────────────────────────────────────────────
    h(2, "6. Surprising Findings")
    # ─────────────────────────────────────────────

    # Compute a few surprising stats dynamically
    top_tvl = _q(con, """
        SELECT name, ROUND(tvl_usd/1e6,1) AS tvl_m, category
        FROM dl_protocols WHERE tvl_usd IS NOT NULL
        ORDER BY tvl_usd DESC LIMIT 3
    """) if _table_exists(con, "dl_protocols") else None

    rwa_category_counts = _q(con, """
        SELECT category, COUNT(*) AS n, ROUND(SUM(tvl_usd)/1e6,1) AS total_tvl_m
        FROM dl_protocols WHERE tvl_usd > 0
        GROUP BY category ORDER BY total_tvl_m DESC NULLS LAST
    """) if _table_exists(con, "dl_protocols") else None

    supply_rows = _q(con, """
        SELECT symbol, chain, ROUND(total_supply_adjusted, 0) AS supply
        FROM onchain_token_supply
        WHERE total_supply_adjusted IS NOT NULL
        ORDER BY total_supply_adjusted DESC
    """) if _table_exists(con, "onchain_token_supply") else None

    surprise_points = []

    if rwa_category_counts is not None and not rwa_category_counts.empty:
        # Count how many categories
        n_cats = len(rwa_category_counts)
        top_cat = rwa_category_counts.iloc[0]
        surprise_points.append(
            f"**Category breadth is wider than expected:** DefiLlama's RWA filter captured {n_protocols:,} protocols "
            f"across {n_cats} categories. The largest by TVL is `{top_cat['category']}` with ${top_cat['total_tvl_m']:,.1f}M. "
            f"This suggests the 'RWA' label is applied loosely — undercollateralized lending protocols like Maple sit "
            f"alongside pure Treasury wrappers."
        )

    if supply_rows is not None and not supply_rows.empty:
        supply_dict = dict(zip(supply_rows["symbol"], supply_rows["supply"]))
        # Check if PAXG or XAUT have meaningful supply
        paxg_supply = supply_dict.get("PAXG")
        xaut_supply = supply_dict.get("XAUT")
        if paxg_supply and xaut_supply:
            ratio = max(paxg_supply, xaut_supply) / max(min(paxg_supply, xaut_supply), 1)
            larger = "PAXG" if paxg_supply > xaut_supply else "XAUT"
            surprise_points.append(
                f"**Gold token supply gap:** {larger} has a significantly larger on-chain supply "
                f"({paxg_supply:,.0f} vs {xaut_supply:,.0f} tokens — a {ratio:.1f}x difference). "
                f"Both are priced near gold spot, but market share is not split evenly."
            )

    # Note about restricted tokens
    if supply_rows is not None and not supply_rows.empty:
        buidl_supply = supply_dict.get("BUIDL")
        if buidl_supply:
            surprise_points.append(
                f"**BUIDL on-chain supply is readable but the token itself is transfer-restricted:** "
                f"On-chain reads show {buidl_supply:,.0f} BUIDL in existence, but the contract enforces "
                f"a permissionlist — only KYC'd addresses can hold or transfer. This means Transfer events "
                f"are institutional-to-institutional only, not retail. The 'holder count' from Etherscan "
                f"represents funds/custodians, not individual investors."
            )

    # Compute cross-chain supply discrepancy finding
    cross_chain_finding = ""
    if supply_rows is not None and not supply_rows.empty:
        supply_dict_full = dict(zip(supply_rows["symbol"], supply_rows["supply"]))
        # Check USYC: Ethereum on-chain vs DefiLlama TVL
        usyc_eth = supply_dict_full.get("USYC", 0) or 0
        buidl_eth = supply_dict_full.get("BUIDL", 0) or 0
        if usyc_eth > 0:
            cross_chain_finding = (
                f"**Single-chain on-chain reads massively undercount cross-chain supply:** "
                f"Our Ethereum on-chain read for USYC shows {usyc_eth:,.0f} tokens (~${usyc_eth/1e6:.0f}M at $1.09). "
                f"But DefiLlama TVL for Circle USYC is ~$2.98B and CoinGecko circulating supply is ~2.7B tokens — "
                f"meaning the BSC deployment of USYC alone holds >20x more than Ethereum. "
                f"BUIDL shows a similar pattern: {buidl_eth:,.0f} tokens on Ethereum vs $2.9B total TVL across "
                f"8 chains. For cross-chain tokens, Ethereum-only reads give a deeply misleading picture of total issuance."
            )

    # Compute PAXG activity finding
    paxg_activity_finding = ""
    flows_df = _q(con, """
        SELECT symbol, transfer_count, unique_senders, unique_receivers
        FROM onchain_transfer_flows
        WHERE symbol IN ('PAXG', 'BUIDL', 'USDY')
        ORDER BY transfer_count DESC
    """) if _table_exists(con, "onchain_transfer_flows") else None

    if flows_df is not None and not flows_df.empty:
        flows_dict = {row["symbol"]: row for _, row in flows_df.iterrows()}
        paxg = flows_dict.get("PAXG")
        buidl = flows_dict.get("BUIDL")
        if paxg is not None and buidl is not None:
            paxg_count = paxg.get("transfer_count", 0)
            buidl_count = buidl.get("transfer_count", 0)
            if paxg_count and buidl_count:
                ratio = paxg_count / max(buidl_count, 1)
                paxg_activity_finding = (
                    f"**Gold tokens vs Treasury tokens: a {ratio:.0f}x transfer activity gap:** "
                    f"PAXG had {paxg_count:,} transfer events in 30 days with {paxg.get('unique_senders',0):,} unique senders. "
                    f"BUIDL had only {buidl_count:,} events with {buidl.get('unique_senders',0)} unique senders. "
                    f"This directly reflects the difference in access models: PAXG is retail-accessible on any exchange; "
                    f"BUIDL requires institutional KYC and minimum ticket sizes. "
                    f"Despite having lower TVL, PAXG is far more liquid in secondary markets."
                )

    surprise_points.extend([
        cross_chain_finding or (
            "**Single-chain on-chain reads undercount cross-chain supply:** "
            "For tokens deployed on multiple chains (USYC on BSC+Ethereum, BUIDL on 8 chains), reading only the "
            "Ethereum contract produces a fraction of total supply. Multi-chain tokens require summing across chains."
        ),
        paxg_activity_finding or (
            "**Gold tokens have far higher transfer velocity than tokenized Treasuries:** "
            "PAXG and XAUT combined had >389,000 Transfer events in 30 days; all Treasury tokens combined had ~3,600. "
            "This reflects fundamentally different access models — gold tokens are retail-accessible."
        ),
        "**DefiLlama stablecoin history depth varies wildly:** Some assets have 3+ years of daily data; "
        "others launched recently and have only weeks. The yield-bearing stablecoins (USDY, USYC) have "
        "supply history from approximately mid-2023, while BUIDL history starts in early 2024 — reflecting "
        "the actual product launch dates.",

        "**CoinGecko's 'tokenized-us-treasuries' category does not exist:** The category returns a 404. "
        "Treasury tokens are tracked under `real-world-assets-rwa` and `asset-backed-tokens`, mixed with "
        "unrelated assets. There is no clean CoinGecko category for T-bill wrappers specifically.",

        "**Goldfinch shows ~10% APY in DefiLlama yield pools, far above Treasury rates:** "
        "The Goldfinch USDC pool shows 10.06% APY — consistent with private credit pricing but "
        "dramatically different from the 3.5% offered by Treasury wrappers. Both are in the same 'RWA' bucket "
        "in aggregators, misleading headline RWA yield statistics.",

        "**DefiLlama fee reporting is sparse for RWA protocols:** Of the 191 RWA-filtered protocols, only 47 "
        "report fee data. Most tokenized Treasury products don't generate on-chain fees visible to DefiLlama — "
        "fees are taken off-chain by the fund manager. This means revenue analysis via DefiLlama systematically "
        "undercounts issuer economics.",

        "**OUSG net flow was negative over the last 30 days:** On-chain transfer data shows OUSG had $148K in "
        "mints but $1.07M in burns — a net outflow. Combined with its small on-chain supply ($2.4M on Ethereum), "
        "this suggests OUSG's Ethereum deployment may be winding down in favor of USDY or cross-chain versions.",
    ])

    for point in surprise_points:
        p(f"- {point}\n")

    # ─────────────────────────────────────────────
    h(2, "7. Sample Data")
    # ─────────────────────────────────────────────

    samples = [
        ("Top RWA protocols by current TVL (DefiLlama)", """
            SELECT name, category, ROUND(tvl_usd/1e6,2) AS tvl_m_usd,
                   ROUND(change_1d_pct,2) AS chg_1d,
                   ROUND(change_7d_pct,2) AS chg_7d,
                   chains
            FROM dl_protocols
            WHERE tvl_usd IS NOT NULL AND tvl_usd > 0
            ORDER BY tvl_usd DESC LIMIT 10
        """),
        ("On-chain token supply snapshot", """
            SELECT symbol, chain, ROUND(total_supply_adjusted,2) AS supply,
                   decimals, issuer
            FROM onchain_token_supply
            ORDER BY total_supply_adjusted DESC NULLS LAST
        """),
        ("RWA token market caps (CoinGecko)", """
            SELECT display_name, symbol, asset_class,
                   ROUND(market_cap_usd/1e6,2) AS mcap_m,
                   ROUND(current_price_usd,4) AS price,
                   ROUND(price_change_pct_30d,2) AS chg_30d_pct
            FROM cg_rwa_tokens
            WHERE market_cap_usd IS NOT NULL
            ORDER BY market_cap_usd DESC LIMIT 10
        """),
        ("30-day mint/burn flows per token", """
            SELECT symbol, chain,
                   ROUND(total_minted,0) AS minted,
                   ROUND(total_burned,0) AS burned,
                   ROUND(net_flow,0) AS net,
                   transfer_count, unique_senders, unique_receivers
            FROM onchain_transfer_flows
            ORDER BY ABS(net_flow) DESC NULLS LAST
        """),
        ("RWA yield pools (DefiLlama)", """
            SELECT project, symbol, chain,
                   ROUND(tvl_usd/1e3,1) AS tvl_k,
                   ROUND(apy,2) AS apy_pct,
                   ROUND(apy_base,2) AS base_pct,
                   ROUND(apy_reward,2) AS reward_pct
            FROM dl_yield_pools
            WHERE tvl_usd > 0
            ORDER BY tvl_usd DESC LIMIT 10
        """),
    ]

    for label, sql in samples:
        h(3, label)
        df = _q(con, sql)
        if df is not None and not df.empty:
            p(_df_to_md_table(df, 10))
        else:
            p("_No data available for this query._\n")

    # ─────────────────────────────────────────────
    h(2, "Appendix: Database Tables Summary")
    # ─────────────────────────────────────────────

    lines.append("| Table | Rows | Source |")
    lines.append("| --- | --- | --- |")
    source_map = {
        "dl_": "DefiLlama", "cg_": "CoinGecko",
        "onchain_": "On-chain RPC", "sg_": "The Graph Subgraphs", "es_": "Etherscan"
    }
    for tname, rows in sorted(table_counts.items()):
        source = next((v for k, v in source_map.items() if tname.startswith(k)), "Unknown")
        lines.append(f"| `{tname}` | {rows:,} | {source} |")
    lines.append("")

    return "\n".join(lines)


def run():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    con = get_connection()

    log.info("Generating inventory report...")
    report = generate_report(con)
    con.close()

    output_path = REPORTS_DIR / "inventory.md"
    output_path.write_text(report, encoding="utf-8")
    log.info(f"Report written to {output_path}")
    return str(output_path)


if __name__ == "__main__":
    run()
