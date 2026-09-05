"""EXIT_MANAGER — active position monitoring + barrier management.
Reviews every open position on every pulse and adjusts individually.

Zynex: "Current exit rules are simple: trail stops, 2x trims, dead-time closes.
The next version has a sixth agent — REAPER — that reviews every open position."

This is REAPER-lite: monitors positions, checks barriers, triggers exits.

Cost: $0 — pure math + on-chain balance checks.
"""
from __future__ import annotations
import json, time, urllib.request, sys
from typing import Optional
from pathlib import Path
from state import log_trade, load_positions, save_positions, load_desk_state, save_desk_state
from config import CONFIG

sys.path.insert(0, str(Path(__file__).parent.parent))
import rh_sell
import auto_trader
import risk_accounting

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from position_executor import (
        PositionExecutor, PositionExecutorConfig, TripleBarrierConfig,
        TrailingStopConfig, PositionManager, CloseType
    )
except ImportError:
    # Fallback: define minimal classes
    class CloseType:
        STOP_LOSS = "stop_loss"
        TAKE_PROFIT = "take_profit"
        TIME_LIMIT = "time_limit"
        TRAILING_STOP = "trailing_stop"

USER_AGENT = "trading-desk-exit/1.0"

# ─── Price fetching ──────────────────────────────────────────────────────

_PRICE_CACHE: dict = {}
_PRICE_CACHE_TS: float = 0
_PRICE_CACHE_TTL = 30  # 30 second cache
_LAST_SELL_PLANS: dict = {}

def _fetch_token_price(token: str, *, token_amount_raw: int,
                       token_decimals: int, paper_mode: bool) -> Optional[float]:
    """Mark in ETH/token from a fresh token->WETH route quote."""
    if token_amount_raw <= 0:
        return None
    try:
        plan = rh_sell.build_sell_plan(
            token, token_amount_raw,
            fee=CONFIG.pool_fee,
            slippage_bps=CONFIG.slippage_bps,
            require_balance=not paper_mode,
        )
    except Exception:
        return None
    _LAST_SELL_PLANS[token] = plan
    token_amount = token_amount_raw / (10 ** token_decimals)
    return (plan.quoted_weth_out / 10**18) / token_amount if token_amount > 0 else None


def _batch_price_update(positions: list, open_positions: Optional[dict] = None,
                        paper_mode: bool = True) -> dict:
    """Fetch route-executable ETH prices for all open positions."""
    source = open_positions or {}
    price_map = {}
    for pos in positions:
        token = pos.config.token if hasattr(pos, "config") else pos.get("token", "")
        data = source.get(token, {})
        raw = int(data.get("token_amount_raw", 0))
        decimals = int(data.get("token_decimals", 18))
        price = _fetch_token_price(
            token, token_amount_raw=raw, token_decimals=decimals,
            paper_mode=paper_mode,
        )
        if price is not None:
            price_map[token] = price
    return price_map


def execute_live_exit(token: str, token_amount_raw: int) -> dict:
    """Approve exact amount if needed, rebuild quote, then verify the sell."""
    try:
        sell_plan = rh_sell.build_sell_plan(token, token_amount_raw)
        approval = None
        if sell_plan.approval_required:
            approval_plan = rh_sell.build_approval_plan(token, token_amount_raw)
            approval = rh_sell.execute_approval_plan(approval_plan)
            if approval.get("outcome") not in {"ok", "already_approved"}:
                return {"outcome": "approval_failed", "approval": approval}
            sell_plan = rh_sell.build_sell_plan(token, token_amount_raw)
        result = rh_sell.execute_sell_plan(sell_plan)
        if approval is not None:
            result["approval"] = approval
        if result.get("outcome") != "ok" or int(result.get("token_balance_delta", 0)) >= 0 or int(result.get("weth_balance_delta", 0)) <= 0:
            return {"outcome": result.get("outcome", "sell_unverified"), "execution": result}
        return result
    except Exception as exc:
        return {"outcome": "sell_preflight_failed", "error": str(exc)}


# ─── Main exit check ────────────────────────────────────────────────────

