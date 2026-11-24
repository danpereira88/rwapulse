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
            # CoinGecko's chain list can be incomplete, so "every listed chain read"
            # isn't enough alone: on-chain must also land near CoinGecko's figure.
            if ov and (not mc or 0.75 <= ratio <= 1.5 or (complete and 0.5 <= ratio <= 2)):
                val, src = ov, "onchain"
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
                         "cross_check": (mc if src == "onchain" else ov),
                         "disputed": bool(ratio and src != "onchain" and complete)})
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


# ── Pages ────────────────────────────────────────────────────────────────────

def page_overview(con):
    rows = universe(con)
    yb, br, al = _by_date(rows, True), _by_date(rows, False), _by_date(rows, False, include_stocks=True)
    dates = list(yb)
    latest, first = dates[-1], dates[0]
    s_l, s_f, b_l, a_l = _stats(yb[latest]), _stats(yb[first]), _stats(br[latest]), _stats(al[latest])
    cls = defaultdict(float)
    for r in al[latest]:
        if r["tvl"]:
            cls[r["category"]] += r["tvl"]
    return {
        "latest": latest, "first": first,
        "yield_total": s_l["total"], "yield_first": s_f["total"],
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
                       "hhi_broad": b["hhi"], "filled_pct": _coverage(a)["filled_pct"],
                       "top5_carry": cm["top5"], "hhi_carry": cm["hhi"]})
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
            pts = [[r["snapshot_date"], r["v"]] for r in sub if r["v"]]
            source = "centrifuge subgraph"
        else:
            agg = defaultdict(float)
            srcs = set()
            for aid in p["ids"]:
                for r in by_id.get(aid, []):
                    if r["tvl"]:
                        agg[r["snapshot_date"]] += r["tvl"]
                        srcs.add(r["source"])
            pts = sorted([d, v] for d, v in agg.items())
            last_src = {r["source"] for aid in p["ids"] for r in by_id.get(aid, [])[-1:]}
            source = last_src.pop() if len(last_src) == 1 else "mixed"
        last = pts[-1][0] if pts else None
        cutoff = (_d(last) - timedelta(days=90)).isoformat() if last else None
        recent = [x for x in pts if cutoff and x[0] >= cutoff]
        chg = (recent[-1][1] / recent[0][1] - 1) * 100 if len(recent) > 1 and recent[0][1] else None
        cards.append({**{k: p[k] for k in ("key", "name", "parent", "kind", "blurb")},
                      "metric": "Value", "source": source, "current": pts[-1][1] if pts else None,
                      "as_of": last, "change_90d": chg, "points": recent, "all_points": pts})
    return {"cards": cards}


def _chain_split(con):
    """Share of on-chain-measured RWA value by chain, per on-chain read date."""
    if not _has(con, "rwa_supply"):
        return [], {}, 0
    rows = _q(con, """SELECT snapshot_date, platform, sum(value_usd) AS v FROM rwa_supply
                      WHERE status = 'ok' AND value_usd > 0 GROUP BY 1, 2""")
    by = defaultdict(dict)
    for r in rows:
        by[r["snapshot_date"]][CHAIN_LABEL.get(r["platform"], r["platform"])] = r["v"]
    series, latest = [], {}
    for d in sorted(by):
        t = sum(by[d].values())
        shares = {k: v / t * 100 for k, v in by[d].items()}
        series.append([d, shares.get("Ethereum", 0)])
        latest = shares
        latest_total = t
    return series, latest, (latest_total if by else 0)


