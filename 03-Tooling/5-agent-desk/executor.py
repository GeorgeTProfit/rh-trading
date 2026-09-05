"""Safe paper/live execution adapter for the copy desk.

The desk supplies signals; the parent risk manager, allowlist and hardened
Robinhood Chain swap module retain final authority. Paper mode uses the same
fresh quote and exact buy simulation but never signs.
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

from config import CONFIG
from state import log_trade, load_positions, save_positions, load_desk_state, save_desk_state

PARENT = Path(__file__).parent.parent
sys.path.insert(0, str(PARENT))

import auto_trader
import rh_swap
import risk_accounting
from position_executor import (
    PositionExecutorConfig,
    PositionManager,
    TrailingStopConfig,
    TripleBarrierConfig,
)
from rh_swap import build_plan, display_plan, execute_plan


def _policy() -> dict:
    return {
        "min_edge_observations": 75,
        "kelly_multiplier": CONFIG.kelly_multiplier,
        "target_volatility": 0.02,
        "max_position_eth": min(CONFIG.max_position_eth, 0.002),
        "max_risk_fraction": CONFIG.max_risk_fraction,
        "daily_stop_fraction": CONFIG.daily_stop_fraction,
        "max_daily_loss_eth": CONFIG.max_daily_loss_eth,
        "max_drawdown_fraction": CONFIG.max_drawdown_fraction,
        "max_trades_per_day": CONFIG.max_trades_per_day,
        "cooldown_seconds": CONFIG.cooldown_seconds,
        "max_cost_to_gross_edge": 0.50,
        "approved_tokens": CONFIG.approved_tokens,
        "blacklist": CONFIG.denied_tokens,
    }


def _default_risk_state(equity_eth: float = 0.0) -> dict:
    return {
        "kill_switch_active": True,
        "strategy_promoted": False,
        "observations": 0,
        "expectancy_lower_bound_eth": 0.0,
        "equity_start_day_eth": equity_eth,
        "equity_peak_eth": equity_eth,
        "equity_current_eth": equity_eth,
        "realized_pnl_today_eth": 0.0,
        "unrealized_pnl_today_eth": 0.0,
        "trades_today": 0,
        "last_trade_ts": 0,
        "expected_gross_edge_eth": 0.0,
        "round_trip_cost_eth": 0.0,
    }


def _load_risk_state() -> dict:
    """Use the parent risk state so HALT_TRADING cannot be bypassed."""
    state = auto_trader.load_risk_state()
    return state if isinstance(state, dict) else _default_risk_state()


def _save_risk_state(risk_state: dict):
    """Persist authoritative parent risk state and mirror telemetry locally."""
    risk_accounting.persist_state(auto_trader.RISK_STATE, risk_state)
    state = load_desk_state() or {}
    state["risk"] = {**risk_state, "_updated_ts": time.time()}
    save_desk_state(state)


def _barriers() -> TripleBarrierConfig:
    return TripleBarrierConfig(
        stop_loss=CONFIG.stop_loss_pct,
        take_profit=CONFIG.take_profit_pct,
        time_limit=CONFIG.time_limit_seconds,
        trailing_stop=TrailingStopConfig(
            activation_price=CONFIG.trail_activation,
            trailing_delta=CONFIG.trail_delta,
        ),
    )


def _load_position_manager() -> PositionManager:
    pm = PositionManager()
    positions = load_positions()
    if not isinstance(positions, dict):
        return pm
    for token, data in positions.get("open", {}).items():
        try:
            cfg = PositionExecutorConfig(
                token=token,
                symbol=data.get("symbol", "?"),
                side="BUY",
                amount_eth=float(data["amount_eth"]),
                entry_price=float(data["entry_price_eth_per_token"]),
                triple_barrier=_barriers(),
                timestamp=float(data["entry_ts"]),
            )
            position = pm.add(cfg)
            position._token_amount_raw = int(data.get("token_amount_raw", 0))
            position._token_decimals = int(data.get("token_decimals", 18))
            position._evidence = dict(data.get("evidence", {}))
            position.record_entry(
                data.get("tx_hash", "recovered"),
                float(data["token_amount"]),
                float(data.get("entry_gas_cost_eth", 0)),
                float(data["entry_price_eth_per_token"]),
            )
            position.current_market_price = float(data.get("current_price_eth_per_token", data["entry_price_eth_per_token"]))
            position.peak_pnl_pct = float(data.get("peak_pnl_pct", 0))
        except (KeyError, TypeError, ValueError):
            continue
    return pm


def _save_position_manager(pm: PositionManager):
    previous = load_positions()
    closed = previous.get("closed", []) if isinstance(previous, dict) else []
    data = {"open": {}, "closed": closed}
    for pos in pm.open_positions:
        data["open"][pos.config.token] = {
            "symbol": pos.config.symbol,
            "side": pos.config.side,
            "amount_eth": pos.amount_eth,
            "token_amount": pos.open_filled_amount,
            "token_amount_raw": int(getattr(pos, "_token_amount_raw", 0)),
            "token_decimals": int(getattr(pos, "_token_decimals", 18)),
            "entry_price_eth_per_token": pos.entry_price,
            "current_price_eth_per_token": pos.current_market_price,
            "gross_pnl_pct": pos.gross_pnl_pct,
            "net_pnl_pct": pos.net_pnl_pct,
            "peak_pnl_pct": pos.peak_pnl_pct,
            "entry_ts": pos.entry_ts,
            "tx_hash": pos._entry_order.order_id if pos._entry_order else None,
            "entry_gas_cost_eth": pos._gas_cost_eth,
            "status": pos.status,
            "evidence": dict(getattr(pos, "_evidence", {})),
        }
    save_positions(data)


def _quote_preflight(signal: dict, amount_eth: float, *, paper: bool) -> dict:
    token = rh_swap.norm_addr(signal.get("token", ""))
    if amount_eth <= 0:
        return {"error": "zero_amount", "paper": paper}
    try:
        plan = build_plan(
            token,
            int(Decimal(str(amount_eth)) * Decimal(10**18)),
            wallet=rh_swap.TRENCHES_WALLET,
            fee=CONFIG.pool_fee,
            slippage_bps=CONFIG.slippage_bps,
            max_gas_cost_wei=int(Decimal(str(CONFIG.max_gas_cost_eth)) * Decimal(10**18)),
        )
    except Exception as exc:
        return {"error": str(exc), "paper": paper, "amount_eth": amount_eth}
    return {
        "paper": paper,
        "token": token,
        "amount_eth": amount_eth,
        "swap_plan": plan,
        "plan": display_plan(plan),
    }


def _paper_preflight(signal: dict) -> dict:
    return _quote_preflight(signal, min(CONFIG.max_position_eth, 0.002), paper=True)


def _real_preflight(signal: dict, risk_state: Optional[dict] = None) -> dict:
    state = risk_state or _load_risk_state()
    amount_eth = auto_trader.live_position_size(state, _policy())
    return _quote_preflight(signal, amount_eth, paper=False)


def _token_scope_result(token: str, *, paper_mode: bool) -> Optional[dict]:
    cfg = _policy()
    if auto_trader.token_is_denied(token, cfg):
        return {"outcome": "token_denied", "token": token}
    if not paper_mode and not auto_trader.token_is_approved(token, cfg):
        return {"outcome": "token_not_approved", "token": token}
    return None


def execute_trade(signal: dict, *, paper_mode: bool = True) -> dict:
    """Quote and open one evidence/live position without unsafe fallbacks."""
    now_ts = time.time()
    try:
        token = rh_swap.norm_addr(signal.get("token", ""))
    except ValueError as exc:
        return {"outcome": "invalid_signal", "error": str(exc)}
    scope = _token_scope_result(token, paper_mode=paper_mode)
    if scope:
        log_trade({**scope, "event": "executor_gate", "ts": now_ts})
        return scope

    risk_state = _load_risk_state()
    if not paper_mode:
        gate = auto_trader.execution_risk_decision(
            risk_state, live_mode=True, now_ts=now_ts, config=_policy()
        )
        if not gate["allowed"]:
            result = {"outcome": "risk_gated", "reasons": gate["reasons"]}
            log_trade({**result, "event": "executor_gate", "ts": now_ts, "token": token})
            return result
        if not os.environ.get("RH_PRIVATE_KEY"):
            return {"outcome": "not_sent", "error": "RH_PRIVATE_KEY is not set"}

    pm = _load_position_manager()
    if pm.get(token):
        return {"outcome": "position_exists", "token": token}

    preflight = _paper_preflight(signal) if paper_mode else _real_preflight(signal, risk_state)
    if preflight.get("error"):
        result = {"outcome": "preflight_failed", **preflight}
        log_trade({**result, "event": "executor_preflight", "ts": now_ts})
        return result

    plan = preflight["swap_plan"]
    execution = None
    if paper_mode:
        token_delta_raw = int(plan.quoted_out)
        tx_hash = "paper:" + str(signal.get("tx_hash", "unknown"))
        outcome = "paper_opened"
        gas_cost_eth = plan.gas_estimate * plan.max_fee_per_gas / 10**18
    else:
        execution = execute_plan(plan)
        if not auto_trader.is_verified_purchase(execution):
            result = {"outcome": execution.get("outcome", "execution_failed"), "execution": execution}
            log_trade({**result, "event": "executor_failed", "ts": now_ts, "token": token})
            return result
        token_delta_raw = int(execution["token_balance_delta"])
        tx_hash = execution["tx_hash"]
        outcome = "executed"
        gas_cost_eth = int(execution.get("gas_used") or 0) * plan.max_fee_per_gas / 10**18

    token_amount = token_delta_raw / (10 ** plan.token_decimals)
    amount_eth = plan.amount_in_wei / 10**18
    entry_price = amount_eth / token_amount
    cfg = PositionExecutorConfig(
        token=token,
        symbol=signal.get("symbol", plan.token_symbol),
        side="BUY",
        amount_eth=amount_eth,
        entry_price=entry_price,
        triple_barrier=_barriers(),
        timestamp=now_ts,
    )
    position = pm.add(cfg)
    position._token_amount_raw = token_delta_raw
    position._token_decimals = plan.token_decimals
    position._evidence = {
        "source_wallet": signal.get("wallet"),
        "wallet_class": signal.get("wallet_class"),
        "composite_score": signal.get("composite_score"),
        "signal_timestamp": signal.get("timestamp"),
    }
    position.record_entry(tx_hash, token_amount, gas_cost_eth, entry_price)
    _save_position_manager(pm)

    if not paper_mode:
        updated_risk = risk_accounting.apply_entry(
            risk_state, gas_cost_eth=gas_cost_eth, now_ts=now_ts
        )
        _save_risk_state(updated_risk)

    result = {
        "outcome": outcome,
        "paper": paper_mode,
        "token": token,
        "symbol": cfg.symbol,
        "amount_eth": amount_eth,
        "token_amount": token_amount,
        "entry_price_eth_per_token": entry_price,
        "tx_hash": tx_hash,
        "plan": preflight["plan"],
    }
    if execution is not None:
        result["execution"] = execution
    log_trade({**result, "event": "executor_execute", "ts": now_ts})
    return result


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    print(json.dumps(execute_trade(payload, paper_mode=True), indent=2, default=str))
