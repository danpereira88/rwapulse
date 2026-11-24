"""
Analysis behind the editorial pages (Overview, Concentration, New Roster,
What Didn't Happen, What We're Watching) — a local rebuild of the Lovable
"RWA Intel" dashboard at rwa-insight-hub.lovable.app.

Asset-level universe (since 2026-11-24). DefiLlama re-based RWA on assets and
moved that data behind its paid API, so the universe is now built from assets
(ingest/rwa_universe.py) and grouped by issuer, the way DefiLlama's RWA
dashboard groups them. Each asset's value on each weekly snapshot comes from,
in order:
  1. on-chain supply × price (ingest/rwa_supply.py) when CoinGecko has no
     figure, the two agree within 0.75–1.5×, or every listed chain was read and
     they agree within 0.5–2× (otherwise CoinGecko, flagged "sources disagree");
  2. CoinGecko market cap;
  3. DefiLlama protocol TVL, for protocols with no token on CoinGecko
     (Maple, Re, Huma…) and for history before on-chain reads began.
Every row carries its source.
"""

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest.rwa_universe import (  # noqa: E402
    CLASS_LABEL, CURATED, DL_FALLBACK, DL_ONLY, NOTES, YIELD_BEARING, classify)

ONCHAIN_MATCH_DAYS = 2   # an on-chain read counts for a CoinGecko snapshot within ±2 days

# New Roster: the six products on the Lovable page
ROSTER = [
    {"key": "buidl", "name": "BlackRock BUIDL", "parent": "BlackRock", "kind": "Money market fund",
     "ids": ["blackrock-usd-institutional-digital-liquidity-fund"],
     "blurb": "Tokenised US Treasury money market fund distributed via Securitize; the benchmark later products are measured against."},
    {"key": "usyc", "name": "Circle USYC", "parent": "Circle", "kind": "Money market fund",
     "ids": ["hashnote-usyc"],
     "blurb": "Short-duration Treasury and reverse-repo fund acquired from Hashnote; Circle's extension from stablecoins into tokenised cash."},
    {"key": "ousg", "name": "Ondo OUSG", "parent": "Ondo Finance", "kind": "Money market fund",
     "ids": ["ousg"],
     "blurb": "Institutional short-term Treasuries product that wraps several funds, BUIDL among them, into one tokenised interest."},
    {"key": "usdy", "name": "Ondo USDY", "parent": "Ondo Finance", "kind": "Tokenised Treasuries",
     "ids": ["ondo-us-dollar-yield"],
     "blurb": "Permissionless, non-US yield-bearing note issued across many chains."},
    {"key": "maple", "name": "Maple Finance", "parent": "Maple Labs", "kind": "Private credit",
     "ids": ["dl:maple"],
     "blurb": "Institutional on-chain lending to vetted borrowers; yield comes from credit spreads rather than government paper."},
    {"key": "centrifuge", "name": "Centrifuge", "parent": "Centrifuge Network Foundation", "kind": "Structured credit",
     "ids": ["subgraph:centrifuge"],
     "blurb": "Infrastructure for tokenising real-world asset funds through per-pool SPVs, close to traditional securitisation."},
]

# Published long-run baseline (reports/DRAFTING_COMPANION.md, data to 2026-05-13,
# protocol-level method). Shown as fixed reference figures.
BASELINE = {
    "as_of": "2026-05-13",
    "yield": {"start": "2024-05-15", "top5_start": 76.8, "top5_end": 51.4, "hhi_start": 1991, "hhi_end": 682,
              "tvl_start_date": "2024-07-09", "tvl_start": 2.194e9, "tvl_end": 24.200e9},
    "broad": {"start": "2024-05-15", "top5_start": 76.5, "top5_end": 47.7, "tvl_start": 1.592e9, "tvl_end": 29.909e9},
    "eth_share": {"start": 68.0, "end": 68.4},
}
CHAIN_LABEL = {"ethereum": "Ethereum", "binance-smart-chain": "BNB Chain", "solana": "Solana", "stellar": "Stellar",
               "polygon-pos": "Polygon", "arbitrum-one": "Arbitrum", "base": "Base", "avalanche": "Avalanche",
               "optimistic-ethereum": "Optimism", "aptos": "Aptos", "sui": "Sui", "xrp": "XRP Ledger",
               "mantle": "Mantle", "etherlink": "Etherlink", "plume-network": "Plume"}


