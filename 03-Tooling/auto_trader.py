#!/usr/bin/env python3
"""Guarded Robinhood Chain alert consumer.

Paper mode obtains a live quote and simulates the exact transaction. Live mode
signs in-process and records a position only after a status-1 receipt and a
positive output-token balance delta.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import rh_swap
import risk_manager

CHAIN_ID = rh_swap.CHAIN_ID
TRENCHES_WALLET = rh_swap.TRENCHES_WALLET
HONEYPOT_URL = "https://trustswap.com/robinhood/honeypot-checker?token="
ROBINHOOD_TOKEN = "0x90a71817bda6dac8c3a28bbfd877b02d667ae2f9"
RHC_TOKEN = "0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"
PERMANENT_DENYLIST = {RHC_TOKEN}

SCRIPT_DIR = Path(__file__).parent.resolve()
ALERTS_FILE = SCRIPT_DIR / "alerts.json"
TRADE_LOG = SCRIPT_DIR / "trade_log.jsonl"
POSITIONS = SCRIPT_DIR / "positions.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
RISK_STATE = SCRIPT_DIR / "risk_state.json"
KILL_SWITCH = SCRIPT_DIR / "HALT_TRADING"

DEFAULT_CONFIG = {
    **risk_manager.DEFAULT_POLICY,
    "min_score": 5.0,
    "max_position_eth": 0.002,
    "max_daily_loss_eth": 0.001,
    "slippage_pct": 5.0,
    "pool_fee": 10_000,
    "max_gas_cost_eth": 0.0015,
    "honeypot_required": True,
    "max_token_age_hours": 48,
    "min_liquidity_usd": 5_000,
    "min_volume_24h_usd": 20_000,
    "approved_tokens": [ROBINHOOD_TOKEN],
    "blacklist": [RHC_TOKEN],
}


def load_config():
    merged = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain a JSON object")
        merged.update(loaded)
    return merged


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_trade(record):
    with open(TRADE_LOG, "a") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def load_alerts():
    if not ALERTS_FILE.exists():
        return []
    try:
        with open(ALERTS_FILE) as handle:
            alerts = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: cannot read alerts.json: {exc}", file=sys.stderr)
        return []
    if not isinstance(alerts, list):
        print("ERROR: alerts.json must contain a JSON list", file=sys.stderr)
        return []
    return alerts


def load_positions():
    if not POSITIONS.exists():
        return {}
    try:
        with open(POSITIONS) as handle:
            positions = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(positions, dict):
        return {}
    normalized = {}
    for token, position in positions.items():
        try:
            normalized[rh_swap.norm_addr(token)] = position
        except ValueError:
            continue
    return normalized


def save_positions(positions):
    with open(POSITIONS, "w") as handle:
        json.dump(positions, handle, indent=2, default=str)


def check_honeypot(token_addr):
    return {"url": HONEYPOT_URL + token_addr, "checked": False}


def token_is_denied(token, config):
    try:
        token = rh_swap.norm_addr(token)
        configured = {rh_swap.norm_addr(item) for item in config.get("blacklist", [])}
    except ValueError:
        return True
    return token in PERMANENT_DENYLIST or token in configured


def token_is_approved(token, config):
    try:
        token = rh_swap.norm_addr(token)
        approved = {rh_swap.norm_addr(item) for item in config.get("approved_tokens", [])}
    except ValueError:
        return False
    return token in approved and not token_is_denied(token, config)


def requires_honeypot_gate(token, config, live_mode):
    if not live_mode or not config.get("honeypot_required", True):
        return False
    return not token_is_approved(token, config)


def is_verified_purchase(result):
    return result.get("outcome") == "ok" and result.get("token_balance_delta", 0) > 0


def daily_pnl():
    if not TRADE_LOG.exists():
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl = 0.0
    with open(TRADE_LOG) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("ts", "").startswith(today):
                pnl += float(record.get("realized_pnl_eth", 0))
                execution = record.get("execution", {})
                if execution.get("outcome") == "reverted":
                    gas_used = int(execution.get("gas_used") or 0)
                    gas_price = int(execution.get("effective_gas_price") or 0)
                    pnl -= gas_used * gas_price / 10**18
    return pnl


def load_risk_state():
    if not RISK_STATE.exists():
        state = {}
    else:
        try:
            with open(RISK_STATE) as handle:
                state = json.load(handle)
        except (json.JSONDecodeError, OSError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    state["kill_switch_active"] = KILL_SWITCH.exists() or bool(state.get("kill_switch_active"))
    return state


def execution_risk_decision(state, *, live_mode, now_ts=None, config=None):
    if not live_mode:
        return {"allowed": True, "reasons": []}
    effective = dict(state or {})
    effective["kill_switch_active"] = KILL_SWITCH.exists() or bool(effective.get("kill_switch_active"))
    return risk_manager.evaluate_gate(effective, config or DEFAULT_CONFIG,
                                      now_ts=int(now_ts if now_ts is not None else time.time()))


def live_position_size(state, config):
    kelly = risk_manager.conservative_kelly(
        state.get("win_probability", 0), state.get("average_win_eth", 0),
        state.get("average_loss_eth", 0), observations=int(state.get("observations", 0) or 0),
        expectancy_lower_bound=float(state.get("expectancy_lower_bound_eth", 0) or 0),
        min_observations=int(config.get("min_edge_observations", 150)),
        kelly_multiplier=float(config.get("kelly_multiplier", 0.25)))
    return risk_manager.position_size_eth(
        equity_eth=state.get("equity_current_eth", 0),
        stop_fraction=state.get("stop_fraction", 0.12), kelly_fraction=kelly,
        realized_volatility=state.get("realized_volatility", 0),
        target_volatility=config.get("target_volatility", 0.02),
        max_position_eth=min(float(config.get("max_position_eth", 0.002)), 0.002),
        max_risk_fraction=config.get("max_risk_fraction", 0.0005))


def build_swap_plan(token_addr, eth_amount, config):
    amount_wei = int(Decimal(str(eth_amount)) * Decimal(10**18))
    slippage_bps = int(Decimal(str(config["slippage_pct"])) * 100)
    max_gas_cost_wei = int(
        Decimal(str(config.get("max_gas_cost_eth", 0.0015))) * Decimal(10**18)
    )
    return rh_swap.build_plan(
        token_addr,
        amount_wei,
        wallet=TRENCHES_WALLET,
        fee=int(config.get("pool_fee", 10_000)),
        slippage_bps=slippage_bps,
        max_gas_cost_wei=max_gas_cost_wei,
    )


def process_alert(alert, config, live_mode):
    score = float(alert.get("score", 0) or 0)
    raw_token = alert.get("token_address") or alert.get("token") or alert.get("pairAddress", "")
    symbol = alert.get("token_symbol", alert.get("symbol", "?"))
    liquidity = float(alert.get("liquidity_usd", 0) or 0)
    volume_24h = float(alert.get("volume_24h_usd", 0) or 0)

    try:
        token = rh_swap.norm_addr(raw_token)
    except ValueError as exc:
        return {"action": "skip", "reason": str(exc)}
    if token_is_denied(token, config):
        return {"action": "skip", "reason": f"token {token} is permanently denied"}
    if score < float(config["min_score"]):
        return {"action": "skip", "reason": f"score {score} < {config['min_score']}"}
    if liquidity < float(config["min_liquidity_usd"]):
        return {"action": "skip", "reason": f"liquidity ${liquidity} < ${config['min_liquidity_usd']}"}
    if volume_24h < float(config["min_volume_24h_usd"]):
        return {"action": "skip", "reason": f"24h vol ${volume_24h} < ${config['min_volume_24h_usd']}"}
    risk_state = load_risk_state()
    risk_decision = execution_risk_decision(risk_state, live_mode=live_mode, config=config)
    if not risk_decision["allowed"]:
        return {"action": "skip", "reason": "risk gate: " + ", ".join(risk_decision["reasons"])}

    pnl = daily_pnl()
    if pnl <= -float(config["max_daily_loss_eth"]):
        return {"action": "skip", "reason": f"daily loss {pnl:.6f} ETH reached limit"}

    positions = load_positions()
    if token in positions:
        return {"action": "skip", "reason": "position already open"}

    honeypot = check_honeypot(token)
    if requires_honeypot_gate(token, config, live_mode):
        return {
            "action": "honeypot_required",
            "token": token,
            "symbol": symbol,
            "honeypot_url": honeypot["url"],
            "score": score,
            "note": "approve this exact address; --force is not a global bypass",
        }

    if live_mode:
        eth_amount = live_position_size(risk_state, config)
        if eth_amount <= 0:
            return {"action": "skip", "reason": "risk gate: calculated live size is zero"}
    else:
        eth_amount = min(float(config["max_position_eth"]), DEFAULT_CONFIG["max_position_eth"])
    try:
        plan = build_swap_plan(token, eth_amount, config)
    except Exception as exc:
        record = {
            "ts": now_iso(), "action": "preflight_failed",
            "mode": "live" if live_mode else "paper",
            "token": token, "symbol": symbol, "score": score, "error": str(exc),
        }
        log_trade(record)
        return record

    record = {
        "ts": now_iso(), "action": "simulated",
        "mode": "live" if live_mode else "paper",
        "token": token, "symbol": symbol, "score": score,
        "honeypot_url": honeypot["url"], "plan": rh_swap.display_plan(plan),
    }
    if not live_mode:
        log_trade(record)
        return record

    execution = rh_swap.execute_plan(plan)
    record["execution"] = execution
    if is_verified_purchase(execution):
        record["action"] = "bought"
        record["tx_hash"] = execution["tx_hash"]
        positions[token] = {
            "entry_ts": now_iso(), "entry_eth": eth_amount,
            "symbol": symbol, "token_amount": execution["token_balance_delta"],
            "tx_hash": execution["tx_hash"], "pool": plan.pool,
            "route": "uniswap_v3_weth_exact_input_single",
            "stop_loss_pct": -15, "tp1_pct": 30, "tp2_pct": 60,
        }
        save_positions(positions)
    else:
        record["action"] = execution.get("outcome", "execution_failed")
    log_trade(record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="continuous loop")
    parser.add_argument("--interval", type=int, default=30, help="watch interval (s)")
    parser.add_argument("--once", action="store_true", help="process current alerts once")
    parser.add_argument("--live", action="store_true", help="sign and broadcast")
    parser.add_argument("--force", action="store_true", help="legacy token-scoped flag")
    parser.add_argument("--approve-token", action="append", default=[], help="approve exact token for this process")
    args = parser.parse_args()

    config = load_config()
    for token in args.approve_token:
        token = rh_swap.norm_addr(token)
        if token_is_denied(token, config):
            print(f"ERROR: cannot approve denied token {token}", file=sys.stderr)
            return 1
        config.setdefault("approved_tokens", []).append(token)
    if args.force:
        print("NOTICE: --force is token-scoped; the permanent denylist is always active.")
    if args.live and not os.environ.get("RH_PRIVATE_KEY"):
        print("ERROR: --live requires RH_PRIVATE_KEY in env", file=sys.stderr)
        return 1

    mode = "LIVE" if args.live else "PAPER"
    approved_count = len(config.get("approved_tokens", []))
    print(f"[{now_iso()}] Auto-trader started ({mode}, min_score={config['min_score']})")
    print(f"  Wallet: {TRENCHES_WALLET}")
    print(f"  Max position: {config['max_position_eth']} ETH")
    print(f"  Daily loss limit: {config['max_daily_loss_eth']} ETH")
    print(f"  Gate: token-scoped ({approved_count} approved; permanent denylist active)")
    print(f"  Watching: {ALERTS_FILE}")

    seen = set()
    had_error = False
    while True:
        alerts = load_alerts()
        if not alerts and args.once:
            print("  -> No alerts found.")
        for alert in alerts:
            raw_token = alert.get("token_address") or alert.get("token") or alert.get("pairAddress", "")
            try:
                token = rh_swap.norm_addr(raw_token)
            except ValueError:
                token = raw_token
            if not token or token in seen:
                continue
            seen.add(token)
            result = process_alert(alert, config, args.live)
            action = result.get("action", "unknown")
            if action == "skip":
                print(f"  -> SKIP: {result.get('reason', '?')}")
            else:
                print(f"  -> {action}: {result.get('symbol', '?')} score={result.get('score', '?')}")
            if result.get("honeypot_url"):
                print(f"    Honeypot: {result['honeypot_url']}")
            plan = result.get("plan")
            if plan:
                print(f"    Quote: {plan['quoted_out_display']} {plan['token_symbol']} (min {plan['amount_out_min_display']})")
                print(f"    Pool: {plan['pool']} fee={plan['fee']}")
                print(f"    Gas: {plan['gas_estimate']} (worst-case {plan['max_gas_cost_eth']} ETH)")
            if result.get("tx_hash"):
                print(f"    TX: {result['tx_hash']}")
            if action in {"preflight_failed", "reverted", "unknown_pending", "receipt_ok_no_tokens", "not_sent"}:
                had_error = True
                error = result.get("error") or result.get("execution", {}).get("error")
                if error:
                    print(f"    Error: {error}")

        if args.once:
            break
        if not args.watch:
            break
        time.sleep(args.interval)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
