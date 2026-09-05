"""Crash-safe local state for the copy desk."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import CONFIG

_PROCESS_LOCK = threading.RLock()


def _default_for(path: str):
    return [] if path.endswith(("signal_queue.json", ".inflight.json")) else {}


def _read_json(path: str):
    p = Path(path)
    if not p.exists():
        return _default_for(path)
    try:
        with p.open() as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return _default_for(path)


def _write_json_unlocked(path: str, data):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f"{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w") as handle:
        json.dump(data, handle, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, destination)


@contextmanager
def _locked(path: str):
    lock_path = Path(path + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json(path: str, data):
    with _locked(path):
        _write_json_unlocked(path, data)


def _append_jsonl(path: str, record: dict):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        with destination.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def queue_path() -> str:
    return CONFIG.signal_queue_path


def inflight_path() -> str:
    return queue_path() + ".inflight.json"


def _signal_id(signal: dict) -> str:
    return str(signal.get("tx_hash") or signal.get("signal_id") or "").lower()


def enqueue_signal(signal: dict) -> bool:
    """Atomically enqueue once by transaction/signal ID."""
    now = time.time()
    with _locked(queue_path()):
        queue = _read_json(queue_path())
        inflight = _read_json(inflight_path())
        queue = queue if isinstance(queue, list) else []
        inflight = inflight if isinstance(inflight, list) else []
        identity = _signal_id(signal)
        known = {_signal_id(item) for item in queue + inflight}
        if identity and identity in known:
            return False
        payload = dict(signal)
        payload["_enqueued_at"] = now
        queue.append(payload)
        _write_json_unlocked(queue_path(), queue)
        return True


def purge_invalid_signals(*, now_ts: Optional[float] = None,
                          max_age_seconds: int = 300,
                          future_tolerance_seconds: int = 5) -> list:
    """Remove stale, future, or malformed queued signals before claiming."""
    now = float(time.time() if now_ts is None else now_ts)
    removed = []
    with _locked(queue_path()):
        queue = _read_json(queue_path())
        inflight = _read_json(inflight_path())
        queue = queue if isinstance(queue, list) else []
        inflight = inflight if isinstance(inflight, list) else []
        kept_queue = []
        for item in queue:
            try:
                timestamp = float(item.get("timestamp"))
                valid = now - max_age_seconds <= timestamp <= now + future_tolerance_seconds
            except (TypeError, ValueError):
                valid = False
            (kept_queue if valid else removed).append(item)
        kept_inflight = []
        for item in inflight:
            if float(item.get("_lease_until", 0) or 0) > now:
                kept_inflight.append(item)
                continue
            try:
                timestamp = float(item.get("timestamp"))
                valid = now - max_age_seconds <= timestamp <= now + future_tolerance_seconds
            except (TypeError, ValueError):
                valid = False
            if valid:
                clean = dict(item)
                clean.pop("_claim_id", None)
                clean.pop("_lease_until", None)
                kept_queue.insert(0, clean)
            else:
                removed.append(item)
        _write_json_unlocked(queue_path(), kept_queue)
        _write_json_unlocked(inflight_path(), kept_inflight)
    return removed


def dequeue_signals(limit: int = 10, *, now_ts: Optional[float] = None,
                    lease_seconds: int = 300) -> list:
    """Claim signals; unacknowledged claims are recoverable after the lease."""
    now = float(time.time() if now_ts is None else now_ts)
    with _locked(queue_path()):
        queue = _read_json(queue_path())
        inflight = _read_json(inflight_path())
        queue = queue if isinstance(queue, list) else []
        inflight = inflight if isinstance(inflight, list) else []

        active = []
        recovered = []
        for item in inflight:
            if float(item.get("_lease_until", 0) or 0) <= now:
                clean = dict(item)
                clean.pop("_claim_id", None)
                clean.pop("_lease_until", None)
                recovered.append(clean)
            else:
                active.append(item)
        if recovered:
            existing = {_signal_id(item) for item in queue}
            queue = [item for item in recovered if not _signal_id(item) or _signal_id(item) not in existing] + queue

        batch = queue[:max(0, int(limit))]
        queue = queue[len(batch):]
        claimed = []
        for item in batch:
            payload = dict(item)
            payload["_claim_id"] = uuid.uuid4().hex
            payload["_lease_until"] = now + max(1, int(lease_seconds))
            active.append(payload)
            claimed.append(payload)
        _write_json_unlocked(queue_path(), queue)
        _write_json_unlocked(inflight_path(), active)
        return claimed


def ack_signal(signal: dict) -> bool:
    claim_id = str(signal.get("_claim_id") or "")
    if not claim_id:
        return False
    with _locked(queue_path()):
        inflight = _read_json(inflight_path())
        inflight = inflight if isinstance(inflight, list) else []
        remaining = [item for item in inflight if str(item.get("_claim_id")) != claim_id]
        removed = len(remaining) != len(inflight)
        _write_json_unlocked(inflight_path(), remaining)
        return removed


def signal_count() -> int:
    with _locked(queue_path()):
        queue = _read_json(queue_path())
        inflight = _read_json(inflight_path())
        return (len(queue) if isinstance(queue, list) else 0) + (len(inflight) if isinstance(inflight, list) else 0)


def log_trade(record: dict):
    _append_jsonl(CONFIG.trade_log_path, record)


def read_trade_log(limit: int = 100) -> list:
    path = Path(CONFIG.trade_log_path)
    if not path.exists():
        return []
    records = []
    with _locked(str(path)):
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records[-limit:]


def load_positions() -> dict:
    return _read_json(CONFIG.positions_path)


def save_positions(positions: dict):
    _write_json(CONFIG.positions_path, positions)


def load_pulse_cache() -> dict:
    return _read_json(CONFIG.pulse_cache_path)


def save_pulse_cache(cache: dict):
    _write_json(CONFIG.pulse_cache_path, cache)


def load_wallet_db() -> dict:
    return _read_json(CONFIG.wallet_db_path)


def save_wallet_db(db: dict):
    _write_json(CONFIG.wallet_db_path, db)


def load_desk_state() -> dict:
    return _read_json(CONFIG.desk_state_path)


def save_desk_state(state: dict):
    _write_json(CONFIG.desk_state_path, state)
