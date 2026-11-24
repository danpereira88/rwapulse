#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# weekly_pipeline.sh — Full RWA Pulse pipeline
#
# Runs weekly via launchd (see the plist for the schedule) (com.rwapulse.weekly-pipeline).
# Sequence:
#   1. DuckDB ingestion + Supabase TVL/token sync  (run_all_with_sync.py)
#   2. On-chain activity enrichment                (run_enrichment.py)
#   3. Freshness check + pipeline_health upsert    (scripts/check_freshness.py)
#
# Step 3 always runs regardless of earlier failures, so pipeline_health is
# updated even on partial failures (status will reflect the actual state).
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python3"
LOG="${ROOT}/logs/weekly_pipeline.log"

cd "${ROOT}"

_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "${LOG}"; }

_log "========================================================"
_log "Weekly pipeline starting"
_log "========================================================"

PIPELINE_STATUS="ok"

# ── Step 1: ingest + sync ─────────────────────────────────────────────────────
_log "Step 1: ingest + Supabase sync"
if "${PYTHON}" run_all_with_sync.py >> "${LOG}" 2>&1; then
    _log "Step 1 complete"
else
    _log "ERROR: Step 1 failed (exit $?)"
    PIPELINE_STATUS="error"
fi

# ── Step 2: enrichment ────────────────────────────────────────────────────────
_log "Step 2: on-chain activity enrichment"
if "${PYTHON}" run_enrichment.py >> "${LOG}" 2>&1; then
    _log "Step 2 complete"
else
    _log "ERROR: Step 2 failed (exit $?)"
    PIPELINE_STATUS="error"
fi

# ── Step 3: freshness check + keep-alive write ────────────────────────────────
# Always runs. Writes pipeline_health (Supabase keep-alive) and sends alert
# if data is stale. Exit code from check_freshness reflects data staleness,
# not pipeline success — don't let it override PIPELINE_STATUS here.
_log "Step 3: freshness check + keep-alive"
"${PYTHON}" scripts/check_freshness.py >> "${LOG}" 2>&1 || true

# ── Write weekly_full health row ──────────────────────────────────────────────
_log "Writing weekly_full health record"
"${PYTHON}" - <<PYEOF >> "${LOG}" 2>&1 || true
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path("${ROOT}")
load_dotenv(ROOT / ".env")

SUPA_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPA_URL or not SUPA_KEY:
    print("SUPABASE creds not set — skipping weekly_full health write")
    sys.exit(0)

payload = [{
    "run_type": "weekly_full",
    "run_at":   datetime.now(timezone.utc).isoformat(),
    "status":   "${PIPELINE_STATUS}",
    "details":  {"pipeline_status": "${PIPELINE_STATUS}"},
}]

resp = requests.post(
    f"{SUPA_URL}/rest/v1/pipeline_health",
    data=json.dumps(payload),
    headers={
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    },
    timeout=15,
)
if resp.ok:
    print(f"weekly_full health row upserted (status=${PIPELINE_STATUS})")
else:
    print(f"weekly_full upsert failed [{resp.status_code}]: {resp.text[:200]}")
PYEOF

# ── Step 4: publish the static dashboard (GitHub → Vercel) ───────────────────
# Skips itself if this folder isn't a git repo with an origin remote.
if [[ "${PIPELINE_STATUS}" == "ok" ]]; then
    _log "Step 4: publish static dashboard"
    "${ROOT}/scripts/publish_site.sh" >> "${LOG}" 2>&1 || _log "WARNING: publish_site refused or failed (see log)"
else
    _log "Step 4: NOT publishing — pipeline status is ${PIPELINE_STATUS}; the live site keeps last week's snapshot"
fi

_log "========================================================"
_log "Weekly pipeline complete (status: ${PIPELINE_STATUS})"
_log "========================================================"

[[ "${PIPELINE_STATUS}" == "ok" ]]
