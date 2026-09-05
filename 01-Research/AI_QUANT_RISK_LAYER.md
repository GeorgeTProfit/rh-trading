# AI Quant Risk Layer — Applied Review

**Status:** implemented and enforced; live strategy remains halted.

## Source assessment

The Veles post correctly argues for separating signal generation from position sizing, risk limits, turnover control, and a kill switch.[1] Its Alpha Arena summary is directionally accurate but calls the experiment "one month"; reporting describes a run from October 17 through November 3 in which six models received $10,000 and GPT-5 ended near -63%.[3]

The experiment does not prove that a particular risk recipe caused the winners. Independent analysis describes the single 16-day season as an anecdote rather than statistical evidence and warns about regime variance and survivorship bias.[2]

## Learnings adopted

1. **Quant veto:** signals may propose; the deterministic risk layer has final authority.
2. **Net expectancy:** estimate wins and losses after round-trip gas, DEX fees, price impact, slippage, taxes, failed transactions, and unsellable outcomes.
3. **Conservative Kelly:** Kelly sizing is zero until at least 150 out-of-sample observations and a positive clustered-bootstrap lower confidence bound. After promotion, use quarter-Kelly, not half- or full-Kelly.
4. **Stop-risk sizing:** cap notional by equity risk divided by executable stop distance.
5. **Volatility scaling:** scale Kelly notional by `min(1, target_vol / realized_vol)`.
6. **Drawdown control:** block at a 10% peak-to-current equity drawdown.
7. **Daily stop:** block at the smaller of 0.001 ETH or 2% of start-of-day equity, using realized plus unrealized P/L.
8. **Turnover control:** at most three entries per UTC day and at least 600 seconds between entries.
9. **Cost-drag gate:** estimated round-trip cost may consume no more than 50% of expected gross edge.
10. **Persistent kill switch:** the existence of `03-Tooling/HALT_TRADING` blocks all live execution.

## Learnings rejected or modified

- Model confidence is not treated as win probability.
- The post's 10% per-position cap is rejected as unsuitable for low-liquidity memecoins.
- Half-Kelly is reduced to quarter-Kelly and remains zero before promotion.
- A week or two of paper trading is insufficient for strategy promotion; the existing 30-day, 100-signal forward-paper gate remains.
- A daily 5% loss limit is replaced by the stricter dynamic limit above.

## Implemented artifacts

- `03-Tooling/risk_manager.py`: expectancy, evidence-gated quarter-Kelly, volatility/stop-risk sizing, daily-loss, drawdown, turnover, cooldown, cost-drag, and kill-switch decisions.
- `03-Tooling/tests/test_risk_manager.py`: 12 deterministic risk tests.
- `03-Tooling/auto_trader.py`: risk manager runs before quote construction or signing; paper mode remains available for evidence collection.
- `03-Tooling/config.json`: conservative risk-policy knobs persisted.
- `03-Tooling/risk_state.json`: explicit unpromoted state with zero observations and zero confidence-bound edge.
- `03-Tooling/HALT_TRADING`: persistent halt marker.

## Effective policy

| Control | Value |
|---|---:|
| Kelly multiplier | 0.25 |
| Minimum edge observations | 150 |
| Target volatility | 2% |
| Maximum position | 0.002 ETH |
| Maximum equity risk per trade | 0.05% |
| Daily stop | min(0.001 ETH, 2% start-day equity) |
| Maximum drawdown | 10% |
| Maximum entries/day | 3 |
| Cooldown | 600 seconds |
| Maximum cost/gross-edge ratio | 50% |

## Verified current behavior

The current risk decision is `allowed=false`; reasons include active kill switch, strategy not promoted, insufficient observations, non-positive expectancy lower bound, missing current equity accounting, and cost drag. Calculated live size is exactly `0.0 ETH`. Paper mode remains available.

The full unit suite passes 55/55. This validates the gate mechanics, not profitability.

## Sources

[1] https://x.com/velesxbt/status/2095813892794408990 — Veles post on quant risk layers
    > "The half that keeps the account alive is the quant layer: the risk math that decides how much to bet, when to sit still, and when to stop."
[2] https://cleansky.io/blog/alpha-arena-llm-trading-benchmark-hyperliquid-2026 — Alpha Arena analysis and statistical limitations
    > "With a single 16-day season, what we have is an interesting anecdote, not statistical evidence."
[3] https://nypost.com/2025/11/08/business/ai-models-given-10k-to-compete-in-first-of-its-kind-crypto-trading-competition-and-most-crashed-and-burned — Alpha Arena reported results
    > "The experimental Alpha Arena contest from company Nof1 gave six AI models $10,000, identical input data and prompted each to make as much money as possible while trading crypto stocks on the open market from Oct. 17 to Nov. 3."
    > "ChatGPT, the most popular bot according to StatCounter, ended with just $3,794 — down 63%."
