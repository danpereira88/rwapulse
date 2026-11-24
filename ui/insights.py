"""
Analysis behind the editorial pages (Overview, Concentration, New Roster,
What Didn't Happen, What We're Watching) — a local rebuild of the Lovable
"RWA Intel" dashboard at rwa-insight-hub.lovable.app.

Everything is computed on the weekly dl_protocols snapshots.

Gap-filling: from 2026-09-20 DefiLlama stopped returning TVL for most of the
largest RWA products (BUIDL, USYC, Ondo, Spiko, gold tokens, Centrifuge...).
Where a protocol has no DefiLlama TVL but its token(s) are in our CoinGecko
snapshots, we use CoinGecko market cap scaled by the protocol's last observed
DefiLlama-TVL / CoinGecko-mcap ratio. Every row carries its source, and each
page reports how much of the total is filled or still unreported.
"""

from collections import defaultdict

# Same exclusions the Supabase dashboard sync used (sync/sync_to_supabase.py)
EXCLUDED = ("sky-rwa", "blockchain-capital", "ondo-global-markets")

# Commodity / equity wrappers removed for the "yield-bearing" universe
# (reports/FINAL_FIGURES.md universe definition)
NON_YIELD = (
    "asa.gold", "cache.gold", "comtech-gold", "digital-gold", "gemswap-by-golden-swan-solutions",
    "gold-dao", "goldfinger", "goldstation-rwa", "meld-gold", "paxos-gold", "tether-gold",
    "pleasing-gold", "xstocks", "prestocks", "matrixdock-xaum",
)

# DefiLlama slug -> CoinGecko ids used to fill missing TVL
CG_MAP = {
    "blackrock-buidl": ["blackrock-usd-institutional-digital-liquidity-fund"],
    "circle-usyc": ["hashnote-usyc"],
    "ondo-yield-assets": ["ousg", "ondo-us-dollar-yield"],
    "tether-gold": ["tether-gold"],
    "paxos-gold": ["pax-gold"],
    "spiko": ["spiko-us-t-bills-money-market-fund", "eutbl", "spiko-amundi-overnight-swap-fund-eur", "safo"],
    "anemoy-capital": ["janus-henderson-anemoy-aaa-clo-fund"],
    "invesco-ustb": ["superstate-short-duration-us-government-securities-fund-ustb"],
    # WisdomTree and OpenEden TBILL: CoinGecko reports $0 market cap, so they
    # can't be filled and stay "missing". Centrifuge is filled from its own
    # subgraph (sg_centrifuge_assets), which matched DefiLlama to 0.1% on 2026-09-13.
}

RATIO_ANCHOR = "2026-09-13"  # last snapshot before DefiLlama dropped the large products

# New Roster: the six products on the Lovable page
ROSTER = [
    {"key": "buidl", "name": "BlackRock BUIDL", "parent": "BlackRock", "kind": "Money market fund",
     "slug": "blackrock-buidl", "cg": ["blackrock-usd-institutional-digital-liquidity-fund"],
     "blurb": "Tokenised US Treasury money market fund distributed via Securitize; the benchmark later products are measured against."},
    {"key": "usyc", "name": "Circle USYC", "parent": "Circle", "kind": "Money market fund",
     "slug": "circle-usyc", "cg": ["hashnote-usyc"],
     "blurb": "Short-duration Treasury and reverse-repo fund acquired from Hashnote; Circle's extension from stablecoins into tokenised cash."},
    {"key": "ousg", "name": "Ondo OUSG", "parent": "Ondo Finance", "kind": "Money market fund",
     "slug": None, "cg": ["ousg"],
     "blurb": "Institutional short-term Treasuries product that wraps several funds, BUIDL among them, into one tokenised interest."},
    {"key": "usdy", "name": "Ondo USDY", "parent": "Ondo Finance", "kind": "Tokenised Treasuries",
     "slug": None, "cg": ["ondo-us-dollar-yield"],
     "blurb": "Permissionless, non-US yield-bearing note issued across many chains."},
    {"key": "maple", "name": "Maple Finance", "parent": "Maple Labs", "kind": "Private credit",
     "slug": "maple", "cg": [],
     "blurb": "Institutional on-chain lending to vetted borrowers; yield comes from credit spreads rather than government paper."},
    {"key": "centrifuge", "name": "Centrifuge", "parent": "Centrifuge Network Foundation", "kind": "Structured credit",
     "slug": "centrifuge-protocol", "cg": [],
     "blurb": "Infrastructure for tokenising real-world asset funds through per-pool SPVs, close to traditional securitisation."},
]