def check_exits(paper_mode: bool = True) -> list:
    """
    Check all open positions for exit conditions.
    Returns list of triggered exit events.
    """
    pm = PositionManager()

    # Load positions from state
    positions_data = load_positions()
    if not isinstance(positions_data, dict):
        return []

    open_positions = positions_data.get("open", {})
    if not open_positions:
        return []

    # Rebuild PositionManager
    for token, pos_data in open_positions.items():
        config = PositionExecutorConfig(
            token=token,
            symbol=pos_data.get("symbol", "?"),
            side=pos_data.get("side", "BUY"),
            amount_eth=float(pos_data.get("amount_eth", 0)),
            entry_price=float(pos_data.get("entry_price_eth_per_token", pos_data.get("entry_price", 0))),
            triple_barrier=TripleBarrierConfig(
                stop_loss=CONFIG.stop_loss_pct,
                take_profit=CONFIG.take_profit_pct,
                time_limit=CONFIG.time_limit_seconds,
                trailing_stop=TrailingStopConfig(
                    activation_price=CONFIG.trail_activation,
                    trailing_delta=CONFIG.trail_delta,
                ),
            ),
            timestamp=float(pos_data.get("entry_ts", time.time())),
        )
        pm.add(config)
        executor = pm.get(token)
        if executor:
            executor.record_entry("recovered", float(pos_data.get("token_amount", 0)),
                                  float(pos_data.get("entry_gas_cost_eth", 0)),
                                  float(pos_data.get("entry_price_eth_per_token", pos_data.get("entry_price", 0))))
            executor.update_market_price(float(pos_data.get("current_price_eth_per_token", pos_data.get("current_price", 0))))
            executor.current_pnl_pct = float(pos_data.get("net_pnl_pct", 0))

    # Update prices
    if pm.open_positions:
        price_map = _batch_price_update(pm.open_positions, open_positions, paper_mode)
        pm.update_all_prices(price_map)

    # Check barriers
    triggered = pm.check_all_barriers()
    exit_events = []

    for executor in triggered:
        event = {
            "token": executor.config.token,
            "symbol": executor.config.symbol,
            "close_type": executor.close_type.value if executor.close_type else "unknown",
            "entry_price": executor.entry_price,
            "exit_price": executor.current_market_price,
            "net_pnl_pct": executor.net_pnl_pct,
            "net_pnl_eth": executor.net_pnl_eth,
            "hold_duration_s": time.time() - executor.entry_ts,
        }
        exit_events.append(event)

        log_trade({
            "event": "exit_manager_trigger",
            "ts": time.time(),
            "token": executor.config.token,
            "symbol": executor.config.symbol,
            "close_type": executor.close_type.value if executor.close_type else "unknown",
            "net_pnl_pct": executor.net_pnl_pct,
            "net_pnl_eth": executor.net_pnl_eth,
        })

    # Update desk state
    state = load_desk_state() or {}
    state["exit_manager"] = {
        "last_check_ts": time.time(),
        "open_positions": len(pm.open_positions),
        "triggered_exits": len(triggered),
        "aggregate_pnl_eth": pm.aggregate_pnl_eth,
    }

    # Persist exits only after a paper fill or a verified live balance delta.
    updated_open = dict(open_positions)
    closed = list(positions_data.get("closed", []))
    for executor in triggered:
        token = executor.config.token
        pos_data = dict(open_positions.get(token, {}))
        event = next((item for item in exit_events if item["token"] == token), {})
        if paper_mode:
            plan = _LAST_SELL_PLANS.get(token)
            entry_eth = float(pos_data.get("amount_eth", 0))
            entry_gas = float(pos_data.get("entry_gas_cost_eth", 0))
            if plan is None:
                conservative_exit_eth = 0.0
                exit_cost_eth = 0.0
                exit_outcome = "unsellable_no_route"
            else:
                conservative_exit_eth = plan.min_weth_out / 10**18
                sell_gas = int(plan.gas_estimate or 350_000)
                approval_gas = 100_000 if plan.approval_required else 0
                exit_cost_eth = (sell_gas + approval_gas) * int(plan.max_fee_per_gas) / 10**18
                exit_outcome = "paper_closed"
            net_pnl_eth = conservative_exit_eth - entry_eth - entry_gas - exit_cost_eth
            event.update({
                "outcome": exit_outcome,
                "estimated_net_pnl_eth": net_pnl_eth,
                "conservative_exit_eth": conservative_exit_eth,
                "modeled_exit_cost_eth": exit_cost_eth,
                "sellability_verified": False,
                "evidence": dict(pos_data.get("evidence", {})),
            })
            closed.append(event)
            updated_open.pop(token, None)
            log_trade({**event, "event": "paper_exit", "ts": time.time()})
            continue
        token_amount_raw = int(pos_data.get("token_amount_raw", 0))
        if token_amount_raw <= 0:
            pos_data["exit_status"] = "missing_token_amount_raw"
            updated_open[token] = pos_data
            continue
        execution = execute_live_exit(token, token_amount_raw)
        if execution.get("outcome") == "ok":
            sell_gas = int(execution.get("gas_used") or 0) * int(execution.get("effective_gas_price") or 0) / 10**18
            approval_result = execution.get("approval") or {}
            approval_gas = int(approval_result.get("gas_used") or 0) * int(approval_result.get("effective_gas_price") or 0) / 10**18
            proceeds_eth = int(execution.get("weth_balance_delta") or 0) / 10**18
            risk_state = auto_trader.load_risk_state()
            updated_risk = risk_accounting.apply_exit(
                risk_state,
                proceeds_eth=proceeds_eth,
                cost_basis_eth=float(pos_data.get("amount_eth", 0)),
                exit_cost_eth=sell_gas + approval_gas,
                now_ts=time.time(),
            )
            risk_accounting.persist_state(auto_trader.RISK_STATE, updated_risk)
            desk_state = load_desk_state() or {}
            desk_state["risk"] = updated_risk
            save_desk_state(desk_state)
            event.update({"outcome": "executed", "execution": execution,
                          "realized_pnl_eth": proceeds_eth - float(pos_data.get("amount_eth", 0)) - sell_gas - approval_gas})
            closed.append(event)
            updated_open.pop(token, None)
            log_trade({**event, "event": "live_exit", "ts": time.time()})
        else:
            pos_data.update({
                "exit_status": execution.get("outcome", "failed"),
                "exit_error": execution.get("error") or execution.get("execution", {}).get("error"),
                "exit_retry_count": int(pos_data.get("exit_retry_count", 0)) + 1,
                "current_price_eth_per_token": executor.current_market_price,
                "gross_pnl_pct": executor.gross_pnl_pct,
                "net_pnl_pct": executor.net_pnl_pct,
            })
            updated_open[token] = pos_data
            log_trade({"event": "live_exit_failed", "ts": time.time(), "token": token,
                       "outcome": pos_data["exit_status"]})
    positions_data["open"] = updated_open
    positions_data["closed"] = closed
    save_positions(positions_data)

    save_desk_state(state)
    return exit_events


if __name__ == "__main__":
    events = check_exits(paper_mode=True)
    print(json.dumps({
        "checked": True,
        "exits_triggered": len(events),
        "events": events,
    }, indent=2, default=str))
