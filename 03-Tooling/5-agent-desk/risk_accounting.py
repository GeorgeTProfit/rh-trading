"""Deterministic, auditable live risk-state accounting."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _day(now_ts: float) -> str:
    return datetime.fromtimestamp(float(now_ts), tz=timezone.utc).date().isoformat()


def _roll_day(state: dict, now_ts: float) -> dict:
    result = dict(state or {})
    day = _day(now_ts)
    if result.get("risk_day_utc") != day:
        equity = float(result.get("equity_current_eth", 0) or 0)
        result.update({
            "risk_day_utc": day,
            "equity_start_day_eth": equity,
            "realized_pnl_today_eth": 0.0,
            "unrealized_pnl_today_eth": 0.0,
            "trades_today": 0,
        })
    return result


def _finish(state: dict) -> dict:
    current = float(state.get("equity_current_eth", 0) or 0)
    peak = max(float(state.get("equity_peak_eth", 0) or 0), current)
    state["equity_peak_eth"] = peak
    state["drawdown_fraction"] = (peak - current) / peak if peak > 0 else 1.0
    state["_updated_ts"] = time.time()
    return state


def apply_entry(state: dict, *, gas_cost_eth: float, now_ts: float) -> dict:
    result = _roll_day(state, now_ts)
    gas = max(0.0, float(gas_cost_eth))
    result["equity_current_eth"] = float(result.get("equity_current_eth", 0) or 0) - gas
    result["realized_pnl_today_eth"] = float(result.get("realized_pnl_today_eth", 0) or 0) - gas
    result["trades_today"] = int(result.get("trades_today", 0) or 0) + 1
    result["last_trade_ts"] = int(now_ts)
    return _finish(result)


def apply_exit(state: dict, *, proceeds_eth: float, cost_basis_eth: float,
               exit_cost_eth: float, now_ts: float) -> dict:
    result = _roll_day(state, now_ts)
    realized = float(proceeds_eth) - float(cost_basis_eth) - max(0.0, float(exit_cost_eth))
    result["realized_pnl_today_eth"] = float(result.get("realized_pnl_today_eth", 0) or 0) + realized
    result["equity_current_eth"] = float(result.get("equity_current_eth", 0) or 0) + realized
    result["last_exit_ts"] = int(now_ts)
    return _finish(result)


def persist_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