def page_didnt_happen(con):
    rows = universe(con)
    yb = _by_date(rows, True)
    dates = list(yb)
    first, latest = dates[0], dates[-1]

    # 1. Chain rotation — measured from on-chain reads (true per-chain balances)
    eth_series, chain_latest, oc_total = _chain_split(con)
    drivers = defaultdict(list)
    if _has(con, "rwa_supply"):
        for r in _q(con, """SELECT platform, coingecko_id, sum(value_usd) AS v FROM rwa_supply
                            WHERE status = 'ok' AND snapshot_date = (SELECT max(snapshot_date) FROM rwa_supply)
                            GROUP BY 1, 2 ORDER BY v DESC"""):
            ch = CHAIN_LABEL.get(r["platform"], r["platform"])
            if ch != "Ethereum" and len(drivers[ch]) < 3 and oc_total and r["v"] / oc_total >= 0.0005:
                nm = next((x["name"] for x in rows if x["slug"] == r["coingecko_id"]), r["coingecko_id"])
                drivers[ch].append([nm, r["v"] / oc_total * 100])
    chains = [[c, s, drivers.get(c, [])] for c, s in sorted(chain_latest.items(), key=lambda x: -x[1])[:8]]
    total_l = _stats(yb[latest])["total"]

    # 2. Broadening beyond Treasuries — asset class share and growth
    def cls_totals(v):
        agg = defaultdict(float)
        for r in v:
            if r["tvl"]:
                agg[r["category"]] += r["tvl"]
        return agg
    c0, c1 = cls_totals(yb[first]), cls_totals(yb[latest])
    t1 = sum(c1.values())
    cats = sorted(([k, c1[k], c1[k] / t1 * 100, (c1[k] / c0[k]) if c0.get(k) else None] for k in c1),
                  key=lambda x: -x[1])

    # 3. Quiet giants — issuers outside the top 10, ≥3× since first snapshot, >$50M
    s_first, s_last = _stats(yb[first]), _stats(yb[latest])
    top10 = {r["slug"] for r in s_last["ranked"][:10]}
    start = {r["slug"]: r["tvl"] for r in s_first["ranked"]}
    risers = [{**_row(r), "start": start[r["slug"]], "multiple": r["tvl"] / start[r["slug"]]}
              for r in s_last["ranked"]
              if r["slug"] not in top10 and r["tvl"] >= 50e6 and start.get(r["slug"])]
    risers.sort(key=lambda x: -x["multiple"])
    giants = [x for x in risers if x["multiple"] >= 3]

    return {"first": first, "latest": latest, "baseline": BASELINE,
            "eth_series": eth_series, "chains": chains,
            "chain_coverage": (oc_total / total_l * 100) if total_l else 0,
            "categories": cats, "giants": giants, "risers": risers[:8]}


def page_watching(con):
    dh = page_didnt_happen(con)
    rows = universe(con)
    yb = _by_date(rows, True)

    # Watch 01 — a non-Treasury asset class >5% of yield-bearing value AND 5× growth
    cand = [c for c in dh["categories"] if c[0] != CLASS_LABEL["treasuries"]]
    lead = max(cand, key=lambda c: c[2]) if cand else None
    w1 = any(c[2] > 5 and (c[3] or 0) >= 5 for c in cand)

    # Watch 02 — Ethereum share of on-chain-measured value below 70%
    eth = dh["eth_series"][-1][1] if dh["eth_series"] else None

    # Watch 03 — an anchor issuer (top-10 by peak) down ≥50% from its peak in the window
    peak, cur = defaultdict(lambda: (0, None)), {}
    for d, v in yb.items():
        for r in _issuers(v):
            if r["tvl"] and r["tvl"] > peak[r["slug"]][0]:
                peak[r["slug"]] = (r["tvl"], d)
    latest = list(yb)[-1]
    for r in _issuers(yb[latest]):
        cur[r["slug"]] = r
    draws = []
    for s in sorted(peak, key=lambda s: -peak[s][0])[:10]:
        p, pd = peak[s]
        c = cur.get(s, {})
        draws.append({"slug": s, "name": s, "peak": p, "peak_date": pd, "current": c.get("tvl"),
                      "source": c.get("source", "missing"), "disputed": c.get("disputed", False),
                      "drawdown": (c["tvl"] / p - 1) * 100 if c.get("tvl") else None})
    draws.sort(key=lambda x: (x["drawdown"] if x["drawdown"] is not None else 0))
    hit = next((d for d in draws if d["drawdown"] is not None and d["drawdown"] <= -50 and not d["disputed"]), None)

    return {
        "latest": latest,
        "watches": [
            {"id": "01", "title": "A non-Treasury category breaks out",
             "rule": "Any asset class other than Treasuries & MMFs above 5% of yield-bearing value with at least 5× growth since the first snapshot.",
             "triggered": w1,
             "reading": (f"Largest: {lead[0]} at {lead[2]:.1f}% share"
                         + (f", {lead[3]:.2f}× since {dh['first']}" if lead[3] else "")) if lead else "—",
             "table": [{"name": c[0], "tvl": c[1], "share": c[2], "multiple": c[3]} for c in cand[:6]]},
            {"id": "02", "title": "Ethereum's share drops below 70%",
             "rule": f"Ethereum's share of RWA value measured on-chain, chain by chain ({dh['chain_coverage']:.0f}% of the yield-bearing total is measured this way so far).",
             "triggered": eth is not None and eth < 70,
             "reading": f"Ethereum at {eth:.1f}%" if eth is not None else "No on-chain reads yet",
             "series": dh["eth_series"]},
            {"id": "03", "title": "Another anchor shrinks at Usual USD0 scale",
             "rule": "Any of the 10 largest issuers (by peak in the window) down 50% or more from its peak.",
             "triggered": hit is not None,
             "reading": (f"{hit['name']} {hit['drawdown']:.0f}% from its {hit['peak_date']} peak" if hit
                         else "No anchor is down 50%+ from its peak"),
             "table": draws},
        ],
    }