def _q(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has(con, table):
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                       [table]).fetchone()[0] > 0


def _d(s):
    return date.fromisoformat(s)


# ── The universe ─────────────────────────────────────────────────────────────

def universe(con):
    """One row per (snapshot_date, asset). Fields: snapshot_date, slug (asset id),
    name, issuer, asset_class, category (label), first_chain, tvl, source,
    last_known, yield_bearing, cross_check, disputed."""
    cg = _q(con, """
        SELECT snapshot_date, coingecko_id AS id, any_value(name) AS name, any_value(symbol) AS symbol,
               max(market_cap_usd) AS mc
        FROM (SELECT snapshot_date, coingecko_id, name, symbol, market_cap_usd FROM cg_rwa_category
              UNION ALL SELECT snapshot_date, coingecko_id, name, symbol, market_cap_usd FROM cg_rwa_tokens)
        WHERE snapshot_date LIKE '20%' GROUP BY 1, 2""")
    cats = {r["id"]: r["c"] for r in _q(con, """SELECT coingecko_id AS id, string_agg(DISTINCT category_id, ',') AS c
                                                FROM cg_rwa_category GROUP BY 1""")}
    dates = sorted({r["snapshot_date"] for r in cg})

    # Classify once per asset (stable across snapshots)
    meta = {}
    for r in cg:
        if r["id"] in meta:
            continue
        cls, issuer = classify(r["id"], r["name"] or "", r["symbol"] or "", cats.get(r["id"], ""))
        if cls:
            meta[r["id"]] = {"name": r["name"] or r["id"], "asset_class": cls, "issuer": issuer}

    # On-chain reads: value per asset per read date, plus chain of the biggest holding
    oc, chain_of = {}, {}
    if _has(con, "rwa_supply"):
        for r in _q(con, """SELECT snapshot_date, coingecko_id AS id,
                                   sum(value_usd) FILTER (WHERE status = 'ok') AS v,
                                   count(*) FILTER (WHERE status = 'ok') AS n_ok, count(*) AS n
                            FROM rwa_supply WHERE platform IS NOT NULL AND snapshot_date LIKE '20%'
                            GROUP BY 1, 2"""):
            oc.setdefault(r["id"], []).append(r)
        for r in _q(con, """SELECT coingecko_id AS id, arg_max(platform, value_usd) AS p FROM rwa_supply
                            WHERE status = 'ok' AND snapshot_date = (SELECT max(snapshot_date) FROM rwa_supply)
                            GROUP BY 1"""):
            chain_of[r["id"]] = CHAIN_LABEL.get(r["p"], r["p"])

    def onchain_for(aid, snap):
        best = None
        for r in oc.get(aid, []):
            gap = abs((_d(r["snapshot_date"]) - _d(snap)).days)
            if gap <= ONCHAIN_MATCH_DAYS and r["v"] and (best is None or gap < best[0]):
                best = (gap, r)
        return best[1] if best else None

    dl = defaultdict(dict)
    for r in _q(con, "SELECT snapshot_date, slug, tvl_usd FROM dl_protocols WHERE snapshot_date LIKE '20%'"):
        if r["tvl_usd"]:
            dl[r["slug"]][r["snapshot_date"]] = r["tvl_usd"]

    cg_by = defaultdict(dict)
    for r in cg:
        cg_by[r["id"]][r["snapshot_date"]] = r["mc"]

    rows = []
    last = {}
    for snap in dates:
        for aid, m in meta.items():
            mc = cg_by[aid].get(snap)
            o = onchain_for(aid, snap)
            ov = o["v"] if o else None
            complete = bool(o and o["n_ok"] == o["n"])
            val, src = None, "missing"
            ratio = (ov / mc) if (ov and mc) else None
            # On-chain wins only when every listed chain was read AND it lands
            # within 0.75–1.5× of CoinGecko. A partial read is used only when
            # CoinGecko has no figure at all (marked "onchain-partial").
            if ov and complete and (not mc or 0.75 <= ratio <= 1.5):
                val, src = ov, "onchain"
            elif ov and not mc:
                val, src = ov, "onchain-partial"
            elif mc and mc > 0:
                val, src = mc, "coingecko"
            elif DL_FALLBACK.get(aid) and dl[DL_FALLBACK[aid]].get(snap):
                val, src = dl[DL_FALLBACK[aid]][snap], "defillama"
            if val is None and aid not in CURATED and aid not in last:
                continue  # tail token not in this snapshot's top-250: simply absent
            if val is None and mc is None and aid not in CURATED:
                continue
            rows.append({"snapshot_date": snap, "slug": aid, "name": m["name"], "issuer": m["issuer"],
                         "asset_class": m["asset_class"], "category": CLASS_LABEL[m["asset_class"]],
                         "first_chain": chain_of.get(aid), "tvl": val, "source": src,
                         "last_known": last.get(aid) if val is None else None,
                         "yield_bearing": m["asset_class"] in YIELD_BEARING,
                         "cross_check": (mc if src.startswith("onchain") else ov),
                         "disputed": bool(ratio and complete and not src.startswith("onchain"))})
            if val:
                last[aid] = val
        for slug, (name, cls, issuer) in DL_ONLY.items():
            v = dl[slug].get(snap)
            if v is None and slug not in last:
                continue
            rows.append({"snapshot_date": snap, "slug": "dl:" + slug, "name": name, "issuer": issuer,
                         "asset_class": cls, "category": CLASS_LABEL[cls], "first_chain": None,
                         "tvl": v, "source": "defillama" if v else "missing",
                         "last_known": last.get(slug) if v is None else None,
                         "yield_bearing": cls in YIELD_BEARING, "cross_check": None, "disputed": False})
            if v:
                last[slug] = v
    return rows


