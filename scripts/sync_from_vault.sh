#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="${VAULT:-$HOME/Obsidian/TradingVault}"
if [[ ! -d "$VAULT" ]]; then echo "Vault not found: $VAULT"; exit 1; fi
echo "[$(date -u +%FT%TZ)] syncing from $VAULT"
for f in rh_swap.py rh_sell.py auto_trader.py risk_manager.py position_executor.py swap_collector.py trader_copy_strategy.py config.json auto_scanner.py rh_scanner.py rh_preflight.py verify_sell_path.py; do
  cp "$VAULT/03-Tooling/$f" "$REPO_ROOT/03-Tooling/$f"
done
for f in config.py state.py wallet_db.py scout.py historian.py context.py pulse.py devil.py executor.py exit_manager.py risk_accounting.py evidence.py evidence_runner.py desk.py run_desk.sh fix_source.sh; do
  cp "$VAULT/03-Tooling/5-agent-desk/$f" "$REPO_ROOT/03-Tooling/5-agent-desk/$f"
done
for f in test_position_executor.py test_auto_trader.py test_swap_collector.py test_trader_copy_strategy.py test_rh_swap.py; do
  [[ -f "$VAULT/03-Tooling/tests/$f" ]] && cp "$VAULT/03-Tooling/tests/$f" "$REPO_ROOT/03-Tooling/tests/$f"
done
for f in test_live_readiness.py test_executor_safety.py test_exit_safety.py test_evidence.py test_fail_closed.py test_risk_accounting.py; do
  [[ -f "$VAULT/03-Tooling/5-agent-desk/tests/$f" ]] && cp "$VAULT/03-Tooling/5-agent-desk/tests/$f" "$REPO_ROOT/03-Tooling/5-agent-desk/tests/$f"
done
for f in 01-Research/AI_QUANT_RISK_LAYER.md 01-Research/HUMMINGBOT_ARCHITECTURE_INTEGRATION.md 01-Research/MERGED_SYSTEM_STATUS.md 01-Research/ROBINHOOD_TRENCHES_COPY_FLOW_STRATEGY.md 02-Strategies/TEST_TRADE_RULES.md; do
  [[ -f "$VAULT/$f" ]] && cp "$VAULT/$f" "$REPO_ROOT/01-Research/$(basename "$f")"
done
echo "[$(date -u +%FT%TZ)] sync complete"
