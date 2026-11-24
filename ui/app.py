"""
RWA Pulse — local data browser.

Serves a single-page UI over data/rwa.duckdb (opened read-only, so it can't
change the data and it can't clash with the pipeline's writes).

Usage (from the project root):
    .venv/bin/python ui/app.py            # http://127.0.0.1:8765
    .venv/bin/python ui/app.py --port 9000

Only needs duckdb, which the project already depends on. Python stdlib otherwise.
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import webbrowser
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insights  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "rwa.duckdb"
INDEX = Path(__file__).resolve().parent / "index.html"

# Rows with this snapshot_date are a legacy marker from before snapshots were
# appended; they are excluded when "latest snapshot" is computed.
LATEST = "(SELECT max(snapshot_date) FROM {t} WHERE snapshot_date LIKE '20%')"
RWA_CATS = "('RWA', 'RWA Lending')"

# Curated views: name -> (title, description, SQL)
VIEWS = {
    "protocols": (
        "RWA protocols",
        "DefiLlama protocols at the latest snapshot. Click a row for its TVL history.",
        f"""SELECT slug, name, category, tvl_usd, change_1d_pct, change_7d_pct, change_1m_pct,
                   mcap, chains, url
            FROM dl_protocols WHERE snapshot_date = {LATEST.format(t='dl_protocols')}
            ORDER BY tvl_usd DESC NULLS LAST""",
    ),
    "tokens": (
        "Tracked RWA tokens",
        "CoinGecko market data for the tokens the pipeline tracks, latest snapshot.",
        f"""SELECT display_name, symbol, asset_class, market_cap_usd, current_price_usd,
                   total_volume_24h_usd, price_change_pct_24h, price_change_pct_7d,
                   price_change_pct_30d, circulating_supply, coingecko_id
            FROM cg_rwa_tokens WHERE snapshot_date = {LATEST.format(t='cg_rwa_tokens')}
            ORDER BY market_cap_usd DESC NULLS LAST""",
    ),
    "categories": (
        "CoinGecko RWA categories",
        "Top tokens in CoinGecko's RWA, asset-backed and tokenized-gold categories, latest snapshot.",
        f"""SELECT category_id, name, symbol, market_cap_usd, current_price_usd,
                   total_volume_24h_usd, price_change_pct_24h, coingecko_id
            FROM cg_rwa_category WHERE snapshot_date = {LATEST.format(t='cg_rwa_category')}
            ORDER BY market_cap_usd DESC NULLS LAST""",
    ),
    "stablecoins": (
        "Stablecoins",
        "Circulating supply on the latest day of history. Click a row for its supply history.",
        """SELECT h.id, h.name, h.symbol, h.total_circulating_usd, h.date
           FROM dl_stablecoin_history h
           WHERE h.date = (SELECT max(date) FROM dl_stablecoin_history x WHERE x.id = h.id)
           ORDER BY h.total_circulating_usd DESC NULLS LAST""",
    ),
    "supply": (
        "On-chain token supply",
        "totalSupply read directly from token contracts, latest snapshot.",
        f"""SELECT symbol, name, chain, asset_class, issuer, total_supply_adjusted, address
            FROM onchain_token_supply WHERE snapshot_date = {LATEST.format(t='onchain_token_supply')}
            ORDER BY total_supply_adjusted DESC NULLS LAST""",
    ),
    "flows": (
        "Mint / burn flows",
        "Net on-chain flows over the lookback window, latest snapshot.",
        f"""SELECT symbol, chain, lookback_days, total_minted, total_burned, net_flow,
                   transfer_count, unique_senders, unique_receivers
            FROM onchain_transfer_flows WHERE snapshot_date = {LATEST.format(t='onchain_transfer_flows')}
            ORDER BY abs(net_flow) DESC NULLS LAST""",
    ),
    "maple": (
        "Maple pools",
        "Maple subgraph, latest snapshot.",
        f"""SELECT name, asset_symbol, open_to_public, tvl, total_assets, principal_out,
                   num_active_loans, apy, unrealized_losses, liquidity_cap, id
            FROM sg_maple_pools WHERE snapshot_date = {LATEST.format(t='sg_maple_pools')}
            ORDER BY TRY_CAST(tvl AS DOUBLE) DESC NULLS LAST""",
    ),
    "centrifuge": (
        "Centrifuge assets",
        "Centrifuge tranche tokens by pool, latest snapshot.",
        f"""SELECT pool_name, name, symbol, total_issuance, token_price,
                   total_issuance * token_price AS value_usd, token_price_at, pool_id
            FROM sg_centrifuge_assets WHERE snapshot_date = {LATEST.format(t='sg_centrifuge_assets')}
            ORDER BY value_usd DESC NULLS LAST""",
    ),
    "yields": (
        "Yield pools",
        "DefiLlama yield pools the pipeline tracks, latest snapshot.",
        f"""SELECT project, symbol, chain, tvl_usd, apy, apy_base, apy_reward, apy_mean_30d,
                   stablecoin, exposure, pool_id
            FROM dl_yield_pools WHERE snapshot_date = {LATEST.format(t='dl_yield_pools')}
            ORDER BY tvl_usd DESC NULLS LAST""",
    ),
    "fees": (
        "Protocol fees & revenue",
        "DefiLlama fees, latest snapshot.",
        f"""SELECT name, total_24h_usd, total_7d_usd, total_30d_usd, total_alltime_usd,
                   revenue_24h_usd, revenue_30d_usd, slug
            FROM dl_protocol_fees WHERE snapshot_date = {LATEST.format(t='dl_protocol_fees')}
            ORDER BY total_30d_usd DESC NULLS LAST""",
    ),
}


def connect():
    return duckdb.connect(str(DB_PATH), read_only=True)


def clean(v):
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return v


def run(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return cols, [[clean(v) for v in r] for r in cur.fetchall()]


def table_names(con):
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1"
    ).fetchall()]


def qident(name):
    return '"' + name.replace('"', '""') + '"'


def paged(con, base_sql, qs, columns_hint=None):
    """Wrap a query with search / sort / paging. base_sql is trusted (ours)."""
    limit = max(1, min(int(qs.get("limit", ["100"])[0]), 5000))
    offset = max(0, int(qs.get("offset", ["0"])[0]))
    q = qs.get("q", [""])[0].strip()
    sort = qs.get("sort", [""])[0]
    direction = "DESC" if qs.get("dir", ["desc"])[0].lower() == "desc" else "ASC"
    snapshot = qs.get("snapshot", [""])[0]

    cols = columns_hint or [d[0] for d in con.execute(f"SELECT * FROM ({base_sql}) LIMIT 0").description]
    where, params = [], []
    if snapshot and "snapshot_date" in cols:
        where.append("snapshot_date = ?")
        params.append(snapshot)
    if q:
        where.append("(" + " OR ".join(f"CAST({qident(c)} AS VARCHAR) ILIKE ?" for c in cols) + ")")
        params += [f"%{q}%"] * len(cols)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    order = f" ORDER BY {qident(sort)} {direction} NULLS LAST" if sort in cols else ""

    total = con.execute(f"SELECT count(*) FROM ({base_sql}) t{w}", params).fetchone()[0]
    cols, rows = run(con, f"SELECT * FROM ({base_sql}) t{w}{order} LIMIT {limit} OFFSET {offset}", params)
    return {"columns": cols, "rows": rows, "total": total, "limit": limit, "offset": offset}


# ── API handlers ─────────────────────────────────────────────────────────────

def api_overview(con, qs):
    out = {}
    out["db_modified"] = datetime.fromtimestamp(DB_PATH.stat().st_mtime).isoformat(timespec="minutes")

    # Freshness per table
    fresh = []
    for t in table_names(con):
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?", [t]).fetchall()]
        n = con.execute(f"SELECT count(*) FROM {qident(t)}").fetchone()[0]
        latest, snaps = None, None
        if "snapshot_date" in cols:
            latest, snaps = con.execute(
                f"SELECT max(snapshot_date), count(DISTINCT snapshot_date) FROM {qident(t)} "
                "WHERE snapshot_date LIKE '20%'").fetchone()
        elif "date" in cols:
            latest = con.execute(f"SELECT max(CAST(date AS VARCHAR)) FROM {qident(t)}").fetchone()[0]
        elif "ingested_at" in cols:
            latest = clean(con.execute(f"SELECT max(ingested_at) FROM {qident(t)}").fetchone()[0])
        fresh.append({"table": t, "rows": n, "latest": clean(latest), "snapshots": snaps})
    out["tables"] = fresh

    latest_p = LATEST.format(t="dl_protocols")
    out["rwa_tvl"], out["rwa_protocols"], out["snapshot"] = con.execute(
        f"SELECT sum(tvl_usd), count(*), max(snapshot_date) FROM dl_protocols "
        f"WHERE snapshot_date = {latest_p} AND category IN {RWA_CATS}").fetchone()

    # Same figure one month earlier (nearest snapshot >= 28 days before), for a delta
    prev = con.execute(
        f"""SELECT snapshot_date, sum(tvl_usd) FROM dl_protocols
            WHERE category IN {RWA_CATS} AND snapshot_date LIKE '20%'
              AND CAST(snapshot_date AS DATE) <= CAST({latest_p} AS DATE) - INTERVAL 28 DAY
            GROUP BY 1 ORDER BY 1 DESC LIMIT 1""").fetchone()
    out["rwa_tvl_prev"] = {"snapshot": prev[0], "tvl": prev[1]} if prev else None

    _, ac = run(con, f"""SELECT asset_class, sum(market_cap_usd), count(*) FROM cg_rwa_tokens
                         WHERE snapshot_date = {LATEST.format(t='cg_rwa_tokens')}
                         GROUP BY 1 ORDER BY 2 DESC""")
    out["asset_classes"] = [{"asset_class": a, "mcap": m, "n": n} for a, m, n in ac]

    out["stablecoin_supply"] = con.execute(
        """SELECT sum(total_circulating_usd) FROM dl_stablecoin_history h
           WHERE date = (SELECT max(date) FROM dl_stablecoin_history x WHERE x.id = h.id)""").fetchone()[0]

    _, top = run(con, f"""SELECT slug, name, category, tvl_usd, change_7d_pct, change_1m_pct
                          FROM dl_protocols WHERE snapshot_date = {latest_p} AND category IN {RWA_CATS}
                          ORDER BY tvl_usd DESC NULLS LAST LIMIT 12""")
    out["top"] = [dict(zip(["slug", "name", "category", "tvl", "c7", "c30"], r)) for r in top]

    # RWA TVL across the weekly snapshots
    _, snaps = run(con, f"""SELECT snapshot_date, sum(tvl_usd) FROM dl_protocols
                            WHERE category IN {RWA_CATS} AND snapshot_date LIKE '20%'
                            GROUP BY 1 ORDER BY 1""")
    out["snapshot_series"] = snaps

    # Daily TVL summed over every protocol with history (last 2 years)
    # DefiLlama returns two points for the most recent day (the daily close plus
    # a live "current" reading), so dedupe per slug/date before summing. The
    # newest day is also only partly reported, so stop at the last day with
    # near-full coverage.
    _, daily = run(con, """
        WITH d AS (SELECT slug, date, max(tvl_usd) AS v FROM dl_protocol_tvl GROUP BY 1, 2),
             s AS (SELECT date, sum(v) AS v, count(*) AS n FROM d
                   WHERE CAST(date AS DATE) >= current_date - INTERVAL 730 DAY GROUP BY 1),
             full_n AS (SELECT max(n) AS m FROM s WHERE CAST(date AS DATE) >= current_date - INTERVAL 30 DAY)
        SELECT date, v FROM s
        WHERE date <= (SELECT max(date) FROM s, full_n WHERE n >= 0.95 * m)
        ORDER BY 1""")
    out["daily_series"] = daily
    return out


def api_series(con, qs):
    kind = qs.get("kind", ["protocol"])[0]
    key = qs.get("key", [""])[0]
    if kind == "protocol":
        _, rows = run(con, "SELECT date, max(tvl_usd) FROM dl_protocol_tvl WHERE slug = ? GROUP BY 1 ORDER BY 1", [key])
        name = con.execute("SELECT max(name) FROM dl_protocol_tvl WHERE slug = ?", [key]).fetchone()[0]
        return {"name": name or key, "unit": "TVL", "points": rows}
    if kind == "stablecoin":
        _, rows = run(con, "SELECT date, total_circulating_usd FROM dl_stablecoin_history WHERE CAST(id AS VARCHAR) = ? ORDER BY date", [key])
        name = con.execute("SELECT max(name) FROM dl_stablecoin_history WHERE CAST(id AS VARCHAR) = ?", [key]).fetchone()[0]
        return {"name": name or key, "unit": "Circulating supply", "points": rows}
    raise ValueError("unknown series kind")


def api_tables(con, qs):
    out = []
    for t in table_names(con):
        cols, = [[r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position", [t]).fetchall()]]
        n = con.execute(f"SELECT count(*) FROM {qident(t)}").fetchone()[0]
        snaps = []
        if "snapshot_date" in cols:
            snaps = [r[0] for r in con.execute(
                f"SELECT DISTINCT snapshot_date FROM {qident(t)} ORDER BY 1 DESC").fetchall()]
        out.append({"name": t, "rows": n, "columns": cols, "snapshots": snaps})
    return {"tables": out}


def api_table(con, qs):
    name = qs.get("name", [""])[0]
    if name not in table_names(con):
        raise ValueError("unknown table")
    return paged(con, f"SELECT * FROM {qident(name)}", qs)


def api_view(con, qs):
    name = qs.get("name", [""])[0]
    if name not in VIEWS:
        raise ValueError("unknown view")
    title, desc, sql = VIEWS[name]
    res = paged(con, sql, qs)
    res.update({"title": title, "description": desc, "sql": " ".join(sql.split())})
    return res


READONLY = re.compile(r"^\s*(select|with|describe|show|summarize|pivot|from|explain)\b", re.I)


def api_sql(con, body):
    sql = (body.get("sql") or "").strip().rstrip(";")
    if not READONLY.match(sql) or ";" in sql:
        raise ValueError("Only a single read-only query (SELECT / WITH / DESCRIBE / SUMMARIZE …) is allowed.")
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(5001)
    return {"columns": cols, "rows": [[clean(v) for v in r] for r in rows[:5000]], "truncated": len(rows) > 5000}


# ── HTTP server ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, default=str))

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        routes = {"/api/overview": api_overview, "/api/series": api_series,
                  "/api/tables": api_tables, "/api/table": api_table, "/api/view": api_view,
                  "/api/page/overview": lambda c, q: clean(insights.page_overview(c)),
                  "/api/page/concentration": lambda c, q: clean(insights.page_concentration(c)),
                  "/api/page/roster": lambda c, q: clean(insights.page_roster(c)),
                  "/api/page/didnt-happen": lambda c, q: clean(insights.page_didnt_happen(c)),
                  "/api/page/watching": lambda c, q: clean(insights.page_watching(c)),
                  "/api/page/reference": lambda c, q: clean(insights.page_reference(c)),
                  "/api/page/solana": lambda c, q: clean(insights.page_chain(c, "solana"))}
        try:
            if u.path == "/api/csv":
                return self.csv(qs)
            if u.path in routes:
                con = connect()
                try:
                    return self.json(routes[u.path](con, qs))
                finally:
                    con.close()
        except duckdb.IOException as e:
            return self.json({"error": f"Database is busy (the pipeline may be writing to it). Try again shortly. {e}"}, 503)
        except Exception as e:
            return self.json({"error": str(e)}, 400)
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/sql":
            return self.send(404, "not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            con = connect()
            try:
                return self.json(api_sql(con, body))
            finally:
                con.close()
        except Exception as e:
            return self.json({"error": str(e)}, 400)

    def csv(self, qs):
        con = connect()
        try:
            kind, name = qs.get("kind", ["table"])[0], qs.get("name", [""])[0]
            if kind == "view" and name in VIEWS:
                sql = VIEWS[name][2]
            elif kind == "table" and name in table_names(con):
                sql = f"SELECT * FROM {qident(name)}"
            else:
                raise ValueError("unknown table or view")
            qs = dict(qs)
            qs["limit"], qs["offset"] = ["5000"], ["0"]
            # Export everything that matches the current filter, not just one page
            cols = [d[0] for d in con.execute(f"SELECT * FROM ({sql}) LIMIT 0").description]
            total = paged(con, sql, {**qs, "limit": ["1"]}, cols)["total"]
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for off in range(0, total, 5000):
                qs["offset"] = [str(off)]
                w.writerows(paged(con, sql, qs, cols)["rows"])
            self.send(200, buf.getvalue(), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{name}.csv"'})
        finally:
            con.close()


def main():
    ap = argparse.ArgumentParser(description="RWA Pulse local data browser")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not DB_PATH.exists():
        sys.exit(f"Database not found at {DB_PATH}. Run the pipeline first: python run_all.py")
    url = f"http://127.0.0.1:{args.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"RWA Pulse data browser → {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