def _by_date(rows, yield_only=True, include_stocks=False):
    out = defaultdict(list)
    for r in rows:
        if yield_only and not r["yield_bearing"]:
            continue
        if not include_stocks and r["asset_class"] == "stocks":
            continue
        out[r["snapshot_date"]].append(r)
    return dict(sorted(out.items()))


def _issuers(rows):
    """Aggregate asset rows to issuer rows (the unit DefiLlama ranks by)."""
    g = defaultdict(list)
    for r in rows:
        g[r["issuer"]].append(r)
    out = []
    for iss, rs in g.items():
        live = [r for r in rs if r["tvl"]]
        tvl = sum(r["tvl"] for r in live)
        srcs = {r["source"] for r in live}
        big = max(live, key=lambda r: r["tvl"]) if live else rs[0]
        out.append({"slug": iss, "name": iss, "issuer": iss, "category": big["category"],
                    "asset_class": big["asset_class"], "first_chain": big["first_chain"],
                    "tvl": tvl or None, "source": srcs.pop() if len(srcs) == 1 else ("mixed" if srcs else "missing"),
                    "last_known": sum(r["last_known"] or 0 for r in rs) or None,
                    "yield_bearing": big["yield_bearing"], "cross_check": None,
                    "disputed": any(r["disputed"] for r in rs), "n_assets": len(rs),
                    "sources": {s: sum(r["tvl"] for r in live if r["source"] == s) for s in {r["source"] for r in live}}})
    return out


def _stats(rows, carry_missing=False):
    if carry_missing:
        rows = [dict(r, tvl=r["last_known"]) if not r["tvl"] and r["last_known"] else r for r in rows]
    iss = _issuers(rows)
    live = [r for r in iss if r["tvl"]]
    total = sum(r["tvl"] for r in live)
    ranked = sorted(live, key=lambda r: -r["tvl"])
    shares = [r["tvl"] / total for r in ranked] if total else []
    by_src = defaultdict(float)
    for r in rows:
        if r["tvl"]:
            by_src[r["source"]] += r["tvl"]
    return {"total": total, "n": len(live), "n_assets": sum(1 for r in rows if r["tvl"]),
            "top5": sum(shares[:5]) * 100, "hhi": sum((s * 100) ** 2 for s in shares), "ranked": ranked,
            "by_source": dict(by_src),
            "missing_last_known": sum((r["last_known"] or 0) for r in rows if not r["tvl"])}


