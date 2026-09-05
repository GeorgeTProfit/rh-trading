#!/usr/bin/env python3
"""Position executor inspired by Hummingbot's TripleBarrier architecture.

Implements active position monitoring with sequential barrier checks:
stop_loss -> trailing_stop -> take_profit -> time_limit.

Adapted for Robinhood Chain Uniswap v3 swaps (no CLOB, no order-book events).
All PnL is computed from on-chain balance deltas; no event bus exists.

Architecture principles from Hummingbot:
- TripleBarrierConfig: stop_loss, take_profit, trailing_stop, time_limit
- Position state machine: OPEN -> RUNNING -> CLOSED (close_type enum)
- Order tracking: entry, exits, failed_orders, partial fills
- Net PnL = gross_pnl - fees (gas, DEX, slippage)
- Volatility-scaled barriers
- Retry logic for failed exit transactions

This is a pure-Python adaptation: no asyncio, no event bus, no CLOB connectors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional


# --- CloseType: why the position was closed ---

class CloseType(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_LIMIT = "time_limit"
    TRAILING_STOP = "trailing_stop"
    EARLY_STOP = "early_stop"
    EXPIRED = "expired"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    FAILED = "failed"


# --- TrailingStopConfig ---

@dataclass
class TrailingStopConfig:
    """Trailing stop parameters."""
    activation_price: float  # PnL% trigger (e.g. 0.10 = 10% gain)
    trailing_delta: float    # Trail distance from peak (e.g. 0.05 = 5% trail)


# --- TripleBarrierConfig ---

@dataclass
class TripleBarrierConfig:
    """Exit barrier configuration (Hummingbot's TripleBarrier pattern).

    All percentages are fractions (0.15 = 15%), not basis points.
    time_limit is in seconds (None = no time limit).
    """
    stop_loss: Optional[float] = None        # Max drawdown % before exit
    take_profit: Optional[float] = None      # Target gain % before exit
    time_limit: Optional[int] = None         # Max hold time in seconds
    trailing_stop: Optional[TrailingStopConfig] = None

    def scale(self, factor: float) -> "TripleBarrierConfig":
        """Return a new config with barriers scaled by volatility factor."""
        if factor <= 0 or factor == 1.0:
            return self
        new_tp = None
        if self.trailing_stop is not None:
            new_tp = TrailingStopConfig(
                activation_price=self.trailing_stop.activation_price * factor,
                trailing_delta=self.trailing_stop.trailing_delta * factor,
            )
        return TripleBarrierConfig(
            stop_loss=(self.stop_loss * factor if self.stop_loss else None),
            take_profit=(self.take_profit * factor if self.take_profit else None),
            time_limit=self.time_limit,
            trailing_stop=new_tp,
        )


# --- PositionExecutorConfig ---

@dataclass
class PositionExecutorConfig:
    """Configuration for a single position executor (Hummingbot-inspired)."""
    token: str                      # Token address (lowercase)
    symbol: str                     # Token symbol
    side: str                       # "BUY" or "SELL"
    amount_eth: float               # Entry cost in ETH (absolute)
    entry_price: float              # Price per token (ETH/token)
    triple_barrier: TripleBarrierConfig = field(default_factory=TripleBarrierConfig)
    max_retries: int = 10           # Retry count for failed exits
    fee_rate: float = 0.003         # DEX fee rate (e.g. 0.003 = 0.3%)
    slippage_estimate: float = 0.01 # Expected slippage fraction
    level_id: Optional[str] = None  # Strategy level identifier
    timestamp: Optional[float] = None  # Unix timestamp (now if None)


# --- TrackedOrder ---

@dataclass
class TrackedOrder:
    """Tracks a single order (entry or exit) with fill state."""
    order_id: str
    order_type: str
    is_filled: bool = False
    is_done: bool = False
    executed_amount: float = 0.0
    cum_fees: float = 0.0
    fill_price: float = 0.0
    last_update: Optional[float] = None
    error: Optional[str] = None

    @property
    def executed_value(self) -> float:
        return self.executed_amount * self.fill_price


# --- PositionExecutor ---

class PositionExecutor:
    """Manages a single position with active barrier monitoring.

    Lifecycle:
      1. ENTRY    - position opened, entry_tx recorded
      2. RUNNING  - active monitoring loop checks barriers
      3. CLOSED   - barrier triggered or position sold

    Barrier evaluation order (sequential, like Hummingbot):
      stop_loss -> trailing_stop -> take_profit -> time_limit
    """

    def __init__(self, config: PositionExecutorConfig):
        self.config = config
        self.entry_price = float(config.entry_price)
        self.amount_eth = float(config.amount_eth)

        # Order tracking
        self._entry_order: Optional[TrackedOrder] = None
        self._exit_order: Optional[TrackedOrder] = None
        self._failed_exits: List[TrackedOrder] = []
        self._trailing_stop_trigger: Optional[float] = None

        # State
        self.status: str = "OPEN"
        self.close_type: Optional[CloseType] = None
        self.peak_pnl_pct: float = 0.0
        self.current_pnl_pct: float = 0.0
        self.current_market_price: float = self.entry_price

        # Timing
        self.entry_ts = float(config.timestamp or time.time())
        self.close_ts: Optional[float] = None

        # PnL tracking
        self._cum_fees_eth: float = 0.0
        self._gas_cost_eth: float = 0.0
        self._slippage_eth: float = 0.0

    # --- Entry tracking ---

    def record_entry(self, tx_hash: str, token_amount: float,
                     gas_cost_eth: float, actual_fill_price: float):
        self._entry_order = TrackedOrder(
            order_id=tx_hash,
            order_type="entry",
            is_filled=True,
            is_done=True,
            executed_amount=token_amount,
            cum_fees=0.0,
            fill_price=actual_fill_price,
            last_update=time.time(),
        )
        self._gas_cost_eth = gas_cost_eth
        self.current_market_price = actual_fill_price
        self.status = "RUNNING"
        self.current_pnl_pct = 0.0
        self.peak_pnl_pct = 0.0

    # --- PnL properties ---

    @property
    def open_filled_amount(self) -> float:
        if not self._entry_order:
            return 0.0
        exit_amount = (self._exit_order.executed_amount if self._exit_order else 0.0)
        return self._entry_order.executed_amount - exit_amount

    @property
    def gross_pnl_pct(self) -> float:
        if self.entry_price <= 0 or self.open_filled_amount <= 0:
            return 0.0
        current_value = self.open_filled_amount * self.current_market_price
        entry_value = self.open_filled_amount * self.entry_price
        if self.config.side == "BUY":
            return (current_value - entry_value) / entry_value
        return (entry_value - current_value) / entry_value

    @property
    def net_pnl_pct(self) -> float:
        if self.entry_price <= 0 or self.open_filled_amount <= 0:
            return 0.0
        current_value = self.open_filled_amount * self.current_market_price
        entry_value = self.open_filled_amount * self.entry_price
        gross_pnl = (current_value - entry_value) if self.config.side == "BUY" else (entry_value - current_value)
        total_costs = self._gas_cost_eth + self._slippage_eth + self._cum_fees_eth
        return gross_pnl / entry_value - (total_costs / entry_value) if entry_value > 0 else 0.0

    @property
    def net_pnl_eth(self) -> float:
        if self.entry_price <= 0 or self.open_filled_amount <= 0:
            return 0.0
        current_value = self.open_filled_amount * self.current_market_price
        entry_value = self.open_filled_amount * self.entry_price
        gross_pnl = (current_value - entry_value) if self.config.side == "BUY" else (entry_value - current_value)
        total_costs = self._gas_cost_eth + self._slippage_eth + self._cum_fees_eth
        return gross_pnl - total_costs

    @property
    def entry_value_eth(self) -> float:
        return self.open_filled_amount * self.entry_price

    @property
    def current_value_eth(self) -> float:
        return self.open_filled_amount * self.current_market_price

    @property
    def is_closed(self) -> bool:
        return self.status == "CLOSED"

    @property
    def is_trading(self) -> bool:
        return self.status == "RUNNING" and self.open_filled_amount > 0

    @property
    def is_expired(self) -> bool:
        if not self.config.triple_barrier.time_limit:
            return False
        elapsed = time.time() - self.entry_ts
        return elapsed >= self.config.triple_barrier.time_limit

    # --- Barrier control (Hummingbot pattern) ---

    def control_barriers(self) -> Optional[CloseType]:
        if self.status != "RUNNING" or self.open_filled_amount <= 0:
            return None

        # 1. Stop loss
        if self._check_stop_loss():
            return self.close_type
        # 2. Trailing stop
        if self._check_trailing_stop():
            return self.close_type
        # 3. Take profit
        if self._check_take_profit():
            return self.close_type
        # 4. Time limit
        if self.is_expired:
            self._close_position(CloseType.TIME_LIMIT)
            return self.close_type
        return None

    def _check_stop_loss(self) -> bool:
        if self.config.triple_barrier.stop_loss is None:
            return False
        if self.net_pnl_pct <= -self.config.triple_barrier.stop_loss:
            self._close_position(CloseType.STOP_LOSS)
            return True
        return False

    def _check_trailing_stop(self) -> bool:
        if self.config.triple_barrier.trailing_stop is None:
            return False
        ts = self.config.triple_barrier.trailing_stop
        gross = self.gross_pnl_pct
        if gross > self.peak_pnl_pct:
            self.peak_pnl_pct = gross
            self.current_pnl_pct = gross
        if self._trailing_stop_trigger is None:
            if self.peak_pnl_pct > ts.activation_price:
                self._trailing_stop_trigger = self.peak_pnl_pct - ts.trailing_delta
        else:
            if self.current_pnl_pct < self._trailing_stop_trigger:
                self._close_position(CloseType.TRAILING_STOP)
                return True
            new_trigger = self.peak_pnl_pct - ts.trailing_delta
            if new_trigger > self._trailing_stop_trigger:
                self._trailing_stop_trigger = new_trigger
        return False

    def _check_take_profit(self) -> bool:
        if self.config.triple_barrier.take_profit is None:
            return False
        if self.net_pnl_pct >= self.config.triple_barrier.take_profit:
            self._close_position(CloseType.TAKE_PROFIT)
            return True
        return False

    # --- Close position ---

    def _close_position(self, close_type: CloseType):
        if self.status != "RUNNING":
            return
        self.close_type = close_type
        self.close_ts = time.time()
        self.status = "CLOSED"

    def record_exit(self, tx_hash: str, token_amount: float,
                    exit_price: float, gas_cost_eth: float,
                    close_type: CloseType, error: Optional[str] = None):
        order = TrackedOrder(
            order_id=tx_hash,
            order_type="exit",
            is_filled=error is None,
            is_done=True,
            executed_amount=token_amount,
            cum_fees=0.0,
            fill_price=exit_price,
            last_update=time.time(),
            error=error,
        )
        if error is not None:
            order.is_done = False
            self._failed_exits.append(order)
            if len(self._failed_exits) >= self.config.max_retries:
                self._close_position(CloseType.FAILED)
        else:
            self._exit_order = order
            self._cum_fees_eth += self.config.fee_rate * token_amount * exit_price
            self._gas_cost_eth += gas_cost_eth
            if self.open_filled_amount <= 0:
                self._close_position(close_type)

    def update_market_price(self, new_price: float):
        self.current_market_price = new_price
        self.current_pnl_pct = self.gross_pnl_pct
        if self.current_pnl_pct > self.peak_pnl_pct:
            self.peak_pnl_pct = self.current_pnl_pct

    def partial_exit(self, token_amount: float, exit_price: float,
                     gas_cost_eth: float):
        if token_amount <= 0 or token_amount > self.open_filled_amount:
            return False
        self._exit_order = TrackedOrder(
            order_id=f"partial_{token_amount:.6f}",
            order_type="exit",
            is_filled=True,
            is_done=True,
            executed_amount=token_amount,
            cum_fees=0.0,
            fill_price=exit_price,
            last_update=time.time(),
        )
        self._cum_fees_eth += self.config.fee_rate * token_amount * exit_price
        self._gas_cost_eth += gas_cost_eth
        if self.open_filled_amount <= 0:
            self._close_position(CloseType.EARLY_STOP)
        return True

    def adjust_for_volatility(self, realized_vol: float, target_vol: float = 0.02):
        if realized_vol <= 0:
            return
        vol_factor = min(1.0, target_vol / realized_vol) if target_vol > 0 else 1.0
        self.config.triple_barrier = self.config.triple_barrier.scale(1.0 / vol_factor)

    # --- Summary ---

    def status_report(self) -> Dict:
        return {
            "token": self.config.token,
            "symbol": self.config.symbol,
            "side": self.config.side,
            "entry_price": self.entry_price,
            "current_market_price": self.current_market_price,
            "open_amount": self.open_filled_amount,
            "entry_value_eth": self.entry_value_eth,
            "current_value_eth": self.current_value_eth,
            "gross_pnl_pct": self.gross_pnl_pct,
            "net_pnl_pct": self.net_pnl_pct,
            "net_pnl_eth": self.net_pnl_eth,
            "cum_fees_eth": self._cum_fees_eth,
            "gas_cost_eth": self._gas_cost_eth,
            "status": self.status,
            "close_type": self.close_type.value if self.close_type else None,
            "is_closed": self.is_closed,
            "is_trading": self.is_trading,
            "is_expired": self.is_expired,
            "trailing_stop_trigger": self._trailing_stop_trigger,
            "peak_pnl_pct": self.peak_pnl_pct,
            "failed_exits_count": len(self._failed_exits),
            "max_retries": self.config.max_retries,
            "entry_ts": self.entry_ts,
            "close_ts": self.close_ts,
            "stop_loss": self.config.triple_barrier.stop_loss,
            "take_profit": self.config.triple_barrier.take_profit,
            "time_limit": self.config.triple_barrier.time_limit,
        }

    def format_status(self, scale: int = 60) -> str:
        lines = []
        if self.is_closed and self.close_type:
            exit_price_str = f"{self._exit_order.fill_price:.6f}" if self._exit_order else "N/A"
            lines.append(
                f"{'=' * 72}\n"
                f"  {self.config.symbol} | {self.config.side} | CLOSED\n"
                f"  Close type: {self.close_type.value}\n"
                f"  Entry: {self.entry_price:.6f} | Exit: {exit_price_str}\n"
                f"  PnL: {self.net_pnl_pct * 100:.2f}% | Net: {self.net_pnl_eth:.8f} ETH"
            )
        elif self.is_trading:
            lines.append(
                f"{'=' * 72}\n"
                f"  {self.config.symbol} | {self.config.side} | RUNNING\n"
                f"  Entry: {self.entry_price:.6f} | Current: {self.current_market_price:.6f}\n"
                f"  Gross PnL: {self.gross_pnl_pct * 100:.2f}% | Net: {self.net_pnl_pct * 100:.2f}%\n"
                f"  Net PnL: {self.net_pnl_eth:.8f} ETH"
            )
            if self.config.triple_barrier.stop_loss and self.config.triple_barrier.take_profit:
                sl_price = self.entry_price * (1 - self.config.triple_barrier.stop_loss)
                tp_price = self.entry_price * (1 + self.config.triple_barrier.take_profit)
                price_range = tp_price - sl_price
                if price_range > 0:
                    progress = (self.current_market_price - sl_price) / price_range
                    progress = max(0, min(1, progress))
                    filled = int(scale * progress)
                    price_bar = f"[SL{'-' * max(0,filled)}|{'-' * max(0,scale-filled)}TP]"
                    lines.append(f"  {sl_price:.5f}{price_bar}{tp_price:.5f}")
            if self.config.triple_barrier.trailing_stop and self._trailing_stop_trigger:
                lines.append(f"  Trailing stop trigger: {self._trailing_stop_trigger * 100:.2f}%")
            if self.config.triple_barrier.time_limit:
                elapsed = time.time() - self.entry_ts
                remaining = self.config.triple_barrier.time_limit - elapsed
                if remaining > 0:
                    lines.append(f"  Time remaining: {remaining:.0f}s")
            lines.append("-" * 72)
        return "\n".join(lines)


# --- PositionManager ---

class PositionManager:
    """Manages a collection of PositionExecutors."""

    def __init__(self):
        self._executors: Dict[str, PositionExecutor] = {}

    def add(self, config: PositionExecutorConfig) -> PositionExecutor:
        executor = PositionExecutor(config)
        self._executors[config.token] = executor
        return executor

    def remove(self, token: str) -> Optional[PositionExecutor]:
        return self._executors.pop(token, None)

    def get(self, token: str) -> Optional[PositionExecutor]:
        return self._executors.get(token)

    def update_all_prices(self, price_map: Dict[str, float]):
        for executor in self._executors.values():
            if executor.is_trading and executor.config.token in price_map:
                executor.update_market_price(price_map[executor.config.token])

    def check_all_barriers(self) -> List[PositionExecutor]:
        triggered = []
        for executor in self._executors.values():
            if executor.status == "RUNNING":
                close_type = executor.control_barriers()
                if close_type is not None:
                    triggered.append(executor)
        return triggered

    @property
    def open_positions(self) -> List[PositionExecutor]:
        return [e for e in self._executors.values() if e.is_trading]

    @property
    def closed_positions(self) -> List[PositionExecutor]:
        return [e for e in self._executors.values() if e.is_closed]

    @property
    def aggregate_pnl_eth(self) -> float:
        return sum(e.net_pnl_eth for e in self._executors.values())

    def summary(self) -> Dict:
        return {
            "total_positions": len(self._executors),
            "open": len(self.open_positions),
            "closed": len(self.closed_positions),
            "aggregate_pnl_eth": self.aggregate_pnl_eth,
            "positions": [
                executor.status_report()
                for executor in self._executors.values()
            ],
        }
