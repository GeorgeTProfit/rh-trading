#!/usr/bin/env bash
# run_desk.sh — One-shot runner for the 5-agent copy-desk.
# Use with cron, Hermes cronjob, or manual invocation.
set -euo pipefail
cd "$(dirname "$0")"

DESK_PY="${PWD}/desk.py"
LOG_DIR="${PWD}/logs"
mkdir -p "${LOG_DIR}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${LOG_DIR}/desk_${TIMESTAMP}.log"

# ─── Config ─────────────────────────────────────────────────────────────
# Set to "live" to enable real trades, "paper" for simulation only
MODE="${DESK_MODE:-paper}"

# ─── Run ────────────────────────────────────────────────────────────────
echo "[$(date)] Starting desk cycle (mode=${MODE})" | tee -a "${LOG_FILE}"

if [ "${MODE}" = "live" ]; then
    python3 "${DESK_PY}" --cycle --live 2>&1 | tee -a "${LOG_FILE}"
else
    python3 "${DESK_PY}" --cycle --paper 2>&1 | tee -a "${LOG_FILE}"
fi

echo "[$(date)] Desk cycle complete" | tee -a "${LOG_FILE}"

# Keep last 50 logs
ls -t "${LOG_DIR}"/desk_*.log 2>/dev/null | tail -n +51 | xargs rm -f 2>/dev/null || true