# Published long-run baseline (reports/DRAFTING_COMPANION.md, data to 2026-05-13).
# The daily history these came from was later deleted by the pipeline, so they
# are shown as fixed reference figures.
BASELINE = {
    "as_of": "2026-05-13",
    "yield": {"start": "2024-05-15", "top5_start": 76.8, "top5_end": 51.4, "hhi_start": 1991, "hhi_end": 682,
              "tvl_start_date": "2024-07-09", "tvl_start": 2.194e9, "tvl_end": 24.200e9},
    "broad": {"start": "2024-05-15", "top5_start": 76.5, "top5_end": 47.7, "tvl_start": 1.592e9, "tvl_end": 29.909e9},
    "eth_share": {"start": 68.0, "end": 68.4},
}


def _q(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def universe(con):
    """Rows: snapshot_date, slug, name, category, first_chain, tvl, source, yield_bearing."""
    map_rows = ", ".join(f"('{s}', '{c}')" for s, ids in CG_MAP.items() for c in ids)
    excl = ", ".join(f"'{s}'" for s in EXCLUDED)
    nonyield = ", ".join(f"'{s}'" for s in NON_YIELD)
    # On-chain supply × price (ingest/rwa_supply.py). Used ahead of the scaled
    # CoinGecko fill when every chain was read, or when it lands within 0.9–1.5×
    # of CoinGecko's market cap (so a missing minor chain doesn't block it).
    has_oc = con.execute("SELECT count(*) FROM information_schema.tables "
                         "WHERE table_name = 'rwa_product_aum'").fetchone()[0] > 0
    # Matched to the nearest dl_protocols snapshot within 2 days, so a run that
    # crosses midnight still lines up.
    oc_cte = (f"""oc_raw AS (SELECT slug, snapshot_date, max(value_usd) AS v FROM rwa_product_aum
                 WHERE value_usd > 0 AND (complete OR vs_coingecko BETWEEN 0.9 AND 1.5)
                   AND snapshot_date LIKE '20%' GROUP BY 1, 2),
    oc AS (SELECT d.snapshot_date, o.slug,
                  arg_min(o.v, abs(date_diff('day', CAST(o.snapshot_date AS DATE), CAST(d.snapshot_date AS DATE)))) AS v
           FROM (SELECT DISTINCT snapshot_date FROM dl_protocols WHERE snapshot_date LIKE '20%') d
           JOIN oc_raw o ON abs(date_diff('day', CAST(o.snapshot_date AS DATE), CAST(d.snapshot_date AS DATE))) <= 2
           GROUP BY 1, 2),""" if has_oc else
              "oc AS (SELECT NULL::VARCHAR AS slug, NULL::VARCHAR AS snapshot_date, NULL::DOUBLE AS v WHERE false),")
    sql = f"""
    WITH map(slug, coingecko_id) AS (VALUES {map_rows}),
    {oc_cte}
    cg AS (
        SELECT snapshot_date, coingecko_id, max(market_cap_usd) AS mc FROM (
            SELECT snapshot_date, coingecko_id, market_cap_usd FROM cg_rwa_category
            UNION ALL SELECT snapshot_date, coingecko_id, market_cap_usd FROM cg_rwa_tokens)
        WHERE snapshot_date LIKE '20%' GROUP BY 1, 2),
    m AS (SELECT map.slug, cg.snapshot_date, sum(cg.mc) AS mc, 'coingecko' AS fill_src
          FROM map JOIN cg USING (coingecko_id) GROUP BY 1, 2
          UNION ALL
          SELECT 'centrifuge-protocol', snapshot_date, sum(total_issuance * token_price), 'centrifuge subgraph'
          FROM sg_centrifuge_assets WHERE snapshot_date LIKE '20%' GROUP BY 2),
    base AS (SELECT snapshot_date, slug, name, category, chains, tvl_usd FROM dl_protocols
             WHERE snapshot_date LIKE '20%' AND slug NOT IN ({excl})),
    -- Calibrate on the last snapshot before DefiLlama's data broke (2026-09-13),
    -- so a later bad DefiLlama figure can't drag the scale factor with it.
    ratio AS (SELECT b.slug, coalesce(
                  arg_max(b.tvl_usd / m.mc, b.snapshot_date) FILTER (WHERE b.snapshot_date <= '{RATIO_ANCHOR}'),
                  arg_max(b.tvl_usd / m.mc, b.snapshot_date)) AS r
              FROM base b JOIN m USING (slug, snapshot_date)
              WHERE b.tvl_usd > 0 AND m.mc > 0 GROUP BY 1),
    lastknown AS (SELECT slug, snapshot_date,
                    last_value(tvl_usd IGNORE NULLS) OVER w AS lk,
                    -- chains also went blank for the dropped products; carry the last list
                    last_value(NULLIF(chains, '') IGNORE NULLS) OVER w AS chains_lk
                  FROM base
                  WINDOW w AS (PARTITION BY slug ORDER BY snapshot_date
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))
    SELECT b.snapshot_date, b.slug, b.name, b.category,
           split_part(coalesce(lk.chains_lk, ''), ',', 1) AS first_chain,
           m.mc * r.r AS cross_check,
           CASE WHEN b.tvl_usd IS NOT NULL THEN b.tvl_usd
                WHEN oc.v IS NOT NULL THEN oc.v
                WHEN m.mc IS NOT NULL AND r.r IS NOT NULL THEN m.mc * r.r END AS tvl,
           CASE WHEN b.tvl_usd IS NOT NULL THEN 'defillama'
                WHEN oc.v IS NOT NULL THEN 'onchain'
                WHEN m.mc IS NOT NULL AND r.r IS NOT NULL THEN m.fill_src
                ELSE 'missing' END AS source,
           CASE WHEN b.tvl_usd IS NULL THEN lk.lk END AS last_known,
           NOT (b.slug IN ({nonyield})
                OR (lower(b.name) LIKE '%gold%' AND b.slug NOT LIKE 'goldfinch%')
                OR lower(b.name) LIKE '%stock%') AS yield_bearing
    FROM base b
    LEFT JOIN oc USING (slug, snapshot_date)
    LEFT JOIN m USING (slug, snapshot_date)
    LEFT JOIN ratio r USING (slug)
    LEFT JOIN lastknown lk USING (slug, snapshot_date)
    """
    return _q(con, sql)


def _by_date(rows, yield_only):
    out = defaultdict(list)
    for r in rows:
        if yield_only and not r["yield_bearing"]:
            continue
        out[r["snapshot_date"]].append(r)
    return dict(sorted(out.items()))


def _stats(rows, carry_missing=False):
    """carry_missing: count unreported protocols at their last DefiLlama value
    (a sensitivity check — what the figure would be if they had held flat)."""
    if carry_missing:
        rows = [dict(r, tvl=r["last_known"]) if r["source"] == "missing" and r["last_known"] else r
                for r in rows]
    live = [r for r in rows if r["tvl"]]
    total = sum(r["tvl"] for r in live)
    filled = sum(r["tvl"] for r in live if r["source"] not in ("defillama", "missing"))
    missing = sum((r["last_known"] or 0) for r in rows if r["source"] == "missing")
    ranked = sorted(live, key=lambda r: -r["tvl"])
    shares = [r["tvl"] / total for r in ranked] if total else []
    return {
        "total": total, "filled": filled, "missing_last_known": missing,
        "n": len(live), "top5": sum(shares[:5]) * 100, "hhi": sum((s * 100) ** 2 for s in shares),
        "ranked": ranked,
    }


def _coverage(st):
    return {"total": st["total"], "filled": st["filled"], "missing": st["missing_last_known"],
            "filled_pct": st["filled"] / st["total"] * 100 if st["total"] else 0}


def _row(r, total=None):
    d = {"slug": r["slug"], "name": r["name"], "category": r["category"], "chain": r["first_chain"],
         "tvl": r["tvl"], "source": r["source"], "cross_check": r.get("cross_check"),
         "disputed": bool(r["source"] == "defillama" and r["tvl"] and r.get("cross_check")
                          and abs(r["cross_check"] / r["tvl"] - 1) > 0.5)}
    if total:
        d["share"] = r["tvl"] / total * 100
    return d


# ── Pages ────────────────────────────────────────────────────────────────────

def page_overview(con):
    rows = universe(con)
    yb, br = _by_date(rows, True), _by_date(rows, False)
    dates = list(yb)
    latest, first = dates[-1], dates[0]
    s_latest, s_first = _stats(yb[latest]), _stats(yb[first])
    b_latest = _stats(br[latest])
    cats = defaultdict(float)
    for r in yb[latest]:
        if r["tvl"]:
            cats[r["category"] or "Other"] += r["tvl"]
    return {
        "latest": latest, "first": first,
        "yield_total": s_latest["total"], "yield_first": s_first["total"],
        "broad_total": b_latest["total"], "gold_total": b_latest["total"] - s_latest["total"],
        "n_protocols": s_latest["n"],
        "coverage": _coverage(s_latest),
        "series": [[d, _stats(v)["total"], _stats(br[d])["total"], _stats(v, carry_missing=True)["total"]]
                   for d, v in yb.items()],
        "filled_series": [[d, _stats(v)["filled"]] for d, v in yb.items()],
        "categories": sorted(([k, v, v / s_latest["total"] * 100] for k, v in cats.items()), key=lambda x: -x[1]),
        "top": [_row(r, s_latest["total"]) for r in s_latest["ranked"][:12]],
        "baseline": BASELINE,
    }


def page_concentration(con):
    rows = universe(con)
    yb, br = _by_date(rows, True), _by_date(rows, False)
    series = []
    for d in yb:
        a, b = _stats(yb[d]), _stats(br[d])
        cm = _stats(yb[d], carry_missing=True)
        series.append({"date": d, "top5": a["top5"], "hhi": a["hhi"], "top5_broad": b["top5"],
                       "hhi_broad": b["hhi"], "filled_pct": _coverage(a)["filled_pct"],
                       "top5_carry": cm["top5"], "hhi_carry": cm["hhi"]})
    latest = list(yb)[-1]
    st = _stats(yb[latest])
    # Who was top-5 on each snapshot, to show roster churn
    rosters = {d: [r["name"] for r in _stats(v)["ranked"][:5]] for d, v in yb.items()}
    return {"latest": latest, "series": series, "baseline": BASELINE,
            "top10": [_row(r, st["total"]) for r in st["ranked"][:10]],
            "roster_first": rosters[list(rosters)[0]], "roster_latest": rosters[latest],
            "coverage": _coverage(st)}


def page_roster(con):
    rows = universe(con)
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["slug"]].append(r)
    cg = _q(con, """SELECT snapshot_date, coingecko_id, max(market_cap_usd) AS mc FROM (
                      SELECT snapshot_date, coingecko_id, market_cap_usd FROM cg_rwa_category
                      UNION ALL SELECT snapshot_date, coingecko_id, market_cap_usd FROM cg_rwa_tokens)
                    WHERE snapshot_date LIKE '20%' GROUP BY 1, 2 ORDER BY 1""")
    cg_by = defaultdict(dict)
    for r in cg:
        cg_by[r["coingecko_id"]][r["snapshot_date"]] = r["mc"]
    cards = []
    for p in ROSTER:
        if p["slug"] == "centrifuge-protocol":
            pts = [[r["snapshot_date"], r["tvl"]] for r in sorted(by_slug[p["slug"]], key=lambda r: r["snapshot_date"])]
            source, metric = "defillama → centrifuge subgraph", "TVL"
        elif p["cg"]:
            dates = sorted(set().union(*[cg_by[c].keys() for c in p["cg"]]))
            pts = [[d, sum(cg_by[c].get(d) or 0 for c in p["cg"])] for d in dates]
            source, metric = "coingecko", "Market cap"
        else:
            pts = [[r["snapshot_date"], r["tvl"]] for r in sorted(by_slug[p["slug"]], key=lambda r: r["snapshot_date"])]
            srcs = {r["source"] for r in by_slug[p["slug"]][-4:]}
            source, metric = ("defillama" if srcs == {"defillama"} else "mixed"), "TVL"
        pts = [x for x in pts if x[1]]
        # 90-day trajectory: snapshots within 90 days of the latest
        last = pts[-1][0] if pts else None
        from datetime import date, timedelta
        cutoff = (date.fromisoformat(last) - timedelta(days=90)).isoformat() if last else None
        recent = [x for x in pts if cutoff and x[0] >= cutoff]
        chg = (recent[-1][1] / recent[0][1] - 1) * 100 if len(recent) > 1 and recent[0][1] else None
        cards.append({**{k: p[k] for k in ("key", "name", "parent", "kind", "blurb")},
                      "metric": metric, "source": source, "current": pts[-1][1] if pts else None,
                      "as_of": last, "change_90d": chg, "points": recent, "all_points": pts})
    return {"cards": cards}


