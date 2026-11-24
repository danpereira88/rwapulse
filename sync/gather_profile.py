"""
AI-powered protocol profile gathering.

Uses Claude with web search to research and populate a protocol profile's
analytical fields. Writes directly to Supabase via profile_io.write_profile().

Usage:
    python -m sync.gather_profile <slug>
    python -m sync.gather_profile <slug> --dry-run    # research only, no write
    python -m sync.gather_profile <slug> --verbose    # print full agent response

Examples:
    python -m sync.gather_profile blackrock-buidl
    python -m sync.gather_profile blackrock-buidl --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

from sync.profile_io import read_profile, write_profile  # noqa: E402 (after dotenv)

# ── Configuration ──────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

MODEL      = "claude-sonnet-4-6"   # newest available Sonnet; update to claude-sonnet-4-7 when released
MAX_TOKENS = 8000
TEMPERATURE = 1  # web_search tool requires temperature=1

# Pricing in USD per token — claude-sonnet-4-7 (as of 2026-05)
_PRICE_INPUT    = 3.00  / 1_000_000   # fresh input tokens
_PRICE_CACHED   = 0.30  / 1_000_000   # cache-read tokens
_PRICE_OUTPUT   = 15.00 / 1_000_000   # output tokens

SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")

# ── Constants ──────────────────────────────────────────────────────────────────

PREFIX = "[gather_profile]"

# Layer prefixes the agent may attach to confidence/citation field keys
LAYER_PREFIXES = ("l1_", "l2_", "l3_")

# These columns are never overwritten by the gathering workflow
IDENTITY_FIELDS = frozenset({
    "id", "defillama_slug", "product_name", "common_name", "issuer_name",
    "tokenisation_platform", "launch_date", "asset_class", "tier", "chains",
    "created_at", "updated_at",
})

# Valid enum values for validation
VALID_NAV_CADENCE         = {"intraday", "daily", "weekly", "monthly", "irregular"}
VALID_ATTEST_CADENCE      = {"daily", "weekly", "monthly", "quarterly", "annual", "none"}
VALID_AUDIT_CADENCE       = {"quarterly", "annual", "none"}
VALID_ELIGIBLE_INVESTORS  = {
    "retail",
    "accredited",
    "qualified_purchaser",
    "qualified_institutional_buyer",
    "non_us_persons_reg_s",
    "non_us_persons_offshore",
    "professional_investor",
    "restricted",
    "mixed",
}
VALID_SECONDARY_DEPTH     = {"none", "thin", "moderate", "deep"}
VALID_COMPOSITE_TIER      = {"Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"}
VALID_CONFIDENCE          = {"high", "medium", "low"}

DEFAULT_VERIFIER = "RWA Intelligence (AI-augmented)"

# Score weights per asset class: (L1, L2, L3)
# These weights are authoritative. The composite is computed deterministically
# by the script from L-scores + these weights; the agent's composite field is
# informational only and is overridden.
SCORE_WEIGHTS = {
    "MMF":                  (0.30, 0.40, 0.30),
    "Tokenised Treasuries": (0.30, 0.40, 0.30),
    "Private Credit":       (0.35, 0.35, 0.30),
    "Structured Credit":    (0.35, 0.35, 0.30),
    "Commodity":            (0.25, 0.35, 0.40),
    "Equity":               (0.25, 0.35, 0.40),
    "Other":                (0.33, 0.34, 0.33),
}

_TIER_THRESHOLDS = [(4.5, "Tier 1"), (4.0, "Tier 2"), (3.5, "Tier 3"), (3.0, "Tier 4")]

def _compute_composite(l1: int, l2: int, l3: int, asset_class: str) -> tuple[float, str]:
    """
    Deterministically compute composite score and tier from L-scores.
    Returns (score, tier_label). Falls back to equal weights if asset_class unknown.
    """
    w1, w2, w3 = SCORE_WEIGHTS.get(asset_class, (0.33, 0.34, 0.33))
    score = round(l1 * w1 + l2 * w2 + l3 * w3, 2)
    tier  = "Tier 5"
    for threshold, label in _TIER_THRESHOLDS:
        if score >= threshold:
            tier = label
            break
    return score, tier


# ── Logging helpers ────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"{PREFIX} {msg}")

def warn(msg: str):
    print(f"{PREFIX} WARNING: {msg}", file=sys.stderr)

def die(msg: str):
    print(f"{PREFIX} ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Prompt construction ────────────────────────────────────────────────────────
#
# The prompt is split into two parts for API-level prompt caching:
#
#   STATIC_SYSTEM_PROMPT  — all rules, layer guidance, output schema.
#                           Identical across every profile run → cached.
#
#   _build_user_message() — protocol identity + per-run context only.
#                           Changes each run → never cached.
#
# The static portion is sent as a system message with cache_control: ephemeral.
# The dynamic portion is sent as the user message.

STATIC_SYSTEM_PROMPT = """\
You are populating structured intelligence profiles for tokenised real-world asset (RWA) products. These profiles are published on a dashboard read by institutional allocators, credit analysts, and tokenisation researchers.

YOUR TASK: Research the protocol described in the user message using web search. Populate every field you can find credible evidence for. Return ONLY a single valid JSON object — no prose, no markdown code fences, just the JSON.