def _coverage(st):
    t = st["total"] or 1
    return {"total": st["total"], "missing": st["missing_last_known"],
            "pct": {k: v / t * 100 for k, v in st["by_source"].items()},
            "filled_pct": (t - st["by_source"].get("defillama", 0)) / t * 100}


def _row(r, total=None):
    d = {k: r.get(k) for k in ("slug", "name", "category", "tvl", "source", "cross_check", "disputed", "n_assets")}
    d["chain"] = r.get("first_chain")
    if total:
        d["share"] = r["tvl"] / total * 100
    return d


def _notes():
    return list(NOTES.values())


# ── Like-for-like comparisons ────────────────────────────────────────────────
# A change between two snapshots only counts assets that have a value at BOTH
# ends from the SAME source. A switch of source (e.g. CoinGecko → on-chain) or an
# asset dropping out of coverage can otherwise look like a market move.

def _panel(v0, v1):
    a = {r["slug"]: r for r in v0 if r["tvl"]}
    b = {r["slug"]: r for r in v1 if r["tvl"]}
    keep = {k for k in a.keys() & b.keys() if a[k]["source"] == b[k]["source"]}
    return [a[k] for k in keep], [b[k] for k in keep]


def _lfl(v0, v1):
    """Like-for-like change between two snapshots' rows."""
    p0, p1 = _panel(v0, v1)
    t0, t1 = sum(r["tvl"] for r in p0), sum(r["tvl"] for r in p1)
    total1 = sum(r["tvl"] for r in v1 if r["tvl"]) or 1
    return {"change_pct": (t1 / t0 - 1) * 100 if t0 else None, "coverage_pct": t1 / total1 * 100,
            "from": t0, "to": t1}


def _ago(dates, latest, days):
    target = _d(latest) - timedelta(days=days)
    past = [d for d in dates if _d(d) <= target]
    return past[-1] if past else None


# ── Pages ────────────────────────────────────────────────────────────────────

def page_overview(con):
    rows = universe(con)
    yb, br, al = _by_date(rows, True), _by_date(rows, False), _by_date(rows, False, include_stocks=True)
    dates = list(yb)
    latest, first = dates[-1], dates[0]
    m30 = _ago(dates, latest, 28)
    s_l, b_l, a_l = _stats(yb[latest]), _stats(br[latest]), _stats(al[latest])
    cls = defaultdict(float)
    for r in al[latest]:
        if r["tvl"]:
            cls[r["category"]] += r["tvl"]
    return {
        "latest": latest, "first": first, "month_ago": m30,
        "yield_total": s_l["total"],
        "change_since_first": _lfl(yb[first], yb[latest]),
        "change_30d": _lfl(yb[m30], yb[latest]) if m30 else None,
        "broad_total": b_l["total"], "gold_total": b_l["total"] - s_l["total"],
        "all_total": a_l["total"], "stocks_total": a_l["total"] - b_l["total"],
        "n_protocols": s_l["n"], "n_assets": s_l["n_assets"],
        "coverage": _coverage(s_l), "notes": _notes(),
        "series": [[d, _stats(v)["total"], _stats(br[d])["total"], _stats(v, carry_missing=True)["total"]]
                   for d, v in yb.items()],
        "categories": sorted(([k, v, v / a_l["total"] * 100] for k, v in cls.items()), key=lambda x: -x[1]),
        "top": [_row(r, s_l["total"]) for r in s_l["ranked"][:12]],
        "baseline": BASELINE,
    }


