"""
Export the dashboard as a static site for Vercel (or any static host).

Writes site/index.html plus site/data/*.json, computed from data/rwa.duckdb.
The static site has the six editorial pages only; Data health, the raw table
browser and the SQL console need the local server (ui/app.py).

Usage (project root):  .venv/bin/python ui/export_static.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import app  # noqa: E402
import insights  # noqa: E402

SITE = ROOT / "site"


def write(name: str, obj) -> None:
    path = SITE / "data" / f"{name}.json"
    path.write_text(json.dumps(app.clean(obj), default=str, separators=(",", ":")))
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:,.0f} KB")


def main():
    # Overwrite in place (no delete), so the folder stays tidy under git
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    con = app.connect()
    try:
        write("page-overview", insights.page_overview(con))
        write("page-concentration", insights.page_concentration(con))
        write("page-roster", insights.page_roster(con))
        write("page-didnt-happen", insights.page_didnt_happen(con))
        write("page-watching", insights.page_watching(con))
        write("page-reference", insights.page_reference(con))
        write("page-solana", insights.page_chain(con, "solana"))
        write("page-ethereum", insights.page_chain(con, "ethereum"))
        write("page-compare", insights.page_compare(con))
        # No raw table dumps (CoinGecko/DefiLlama tables) on the public site — only
        # the derived editorial pages above. See README "Data sources and terms".
        for stale in ("overview", "view-protocols", "view-tokens"):
            write(stale, {"removed": "not published on the static site"})
        ov = insights.page_overview(con)
        write("meta", {"exported_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                       "data_as_of": ov["latest"], "methodology": "v2 (asset-level, 2026-11-24)",
                       "yield_total": ov["yield_total"], "missing_pct": (ov["coverage"]["missing"] or 0) / (ov["yield_total"] or 1) * 100})
    finally:
        con.close()
    html = (HERE / "index.html").read_text()
    html = html.replace("<script>", "<script>window.STATIC_SITE = true;</script>\n<script>", 1)
    (SITE / "index.html").write_text(html)
    print(f"Static site written to {SITE.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