RULES:
1. Cite a source URL for every factual field you populate. No citation = leave the field null.
2. SOURCE HIERARCHY — classify your source before assigning confidence:

   PRIMARY SOURCES (high confidence allowed):
   - SEC filings (10-K, 10-Q, Form D, prospectuses, offering memoranda)
   - Audit reports from named third-party auditors
   - Attestation documents from named attestors
   - Other regulatory filings (FINRA, FCA, SFC, etc.)
   - Direct on-chain reads (Etherscan, contract calls, oracle data)
   - Independent third-party fund administrator reports

   ISSUER OFFICIAL COMMUNICATIONS (medium confidence at most):
   - Press releases (BusinessWire, PR Newswire, GlobeNewswire)
   - Company blog posts, marketing materials, fact sheets
   - Issuer Twitter/X announcements
   - Issuer-hosted documentation that is not a regulatory filing
   An issuer making a claim about its own product is NOT independent verification. Press releases can confirm corporate actions (e.g. "launched on X chain on Y date") but for analytical claims about composition, performance, or structure, confidence is 'medium' at most.

   THIRD-PARTY REPORTING (medium confidence):
   - Financial press (CoinDesk, The Block, Bloomberg, FT, Reuters)
   - Research firms (Galaxy, Steakhouse, Block Analitica, Tiger Research)
   Treat as medium unless the article clearly cites a primary document you could also verify.

   AGGREGATOR SITES (low confidence):
   - rwa.xyz, CoinGecko, DefiLlama descriptive text, Wikipedia, crypto encyclopedias
   Useful for orientation; not citeable as sole source.

   CONFIDENCE FLAG RULE: If your only source for a field is a press release or marketing material, confidence is 'medium' — never 'high'. "Official" does not mean "primary". Primary means there is independent verification or regulatory consequence for false statements.

   CONFIDENCE FOR NULL FIELDS: If you leave a field NULL (no value found, not applicable, no credible source), do NOT return a confidence entry for that field. The absence of a confidence entry signals "not applicable or not found." The presence of a confidence entry signals "value populated with this quality of evidence."

   Do not return confidence values like 'null', 'n/a', or empty strings. Either populate the field with a value AND a confidence flag, or omit both.

3. If you cannot find credible evidence for a field, return null. Do not guess.
4. When citing sources, note the observation date if the data may be stale, e.g. "(as of 2024-11)".
5. For editorial paragraphs: calm-observer voice. Short declarative sentences. No hedging words (arguably, appears, seems). No marketing language (innovative, leading, pioneering). Specific numbers and proper nouns. State what is true; do not editorialize.

RESEARCH GUIDANCE:

LAYER 1 — THE UNDERLYING:
- holdings_composition: What does the fund hold? Look for attestation reports, fact sheets, SEC filings (EDGAR). E.g. "US Treasury bills with maturities <90 days, repurchase agreements, and cash".
  PRIMARY VS MARKETING SOURCES: Prefer specific percentages from monthly attestations, fund fact sheets, or SEC filings (e.g. "76% T-bills, 22% repo, 2% cash"). Avoid generic issuer phrases like "dollar-like cash assets," "high-quality short-term instruments," or "money market portfolio." If only marketing language is available, populate the field with the marketing description but mark confidence as 'low' and note in the source citation that this is issuer-stated rather than independently verified composition.
  This applies generally: when a field's only source is the issuer's own marketing material, confidence should be 'medium' at most. Reserve 'high' for primary regulatory filings, third-party audits, attestation reports, or on-chain reads.
