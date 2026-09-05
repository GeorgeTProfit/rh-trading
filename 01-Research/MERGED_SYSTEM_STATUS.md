# Merged Trading System — Status Report

## Systems Merged

### System A: Core Trading Tooling (03-Tooling/)
- `rh_swap.py` — Verified Uniswap v3 buy/sell execution
- `rh_preflight.py` — Preflight checks
- `risk_manager.py` — Quant risk layer (expectancy, Kelly, drawdown, kill switch)
- `position_executor.py` — Hummingbot-inspired triple barrier position manager
- `auto_trader.py` — Alert consumer with risk gating
- `swap_collector.py` — Event-level swap data fetcher
- `trader_copy_strategy.py` — Dashboard copy-flow evaluator
- `config.json` — Shared configuration

### System B: 5-Agent Copy-Desk (03-Tooling/5-agent-desk/)
- `scout.py` — Wallet watcher via Trenches tape + GMGN API
- `historian.py` — Wallet history scorer (heuristic, $0)
- `context.py` — Token health via Dexscreener ($0)
- `pulse.py` — Macro mood via CoinGecko/Binance ($0)
- `devil.py` — Adversarial veto via GoPlus ($0)
- `executor.py` — Wraps risk_manager + position_executor + rh_swap
- `exit_manager.py` — Triple barrier exit monitoring
- `desk.py` — Central orchestrator running full pipeline

## Integration Points (Already Wired)

| Integration | File | How |
|---|---|---|
| Configuration sync | `config.py` / `config.json` | Same risk params (max_position_eth=0.002, stop_loss=0.12) |
| Risk gate | `executor.py` → `risk_manager.py` | `evaluate_gate()` before every trade |
| Position tracking | `executor.py` → `position_executor.py` | `PositionExecutor` for each entry |
| Swap execution | `executor.py` → `rh_swap.py` | `build_plan()` + `execute_plan()` |
| Exit monitoring | `exit_manager.py` → `position_executor.py` | `control_barriers()` every cycle |
| State files | `state.py` → JSON files | Shared data dir for positions, trade log, risk state |

## Merge Verification

| Check | Result |
|---|---|
| 91/91 tests pass | ✅ Full test suite OK |
| 5-agent-desk imports (risk_manager, position_executor, rh_swap) | ✅ All imports clean |
| Desk cycle --paper | ✅ Full pipeline: SCOUT(33) → HISTORIAN → CONTEXT → PULSE → DEVIL → EXECUTOR → EXIT_MANAGER |
| Live pipeline decisions | ✅ Correct: small buys = SKIP/WATCH, composite 0.34-0.41 |
| Wallet DB populated | ✅ 33 wallets discovered from Trenches |

## Pipeline Architecture (Zynex-Inspired)

```
SCOUT (Trenches tape + GMGN)
  → HISTORIAN (wallet history, heuristic, $0)
    → CONTEXT (Dexscreener token health, $0)
      → PULSE (CoinGecko/Binance macro mood, $0)
        → DEVIL (GoPlus honeypot + adversarial, $0)
          → EXECUTOR (risk gate + position_executor + rh_swap)
            → EXIT_MANAGER (triple barrier check, every cycle)
```

**Total cost per cycle: $0** (0 LLM calls, all free APIs).

## Tuning Needed

Current pipeline produces WATCH (composite ~0.41) not TRADE. To reach TRADE (>=0.62):
- Need larger buys ($2k+) on better-known tokens
- Or the current threshold is correct and signals should remain WATCH until data improves
- **Recommended**: Keep threshold at 0.62; accumulate evidence; revisit after 100+ TRADE-quality signals are collected

## Commands

```bash
# Run one cycle (paper, no real trades)
cd 03-Tooling/5-agent-desk && python3 desk.py --cycle

# Check status
cd 03-Tooling/5-agent-desk && python3 desk.py --status

# Run full test suite
cd 03-Tooling && python3 -m unittest discover -s tests

# Refresh wallet database
cd 03-Tooling/5-agent-desk && python3 desk.py --refresh-wallets
```