def page_concentration(con):
    rows = universe(con)
    yb, br = _by_date(rows, True), _by_date(rows, False)
    series = []
    for d in yb:
        a, b, cm = _stats(yb[d]), _stats(br[d]), _stats(yb[d], carry_missing=True)
        series.append({"date": d, "top5": a["top5"], "hhi": a["hhi"], "top5_broad": b["top5"],
                       "hhi_broad": b["hhi"], "top5_carry": cm["top5"], "hhi_carry": cm["hhi"]})
    latest = list(yb)[-1]
    st = _stats(yb[latest])
    rosters = {d: [r["name"] for r in _stats(v)["ranked"][:5]] for d, v in yb.items()}
    return {"latest": latest, "series": series, "baseline": BASELINE,
            "top10": [_row(r, st["total"]) for r in st["ranked"][:10]],
            "roster_first": rosters[list(rosters)[0]], "roster_latest": rosters[latest],
            "coverage": _coverage(st)}


def page_roster(con):
    rows = universe(con)
    by_id = defaultdict(list)
    for r in rows:
        by_id[r["slug"]].append(r)
    sub = _q(con, """SELECT snapshot_date, sum(total_issuance * token_price) AS v FROM sg_centrifuge_assets
                     WHERE snapshot_date LIKE '20%' GROUP BY 1 ORDER BY 1""")
    cards = []
    for p in ROSTER:
        if p["ids"] == ["subgraph:centrifuge"]:
            pts = [[r["snapshot_date"], r["v"], "centrifuge subgraph"] for r in sub if r["v"]]
        else:
            agg, srcs = defaultdict(float), defaultdict(set)
            for aid in p["ids"]:
                for r in by_id.get(aid, []):
                    if r["tvl"]:
                        agg[r["snapshot_date"]] += r["tvl"]
                        srcs[r["snapshot_date"]].add(r["source"])
            pts = sorted([d, v, "+".join(sorted(srcs[d]))] for d, v in agg.items())
        last = pts[-1][0] if pts else None
        src_now = pts[-1][2] if pts else "missing"
        cutoff = (_d(last) - timedelta(days=90)).isoformat() if last else None
        # Trajectory only over the run of snapshots measured the same way as today
        run = []
        for x in reversed(pts):
            if x[2] != src_now or (cutoff and x[0] < cutoff):
                break
            run.append(x)
        run.reverse()
        chg = (run[-1][1] / run[0][1] - 1) * 100 if len(run) > 1 and run[0][1] else None
        cards.append({**{k: p[k] for k in ("key", "name", "parent", "kind", "blurb")},
                      "metric": "Value", "source": src_now, "current": pts[-1][1] if pts else None,
                      "as_of": last, "change_90d": chg, "change_from": run[0][0] if run else None,
                      "points": [[x[0], x[1]] for x in run]})
    return {"cards": cards}


MIN_CHAIN_COVERAGE = 90  # % of yield-bearing value read on-chain before Watch 02 can fire
MIN_CHAIN_READS = 2      # consecutive on-chain reads before Watch 02 can fire
MIN_PANEL_COVERAGE = 80  # % of an issuer's value that must be like-for-like for Watch 03


def _chain_split(con, rows, latest):
    """Share by chain of the yield-bearing value the dashboard actually counts from
    on-chain reads (assets whose accepted source on the latest snapshot is on-chain)."""
    if not _has(con, "rwa_supply"):
        return [], {}, 0, 0
    ids = [r["slug"] for r in rows if r["snapshot_date"] == latest and r["yield_bearing"]
           and r["source"] == "onchain" and r["tvl"]]
    if not ids:
        return [], {}, 0, 0
    marks = ",".join("?" * len(ids))
    q = _q(con, f"""SELECT snapshot_date, platform, sum(value_usd) AS v FROM rwa_supply
                    WHERE status = 'ok' AND value_usd > 0 AND snapshot_date LIKE '20%'
                      AND coingecko_id IN ({marks}) GROUP BY 1, 2""", ids)
    by = defaultdict(dict)
    for r in q:
        by[r["snapshot_date"]][CHAIN_LABEL.get(r["platform"], r["platform"])] = r["v"]
    series, shares, total = [], {}, 0
    for d in sorted(by):
        total = sum(by[d].values())
        shares = {k: v / total * 100 for k, v in by[d].items()}
        series.append([d, shares.get("Ethereum", 0)])
    return series, shares, total, len(by)


