#!/usr/bin/env python3
"""Deterministic quant risk layer for Robinhood Chain strategies.

Signal generators may propose trades. This module alone determines whether live
risk is allowed and computes a capped size. It performs no network or wallet IO.
"""
from __future__ import annotations

DEFAULT_POLICY = {
    "kelly_multiplier": 0.25,
    "min_edge_observations": 75,
    "target_volatility": 0.02,
    "max_position_eth": 0.002,
    "max_risk_fraction": 0.0005,
    "daily_stop_fraction": 0.02,
    "max_daily_loss_eth": 0.001,
    "max_drawdown_fraction": 0.10,
    "max_trades_per_day": 3,
    "cooldown_seconds": 600,
    "max_cost_to_gross_edge": 0.50,
}


def compute_expectancy(wins, losses, *, round_trip_cost=0.0):
    """Return per-trade expectancy from realized net win/loss magnitudes."""
    wins = [float(x) for x in wins]
    losses = [abs(float(x)) for x in losses]
    observations = len(wins) + len(losses)
    if not observations:
        return {"observations": 0, "win_probability": 0.0,
                "gross_expectancy": 0.0, "net_expectancy": -float(round_trip_cost)}
    gross = (sum(wins) - sum(losses)) / observations
    return {"observations": observations,
            "win_probability": len(wins) / observations,
            "average_win": sum(wins) / len(wins) if wins else 0.0,
            "average_loss": sum(losses) / len(losses) if losses else 0.0,
            "gross_expectancy": gross,
            "net_expectancy": gross - float(round_trip_cost)}


def conservative_kelly(win_probability, average_win, average_loss, *, observations,
                       expectancy_lower_bound, min_observations=75,
                       kelly_multiplier=0.25):
    """Quarter-Kelly only after enough data and a positive confidence bound."""
    if observations < min_observations or expectancy_lower_bound <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(win_probability)))
    avg_win = float(average_win)
    avg_loss = float(average_loss)
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0
    b = avg_win / avg_loss
    full_kelly = (b * p - (1.0 - p)) / b
    return max(0.0, min(1.0, full_kelly * float(kelly_multiplier)))


def position_size_eth(*, equity_eth, stop_fraction, kelly_fraction,
                      realized_volatility, target_volatility,
                      max_position_eth, max_risk_fraction):
    """Cap notional by Kelly, stop-risk budget, volatility, and absolute size."""
    equity = max(0.0, float(equity_eth))
    stop = float(stop_fraction)
    kelly = max(0.0, float(kelly_fraction))
    if equity <= 0 or stop <= 0 or kelly <= 0:
        return 0.0
    realized_vol = max(0.0, float(realized_volatility))
    target_vol = max(0.0, float(target_volatility))
    vol_scale = 1.0 if realized_vol == 0 else min(1.0, target_vol / realized_vol)
    kelly_notional = equity * kelly * vol_scale
    risk_notional = equity * float(max_risk_fraction) / stop
    return max(0.0, min(float(max_position_eth), kelly_notional, risk_notional))


def evaluate_gate(state, policy=None, *, now_ts):
    """Return all deterministic reasons a proposed live trade must be refused."""
    cfg = dict(DEFAULT_POLICY)
    if policy:
        cfg.update(policy)
    reasons = []
    if state.get("kill_switch_active"):
        reasons.append("kill_switch")
    if not state.get("strategy_promoted", False):
        reasons.append("strategy_not_promoted")
    if int(state.get("observations", 0) or 0) < int(cfg["min_edge_observations"]):
        reasons.append("insufficient_edge_observations")
    if float(state.get("expectancy_lower_bound_eth", 0) or 0) <= 0:
        reasons.append("nonpositive_expectancy_lower_bound")

    start_equity = max(0.0, float(state.get("equity_start_day_eth", 0) or 0))
    daily_limit = min(float(cfg["max_daily_loss_eth"]),
                      start_equity * float(cfg["daily_stop_fraction"]))
    daily_pnl = (float(state.get("realized_pnl_today_eth", 0) or 0)
                 + float(state.get("unrealized_pnl_today_eth", 0) or 0))
    if daily_limit <= 0 or daily_pnl <= -daily_limit:
        reasons.append("daily_loss")

    peak = max(0.0, float(state.get("equity_peak_eth", 0) or 0))
    current = max(0.0, float(state.get("equity_current_eth", 0) or 0))
    drawdown = (peak - current) / peak if peak else 1.0
    if drawdown >= float(cfg["max_drawdown_fraction"]):
        reasons.append("drawdown")

    if int(state.get("trades_today", 0) or 0) >= int(cfg["max_trades_per_day"]):
        reasons.append("turnover")
    last_trade = int(state.get("last_trade_ts", 0) or 0)
    if last_trade and int(now_ts) - last_trade < int(cfg["cooldown_seconds"]):
        reasons.append("cooldown")

    gross_edge = float(state.get("expected_gross_edge_eth", 0) or 0)
    cost = max(0.0, float(state.get("round_trip_cost_eth", 0) or 0))
    if gross_edge <= 0 or cost > gross_edge * float(cfg["max_cost_to_gross_edge"]):
        reasons.append("cost_drag")
    return {"allowed": not reasons, "reasons": reasons,
            "daily_pnl_eth": daily_pnl, "daily_loss_limit_eth": daily_limit,
            "drawdown_fraction": drawdown}
