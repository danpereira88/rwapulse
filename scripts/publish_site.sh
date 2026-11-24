#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# publish_site.sh — rebuild the static dashboard and push it to GitHub.
# Vercel redeploys on every push to main.
#
# Called at the end of weekly_pipeline.sh. Safe to run by hand.
# Only commits site/ (the exported snapshot), never code or data/.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1 || ! git remote get-url origin >/dev/null 2>&1; then
    echo "publish_site: no git repo with an 'origin' remote — skipping"
    exit 0
fi

"${ROOT}/.venv/bin/python3" ui/export_static.py

git add site/
if git diff --cached --quiet -- site/; then
    echo "publish_site: snapshot unchanged — nothing to push"
    exit 0
fi
git commit -m "Update dashboard snapshot $(date -u '+%Y-%m-%d')" -- site/
git push origin HEAD
echo "publish_site: pushed — Vercel will redeploy"