def page_didnt_happen(con):
    rows = universe(con)
    yb = _by_date(rows, True)
    dates = list(yb)
    first, latest = dates[0], dates[-1]
    total_l = _stats(yb[latest])["total"]

    # 1. Chain rotation — measured on-chain, chain by chain
    eth_series, chain_latest, oc_total, n_reads = _chain_split(con, rows, latest)
    chains = [[c, s, []] for c, s in sorted(chain_latest.items(), key=lambda x: -x[1])[:8]]

    # 2. Broadening beyond Treasuries — like-for-like growth by asset class
    cats = []
    t1 = sum(r["tvl"] for r in yb[latest] if r["tvl"]) or 1
    for label in sorted({r["category"] for r in yb[latest] if r["tvl"]}):
        v0 = [r for r in yb[first] if r["category"] == label]
        v1 = [r for r in yb[latest] if r["category"] == label]
        now = sum(r["tvl"] for r in v1 if r["tvl"])
        lfl = _lfl(v0, v1)
        mult = (lfl["to"] / lfl["from"]) if lfl["from"] else None
        cats.append([label, now, now / t1 * 100, mult, lfl["coverage_pct"]])
    cats.sort(key=lambda x: -x[1])

    # 3. Quiet giants — issuers outside the top 10, ≥3× like-for-like, >$50M
    s_last = _stats(yb[latest])
    top10 = {r["slug"] for r in s_last["ranked"][:10]}
    risers = []
    for r in s_last["ranked"]:
        if r["slug"] in top10 or r["tvl"] < 50e6:
            continue
        v0 = [x for x in yb[first] if x["issuer"] == r["slug"]]
        v1 = [x for x in yb[latest] if x["issuer"] == r["slug"]]
        lfl = _lfl(v0, v1)
        if lfl["from"] and lfl["coverage_pct"] >= 80:
            risers.append({**_row(r), "start": lfl["from"], "multiple": lfl["to"] / lfl["from"]})
    risers.sort(key=lambda x: -x["multiple"])
    giants = [x for x in risers if x["multiple"] >= 3]

    return {"first": first, "latest": latest, "baseline": BASELINE,
            "eth_series": eth_series, "chains": chains, "chain_reads": n_reads,
            "chain_min_coverage": MIN_CHAIN_COVERAGE,
            "chain_coverage": (oc_total / total_l * 100) if total_l else 0,
            "categories": cats, "giants": giants, "risers": risers[:8]}




