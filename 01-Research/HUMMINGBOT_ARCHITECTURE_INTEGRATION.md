# Hummingbot Architecture Integration

## What Was Studied

Hummingbot's open-source Python framework (Apache 2.0, 140+ exchange connectors) provides battle-tested patterns for automated trading. The key component studied was the `PositionExecutor` at:

- `hummingbot/strategy_v2/executors/position_executor/position_executor.py`
- `hummingbot/strategy_v2/executors/position_executor/data_types.py`

## Key Architectural Patterns Adopted

### 1. Triple Barrier Control (Exact Port)

Hummingbot manages each position with **three sequential exit barriers**:

```
control_barriers():
  1. stop_loss      → OPEN → CLOSED (STOP_LOSS)
  2. trailing_stop  → tracks peak PnL, fires on drawdown
  3. take_profit    → OPEN → CLOSED (TAKE_PROFIT)
  4. time_limit     → OPEN → CLOSED (TIME_LIMIT)
```

Each barrier is checked in order, and the sequence short-circuits on fire. This prevents a time limit closing a position that should have stopped earlier.

**Implemented in**: `03-Tooling/position_executor.py` — `PositionExecutor.control_barriers()` at line 248

### 2. Position State Machine

Hummingbot uses a three-state lifecycle:
- **OPEN** → Executor created, entry order pending
- **RUNNING** → Entry filled, position active, barriers monitored
- **CLOSED** → Exit completed (barrier triggered or manual)

**Implemented in**: `position_executor.py` — `self.status` in `["OPEN", "RUNNING", "CLOSED"]`

### 3. CloseType Enum

Explicit exit reason tracking, exactly matching Hummingbot's 8 types:

| CloseType | Meaning |
|---|---|
| `TAKE_PROFIT` | Take profit barrier triggered |
| `STOP_LOSS` | Stop loss barrier triggered |
| `TIME_LIMIT` | Max hold time expired |
| `TRAILING_STOP` | Trailing stop fired after peak |
| `EARLY_STOP` | Manual/strategy stop |
| `EXPIRED` | Position expired before entry |
| `INSUFFICIENT_BALANCE` | Not enough budget |
| `FAILED` | Max retries exceeded on exit |

**Implemented in**: `position_executor.py` — `CloseType` enum (line 31)

### 4. Net PnL = Gross PnL − All Costs

Hummingbot's PositionExecutor tracks `trade_pnl_quote - cum_fees_quote = net_pnl_quote`. We adapted this for on-chain swaps:

```python
net_pnl_eth = gross_pnl_eth - gas_cost_eth - slippage_eth - cum_fees_eth
net_pnl_pct = (gross_pnl - total_costs) / entry_value
```

**Implemented in**: `PositionExecutor.net_pnl_eth` and `net_pnl_pct` (lines 214-222)

### 5. Volatility-Adjusted Barriers

Hummingbot's `TripleBarrierConfig.new_instance_with_adjusted_volatility()`
scales barriers multiplicatively when realized volatility differs from target. Higher realized vol → wider barriers (wider stops, wider targets).

**Implemented in**: `position_executor.py` — `TripleBarrierConfig.scale()` (line 65) and `PositionExecutor.adjust_for_volatility()` (line 360)

### 6. Failed Exit Retries

Hummingbot retries failed close orders up to `max_retries`. We mirror this: failed exit transactions are tracked in `_failed_exits` and position closes as `FAILED` when max_retries is exceeded.

**Implemented in**: `PositionExecutor.record_exit()` (line 316)

### 7. PositionManager (Aggregation)

Hummingbot manages executors in a pool. Our `PositionManager` provides:
- `add(config)` / `remove(token)` / `get(token)`
- `update_all_prices(price_map)` — batch price update
- `check_all_barriers()` — run barriers on all RUNNING positions
- `aggregate_pnl_eth` — sum across all positions
- `summary()` — full status report

**Implemented in**: `position_executor.py` — `PositionManager` class (line 440)

## What Was Rejected (Doesn't Apply)

| Hummingbot Pattern | Reason Rejected |
|---|---|
| Event bus (process_order_created, etc.) | CLOB-centric; our system has no exchange order events |
| Order book price fetching | No limit order book on Robinhood Chain DEX |
| Order candidates / balance validation | On-chain swaps verify balance at transaction time, not pre-flight |
| `connector_name` / `trading_pair` abstraction | Unnecessary indirection for single-chain setup |
| Gateway middleware | Robinhood Chain is direct RPC, no gateway needed |
| Perpetual / leverage support | Spot-only strategy |
| Limit order types | Uniswap v3 only supports market swaps |

## Files Changed

| File | Changes |
|---|---|
| `03-Tooling/position_executor.py` | **NEW** — 496 lines. Full TripleBarrier executor + PositionManager |
| `03-Tooling/tests/test_position_executor.py` | **NEW** — 646 lines. 36 tests: barriers, PnL, trailing, retries, volatility, aggregation |

## Test Coverage

**91/91 tests passing** (+36 from prior):

| Test file | Count | Coverage |
|---|---|---|
| `test_position_executor.py` | 36 | Barriers, PnL, state machine, retries, volatility, aggregation |
| `test_risk_manager.py` | 12 | Expectancy, Kelly, position sizing, risk gate |
| `test_auto_trader.py` | 8 | Denylist, quant-risk gate, token approval, paper mode |
| `test_rh_swap.py` | 14 | Calldata, denylist, receipt, normalize, slippage |
| `test_swap_collector.py` | 13 | Decode, direction, pagination, RPC,  CLI |
| `test_trader_copy_strategy.py` | 8 | Trader selection, consensus, fill safety |

## Integration Points

The `PositionExecutor` is designed to be called from:
- **`auto_trader.py`** — After executing a buy (via `rh_swap`), instantiate `PositionExecutor` with the config and call `record_entry()`. Run `control_barriers()` periodically as part of the monitoring loop.
- **`backtest.py`** (not yet built) — Replay historical price data through the barrier logic, recording `CloseType` for each position.
- **`risk_manager.py`** — Positions contribute to daily PnL tracking and drawdown calculations via `status_report().net_pnl_eth`.

### Live Monitor Loop (next step)

```python
manager = PositionManager()
manager.add(config)
executor.record_entry(tx_hash, amount, gas, price)

while position.is_trading:
    price = fetch_current_price(weth, token)
    manager.update_all_prices({token: price})
    triggered = manager.check_all_barriers()
    for closed in triggered:
        execute_exit(closed)  # calls rh_swap for sell
    time.sleep(interval)
```
