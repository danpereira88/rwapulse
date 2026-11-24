"""
The asset-level RWA universe: which tokens count, what asset class each one is,
and which issuer it belongs to. Shared by the pipeline (ingest/rwa_supply.py
decides which tokens to read on-chain) and the dashboard (ui/insights.py).

DefiLlama re-based its RWA coverage on assets in 2026, so this does too:
  1. Every token in CoinGecko's RWA and tokenized-gold categories,
  2. minus governance/infrastructure tokens, wrapped crypto, plain stablecoins
     and Figure's HELOC loan records (see EXCLUDE / NOTES),
  3. plus curated institutional funds CoinGecko lists outside those categories,
  4. plus DefiLlama protocols that still report TVL and have no token on
     CoinGecko (Maple, Re, Huma...).

Everything here is a rule you can read and change. Asset classes:
  treasuries  — tokenised T-bills, money-market and government bond funds
  credit      — private and structured credit, receivables, reinsurance
  commodities — gold, silver and other physical commodities
  stocks      — tokenised equities and ETFs (tracked, kept out of yield-bearing)
  other       — other real-world assets (real estate, vaults, mixed funds); kept
                out of the yield-bearing headline until reviewed
"""

import re

CATEGORIES = ("real-world-assets-rwa", "tokenized-gold")

# Governance, infrastructure and platform tokens that sit in CoinGecko's RWA
# category but are not themselves real-world assets.
EXCLUDE = {
    "chainlink", "stellar", "quant-network", "ondo-finance", "algorand", "injective-protocol",
    "xdce-crowd-sale", "hash-2", "origintrail", "zebec-network", "reserve-rights-token", "plume",
    "redstone-oracles", "velo", "dusk-network", "creditcoin-2", "polymesh", "securitize-corp",
    "sharex-2", "syrup", "collector-crypt", "reental", "maple", "centrifuge", "goldfinch",
    "clearpool", "propbase", "realtoken-ecosystem-governance", "mantra-dao", "pendle",
    # plain or near-plain stablecoins
    "usa", "ondo-u-s-dollar-token", "precious-metals-usd", "usual-usd", "mountain-protocol-usdm",
    # counted through DefiLlama instead (bigger, fuller figure there)
    "onyc", "re",
    # venture-fund token reported flat at ~$965M (excluded in the original analysis too);
    # staked-stablecoin wrapper
    "blockchain-capital", "staked-usdt",
}
NOTES = {
    "figure-heloc": "Figure HELOC (~$29B, on Provenance) is excluded by RWA Pulse's methodology: it "
                    "represents loan records rather than a freely transferable token. DefiLlama and "
                    "rwa.xyz both list it, so totals here run lower than theirs by that amount.",
}
EXCLUDE |= set(NOTES)

# Institutional funds to include even when they're outside CoinGecko's RWA
# categories or its top-250 (ids as CoinGecko lists them).
CURATED = {
    "blackrock-usd-institutional-digital-liquidity-fund", "hashnote-usyc", "ousg",
    "ondo-us-dollar-yield", "spiko-us-t-bills-money-market-fund", "eutbl",
    "spiko-amundi-overnight-swap-fund-eur", "safo", "spiko-uk-t-bills-money-market-fund",
    "spiko-amundi-overnight-swap-fund-gbp", "spiko-amundi-overnight-swap-fund-chf",
    "janus-henderson-anemoy-aaa-clo-fund", "janus-henderson-anemoy-treasury-fund",
    "wisdomtree-treasury-money-market-digital-fund", "openeden-tbill",
    "superstate-short-duration-us-government-securities-fund-ustb", "tether-gold", "pax-gold",
    "franklin-templeton-benji", "midas-mtbill", "vaneck-treasury-fund",
}

# Tokens whose issuer is counted through DefiLlama in the totals (so they're in
# EXCLUDE above) but which should still be read on-chain, chain by chain, for the
# per-chain views. Chain views use on-chain reads only, so nothing is double counted.
CHAIN_ONLY = {"onyc"}