def page_watching(con):
    dh = page_didnt_happen(con)
    rows = universe(con)
    yb = _by_date(rows, True)
    dates = list(yb)
    latest = dates[-1]

    # Watch 01 — a non-Treasury asset class >5% of yield-bearing value AND ≥5× like-for-like
    cand = [c for c in dh["categories"] if c[0] != CLASS_LABEL["treasuries"]]
    lead = max(cand, key=lambda c: c[2]) if cand else None
    w1 = "triggered" if any(c[2] > 5 and (c[3] or 0) >= 5 for c in cand) else "clear"

    # Watch 02 — Ethereum share below 70%, only once coverage and history allow
    eth = dh["eth_series"][-1][1] if dh["eth_series"] else None
    if eth is None or dh["chain_coverage"] < MIN_CHAIN_COVERAGE or dh["chain_reads"] < MIN_CHAIN_READS:
        w2, w2_read = "insufficient", (
            f"Not enough data yet: {dh['chain_coverage']:.0f}% of value read on-chain over "
            f"{dh['chain_reads']} read(s); needs {MIN_CHAIN_COVERAGE}% over {MIN_CHAIN_READS}."
        )
    else:
        w2, w2_read = ("triggered" if eth < 70 else "clear"), f"Ethereum at {eth:.1f}%"

    # Watch 03 — an anchor issuer down ≥50%, like-for-like against each earlier snapshot
    cur = {r["slug"]: r for r in _issuers(yb[latest])}
    anchors = sorted(cur, key=lambda k: -(cur[k]["tvl"] or 0))[:10]
    draws = []
    for iss in anchors:
        now_rows = [r for r in yb[latest] if r["issuer"] == iss]
        worst = None
        for d in dates[:-1]:
            then_rows = [r for r in yb[d] if r["issuer"] == iss]
            lfl = _lfl(then_rows, now_rows)
            if lfl["from"] and lfl["coverage_pct"] >= MIN_PANEL_COVERAGE:
                if worst is None or lfl["change_pct"] < worst[0]:
                    worst = (lfl["change_pct"], d, lfl["from"], lfl["to"])
        draws.append({"slug": iss, "name": iss, "current": cur[iss]["tvl"], "source": cur[iss]["source"],
                      "disputed": cur[iss]["disputed"],
                      "drawdown": worst[0] if worst else None, "peak_date": worst[1] if worst else None,
                      "peak": worst[2] if worst else None})
    draws.sort(key=lambda x: (x["drawdown"] if x["drawdown"] is not None else 0))
    hit = next((d for d in draws if d["drawdown"] is not None and d["drawdown"] <= -50 and not d["disputed"]), None)

    return {
        "latest": latest,
        "watches": [
            {"id": "01", "title": "A non-Treasury category breaks out",
             "rule": "Any asset class other than Treasuries & MMFs above 5% of yield-bearing value, with at least 5× like-for-like growth since the first weekly snapshot.",
             "status": w1,
             "reading": (f"Largest: {lead[0]} at {lead[2]:.1f}% share"
                         + (f", {lead[3]:.2f}× like-for-like since {dh['first']}" if lead[3] else "")) if lead else "—",
             "table": [{"name": c[0], "tvl": c[1], "share": c[2], "multiple": c[3]} for c in cand[:6]]},
            {"id": "02", "title": "Ethereum's share drops below 70%",
             "rule": f"Ethereum's share of yield-bearing value read on-chain, chain by chain. Fires only once at least {MIN_CHAIN_COVERAGE}% of value is read this way, over {MIN_CHAIN_READS}+ weekly reads.",
             "status": w2, "reading": w2_read, "series": dh["eth_series"]},
            {"id": "03", "title": "An anchor issuer halves",
             "rule": f"Any of the 10 largest issuers down 50% or more, like-for-like (same assets, same source) against any earlier weekly snapshot covering at least {MIN_PANEL_COVERAGE}% of its value.",
             "status": "triggered" if hit else "clear",
             "reading": (f"{hit['name']} {hit['drawdown']:.0f}% like-for-like since {hit['peak_date']}" if hit
                         else "No anchor issuer is down 50%+ like-for-like"),
             "table": draws},
        ],
    }


def page_reference(con):
    """Every asset counted, with the value used and its source (latest snapshot)."""
    rows = universe(con)
    al = _by_date(rows, False, include_stocks=True)
    latest = list(al)[-1]
    assets = sorted((r for r in al[latest] if r["tvl"]), key=lambda r: -r["tvl"])
    return {"latest": latest,
            "assets": [{"name": r["name"], "issuer": r["issuer"], "category": r["category"],
                        "chain": r["first_chain"], "tvl": r["tvl"], "source": r["source"],
                        "disputed": r["disputed"]} for r in assets]}


# ── Chain pages (Solana first) ───────────────────────────────────────────────
# Built only from on-chain reads (rwa_supply): what each token's mint/contract on
# the chain reports, × its price. No CoinGecko market caps or DefiLlama TVL mixed
# in, so every figure here is measured. Tokenised stocks are included, shown as
# their own class, because chain views are about where assets live.

CHAIN_ONLY_META = {"onyc": ("OnRe Tokenized Reinsurance", "credit", "OnRe")}


