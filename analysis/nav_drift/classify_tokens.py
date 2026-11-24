"""
Token definitions for the NAV drift analysis.

All four tokens are price-accumulating: the token balance stays constant
and the price rises as yield accrues. None of these tokens rebase.

BUIDL is excluded: transfer-restricted, no open secondary market, price is
oracle-posted at exactly $1.00 by construction. Nothing to measure.
BENJI is excluded: no price history available in DefiLlama.
"""

TOKENS = {
    "USDY": {
        "name": "Ondo US Dollar Yield",
        "issuer": "Ondo Finance",
        "address": "ethereum:0x96F6eF951840721AdBF46Ac996b59E0235CB985C",
        "mechanism": "accumulating",
        "color": "#1f3a5f",
    },
    "OUSG": {
        "name": "Ondo Short-Term US Gov Bond Fund",
        "issuer": "Ondo Finance",
        "address": "ethereum:0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92",
        "mechanism": "accumulating",
        "color": "#2e6da4",
    },
    "USYC": {
        "name": "Short Duration Yield Coin",
        "issuer": "Hashnote",
        "address": "ethereum:0x136471a34f6ef19fE571EFFC1CA711fdb8E49f2b",
        "mechanism": "accumulating",
        "color": "#d64e12",
    },
    "USTB": {
        "name": "Superstate Short Duration US Gov Fund",
        "issuer": "Superstate",
        "address": "ethereum:0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e",
        "mechanism": "accumulating",
        "color": "#4a7c59",
    },
}

# Tokens excluded and why — used in the report
EXCLUDED = {
    "BUIDL": "Transfer-restricted; no open secondary market. Price is oracle-posted at exactly $1.00 by construction — drift is zero by design, not by performance.",
    "BENJI": "No price history available in DefiLlama or CoinGecko (no API key). Current price oracle shows $1.00 but no time series exists.",
    "TBILL": "Price history ends May 2024 — data is 12+ months stale. Not comparable to the current-window analysis.",
}