def page_didnt_happen(con):
    rows = universe(con)
    yb, br = _by_date(rows, True), _by_date(rows, False)
    dates = list(br)
    first, latest = dates[0], dates[-1]

    # 1. Chain rotation — first-chain attribution (same method as the May analysis)
    chain_series, chain_latest = [], {}
    for d, v in br.items():
        st = _stats(v)
        agg = defaultdict(float)
        for r in st["ranked"]:
            agg[r["first_chain"] or "unknown"] += r["tvl"]
        shares = {k: x / st["total"] * 100 for k, x in agg.items()}
        chain_series.append([d, shares.get("Ethereum", 0)])
        chain_latest = shares
    chains = sorted(chain_latest.items(), key=lambda x: -x[1])[:8]
    # What drives each non-Ethereum chain's share (first-chain attribution is crude)
    st_l = _stats(br[latest])
    drivers = defaultdict(list)
    for r in st_l["ranked"]:
        if ((r["first_chain"] or "unknown") != "Ethereum" and len(drivers[r["first_chain"]]) < 3
                and r["tvl"] / st_l["total"] >= 0.0005):
            drivers[r["first_chain"] or "unknown"].append([r["name"], r["tvl"] / st_l["total"] * 100])
    chains = [[c, s, drivers.get(c, [])] for c, s in chains]

    # 2. Category broadening — share and growth by DefiLlama category
    def cat_totals(v):
        agg = defaultdict(float)
        for r in v:
            if r["tvl"]:
                agg[r["category"] or "Other"] += r["tvl"]
        return agg
    c0, c1 = cat_totals(br[first]), cat_totals(br[latest])
    t1 = sum(c1.values())
    cats = sorted(([k, c1[k], c1[k] / t1 * 100, (c1[k] / c0[k]) if c0.get(k) else None] for k in c1),
                  key=lambda x: -x[1])

    # 3. Quiet giants — outside the top 10, >3x since first snapshot, >$50M now
    s_first, s_last = _stats(yb[list(yb)[0]]), _stats(yb[list(yb)[-1]])
    top10 = {r["slug"] for r in s_last["ranked"][:10]}
    start = {r["slug"]: r["tvl"] for r in s_first["ranked"]}
    giants = []
    for r in s_last["ranked"]:
        if r["slug"] in top10 or r["tvl"] < 50e6 or not start.get(r["slug"]):
            continue
        mult = r["tvl"] / start[r["slug"]]
        if mult >= 3:
            giants.append({**_row(r), "start": start[r["slug"]], "multiple": mult})
    # Near misses, so the page shows what was checked
    risers = []
    for r in s_last["ranked"]:
        if r["slug"] in top10 or r["tvl"] < 50e6 or not start.get(r["slug"]):
            continue
        risers.append({**_row(r), "start": start[r["slug"]], "multiple": r["tvl"] / start[r["slug"]]})
    risers.sort(key=lambda x: -x["multiple"])

    return {"first": first, "latest": latest, "baseline": BASELINE,
            "eth_series": chain_series, "chains": chains,
            "categories": cats, "giants": giants, "risers": risers[:8]}


