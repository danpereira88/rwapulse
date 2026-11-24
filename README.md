# RWA Data Exploration

Local data ingestion and inventory for tokenized real-world assets (RWAs).
Stores everything in a single DuckDB file at `data/rwa.duckdb`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and add your Etherscan API key (free at etherscan.io/apis)
```

## Running ingestion

Run all sources at once:
```bash
python run_all.py
```

Run individual sources:
```bash
python ingest/defillama.py
python ingest/coingecko.py
python ingest/onchain.py
python ingest/subgraphs.py
python ingest/etherscan.py   # requires ETHERSCAN_API_KEY in .env
```

Generate the inventory report:
```bash
python explore/inventory.py
# Output: reports/inventory.md
```

## Project structure

```
rwa-data-exploration/
├── ingest/
│   ├── defillama.py      # DefiLlama protocol, TVL, stablecoin, yield data
│   ├── onchain.py        # Direct EVM reads via public RPC
│   ├── subgraphs.py      # Maple, Centrifuge, Goldfinch subgraphs
│   ├── coingecko.py      # CoinGecko prices and market caps
│   └── etherscan.py      # Holder counts, transactions, verification status
├── explore/
│   ├── inventory.py      # Generates reports/inventory.md
│   └── queries.py        # Ad-hoc exploratory SQL
├── data/
│   └── rwa.duckdb        # Local database (gitignored)
├── reports/
│   └── inventory.md      # Generated inventory report
├── db.py                 # Shared DuckDB connection
├── .env.example
├── requirements.txt
└── run_all.py
```

## Data sources

| Source | Key required | Coverage |
|--------|-------------|----------|
| DefiLlama | No | Protocol TVL, stablecoin supplies, yield pools |
| CoinGecko | No (rate-limited) | Prices, market caps, volumes |
| On-chain (public RPC) | No | totalSupply, Transfer events |
| The Graph subgraphs | No | Maple, Centrifuge pool/loan data |
| Etherscan | Yes (free) | Holder counts, transactions |

## Data sources after DefiLlama's RWA change (Nov 2026)

From 2026-09-20 DefiLlama's free `/protocols` API stopped returning TVL for the
large RWA products (BUIDL, USYC, Ondo, Spiko, WisdomTree, gold tokens…). They
moved to DefiLlama's RWA dashboard, whose `/rwa/*` API needs a paid API plan.

The pipeline now uses free sources for those products:

| Step | Source | What it gives |
|------|--------|---------------|
| `ingest/rwa_supply.py` | CoinGecko `/coins/{id}` (no key) | contract address per chain, decimals, price/NAV |
| `ingest/rwa_supply.py` | public RPC (EVM chains, Solana), Stellar Horizon | `totalSupply` per chain |
| `ingest/chain_sweep.py` | CoinGecko `/coins/markets` by category + `/coins/list`, Solana RPC | every tokenised asset CoinGecko lists on Solana: supply (Token-2022 multipliers applied) minus issuer-held inventory |
| `ui/insights.py` | the above, else CoinGecko market cap or Centrifuge subgraph scaled to DefiLlama's 2026-09-13 level | gap-filled TVL, every row labelled with its source |

Run on its own: `python ingest/rwa_supply.py --dry-run` (prints, writes nothing).
Solana sweep on its own: `python ingest/chain_sweep.py --dry-run`.
Chains read: EVM (Ethereum, BNB, Polygon, Arbitrum, Base, Optimism, Avalanche, Mantle, Etherlink, Plume), Solana, Stellar, XRPL, Aptos, Sui.
The universe is asset-level (ingest/rwa_universe.py), grouped by issuer like DefiLlama's RWA dashboard.
`dl_protocol_tvl` is now merged on each run instead of rebuilt, so history is kept.
Local UI: `.venv/bin/python ui/app.py` → http://127.0.0.1:8765