# DefiLlama protocols still reporting TVL, with no matching token on CoinGecko.
# slug -> (display name, asset class, issuer)
DL_ONLY = {
    "maple": ("Maple (syrup pools)", "credit", "Maple"),
    "re": ("Re", "credit", "Re"),
    "huma": ("Huma", "credit", "Huma"),
    "onre": ("OnRe", "credit", "OnRe"),
    "r25": ("R25", "credit", "R25"),
    "usd-ai": ("USD AI", "credit", "USD AI"),
    "realt-tokens": ("RealT Tokens", "other", "RealT"),
}

# When a curated token has no CoinGecko market cap on a past snapshot, fall back
# to the DefiLlama protocol TVL of the product it belongs to (history before the
# on-chain reads started). Only for single-token products, to avoid double counts.
DL_FALLBACK = {
    "wisdomtree-treasury-money-market-digital-fund": "wisdomtree",
    "openeden-tbill": "openeden-tbill",
    "franklin-templeton-benji": None,
}

_CLASS_RULES = [
    ("stocks", r"xstock|bstock|tokenized stock|tokenized etf|prestock|warrants|\(ondo tokenized|tokenized\)$"),
    ("commodities", r"gold|silver|xau|paxg|platinum|palladium|emerald|metal|commodit"),
    ("treasuries", r"treasur|t-bill|tbill|money market|liquidity fund|government|overnight|"
                   r"\bustb\b|usyc|buidl|usdy|ousg|benji|ylds|reserve vehicle|fiusd|yield"),
    ("credit", r"clo\b|credit|sstn|sstl|\bssl\b|receivable|financing|lender|lending|reinsurance|loan|bnpl|fintech"),
]

# Issuer (brand) rules, checked in order against "<id> <name>"
_ISSUER_RULES = [
    ("BlackRock", r"blackrock|buidl"),
    ("Franklin Templeton", r"franklin|benji"),
    ("Spiko", r"spiko|eutbl|safo|ustbl-?tokenized"),
    ("Ondo", r"\bondo\b|ondo-|ousg|usdy"),
    ("Circle", r"usyc|hashnote"),
    ("Tether", r"tether|xaut"),
    ("Paxos", r"paxos|pax-gold|paxg"),
    ("Janus Henderson (Anemoy)", r"janus|anemoy"),
    ("Superstate / Invesco", r"superstate|invesco|\bustb\b"),
    ("WisdomTree", r"wisdomtree"),
    ("OpenEden", r"openeden"),
    ("Kinesis", r"kinesis"),
    ("Matrixdock", r"matrixdock"),
    ("Midas", r"midas"),
    ("VanEck", r"vaneck"),
    ("Apollo", r"apollo"),
    ("Securitize", r"securitize"),
    ("Tradable", r"tradable|sstn|sstl"),
    ("Theo", r"\btheo\b|theo-"),
    ("Sygnum", r"sygnum"),
    ("Figure", r"\bylds\b|figure"),
    ("Backed (xStocks)", r"xstock"),
    ("bStocks", r"bstock"),
]


def classify(cg_id: str, name: str = "", symbol: str = "", categories: str = ""):
    """Return (asset_class, issuer), or (None, None) if the token is excluded."""
    if cg_id in EXCLUDE:
        return None, None
    cats = categories or ""
    if cats and not any(c in cats for c in CATEGORIES) and cg_id not in CURATED:
        return None, None  # e.g. asset-backed-tokens (wrapped crypto)
    hay = f"{cg_id} {name} {symbol}".lower()
    cls = "other"
    if "tokenized-gold" in cats:
        cls = "commodities"
    else:
        for c, pat in _CLASS_RULES:
            if re.search(pat, hay):
                cls = c
                break
    issuer = next((iss for iss, pat in _ISSUER_RULES if re.search(pat, hay)), None)
    return cls, issuer or (name or cg_id)


YIELD_BEARING = {"treasuries", "credit"}  # "other" stays out until reviewed
CLASS_LABEL = {"treasuries": "Treasuries & MMFs", "credit": "Private & structured credit",
               "commodities": "Gold & commodities", "stocks": "Tokenised stocks & ETFs",
               "other": "Other RWAs"}
