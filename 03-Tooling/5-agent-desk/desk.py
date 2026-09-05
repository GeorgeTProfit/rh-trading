"""DESK — the central orchestrator.
Runs the full 5-agent pipeline on a cycle:
SCOUT -> HISTORIAN -> CONTEXT -> PULSE -> DEVIL -> EXECUTOR -> EXIT_MANAGER

Zynex's exact architecture, adapted for flat-file Hermes operation.
No Redis. No Postgres. No cloud VM.

Run:
  python3 desk.py --cycle     # one full cycle
  python3 desk.py --daemon    # continuous loop (for cron/background)
  python3 desk.py --status    # print current desk state
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# Ensure parent dir is on path for imports
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from config import CONFIG
from state import (
    enqueue_signal, dequeue_signals, ack_signal, purge_invalid_signals, signal_count, log_trade,
    load_desk_state, save_desk_state, load_positions, save_positions,
)
from wallet_db import refresh_wallet_db, wallet_count

# Scout
from scout import scout_cycle

# Agents
from historian import evaluate as historian_evaluate
from context import evaluate as context_evaluate
from pulse import evaluate as pulse_evaluate, score_market
from devil import evaluate as devil_evaluate

# Execution
from executor import execute_trade, _load_risk_state, _save_risk_state
from exit_manager import check_exits


# ─── Composite scoring (Zynex's evolved tuning) ─────────────────────────

def _compute_composite(signal: dict) -> tuple[float, str]:
    """
    Compute composite score from all agents.
    Zynex's thresholds:
      >0.80 = overcorrected zone (too tight)
      ~0.62 = sweet spot (34 trades/day, 65%+ win rate)
      <0.40 = dropped
    """
    copy_score = float(signal.get("copy_score", 0))
    narrative_score = float(signal.get("narrative_score", 0.5))
    devil_score = float(signal.get("devil_score", 1.0))
    go_signal = float(signal.get("pulse_go_signal", 0.5))

    # Weight map (Zynex-inspired, tuned for our setup)
    composite = (
        copy_score * 0.35 +       # Wallet history (HISTORIAN)
        narrative_score * 0.25 +   # Token health (CONTEXT)
        go_signal * 0.20 +        # Macro mood (PULSE)
        devil_score * 0.20        # Adversarial (DEVIL)
    )
    composite = max(0.0, min(1.0, composite))

    if composite >= CONFIG.composite_threshold:
        return composite, "TRADE"
    elif composite >= CONFIG.historian_threshold:
        return composite, "WATCH"  # borderline — log but don't trade
    else:
        return composite, "SKIP"


def signal_is_fresh(signal: dict, *, now_ts: Optional[float] = None,
                    max_age_seconds: Optional[int] = None) -> bool:
    """Reject stale, missing, or future-dated signals before analysis."""
    now = float(time.time() if now_ts is None else now_ts)
    max_age = int(CONFIG.max_signal_age_seconds if max_age_seconds is None else max_age_seconds)
    try:
        signal_ts = float(signal["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    age = now - signal_ts
    return 0 <= age <= max_age


# ─── Signal pipeline ────────────────────────────────────────────────────

def process_signal(signal: dict, market: dict) -> Optional[dict]:
    """
    Run a single signal through the full 5-agent pipeline.
    Returns the final enriched signal or None if vetoed early.
    """
    pipe_start = time.time()
    pipeline_log = {"signal": signal, "stages": [], "total_latency_ms": 0}
    vetoed = False

    # Stage 1: HISTORIAN
    h_result = historian_evaluate(signal)
    pipeline_log["stages"].append({
        "agent": "historian",
        "latency_ms": h_result.get("historian_latency_ms", 0),
        "copy_score": h_result.get("copy_score", 0),
        "decision": h_result.get("historian_decision", "skip"),
    })
    if h_result.get("historian_decision") == "veto":
        pipeline_log["vetoed_at"] = "historian"
        vetoed = True
    log_trade({**pipeline_log["stages"][-1], "event": "pipeline_stage", "ts": time.time(),
               "token": signal.get("token", ""), "symbol": signal.get("symbol", "?")})

    # Stage 2: CONTEXT (runs even after historian veto for data)
    c_result = context_evaluate(signal if vetoed else h_result)
    pipeline_log["stages"].append({
        "agent": "context",
        "latency_ms": c_result.get("context_latency_ms", 0),
        "narrative_score": c_result.get("narrative_score", 0),
        "status": c_result.get("context_status", "unknown"),
        "veto": c_result.get("context_veto", False),
    })
    if c_result.get("context_veto"):
        pipeline_log["vetoed_at"] = "context"
        vetoed = True
    log_trade({**pipeline_log["stages"][-1], "event": "pipeline_stage", "ts": time.time(),
               "token": signal.get("token", ""), "symbol": signal.get("symbol", "?")})

    if vetoed:
        pipeline_log["vetoed"] = True
        pipeline_log["total_latency_ms"] = int((time.time() - pipe_start) * 1000)
        log_trade({**pipeline_log, "event": "pipeline_vetoed", "ts": time.time()})
        # Still return context-enriched signal for logging
        return {**c_result, "pipeline_veto": True, "pipeline_reason": pipeline_log.get("vetoed_at")}

    # Stage 3: PULSE
    p_result = pulse_evaluate(c_result)
    pipeline_log["stages"].append({
        "agent": "pulse",
        "latency_ms": p_result.get("pulse_latency_ms", 0),
        "go_signal": p_result.get("pulse_go_signal", 0),
        "phase": p_result.get("pulse_phase", "unknown"),
        "stand_down": p_result.get("pulse_stand_down", False),
    })
    if p_result.get("pulse_stand_down"):
        pipeline_log["vetoed_at"] = "pulse"
        vetoed = True
    log_trade({**pipeline_log["stages"][-1], "event": "pipeline_stage", "ts": time.time(),
               "token": signal.get("token", ""), "symbol": signal.get("symbol", "?")})

    if vetoed:
        log_trade({**pipeline_log, "event": "pipeline_vetoed", "ts": time.time()})
        return {**p_result, "pipeline_veto": True}

    # Stage 4: DEVIL
    d_result = devil_evaluate(signal, h_result, c_result, p_result)
    pipeline_log["stages"].append({
        "agent": "devil",
        "latency_ms": d_result.get("devil_latency_ms", 0),
        "devil_score": d_result.get("devil_score", 0),
        "veto": d_result.get("devil_veto", False),
        "reasons": d_result.get("devil_veto_reasons", []),
    })
    if d_result.get("devil_veto"):
        pipeline_log["vetoed_at"] = "devil"
        vetoed = True
    log_trade({**pipeline_log["stages"][-1], "event": "pipeline_stage", "ts": time.time(),
               "token": signal.get("token", ""), "symbol": signal.get("symbol", "?")})

    # Compute composite
    composite, action = _compute_composite(d_result)
    pipeline_log["composite_score"] = composite
    pipeline_log["action"] = action

    result = {
        **d_result,
        "composite_score": round(composite, 4),
        "desk_action": action,
        "pipeline_veto": vetoed,
        "pipeline_reason": pipeline_log.get("vetoed_at"),
        "pipeline_stages": pipeline_log["stages"],
        "pipeline_latency_ms": int((time.time() - pipe_start) * 1000),
    }

    pipeline_log["total_latency_ms"] = result["pipeline_latency_ms"]
    log_trade({**pipeline_log, "event": "pipeline_complete",
               "ts": time.time(), "action": action, "composite": composite})

    return result


# ─── Desk cycle ─────────────────────────────────────────────────────────

def desk_cycle(paper_mode: bool = True) -> dict:
    """
    One complete desk cycle: wallet refresh -> scout -> pipeline -> exec -> exits.
    Returns summary dict.
    """
    cycle_start = time.time()
    results = {
        "cycle_ts": cycle_start,
        "wallet_count": 0,
        "scout_signals": 0,
        "pipeline_run": 0,
        "trades_executed": 0,
        "paper_positions_opened": 0,
        "exits_triggered": 0,
        "latency_ms": 0,
    }

    print(f"\n{'='*60}")
    print(f"  DESK CYCLE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # 1. Refresh wallet DB (every 4 hours — time-limited by wallet_db.py)
    if wallet_count() == 0:
        print("  [DESK] Initial wallet refresh...")
        refresh_wallet_db()
    results["wallet_count"] = wallet_count()
    print(f"  [DESK] Monitoring {results['wallet_count']} wallets")

    # 2. SCOUT
    scout_start = time.time()
    signals_found = scout_cycle()
    results["scout_signals"] = signals_found
    print(f"  [DESK] SCOUT: {signals_found} signals in {(time.time()-scout_start)*1000:.0f}ms")

    # 3. Purge stale backlog, then claim fresh signals even if SCOUT found none.
    purged = purge_invalid_signals(max_age_seconds=CONFIG.max_signal_age_seconds)
    for stale in purged:
        log_trade({"event": "stale_signal_rejected", "ts": time.time(),
                   "token": stale.get("token", ""), "signal_ts": stale.get("timestamp"),
                   "tx_hash": stale.get("tx_hash")})
    signals = dequeue_signals(limit=10)
    if not signals:
        print("  [DESK] No queued signals — checking exits and returning")
        exit_events = check_exits(paper_mode=paper_mode)
        results["exits_triggered"] = len(exit_events)
        state = load_desk_state() or {}
        state["desk"] = {**results, "status": "idle", "last_cycle_ts": time.time()}
        save_desk_state(state)
        return results

    pipeline_start = time.time()
    print(f"  [DESK] Processing {len(signals)} signals through pipeline...")

    for i, signal in enumerate(signals):
        print(f"\n  [DESK] Signal #{i+1}: {signal.get('handle','?')} -> ${signal.get('symbol','?')} "
              f"(${signal.get('amount_usd',0):,.0f})")
        if not signal_is_fresh(signal):
            log_trade({"event": "stale_signal_rejected", "ts": time.time(),
                       "token": signal.get("token", ""), "signal_ts": signal.get("timestamp")})
            print("  [DESK] SKIP: stale or invalid signal timestamp")
            results["pipeline_run"] += 1
            ack_signal(signal)
            continue

        result = process_signal(signal, {})

        if result and not result.get("pipeline_veto") and result.get("desk_action") == "TRADE":
            composite = result.get("composite_score", 0)
            wallet_class = result.get("wallet_class", "sniper")
            symbol = result.get("symbol", "?")
            print(f"  [DESK] >>> TRADE SIGNAL: {symbol} | composite={composite:.2f} | "
                  f"class={wallet_class} | ${signal.get('amount_usd',0):,.0f}")

            exec_result = execute_trade(result, paper_mode=paper_mode)
            if exec_result.get("outcome") == "executed":
                results["trades_executed"] += 1
                print(f"  [DESK] >>> EXECUTED: {exec_result.get('amount_eth',0):.6f} ETH "
                      f"of {symbol}")
            elif exec_result.get("outcome") == "paper_opened":
                results["paper_positions_opened"] += 1
                print(f"  [DESK] >>> PAPER OPEN: {exec_result.get('amount_eth',0):.6f} ETH "
                      f"of {symbol}")
            else:
                print(f"  [DESK] >>> EXEC REFUSED: {exec_result.get('error', exec_result.get('outcome','?'))}")

        results["pipeline_run"] += 1
        ack_signal(signal)

    pipeline_elapsed = time.time() - pipeline_start
    print(f"\n  [DESK] Pipeline: {results['pipeline_run']} signals in {pipeline_elapsed*1000:.0f}ms")

    # 4. Check exits
    exit_events = check_exits(paper_mode=paper_mode)
    results["exits_triggered"] = len(exit_events)
    for ev in exit_events:
        print(f"  [DESK] EXIT: {ev['symbol']} @ {ev['close_type']} | "
              f"PnL={ev['net_pnl_pct']*100:.2f}% | ${ev['entry_price']:.8f} -> ${ev['exit_price']:.8f}")

    results["latency_ms"] = int((time.time() - cycle_start) * 1000)
    print(f"\n  [DESK] Cycle complete in {results['latency_ms']}ms "
          f"(SCOUT={results.get('scout_signals',0)} | "
          f"PIPE={results.get('pipeline_run',0)} | "
          f"EXEC={results.get('trades_executed',0)} | "
          f"EXIT={results.get('exits_triggered',0)})")
    print(f"{'='*60}\n")

    # Save desk state
    state = load_desk_state() or {}
    state["desk"] = {**results, "status": "ok", "last_cycle_ts": time.time()}
    save_desk_state(state)

    return results


# ─── Status ──────────────────────────────────────────────────────────────

def print_status():
    """Print current desk status."""
    state = load_desk_state()
    if not state:
        print("Desk state: empty (no cycles run yet)")
        return

    print(f"\n{'='*60}")
    print(f"  DESK STATUS REPORT")
    print(f"{'='*60}")

    desk = state.get("desk", {})
    risk = state.get("risk", {})
    scout = state.get("scout", {})
    exit_mgr = state.get("exit_manager", {})

    print(f"  Status: {desk.get('status', 'unknown')}")
    print(f"  Last cycle: {datetime.fromtimestamp(desk.get('last_cycle_ts', 0)).strftime('%H:%M:%S UTC') if desk.get('last_cycle_ts') else 'never'}")
    print(f"\n  Wallets monitored: {desk.get('wallet_count', 0)}")
    print(f"  Queue depth: {signal_count()}")
    print(f"  Open positions: {exit_mgr.get('open_positions', 0)}")
    print(f"  Aggregate PnL: {exit_mgr.get('aggregate_pnl_eth', 0):.8f} ETH")

    if risk:
        print(f"\n  Today: {risk.get('trades_today', 0)} trades | "
              f"PnL={risk.get('realized_pnl_today_eth', 0):.6f} ETH | "
              f"Drawdown={risk.get('drawdown_fraction', 0)*100:.1f}%")

    if scout:
        print(f"\n  SCOUT: {scout.get('signals_found', 0)} signals last cycle | "
              f"{scout.get('cycle_ms', 0)}ms | "
              f"depth={scout.get('queue_depth', 0)}")

    positions = load_positions()
    if isinstance(positions, dict) and positions.get("open"):
        print(f"\n  Open positions ({len(positions['open'])}):")
        for token, p in positions["open"].items():
            print(f"    {p.get('symbol','?'):8s} | entry=${p.get('entry_price',0):.8f} | "
                  f"PnL={float(p.get('net_pnl_pct',0))*100:.2f}% | "
                  f"{p.get('status','?')}")

    print(f"{'='*60}\n")


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Zynex-inspired 5-Agent Copy-Desk Orchestrator")
    ap.add_argument("--cycle", action="store_true", help="Run one full desk cycle")
    ap.add_argument("--daemon", type=int, default=0, metavar="SECONDS",
                    help="Run continuous cycles every N seconds")
    ap.add_argument("--status", action="store_true", help="Print desk status")
    ap.add_argument("--paper", action="store_true", default=True,
                    help="Paper mode (no real trades)")
    ap.add_argument("--live", action="store_true",
                    help="Live mode (execute real trades)")
    ap.add_argument("--scout-only", action="store_true",
                    help="Run SCOUT only (test mode)")
    ap.add_argument("--refresh-wallets", action="store_true",
                    help="Refresh wallet database now")
    args = ap.parse_args()

    paper_mode = not args.live

    if args.status:
        print_status()
        return

    if args.refresh_wallets:
        refresh_wallet_db()
        return

    if args.scout_only:
        while True:
            n = scout_cycle()
            print(json.dumps({"outcome": "scout_cycle", "signals": n}))
            if not args.daemon:
                break
            time.sleep(args.daemon or CONFIG.scout_interval_seconds)
        return

    if args.cycle or args.daemon:
        if args.daemon:
            print(f"  [DESK] Starting continuous mode every {args.daemon}s — Ctrl+C to stop")
            try:
                while True:
                    desk_cycle(paper_mode=paper_mode)
                    time.sleep(args.daemon)
            except KeyboardInterrupt:
                print("\n  [DESK] Stopped by user")
        else:
            result = desk_cycle(paper_mode=paper_mode)
            print(json.dumps(result, indent=2, default=str))
        return

    # Default: print status
    print_status()


if __name__ == "__main__":
    main()