def page_watching(con):
    dh = page_didnt_happen(con)
    rows = universe(con)
    yb = _by_date(rows, True)

    # Watch 01 — a category other than RWA with >5% share AND 5x growth
    cand = [c for c in dh["categories"] if c[0] != "RWA"]
    lead = max(cand, key=lambda c: c[2]) if cand else None
    w1_trig = any(c[2] > 5 and (c[3] or 0) >= 5 for c in cand)

    # Watch 02 — Ethereum share below 70% (broad universe, first-chain attribution)
    eth = dh["eth_series"][-1][1] if dh["eth_series"] else None

    # Watch 03 — an anchor (top-10 by peak) down >=50% from its peak in the window
    peak = defaultdict(lambda: (0, None))
    cur = {}
    names = {}
    for d, v in yb.items():
        for r in v:
            names[r["slug"]] = r["name"]
            if r["tvl"] and r["tvl"] > peak[r["slug"]][0]:
                peak[r["slug"]] = (r["tvl"], d)
    latest = list(yb)[-1]
    for r in yb[latest]:
        cur[r["slug"]] = (r["tvl"], r["source"], r["cross_check"])
    anchors = sorted(peak, key=lambda s: -peak[s][0])[:10]
    draws = []
    for s in anchors:
        p, pd = peak[s]
        c, src, xc = cur.get(s, (None, "missing", None))
        # Flag when DefiLlama and the token's own market cap disagree by >50%
        disputed = bool(src == "defillama" and c and xc and abs(xc / c - 1) > 0.5)
        draws.append({"slug": s, "name": names[s], "peak": p, "peak_date": pd, "current": c, "source": src,
                      "cross_check": xc, "disputed": disputed,
                      "drawdown": (c / p - 1) * 100 if c else None})
    draws.sort(key=lambda x: (x["drawdown"] if x["drawdown"] is not None else 0))
    w3_trig = any(d["drawdown"] is not None and d["drawdown"] <= -50 and not d["disputed"] for d in draws)
    w3_hit = next((d for d in draws if d["drawdown"] is not None and d["drawdown"] <= -50 and not d["disputed"]), None)

    return {
        "latest": latest,
        "watches": [
            {"id": "01", "title": "A non-Treasury category breaks out",
             "rule": "Any DefiLlama category other than RWA above 5% of TVL with at least 5× growth since the first snapshot.",
             "triggered": w1_trig,
             "reading": (f"Largest: {lead[0]} at {lead[2]:.1f}% share" + (f", {lead[3]:.1f}× since {dh['first']}" if lead and lead[3] else "")) if lead else "—",
             "table": [{"name": c[0], "tvl": c[1], "share": c[2], "multiple": c[3]} for c in cand[:6]]},
            {"id": "02", "title": "Ethereum's share drops below 70%",
             "rule": "Share of RWA TVL whose first listed chain is Ethereum (broad universe). First-chain attribution over-assigns multichain products.",
             "triggered": eth is not None and eth < 70,
             "reading": f"Ethereum at {eth:.1f}%" if eth is not None else "—",
             "series": dh["eth_series"]},
            {"id": "03", "title": "Another anchor shrinks at Usual USD0 scale",
             "rule": "Any of the 10 largest protocols (by peak in the window) down 50% or more from its peak.",
             "triggered": w3_trig,
             "reading": (f"{w3_hit['name']} {w3_hit['drawdown']:.0f}% from its {w3_hit['peak_date']} peak" if w3_hit
                         else "No undisputed anchor is down 50%+"),
             "table": draws},
        ],
    }
