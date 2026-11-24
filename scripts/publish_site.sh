#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# publish_site.sh — rebuild the static dashboard and push it to GitHub.
# Vercel redeploys on every push to main.
#
# Called at the end of weekly_pipeline.sh (only when the pipeline succeeded).
# Safe to run by hand. Guards:
#   • refuses if this checkout has unpushed commits of its own (so it never
#     pushes code by accident — only the site/ snapshot),
#   • refuses if scripts/check_snapshot.py finds the new snapshot broken
#     (zero total, >±25% move, low coverage, date going backwards) and
#     restores the last published snapshot.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python3"
cd "${ROOT}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1 || ! git remote get-url origin >/dev/null 2>&1; then
    echo "publish_site: no git repo with an 'origin' remote — skipping"
    exit 0
fi

git fetch -q origin main
if [ "$(git rev-list --count origin/main..HEAD)" != "0" ]; then
    echo "publish_site: this checkout has unpushed commits — push or reset them first; not publishing"
    exit 1
fi
# If this checkout is simply behind GitHub (no commits of its own), move the branch
# pointer forward without touching files on disk, so the snapshot commit lands on top.
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    git reset -q origin/main
fi

"${PY}" ui/export_static.py

if ! "${PY}" scripts/check_snapshot.py; then
    git checkout -- site/        # put the last published snapshot back
    exit 1
fi

git add site/
if git diff --cached --quiet -- site/data/page-*.json; then
    git reset -q site/
    git checkout -- site/
    echo "publish_site: data unchanged since the last snapshot — nothing to push"
    exit 0
fi
git commit -q -m "Update dashboard snapshot $(date -u '+%Y-%m-%d')" -- site/
git push -q origin HEAD:main
echo "publish_site: pushed — Vercel will redeploy"