- wam_days: Weighted average maturity of holdings, in days. Found in fact sheets and attestation reports.
- yield_current_pct: Most recently published yield or dividend rate as a decimal percentage (e.g. 5.23 for 5.23%). Include observation date in source citation.
- yield_benchmark_pct: Relevant benchmark at the same time (3-month T-bill rate or SOFR).
- yield_gap_bps: Integer. (yield_current - yield_benchmark) × 100. Compute from the above two.
  DERIVED FIELDS: If you populate l1_yield_current_pct, you must also attempt l1_yield_benchmark_pct and l1_yield_gap_bps. Look up the 3-month US Treasury bill rate as of the same "as of" date as the yield (use FRED series DGS3MO at https://fred.stlouisfed.org/series/DGS3MO). Compute the gap as (yield_current - yield_benchmark) * 100, expressed in basis points. If you cannot determine the as-of date of the yield, mark yield itself as low confidence rather than leaving benchmark and gap as NULL.
  TIME-STAMPED FIELDS: For l1_yield_current_pct, the source citation MUST include an "as of" date in the URL or in your citation note. If the source doesn't specify when the yield was measured, do not populate the field. A yield without a date is worse than no yield at all.
- fund_administrator: Named in the prospectus or fund fact sheet. E.g. "BNY Mellon", "State Street Global Advisors".
- auditor: The external auditor of the underlying fund. E.g. "PricewaterhouseCoopers", "Deloitte".
- score (1-5): 5 = institutional-grade with frequent transparent disclosure. 4 = high quality, minor gaps. 3 = adequate with notable caveats. 2 = significant weaknesses. 1 = unsuitable.

LAYER 2 — THE WRAPPER:
- legal_structure: Exact legal form of the entity. Found in SEC Form D, offering memorandum, prospectus. E.g. "Delaware LP", "Cayman Fund", "BVI Segregated Portfolio Company".
- securities_classification: The specific securities law exemption. Found in SEC EDGAR Form D. Reg D 506(c) and 3(c)(7) are different — be precise. E.g. "Reg D 506(c)", "Reg S", "3(c)(7)", "Regulation S".
- issuer_legal_entity: The exact registered legal name of the SPV or fund entity that issues the token. Not the issuer's brand name — the entity name.
- custodian: Who physically custodies the underlying assets. E.g. "BNY Mellon", "State Street Bank", "Bank of New York Mellon".
- token_transfer_agent: Who manages the token registry and investor onboarding on-chain. E.g. "Securitize", "Tokeny", "Polymath".
- nav_cadence: How frequently NAV is computed and applied. One of: intraday, daily, weekly, monthly, irregular.
- attestation_cadence: Periodic third-party verification of reserves — this is the institutional credibility mechanism, typically performed by Big Four firms (PwC, KPMG, EY, Deloitte), fund administrators (BNY Mellon), or specialist attestors. Look in the fund's official reports, press releases announcing attestation arrangements, or the offering memorandum. One of: daily, weekly, monthly, quarterly, annual, none.
  CRITICAL: do NOT confuse periodic third-party attestation with on-chain oracle feeds. Chronicle, RedStone, Chainlink, and similar networks report NAV continuously on-chain — they are NOT attestations. If only oracle feeds exist and no periodic third-party attestation is documented, mark attestation_cadence as "none" and populate nav_oracle_cadence separately.
- attestor: Name of the firm providing the PERIODIC THIRD-PARTY ATTESTATION (not the oracle provider). E.g. "PricewaterhouseCoopers", "Deloitte", "BNY Mellon (administrator)". Leave null if no third-party periodic attestation exists.
- nav_oracle_cadence: How often NAV is reported on-chain via oracle feeds (e.g. Chronicle, RedStone, Chainlink). Distinct from third-party attestation. Look for the oracle provider's documentation or the issuer's announcements. Example values: "continuous", "hourly", "daily", "none".
- audit_cadence: External audit frequency. One of: quarterly, annual, none.
- redemption_mechanics: How investors can redeem. Include timing (T+0, T+1, T+2), cash vs in-kind, any gates or lockups.
- subscription_minimum_usd: Minimum investment as integer USD. E.g. 5000000 for $5M.
- eligible_investors: Determine which category of investors can subscribe. Choose the MOST SPECIFIC applicable value from the following set. If multiple categories apply (e.g. a product with both Reg D 506(c) and Reg S share classes), use 'mixed' and detail in transfer_restrictions.

  Valid values and when to use each:

  - 'retail': open to general retail investors. Rare for RWA products.
  - 'accredited': US accredited investors under Reg D 506(c) — individuals with $1M+ net worth excluding primary residence, or $200k+ income. Use for US-registered Reg D 506(c) offerings.
  - 'qualified_purchaser': US qualified purchasers under 3(c)(7) — typically $5M+ in investments. More restrictive than accredited. Use for US 3(c)(7) products like BUIDL.
  - 'qualified_institutional_buyer': US QIBs — institutional investors with $100M+ in securities. Used for some institutional-only products.
  - 'non_us_persons_reg_s': Products specifically structured under SEC Regulation S, targeting non-US persons and exempt from US registration. Common for Cayman/Bermuda-domiciled tokenized funds. PREFER THIS over 'restricted' when the product is genuinely Reg S structured.
  - 'non_us_persons_offshore': Products available only to non-US persons but not specifically Reg S structured. Use when clearly offshore-only but Reg S cannot be confirmed.
  - 'professional_investor': Non-US classifications including HK Professional Investor, EU MiFID II Professional Client, UK Sophisticated Investor, Singapore Accredited Investor, and similar regional categories.
  - 'restricted': Products where eligibility is operationally enforced via on-chain whitelist or KYC gate, regardless of jurisdiction. Use when the structural eligibility category is unclear but whitelist enforcement is confirmed. Do NOT use this as a catch-all for Reg S products — use 'non_us_persons_reg_s' for those.
  - 'mixed': Products with multiple share classes serving different investor categories. Detail the breakdown in transfer_restrictions.

  PREFER SPECIFICITY: If a product is genuinely Reg S, use 'non_us_persons_reg_s' rather than 'restricted'. If it is US Reg D 506(c), use 'accredited' rather than 'restricted'. This field is used for analytical filtering — collapse only when you cannot determine specificity.

  Source quality: Typically high-confidence if you find the offering memorandum, Form D, or equivalent regulatory filing. Medium-confidence if only issuer marketing material specifies. Low-confidence if inferred from secondary sources.
- transfer_restrictions: How token transfers are controlled on-chain. E.g. "KYC/AML whitelist enforced via smart contract; no permissionless secondary transfers". For Reg S products, also note: "Restricted to non-U.S. persons under Regulation S."
- bankruptcy_remote: true if the legal structure isolates investor assets from issuer bankruptcy. false if not isolated. null if unclear.
- score (1-5): 5 = bulletproof institutional-grade legal wrapper, full transparency. 4 = high quality, minor issues. 3 = adequate with clear caveats. 2 = significant structural issues. 1 = unsuitable.

LAYER 3 — ON-CHAIN:
- primary_contract: The canonical token contract address on the primary chain. Format: "0x...". Verify on Etherscan or the chain's explorer.
- multichain_contracts: Array of {"chain": "...", "address": "0x..."} objects for every chain where the token is deployed.
- smart_contract_auditor: Who audited the token's smart contracts. Look for audit reports in protocol docs, GitHub, or Certik/Quantstamp/Trail of Bits databases.
- holder_count: Number of distinct holder addresses. Check Etherscan token holders page. Include observation date in citation.
- top10_holder_pct: Query Etherscan's holders page directly: https://etherscan.io/token/{contract_address}#balances — sum the balances of the top 10 holder addresses and divide by total supply. Report as a decimal percentage with one decimal place (e.g. 87.3). This is a HIGH CONFIDENCE source. If you can access Etherscan and read the holders table, populate this field with confidence "high". For multichain tokens, focus on the chain holding the largest supply (typically the primary deployment chain).
- oracle_mechanism: How NAV is reported on-chain. E.g. "Daily price feed pushed by Securitize via off-chain oracle", "Chainlink price feed", "Rebasing mechanism".
- composability: List of OTHER tokenisation platforms or stablecoin issuers that hold this token as a balance-sheet RESERVE (e.g. Ondo Finance using BUIDL as reserves for OUSG, Ethena using BUIDL in its reserve mix, Frax holding BUIDL as treasury). This is B2B institutional adoption — one platform trusting another's product as cash-equivalent. Do NOT include DeFi lending protocols here — those go in defi_collateral_use.
- defi_collateral_use: List of DEFI LENDING PROTOCOLS that accept this token as collateral for borrowing (e.g. Aave, Morpho, Maker, Compound, Spark). These are permissionless or semi-permissionless DeFi protocols, not B2B reserve relationships. Look in DeFi protocol documentation, governance forum posts, or integration tracker sites. If the token is transfer-restricted (whitelist-only), DeFi collateral use is typically rare or absent — note this if relevant. Return null if none found.
  LIST FIELD CITATION DISCIPLINE: For composability, defi_collateral_use, and all other array fields — every entry in the list must be supported by a source you cite. Do not add entries from background knowledge or general impressions. Before finalising a list, audit each entry: is it mentioned in a source I cited? If yes, keep it. If no, either find a source and add it to citations, or remove the entry. Do NOT pad lists to look comprehensive. A shorter fully-cited list is better than a longer list with uncited entries.
  Example of failure mode: citing a source that names Ondo, Ethena, Frax — then adding Sky and M0 from general knowledge. RIGHT: if Sky and M0 belong, find and cite a separate source for them.
- secondary_market_depth: One of: none (no secondary), thin (illiquid DEX), moderate (reasonable AMM depth), deep (liquid, tight spreads).
- score (1-5): 5 = fully audited, deep composability, transparent on-chain data. 4 = high quality. 3 = adequate. 2 = limited transparency. 1 = unsuitable.

PROSE GROUNDING RULE (applies to Section 2 and Section 6):
When the editorial paragraphs reference facts that also appear as structured fields, the prose MUST use the value from the Identity section and the structured fields you populated — NOT the value from your most recent web research.

Your web research has two purposes:
1. Populating analytical fields (Layer 1, 2, 3) with cited evidence
2. Verifying claims against primary sources

Your web research is NOT a license to override the canonical Identity section in prose. If your research finds something that contradicts Identity (e.g. additional chains beyond chains_deployed, a different issuer name, an updated custodian), DO NOT incorporate the contradiction into the prose. Instead, add a discrepancy to identity_discrepancies for human review.

Example of the failure mode to avoid:
- Identity contains chains_deployed = ['Ethereum', 'Aptos', 'Arbitrum', 'Avalanche', 'Optimism', 'Polygon']
- Your research finds news that Solana and BNB Chain were added recently
- WRONG: write prose listing 8 chains
- RIGHT: write prose listing the 6 chains in chains_deployed, AND populate identity_discrepancies with:
  {"field": "chains_deployed", "current_value": [...6 chains...], "found_value": ["Solana", "BNB Chain"], "source_url": "...", "note": "Recent additions per source not yet reflected in Identity"}

When in doubt: the structured fields are canonical, your research is investigative.

SECTION 2 — EDITORIAL SUMMARY (80-120 words):
Describe the product, its intended investors, and what is structurally distinctive. Calm observer voice. Reference specific facts from the fields above. Apply the PROSE GROUNDING RULE: every chain, composability partner, and eligibility criterion named in this paragraph must match the structured fields, not your research findings. Example: "BUIDL is BlackRock's tokenised money market fund, structured as a Delaware LP and issued in token form by Securitize. It holds short-term US Treasuries, repo agreements, and cash, and distributes yield daily via balance rebasing. Launched in March 2024, it was the first tokenised MMF issued by a major TradFi asset manager and established the structural template others followed. Eligible investors are restricted to qualified purchasers; transfers are permissioned via KYC whitelist. Secondary market liquidity is thin."

SECTION 6 — COMPOSITE FRAMING (80-120 words):
Apply the PROSE GROUNDING RULE. Every factual claim must match a structured field you populated. Do not introduce new facts here that weren't already in the structured fields.

SURFACE THE ANALYTICALLY INTERESTING. Identify the 1-2 most analytically interesting facts in the structured fields and reference them directly. Examples:
- Unusual concentration (very low holder count, very high top-10 concentration)
- Unusual yield gaps (significantly above or below the benchmark)
- Unusual restrictions (transfer-restricted despite multichain deployment)
- Unusual structural choices (BVI vs Delaware, qualified purchaser vs accredited investor)

Do not just summarize the scores. Point out what is structurally distinctive, not recap the section above.

Synthesise the three layer scores into a cross-layer view. Where is the product strong, where are the weaknesses, what does this mean for an institutional allocator. Reference the specific scores you assigned. Calm observer voice.

COMPOSITE SCORE:
Tier thresholds: 4.5–5.0 = Tier 1, 4.0–4.4 = Tier 2, 3.5–3.9 = Tier 3, 3.0–3.4 = Tier 4, below 3.0 = Tier 5
Score weights by asset class:
  MMF:                  L1=30%, L2=40%, L3=30%
  Tokenised Treasuries: L1=30%, L2=40%, L3=30%
  Private Credit:       L1=35%, L2=35%, L3=30%
  Structured Credit:    L1=35%, L2=35%, L3=30%
  Commodity:            L1=25%, L2=35%, L3=40%
  Equity:               L1=25%, L2=35%, L3=40%
  Other:                L1=33%, L2=34%, L3=33%
Use the weights for the asset class specified in the Protocol Identity. The user message confirms the exact weights to apply.
Compute: composite = (l1_score × w_l1) + (l2_score × w_l2) + (l3_score × w_l3)

IDENTITY VERIFICATION:
Before populating analytical fields, cross-check the Identity section values from the user message against your primary sources. If any contradict what you find, list the discrepancies under the top-level JSON key "identity_discrepancies". Each discrepancy should be an object with: field_name, current_value, found_value, source_url.
Do NOT modify the Identity fields yourself — only flag discrepancies for human review.
If everything matches, set "identity_discrepancies" to an empty array [].

identity_discrepancies: Use ONLY when your research finds a value that contradicts the Identity section (chains_deployed, product_name, issuer, tokenisation_platform, launch_date, asset_class). These are facts that should be verified by a human before being incorporated into prose.

source_conflicts: Use when two credible sources disagree on the value of a NON-Identity field (auditor, custodian, smart contract auditor, attestor, fund administrator, etc.). Structure each entry as:
{
  "field": "l1_auditor",
  "value_a": "Cohen and Company",
  "source_a": "https://docs.hashnote.com/...",
  "value_b": "Valaston International",
  "source_b": "https://app.rwa.xyz/...",
  "note": "Hashnote docs predate Circle acquisition; RWA.xyz may reflect post-acquisition change"
}
Surface conflicts rather than picking one source — human review will resolve. Set "source_conflicts" to an empty array [] if none found.

OUTPUT FORMAT — return ONLY this JSON, with no text before or after:

{
  "section_2_editorial_summary": "string, 80-120 words",
  "layer_1": {
    "holdings_composition": "string or null",
    "wam_days": null,
    "yield_current_pct": null,
    "yield_benchmark_pct": null,
    "yield_gap_bps": null,
    "fund_administrator": "string or null",
    "auditor": "string or null",
    "score": 1,
    "score_rationale": "one sentence"
  },
  "layer_2": {
    "legal_structure": "string or null",
    "securities_classification": "string or null",
    "issuer_legal_entity": "string or null",
    "custodian": "string or null",
    "token_transfer_agent": "string or null",
    "nav_cadence": "daily",
    "attestation_cadence": "monthly",
    "attestor": "string or null",
    "nav_oracle_cadence": "continuous",
    "audit_cadence": "annual",
    "redemption_mechanics": "string or null",
    "subscription_minimum_usd": null,
    "eligible_investors": "qualified_purchaser",
    "transfer_restrictions": "string or null",
    "bankruptcy_remote": null,
    "score": 1,
    "score_rationale": "one sentence"
  },
  "layer_3": {
    "primary_contract": "0x... or null",
    "multichain_contracts": null,
    "smart_contract_auditor": "string or null",
    "holder_count": null,
    "top10_holder_pct": null,
    "oracle_mechanism": "string or null",
    "composability": null,
    "defi_collateral_use": null,
    "secondary_market_depth": "none",
    "score": 1,
    "score_rationale": "one sentence"
  },
  "section_6_composite_framing": "string, 80-120 words",
  "composite": {
    "score": 0.0,
    "tier": "Tier 1"
  },
  "field_confidence": {
    "l1_holdings_composition": "high"
  },
  "source_citations": [
    {"field": "l1_holdings_composition", "url": "https://example.com"}
  ],
  "identity_discrepancies": [],
  "source_conflicts": []
}"""


def _build_user_message(profile: dict) -> str:
    """Build the short per-run user message containing only protocol-specific context."""
    name         = profile["product_name"]
    slug         = profile.get("defillama_slug", "")
    issuer       = profile.get("issuer_name") or "unknown issuer"
    platform     = profile.get("tokenisation_platform") or "unknown platform"
    asset_cls    = profile.get("asset_class") or "Other"
    chains_str   = ", ".join(profile.get("chains") or []) or "unknown"
    legal_struct = profile.get("l2_legal_structure") or "not set"
    launch_dt    = profile.get("launch_date") or "not set"
    today        = date.today().isoformat()

    w = SCORE_WEIGHTS.get(asset_cls, (0.33, 0.34, 0.33))
    w_str = f"L1={w[0]:.0%}, L2={w[1]:.0%}, L3={w[2]:.0%}"

    return f"""Research and populate the analytical sections for this RWA protocol.

PROTOCOL IDENTITY:
- Product name: {name}
- Issuer: {issuer}
- Tokenisation platform: {platform}
- Asset class: {asset_cls}
- Chains deployed: {chains_str}
- DefiLlama slug: {slug}
- Research date: {today}
- Legal structure (current value): {legal_struct}
- Launch date: {launch_dt}

Asset class weights for {asset_cls}: {w_str}
Cross-check the Identity values above against your primary sources and populate identity_discrepancies for any contradictions."""


# ── Agent call ─────────────────────────────────────────────────────────────────

def _call_agent(user_msg: str, verbose: bool = False):
    """
    Call Claude with web search enabled and prompt caching on the static system prompt.
    Returns (raw_text, usage) where usage is the response.usage object.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    log("Calling Claude with web search enabled...")
    t0 = time.time()

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=[
            {
                "type": "text",
                "text": STATIC_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 25,
        }],
    )

    elapsed = time.time() - t0
    usage   = response.usage
    total   = usage.input_tokens + usage.output_tokens
    log(f"Agent response received ({total:,} tokens, {elapsed:.1f}s)")

    # Extract text from all text blocks in the response
    text_parts = []
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            text_parts.append(block.text)

    raw_text = "\n".join(text_parts)

    if verbose:
        print("\n── RAW AGENT OUTPUT ──────────────────────────────────")
        print(raw_text)
        print("──────────────────────────────────────────────────────\n")

    return raw_text, usage


