#!/usr/bin/env bash
# run_paper_campaign.sh — bounded paper-only evidence campaign.
#
# This script NEVER sends a live transaction. It runs the desk in
# paper mode, accumulates evidence, and writes to data/evidence_report.json.
#
# Usage:
#   ./scripts/run_paper_campaign.sh [cycles] [interval_seconds]
#
# Defaults: 100 cycles, 60-second interval.
#
# Required env:
#   RH_PRIVATE_KEY is NOT required for paper mode.
#   The vault stores it for the future live canary; this script does
#   not need it.

set -euo pipefail

CYCLES="${1:-100}"
INTERVAL="${2:-60}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT/03-Tooling/5-agent-desk"

mkdir -p data logs

PY="$(command -v python3)"
if [[ -z "$PY" ]]; then
  echo "python3 not found" >&2
  exit 1
fi

echo "[$(date -u +%FT%TZ)] starting paper campaign: cycles=$CYCLES interval=${INTERVAL}s"
exec "$PY" evidence_runner.py \
  --cycles "$CYCLES" \
  --interval "$INTERVAL" \
  --report data/evidence_report.json
