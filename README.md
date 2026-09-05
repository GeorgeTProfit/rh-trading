# RH Chain Trading System — Deployment

A hardened, evidence-gated Uniswap v3 trading system for Robinhood Chain.

This repository is the **deployable mirror** of the trading system
developed in the Obsidian vault. The vault is the source of truth for
research, journal, and runtime state. This repo contains only code,
configs, tests, and run scripts — no live trade logs, no wallet
databases, no evidence artifacts, no secrets.

## What is here

```
03-Tooling/
  rh_swap.py             # Hardened native ETH -> token v3 buy helper
  rh_sell.py             # Hardened token -> WETH v3 sell helper
  auto_trader.py         # Allowlisted buy consumer with risk veto
  risk_manager.py        # Expectancy, quarter-Kelly, drawdown, kill switch
  position_executor.py   # Hummingbot-inspired position lifecycle
  swap_collector.py      # Uniswap v3 event collector
  trader_copy_strategy.py# Dashboard trader qualification / consensus
  config.json            # Halved-threshold core config
  auto_scanner.py        # Token scanner
  rh_scanner.py          # Robinhood Chain scanner
  rh_preflight.py        # Pre-trade route preflight
  tests/                 # Core test suite
  5-agent-desk/          # Copy desk (scout -> historian -> context ->
                         #   pulse -> devil -> executor -> exit)
    config.py            # Halved-threshold desk config
    state.py             # Crash-safe fcntl-locked queue
    scout.py             # Trenches tape wallet watcher
    historian.py         # Wallet-history heuristic scorer
    context.py           # Dexscreener token-context scorer
    pulse.py             # Broad-market heuristic
    devil.py             # Adversarial / security veto
    executor.py          # Risk-gated paper + verified live execution
    exit_manager.py      # Barrier monitoring
    risk_accounting.py   # Deterministic live risk-state accounting
    evidence.py          # Clustered paper-evidence reporting
    evidence_runner.py   # Bounded paper-only campaign driver
    desk.py              # Orchestrator
    tests/               # Desk test suite
01-Research/             # Strategy + architecture research notes
scripts/                 # Run / sync / verify scripts
```

## What is NOT here

- Live `trade_log.jsonl`, `positions.json`, `risk_state.json`,
  `HALT_TRADING` marker.
- Real wallet addresses in `wallet_db.json` / `seen_fills.json`.
- Evidence reports and campaign state.
- Log files.
- Any private keys, mnemonics, or seed phrases.
- The Obsidian journal, trade journal, and personal notes.

The vault owns all of the above. This repo can be shared, archived, or
cloned onto a deployment host without leaking operational state.

## Hard safety controls (always enforced)

- RHC `0x7F04DA8CC451DddFBf80D6Fa3Aae3EE0642F8AB9` permanently denied.
- Live token allowlist; RHC denylist takes precedence.
- Max position: `0.002 ETH`.
- Max equity risk per trade: `0.05%`.
- Daily loss limit: lower of `0.001 ETH` or `2%` of start-day equity.
- Max drawdown: `10%`.
- Persistent kill switch (`HALT_TRADING`).
- Fresh route quote, exact `eth_call`, gas estimation before signing.
- Receipt `status == 1` + positive destination token delta required.
- Sellability gate on every candidate.

## Halved thresholds (evidence + signal, not safety)

- Core scanner score: `2.5` (was `5.0`).
- Min edge observations: `75` (was `150`).
- Desk historian: `0.20` (was `0.40`).
- Desk composite: `0.31` (was `0.62`).
- Desk elite: `0.40` (was `0.80`).
- Desk pulse minimum: `0.15` (was `0.30`).
- Copy min closed trades: `3` (was `5`).
- Copy min buy USD: `$125` (was `$250`).
- Copy elite score: `0.375` (was `0.75`).
- Copy elite buy USD: `$1,000` (was `$2,000`).
- BTC crash trigger: kept at `-4%` (deliberately not halved).

## Secrets and credentials

This repo never contains private keys, seed phrases, API tokens, or
dashboard session cookies. `RH_PRIVATE_KEY` and `TRENCHES_KEY` are read
at runtime from environment variables. `.env` and credential files are
excluded by `.gitignore`. Rotate immediately if a secret ever appears
in a commit.

## Setup on a deployment host

```bash
# 1. Clone
git clone <this-repo-url> rh-trading-deploy
cd rh-trading-deploy

# 2. Install dependencies (Python 3.11+, venv recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install web3 eth-account eth-abi requests

# 3. Configure secrets in your shell env, not in this repo
export RH_PRIVATE_KEY=...      # DO NOT COMMIT
# export TRENCHES_KEY=...      # DO NOT COMMIT
# export RH_RPC_URL=...        # optional override

# 4. Run the full test suite
python3 -m unittest discover -s 03-Tooling/tests -v
python3 -m unittest discover -s 03-Tooling/5-agent-desk/tests -v

# 5. Run the paper evidence campaign (paper only, no live trades)
cd 03-Tooling/5-agent-desk
python3 evidence_runner.py --cycles 100 --interval 60 \
  --report data/evidence_report.json
```

## Deployment host prerequisites

- Python 3.11+
- `web3`, `eth-account`, `eth-abi`, `requests`
- Outbound HTTPS to `https://rpc.mainnet.chain.robinhood.com`
- Outbound HTTPS to `https://robinhoodtrenches.com` (dashboard scrape)
- Outbound HTTPS to Dexscreener and GoPlus (security + token context)
- A scheduler (`launchd`, `cron`, `systemd`) to run `evidence_runner.py`
  every minute or so

## Promotion to live

Live trading only happens after the evidence campaign reports:

- `>= 75` completed paper exits
- positive net expectancy
- positive lower confidence bound on clustered bootstrap
- successful on-chain sell verification
- drawdown within limits
- cost-to-edge ratio within limits

Until all of those pass, the desk stays in `paper_mode=True` and the
persistent kill switch stays on. The deployment host must keep
`HALT_TRADING` in place until promotion is approved.