# ── JSON extraction ────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """
    Extract and parse the JSON object from the agent's response.
    Strips markdown fences if present. Raises ValueError on failure.
    """
    text = raw.strip()

    # Strip markdown fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Find the outermost JSON object
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in agent response")

    # Walk forward to find the matching closing brace
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

    if end == -1:
        raise ValueError("Unbalanced JSON braces in agent response")

    return json.loads(text[start:end])


# ── Validation ─────────────────────────────────────────────────────────────────

def _validate(data: dict) -> list[str]:
    """
    Validate the agent's JSON structure. Returns list of error strings.
    Empty list = valid.
    """
    errors = []

    required_top = ["section_2_editorial_summary", "layer_1", "layer_2",
                    "layer_3", "section_6_composite_framing", "composite"]
    for key in required_top:
        if key not in data:
            errors.append(f"Missing top-level key: {key}")

    # Validate scores
    for layer_key in ["layer_1", "layer_2", "layer_3"]:
        layer = data.get(layer_key)
        if not isinstance(layer, dict):
            errors.append(f"{layer_key} must be a dict")
            continue
        score = layer.get("score")
        if score is None or not isinstance(score, int) or not (1 <= score <= 5):
            errors.append(f"{layer_key}.score must be integer 1-5, got: {score!r}")
        if not layer.get("score_rationale"):
            errors.append(f"{layer_key}.score_rationale is required")

    # Validate composite
    composite = data.get("composite", {})
    if not isinstance(composite, dict):
        errors.append("composite must be a dict")
    else:
        comp_score = composite.get("score")
        if comp_score is None:
            errors.append("composite.score is required")
        comp_tier = composite.get("tier")
        if comp_tier not in VALID_COMPOSITE_TIER:
            errors.append(f"composite.tier must be one of {VALID_COMPOSITE_TIER}, got: {comp_tier!r}")

    # Validate enum fields in layer_2 (only if non-null)
    l2 = data.get("layer_2", {}) or {}
    enum_checks = {
        "nav_cadence":         VALID_NAV_CADENCE,
        "attestation_cadence": VALID_ATTEST_CADENCE,
        "audit_cadence":       VALID_AUDIT_CADENCE,
        "eligible_investors":  VALID_ELIGIBLE_INVESTORS,
    }
    for field, valid_set in enum_checks.items():
        val = l2.get(field)
        if val is not None and val not in valid_set:
            errors.append(f"layer_2.{field} invalid value: {val!r}. Valid: {valid_set}")

    l3 = data.get("layer_3", {}) or {}
    sec_depth = l3.get("secondary_market_depth")
    if sec_depth is not None and sec_depth not in VALID_SECONDARY_DEPTH:
        errors.append(f"layer_3.secondary_market_depth invalid: {sec_depth!r}")

    # Validate source_citations structure
    citations = data.get("source_citations", [])
    if citations and not isinstance(citations, list):
        errors.append("source_citations must be a list")
    for i, c in enumerate(citations or []):
        if not isinstance(c, dict) or "field" not in c or "url" not in c:
            errors.append(f"source_citations[{i}] must have 'field' and 'url' keys")
            continue
        if not str(c.get("url", "")).startswith("http"):
            errors.append(f"source_citations[{i}].url doesn't look like a URL: {c['url']!r}")

    return errors


# ── Schema mapping ─────────────────────────────────────────────────────────────

def _to_int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _to_float_or_none(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_key(key: str, valid_cols: set) -> Optional[str]:
    """
    Normalize a field name to a known DB column name.

    1. If the key already matches a valid column, return it as-is.
    2. If stripping an l1_/l2_/l3_ prefix produces a match, return the stripped name.
    3. Otherwise return None (caller should warn and drop the key).
    """
    if key in valid_cols:
        return key
    for prefix in LAYER_PREFIXES:
        if key.startswith(prefix):
            stripped = key[len(prefix):]
            if stripped in valid_cols:
                return stripped
    return None


def _map_to_schema(agent: dict, existing: dict) -> dict:
    """
    Map the validated agent JSON to protocol_profiles column names.
    - Non-null agent values always win.
    - Null agent values do NOT overwrite existing non-null DB values.
    - field_confidence and source_citations are merged (not replaced).
    """
    l1 = agent.get("layer_1") or {}
    l2 = agent.get("layer_2") or {}
    l3 = agent.get("layer_3") or {}
    composite = agent.get("composite") or {}

    raw_update = {
        # Section 2
        "editorial_summary":          agent.get("section_2_editorial_summary"),

        # L1
        "l1_holdings_composition":    l1.get("holdings_composition"),
        "l1_wam_days":                _to_int_or_none(l1.get("wam_days")),
        "l1_yield_current":           _to_float_or_none(l1.get("yield_current_pct")),
        "l1_yield_benchmark":         _to_float_or_none(l1.get("yield_benchmark_pct")),
        "l1_yield_gap_bps":           _to_int_or_none(l1.get("yield_gap_bps")),
        "l1_fund_administrator":      l1.get("fund_administrator"),
        "l1_auditor":                 l1.get("auditor"),
        "l1_score":                   _to_int_or_none(l1.get("score")),
        "l1_score_rationale":         l1.get("score_rationale"),

        # L2
        "l2_legal_structure":         l2.get("legal_structure"),
        "l2_securities_classification": l2.get("securities_classification"),
        "l2_issuer_legal_entity":     l2.get("issuer_legal_entity"),
        "l2_custodian":               l2.get("custodian"),
        "l2_token_transfer_agent":    l2.get("token_transfer_agent"),
        "l2_nav_cadence":             l2.get("nav_cadence"),
        "l2_attestation_cadence":     l2.get("attestation_cadence"),
        "l2_attestor":                l2.get("attestor"),
        "l2_audit_cadence":           l2.get("audit_cadence"),
        "l2_redemption_mechanics":    l2.get("redemption_mechanics"),
        "l2_subscription_minimum_usd": _to_int_or_none(l2.get("subscription_minimum_usd")),
        "l2_eligible_investors":      l2.get("eligible_investors"),
        "l2_transfer_restrictions":   l2.get("transfer_restrictions"),
        "l2_bankruptcy_remote":       l2.get("bankruptcy_remote"),
        "l2_score":                   _to_int_or_none(l2.get("score")),
        "l2_score_rationale":         l2.get("score_rationale"),

        # L3
        "l3_primary_contract":        l3.get("primary_contract"),
        "l3_multichain_contracts":    l3.get("multichain_contracts"),
        "l3_smart_contract_auditor":  l3.get("smart_contract_auditor"),
        "l3_holder_count":            _to_int_or_none(l3.get("holder_count")),
        "l3_top10_holder_pct":        _to_float_or_none(l3.get("top10_holder_pct")),
        "l3_oracle_mechanism":        l3.get("oracle_mechanism"),
        "l3_composability":           l3.get("composability"),
        "defi_collateral_use":        l3.get("defi_collateral_use"),
        "l3_secondary_market_depth":  l3.get("secondary_market_depth"),
        "l3_score":                   _to_int_or_none(l3.get("score")),
        "l3_score_rationale":         l3.get("score_rationale"),

        # New fields (from migration 003)
        "nav_oracle_cadence": l2.get("nav_oracle_cadence"),

        # Composite — score and tier are computed deterministically below;
        # the agent's values are informational only and will be overridden.
        "composite_score":  _to_float_or_none(composite.get("score")),
        "composite_tier":   composite.get("tier"),
        "composite_framing": agent.get("section_6_composite_framing"),

        # Metadata
        "last_verified_date": date.today().isoformat(),
    }

    # Remove identity fields (should never be in raw_update, but be safe)
    raw_update = {k: v for k, v in raw_update.items() if k not in IDENTITY_FIELDS}

    # Rule: null agent values do NOT overwrite existing non-null DB values
    update = {}
    for key, value in raw_update.items():
        if value is not None:
            update[key] = value
        elif existing.get(key) is None:
            # Both null — no-op, skip to avoid unnecessary writes
            pass
        # else: agent returned null but DB has a value — skip (don't overwrite)

    # ── Deterministic composite override ─────────────────────────────────────
    # The agent computes composite_score/tier from L-scores, but temperature=1
    # allows numerical variance. The script always overrides with the deterministic
    # calculation from SCORE_WEIGHTS so the composite is reproducible regardless
    # of what the agent returned.
    l1_s = update.get("l1_score") or existing.get("l1_score")
    l2_s = update.get("l2_score") or existing.get("l2_score")
    l3_s = update.get("l3_score") or existing.get("l3_score")
    asset_class = existing.get("asset_class") or ""
    if l1_s is not None and l2_s is not None and l3_s is not None and asset_class:
        det_score, det_tier = _compute_composite(int(l1_s), int(l2_s), int(l3_s), asset_class)
        agent_score = update.get("composite_score")
        if agent_score is not None and abs(det_score - float(agent_score)) > 0.01:
            log(f"Composite override: agent={agent_score} → deterministic={det_score} "
                f"(L1={l1_s}, L2={l2_s}, L3={l3_s}, {asset_class})")
        update["composite_score"] = det_score
        update["composite_tier"]  = det_tier

    # ── Merge field_confidence ────────────────────────────────────────────────
    # Build the set of valid column names from the existing row (all known DB cols)
    valid_cols     = set(existing.keys()) | set(update.keys())
    existing_conf  = existing.get("field_confidence") or {}
    new_conf_raw   = agent.get("field_confidence") or {}
    merged_conf    = dict(existing_conf)
    # Values that mean "field is null / not applicable" — skip silently
    _SKIP_CONFIDENCE = {None, "null", "n/a", ""}

    for field, conf in new_conf_raw.items():
        # Skip confidence entries for null/n/a values silently
        if conf in _SKIP_CONFIDENCE:
            continue
        norm = _normalize_key(field, valid_cols)
        if norm is None:
            warn(f"confidence key '{field}' doesn't map to any column; dropping")
            continue
        if norm != field:
            log(f"confidence key '{field}' normalized to '{norm}'")
        if conf in VALID_CONFIDENCE:
            merged_conf[norm] = conf
        else:
            warn(f"Invalid confidence '{conf}' for field '{norm}' → treating as 'low'")
            merged_conf[norm] = "low"
    update["field_confidence"] = merged_conf

    # ── Merge source_citations ────────────────────────────────────────────────
    existing_cites = existing.get("source_citations") or []
    new_cites      = agent.get("source_citations") or []
    # Index existing by field name; new citations overwrite same-field existing
    by_field = {c["field"]: c for c in existing_cites if isinstance(c, dict) and "field" in c}
    for c in new_cites:
        if not (isinstance(c, dict) and "field" in c and "url" in c):
            continue
        raw_field = c["field"]
        norm = _normalize_key(raw_field, valid_cols)
        if norm is None:
            warn(f"citation field '{raw_field}' doesn't map to any column; dropping")
            continue
        if norm != raw_field:
            log(f"citation field '{raw_field}' normalized to '{norm}'")
        by_field[norm] = {**c, "field": norm}
    update["source_citations"] = list(by_field.values())

    # ── Merge source_conflicts ────────────────────────────────────────────────
    # Additive: new conflicts are appended; duplicates (same field+value_a+value_b) dropped
    existing_conflicts = existing.get("source_conflicts") or []
    new_conflicts      = agent.get("source_conflicts") or []
    if new_conflicts and isinstance(new_conflicts, list):
        seen = {
            (c.get("field"), c.get("value_a"), c.get("value_b"))
            for c in existing_conflicts if isinstance(c, dict)
        }
        for c in new_conflicts:
            if not (isinstance(c, dict) and "field" in c):
                continue
            key = (c.get("field"), c.get("value_a"), c.get("value_b"))
            if key not in seen:
                existing_conflicts.append(c)
                seen.add(key)
    update["source_conflicts"] = existing_conflicts or []

    return update


# ── Summary stats ──────────────────────────────────────────────────────────────

def _summarise(update: dict, agent: dict):
    """Print population and confidence stats."""
    # Count non-null analytical fields (exclude meta, confidence, citations, identity)
    skip = IDENTITY_FIELDS | {"field_confidence", "source_citations", "last_verified_date",
                               "editorial_summary", "composite_framing"}
    analytical = {k: v for k, v in update.items() if k not in skip}
    populated  = sum(1 for v in analytical.values() if v is not None)

    # Total analytical fields in schema
    total_analytical = 34  # L1(9) + L2(16) + L3(10) – 1 (scores are included)

    conf   = agent.get("field_confidence") or {}
    n_high = sum(1 for v in conf.values() if v == "high")
    n_med  = sum(1 for v in conf.values() if v == "medium")
    n_low  = sum(1 for v in conf.values() if v == "low")
    n_null = total_analytical - len(conf)

    log(f"Validation passed: {populated}/{total_analytical} analytical fields populated")
    log(f"Confidence breakdown: {n_high} high, {n_med} medium, {n_low} low, {n_null} NULL")

    low_fields = [f for f, v in conf.items() if v == "low"]
    null_count = total_analytical - populated
    if low_fields:
        log(f"Suggested human review: {len(low_fields)} low-confidence fields: {', '.join(low_fields)}")
    if null_count:
        log(f"Unpopulated fields ({null_count}): consider re-running or manual entry")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI profile gathering — populate protocol_profiles from web research")
    parser.add_argument("slug", help="DefiLlama slug, e.g. blackrock-buidl")
    parser.add_argument("--dry-run", action="store_true",
                        help="Research and parse, but do not write to Supabase")
    parser.add_argument("--verbose", action="store_true",
                        help="Print full raw agent response")
    args = parser.parse_args()

    # ── Preflight checks ──────────────────────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        die("ANTHROPIC_API_KEY not set. Add it to .env or export it in your shell.")

    # ── Step 1: Load existing profile ─────────────────────────────────────────
    log(f"Loading profile: {args.slug}")
    profile = read_profile(args.slug)
    if not profile:
        die(f"No profile found for slug '{args.slug}'. Create a stub row first via profile_io.write_profile().")
    if not profile.get("product_name"):
        die("Profile exists but product_name is empty. Populate the Identity section first.")

    log(f"Identity loaded: {profile['product_name']} ({profile.get('common_name') or 'no ticker'})")

    # ── Step 2: Build user message and call Claude ────────────────────────────
    user_msg = _build_user_message(profile)
    raw, usage = _call_agent(user_msg, verbose=args.verbose)

    # ── Step 3: Extract JSON ───────────────────────────────────────────────────
    log("Parsing response...")
    try:
        agent_data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        if not args.verbose:
            print("\n── RAW AGENT OUTPUT (first 2000 chars) ───────────────")
            print(raw[:2000])
            print("──────────────────────────────────────────────────────\n")
        die(f"Failed to parse agent JSON: {e}")

    # ── Step 3b: Surface identity discrepancies ───────────────────────────────
    discrepancies = agent_data.get("identity_discrepancies") or []
    if discrepancies:
        print("\n── IDENTITY DISCREPANCIES (human review required) ────")
        for d in discrepancies:
            print(f"  Field:     {d.get('field_name') or d.get('field')}")
            print(f"  Current:   {d.get('current_value')}")
            print(f"  Found:     {d.get('found_value')}")
            print(f"  Source:    {d.get('source_url')}")
            print()
        print("──────────────────────────────────────────────────────\n")
    else:
        log("Identity verification passed — no discrepancies found")

    # ── Step 3c: Surface source conflicts ─────────────────────────────────────
    conflicts = agent_data.get("source_conflicts") or []
    if conflicts:
        print("\n── SOURCE CONFLICTS (human review required) ──────────")
        for c in conflicts:
            print(f"  Field:     {c.get('field')}")
            print(f"  Value A:   {c.get('value_a')}  ({c.get('source_a')})")
            print(f"  Value B:   {c.get('value_b')}  ({c.get('source_b')})")
            if c.get("note"):
                print(f"  Note:      {c.get('note')}")
            print()
        print("──────────────────────────────────────────────────────\n")

    # ── Step 4: Validate ──────────────────────────────────────────────────────
    errors = _validate(agent_data)
    if errors:
        print("\n── VALIDATION ERRORS ─────────────────────────────────")
        for e in errors:
            print(f"  • {e}")
        print("──────────────────────────────────────────────────────\n")
        die(f"{len(errors)} validation error(s). Aborting.")

    # ── Step 5: Map to schema and merge ───────────────────────────────────────
    update = _map_to_schema(agent_data, profile)
    update["defillama_slug"] = args.slug  # ensure the write targets the right row

    _summarise(update, agent_data)

    # ── Step 5b: Ensure verified_by is populated ─────────────────────────────
    # (runs before cost log so both appear together at the end)
    if not update.get("verified_by") or str(update["verified_by"]).strip() in ("", "—", "-"):
        update["verified_by"] = DEFAULT_VERIFIER

    # ── Step 5c: Cost logging ─────────────────────────────────────────────────
    cached_in   = getattr(usage, "cache_read_input_tokens",    0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens",0) or 0
    fresh_in    = usage.input_tokens or 0   # API returns fresh-only input separately
    out_tokens  = usage.output_tokens or 0
    # cache_write charged at regular input rate; cache_read charged at 90%-discounted rate
    cost = (cached_in * _PRICE_CACHED) + (fresh_in * _PRICE_INPUT) + (cache_write * _PRICE_INPUT) + (out_tokens * _PRICE_OUTPUT)
    log(f"Token usage: {cached_in:,} cached input, {fresh_in:,} fresh input, "
        f"{cache_write:,} cache write, {out_tokens:,} output. "
        f"Estimated cost: ${cost:.4f}")

    # ── Step 6: Write or dry-run ──────────────────────────────────────────────
    if args.dry_run:
        log("--dry-run: skipping Supabase write")
        print("\n── DRY-RUN OUTPUT (what would be written) ────────────")
        print(json.dumps(update, indent=2, default=str))
        print("──────────────────────────────────────────────────────")
        print("\n── FULL AGENT JSON ───────────────────────────────────")
        print(json.dumps(agent_data, indent=2, default=str))
        print("──────────────────────────────────────────────────────")
    else:
        log("Writing to Supabase...")
        write_profile(update)
        supa_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        log(f"Done. Verify: python -m sync.profile_io read {args.slug}")
        log(f"Or in Supabase: {supa_url}/dashboard/project/_/editor")


if __name__ == "__main__":
    main()
