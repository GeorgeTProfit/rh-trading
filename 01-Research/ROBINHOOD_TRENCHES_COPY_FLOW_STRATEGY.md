# Robinhood Trenches Copy-Flow Strategy v0.1

**Status:** paper research only; no evidence of positive expectancy and no authorization for arbitrary-token live execution.

## Evidence snapshot

Captured from the public dashboard/API on 2026-09-05. The seven-day overview reported 14,937 fills across 1,311 tokens, $59.49M volume, -$836,540 realized P/L, +$8.31M unrealized P/L, 2,016 closed trades, and a 42.76% win rate.[2]

The trader endpoint exposes wallet/handle, volume, fills, realized P/L, closed trades, wins, best/worst trade, open-bag cost/value, unrealized P/L, and activity state for 108 tracked traders.[3] The tape exposes transaction-level side, USD amount, price, first-position status, block, pricing basis, wallet, token, liquidity, and flags.[1][4]

Headline net P/L is unsuitable as a copy ranking because it combines realized outcomes with marked open bags. In the captured seven-day snapshot, some of the highest headline accounts had negative realized P/L, and dashboard-wide positive net P/L was more than explained by unrealized marks.[2][3]

## Hypothesis

A delayed follower may have positive net expectancy only when a historically repeatable trader's genuine first buy is independently confirmed by another qualified trader or is unusually large, while executable liquidity and order flow remain favorable.

This is a falsifiable hypothesis, not a profitability claim.

## Frozen trader selection

At the start of each walk-forward fold:

1. Use only trades closed before the fold starts.
2. Require at least 5 closed trades, positive realized P/L, positive economic volume, and active status.
3. Shrink win rate with a Beta(5,5) prior: `(wins + 5) / (closed + 10)`; require at least 0.50.
4. Score using 55% shrunk win rate, 25% capped realized-P/L/volume, and 20% sample confidence capped at 25 trades.
5. Ignore follower count and unrealized P/L.
6. Exclude deployers, team wallets, routers, liquidity managers, linked/sybil wallets, wash traders, transfer-funded positions, and wallets that repeatedly sell into follower inflows when those classifications become available.

The 2026-09-05 seven-day snapshot yielded 13 preliminary eligible wallets out of 108. That cohort is frozen for forward paper observation; it is not a live allowlist.[3]

## Entry signal

A dashboard fill is eligible only if all are true:

- Genuine `buy`, `new_position=1`, `priced=cash_leg`, no airdrop/not-real-buy flag.
- Memecoin, not tokenized stock.
- Leader buy at least $250.
- Dashboard liquidity at least $50,000.
- Leader fill no more than 5% of displayed liquidity.
- Signal age no more than 120 seconds.
- Either two distinct qualified leaders first-buy the token within 120 seconds, or one elite leader (score >=0.75) first-buys at least $2,000.

Before any hypothetical order, require the independent chain gate: permanent denylist, approved route/pool/router, fresh quote, exact buy simulation, gas estimate, 2% executable depth, holder/deployer checks, executable approve and sell simulations, tax/FoT rejection, and stale-signal rejection.

## Exit signal

Never mirror a leader's sizing or depend exclusively on their exit. Use our own executable-price controls:

1. Emergency exit on sellability failure, liquidity removal >=25%, or route invalidation.
2. Hard stop at -12% versus executable entry value including gas.
3. Partial profit at +20%; trail the remainder 10% below the highest executable mark.
4. Flow exit when two qualified leaders sell or short-window sell imbalance exceeds the frozen threshold.
5. Time stop at 10 minutes when momentum does not continue.
6. A leader sell may accelerate an exit but may not loosen any risk rule.

## Position sizing

Paper sizing initially models the minimum of:

- 0.002 ETH absolute cap.
- 0.05% wallet equity at risk divided by stop distance plus round-trip cost.
- 2% of executable sell-side depth.
- 10% of recent qualified-leader net inflow.

No arbitrary-token live execution is permitted until the sell path and all promotion gates pass.

## Causal evaluation

The dashboard does not expose historical leaderboard snapshots. Therefore, selecting today's leaders and replaying their earlier fills would be look-ahead biased. Validation must construct rolling rankings from timestamped fills/closed positions or freeze a cohort now and score only future actions.

Required comparison arms:

- Order-flow momentum without trader identity.
- Qualified-trader first buys without consensus.
- Qualified-trader consensus plus order flow.
- Random matched entries by token age/liquidity/time.

