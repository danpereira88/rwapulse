"""
Sanity-check a freshly exported site/ snapshot before it is published.

Compares site/data/meta.json with the version last committed to git (HEAD) and
refuses (exit 1) when the new snapshot looks broken:
  • the yield-bearing total is missing or zero,
  • it moved more than ±25% from the last published snapshot,
  • more than 30% of value has no source this week (coverage too low),
  • the data date went backwards.

Usage (project root):  .venv/bin/python scripts/check_snapshot.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_MOVE = 0.25
MAX_MISSING_PCT = 30.0


def previous_meta():
    try:
        out = subprocess.run(["git", "show", "HEAD:site/data/meta.json"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    except Exception:
        return None


def check(new: dict, old) -> list:
    problems = []
    total = new.get("yield_total") or 0
    if total <= 0:
        problems.append("yield-bearing total is zero or missing")
    if (new.get("missing_pct") or 0) > MAX_MISSING_PCT:
        problems.append(f"{new['missing_pct']:.0f}% of value has no source (limit {MAX_MISSING_PCT:.0f}%)")
    if old and old.get("yield_total"):
        move = total / old["yield_total"] - 1
        if abs(move) > MAX_MOVE:
            problems.append(f"total moved {move:+.0%} since the last published snapshot (limit ±{MAX_MOVE:.0%})")
        if old.get("data_as_of") and new.get("data_as_of") and new["data_as_of"] < old["data_as_of"]:
            problems.append(f"data date went backwards ({old['data_as_of']} → {new['data_as_of']})")
    return problems


def main():
    new = json.loads((ROOT / "site" / "data" / "meta.json").read_text())
    problems = check(new, previous_meta())
    if problems:
        print("check_snapshot: NOT publishing —", "; ".join(problems))
        sys.exit(1)
    print(f"check_snapshot: ok (data as of {new.get('data_as_of')}, total ${new.get('yield_total', 0)/1e9:.2f}B)")


if __name__ == "__main__":
    main()