def page_chain(con, platform="solana"):
    from ingest.rwa_universe import CHAIN_ONLY
    label = CHAIN_LABEL.get(platform, platform)
    if not _has(con, "rwa_supply"):
        return {"chain": label, "reads": [], "assets": [], "series": [], "classes": [], "unread": []}
    names = {r["id"]: r for r in _q(con, """
        SELECT coingecko_id AS id, any_value(name) AS name, any_value(symbol) AS symbol
        FROM (SELECT coingecko_id, name, symbol FROM cg_rwa_category UNION ALL
              SELECT coingecko_id, name, symbol FROM cg_rwa_tokens) GROUP BY 1""")}
    cats = {r["id"]: r["c"] for r in _q(con, """SELECT coingecko_id AS id, string_agg(DISTINCT category_id, ',') AS c
                                                FROM cg_rwa_category GROUP BY 1""")}

    def meta(cid):
        if cid in CHAIN_ONLY:
            n, c, i = CHAIN_ONLY_META.get(cid, (cid, "other", cid))
            return n, c, i
        n = (names.get(cid) or {}).get("name") or cid
        c, i = classify(cid, n, (names.get(cid) or {}).get("symbol") or "", cats.get(cid, ""))
        return n, c, i

    rows = _q(con, """SELECT snapshot_date, coingecko_id AS id, platform, status, value_usd
                      FROM rwa_supply WHERE platform IS NOT NULL AND snapshot_date LIKE '20%'""")
    dates = sorted({r["snapshot_date"] for r in rows})
    latest = dates[-1] if dates else None

    by_date = defaultdict(lambda: defaultdict(float))       # date -> class -> value on chain
    all_chains_total = defaultdict(float)                   # date -> measured value, every chain
    asset_total = defaultdict(float)                         # (date, id) -> value, every chain
    on_chain = defaultdict(float)                            # (date, id) -> value on this chain
    unread = []
    for r in rows:
        name, cls, issuer = meta(r["id"])
        if cls is None:
            continue  # excluded token (governance etc.)
        ok = r["status"] == "ok" and r["value_usd"]
        if ok:
            all_chains_total[r["snapshot_date"]] += r["value_usd"]
            asset_total[(r["snapshot_date"], r["id"])] += r["value_usd"]
        if r["platform"] == platform:
            if ok:
                on_chain[(r["snapshot_date"], r["id"])] += r["value_usd"]
                by_date[r["snapshot_date"]][cls] += r["value_usd"]
            elif r["status"] != "ok" and r["snapshot_date"] == latest:
                unread.append({"name": name, "issuer": issuer, "status": r["status"]})

    assets = []
    for (d, cid), v in on_chain.items():
        if d != latest or v < 1e5:
            continue
        name, cls, issuer = meta(cid)
        tot = asset_total[(d, cid)]
        assets.append({"name": name, "issuer": issuer, "category": CLASS_LABEL[cls], "asset_class": cls,
                       "tvl": v, "share_of_asset": v / tot * 100 if tot else None, "source": "onchain"})
    assets.sort(key=lambda a: -a["tvl"])

    total = sum(a["tvl"] for a in assets)
    issuers = defaultdict(float)
    for a in assets:
        issuers[a["issuer"]] += a["tvl"]
    cls_latest = by_date.get(latest, {})
    series = [[d, sum(by_date[d].values())] for d in dates]
    prev = dates[-2] if len(dates) > 1 else None
    return {
        "chain": label, "platform": platform, "latest": latest, "reads": dates,
        "total": total,
        "yield_bearing": sum(v for c, v in cls_latest.items() if c in YIELD_BEARING),
        "stocks": cls_latest.get("stocks", 0), "commodities": cls_latest.get("commodities", 0),
        "share_of_measured": (total / all_chains_total[latest] * 100) if latest and all_chains_total[latest] else None,
        "measured_all_chains": all_chains_total.get(latest, 0),
        "change_vs_prev": ((series[-1][1] / series[-2][1] - 1) * 100) if prev and series[-2][1] else None,
        "classes": sorted(([CLASS_LABEL[c], v, v / total * 100] for c, v in cls_latest.items() if total), key=lambda x: -x[1]),
        "issuers": sorted(([k, v, v / total * 100] for k, v in issuers.items()), key=lambda x: -x[1])[:10],
        "assets": assets, "series": series, "unread": unread,
    }