Replay the follower at observed event time plus measured ingestion and transaction latency, never at the leader's price. Charge DEX fees, gas including reverts/approvals, tick-crossing impact, slippage, taxes, MEV stress, and unsellable outcomes.

## Promotion gates

Historical out-of-sample gate:

- At least 150 completed trades, 50 tokens, and 30 days.
- Profit factor >=1.20 after all costs.
- Clustered-bootstrap 95% lower bound of mean net P/L per trade >0.
- Maximum drawdown <=15%.
- No token contributes >15% of total P/L.
- Remains positive under 2x gas, +50% slippage, +1 block latency, and 1% per-leg MEV stress; stressed profit factor >=1.05.

Forward paper gate:

- At least 30 days, 100 executable signals, and 30 tokens.
- Event capture >=99.5%; quote/simulation success >=98%.
- Zero unexplained sell failures or false-safe tokens.
- Positive net expectancy at measured latency; bootstrap 95% lower bound >0.

Only after both gates and a verified buy-approve-sell lifecycle may a 0.002 ETH live canary be considered. Any failed safety or expectancy gate returns the system to paper mode.

## Current result

The implemented one-shot evaluator found zero qualifying fresh signals in the captured 400-fill tape. Correct action: no trade. The strategy has not been shown profitable.

## Artifacts

- `03-Tooling/trader_copy_strategy.py` — read-only dashboard fetch, trader scoring, copyability filters, consensus signal builder.
- `03-Tooling/tests/test_trader_copy_strategy.py` — 10 unit tests.
- `03-Tooling/data/trenches_copy_baseline_7d.json` — captured seven-day baseline and zero-signal result.

## Sources

[1] https://robinhoodtrenches.com — Robinhood Trenches dashboard
    > "chain robinhood/4663  wallets 108  block 54,678,706  feed websocket  lag 0.1s  block→tape 1.1s (p90 1.5s)  fills indexed 30,084  replaying last 40 fills"
[2] https://robinhoodtrenches.com/api/overview?window=7d&stocks=false — Robinhood Trenches seven-day overview API
    > ""window": "7d", "fills": 14938, "buys": 8993, "sells": 5945, "active_traders": 93, "tokens": 1311, "volume": 59489054.38631937, "bought": 32195840.71572604, "sold": 27293213.670593332, "realized_pnl": -837603.0147080706, "unrealized_pnl": 8490703.658259904, "net_pnl": 7653100.643551834, "open_bags": 1593, "open_cost": 11679123.946802942, "open_value": 20169827.605062846, "unpriced_bags": 432, "closed_trades": 2017, "win_rate": 0.42736737729300944"
[3] https://robinhoodtrenches.com/api/traders?window=7d&stocks=false — Robinhood Trenches seven-day trader API
    > "{"address": "0x9ce0cb4a193acbce0dca3283972341aed6f3f614", "handle": "PoorGoat_", "display_name": "PoorGoat\ud83d\udc02\ud83c\udc04\ufe0f\ud83d\udc9b\ud83d\udc08", "followers": 497807, "profile_url": "https://fomo.family/profile/PoorGoat_", "volume": 6888.644920164178, "fills": 17, "buys": 17, "sells": 0, "last_ts": 1788531230, "realized_pnl": 0, "closed_trades": 0, "wins": 0, "best_trade": null, "worst_trade": null, "open_bags": 24, "open_cost": 6900.102489265827, "open_value": 6899.788445672957, "unrealized_pnl": -0.3140435928698935, "net_pnl": -0.3140435928698935, "win_rate": null, "state": "flat", "active": true}"
[4] https://robinhoodtrenches.com/api/tape?limit=400&stocks=false — Robinhood Trenches live tape API
    > "{"id": 38587, "ts": 1788589971, "tx": "0xa486fc61261523d4adc8c00a27ab388b464a8ca231a86d1f1e965c5620504115", "side": "buy", "usd": 48.755765, "amount": 261233.90587611918, "price": 0.0001866364354063621, "new_position": 0, "is_stock": 0, "block": 54909353, "priced": "cash_leg", "handle": "31337___", "display_name": "31337", "followers": 38559, "wallet": "0xc9cafef2258a07f8455da92438360318b34848e2", "token": "0xf0dbd85e85bd6704b00eedb7c2590559ca12b871", "symbol": "RADIO", "name": "Radio", "mark": 0.0001824, "liquidity": 39154.77, "pair_url": "https://dexscreener.com/robinhood/0x5ddec6dcb59c730efd3ac1c7a0db6dfeb6696e2d83e2573ad051c26a45c69cc3", "flags": []}"
