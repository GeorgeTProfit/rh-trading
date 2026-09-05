"""SCOUT — the only agent that doesn't use an LLM. Pure code.
Job: watch the active wallet list and emit a signal every time one opens a position.

Polls the Robinhood Trenches tape (free, real-time) and GMGN API.
Emits signals to the shared signal_queue.json.

Production target: signal-in-queue under 500ms from block confirmation.

Cost: $0 — pure API calls.
"""
from __future__ import annotations
import json, time, urllib.request, urllib.parse, sys
from typing import Dict, List, Optional, Set
from state import enqueue_signal, load_desk_state, save_desk_state, signal_count
from config import CONFIG
from wallet_db import get_active_wallet_list

TRENCHES_WS_URL = "https://robinhoodtrenches.com/api/tape"
GMGN_API = "https://goapi.gmgn.ai/v1"
USER_AGENT = "trading-desk-scout/1.0"
POLL_INTERVAL = CONFIG.scout_interval_seconds


def http_json(url: str, timeout: int = 20) -> any:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ─── Watchlist cache (avoid re-parsing wallet DB on every poll) ──────────

_watch_addresses: Set[str] = set()
_watch_handles: Dict[str, str] = {}  # address -> handle
_watch_classes: Dict[str, str] = {}  # address -> wallet_class
_watch_cache_ts: float = 0
_WATCH_CACHE_TTL = 300  # refresh wallet list every 5 min


def _refresh_watchlist():
    global _watch_addresses, _watch_handles, _watch_classes, _watch_cache_ts
    now = time.time()
    if now - _watch_cache_ts < _WATCH_CACHE_TTL:
        return
    wallets = get_active_wallet_list()
    _watch_addresses = set()
    _watch_handles = {}
    _watch_classes = {}
    for w in wallets:
        addr = w.get("address", "").lower()
        if addr:
            _watch_addresses.add(addr)
            _watch_handles[addr] = w.get("handle", "")
            _watch_classes[addr] = w.get("wallet_class", "sniper")
    _watch_cache_ts = now


# ─── Track seen fills (avoid duplicate signals) ──────────────────────────

_seen_fills: Set[str] = set()  # tx hash set across a window
_SEEN_WINDOW = 3600  # forget after 1 hour
_seen_timestamps: Dict[str, float] = {}
SEEN_FILLS_PATH = CONFIG.DATA / "seen_fills.json" if hasattr(CONFIG, "DATA") else __import__("pathlib").Path(CONFIG.signal_queue_path).with_name("seen_fills.json")
_seen_loaded = False


def _load_seen_fills() -> None:
    global _seen_loaded
    if _seen_loaded:
        return
    _seen_loaded = True
    try:
        data = json.loads(SEEN_FILLS_PATH.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        data = {}
    if isinstance(data, dict):
        for tx_hash, ts in data.items():
            try:
                _seen_timestamps[str(tx_hash)] = float(ts)
                _seen_fills.add(str(tx_hash))
            except (TypeError, ValueError):
                continue


def _persist_seen_fills() -> None:
    SEEN_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILLS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_seen_timestamps, sort_keys=True))
    tmp.replace(SEEN_FILLS_PATH)


def _is_new_fill(tx_hash: str, *, now_ts: Optional[float] = None) -> bool:
    global _seen_loaded
    if not _seen_timestamps and not _seen_fills:
        _seen_loaded = False
    _load_seen_fills()
    now = float(time.time() if now_ts is None else now_ts)
    expired = [key for key, ts in _seen_timestamps.items() if now - ts >= _SEEN_WINDOW]
    for key in expired:
        del _seen_timestamps[key]
        _seen_fills.discard(key)
    if tx_hash in _seen_fills:
        return False
    _seen_fills.add(tx_hash)
    _seen_timestamps[tx_hash] = now
    _persist_seen_fills()
    return True


# ─── Source 1: Robinhood Trenches tape ───────────────────────────────────

def _scan_trenches() -> List[dict]:
    """Scan robinhoodtrenches.com tape API for watched wallet buys."""
    signals = []
    try:
        data = http_json(f"{TRENCHES_WS_URL}?limit=200&stocks=false")
    except Exception as e:
        return signals

    fills = data if isinstance(data, list) else data.get("data", data.get("fills", []))
    _refresh_watchlist()

    for fill in fills:
        if not isinstance(fill, dict):
            continue
        side = str(fill.get("side", "")).lower()
        if side != "buy":
            continue
        if not fill.get("new_position"):
            continue
        tx = str(fill.get("tx", ""))
        if not tx or not _is_new_fill(tx):
            continue

        wallet = str(fill.get("wallet", "")).lower()
        if wallet not in _watch_addresses:
            continue

        token = str(fill.get("token", ""))
        if not token:
            continue
        usd_str = fill.get("usd", 0)
        usd = float(usd_str) if usd_str is not None else 0
        liq_str = fill.get("liquidity", 0)
        liquidity = float(liq_str) if liq_str is not None else 0
        ts_raw = fill.get("ts", time.time())
        ts = int(ts_raw) if ts_raw is not None else int(time.time())
        handle = str(fill.get("handle", ""))
        symbol = str(fill.get("symbol", "?"))

        signal = {
            "source": "trenches",
            "type": "insider_buy",
            "wallet": wallet,
            "handle": handle or _watch_handles.get(wallet, ""),
            "wallet_class": _watch_classes.get(wallet, "sniper"),
            "token": token.lower(),
            "symbol": symbol,
            "amount_usd": usd,
            "liquidity_usd": liquidity,
            "tx_hash": tx,
            "timestamp": ts,
            "scout_latency_ms": int((time.time() - ts) * 1000),
        }
        signals.append(signal)

    return signals


# ─── Source 2: GMGN API (supplementary) ─────────────────────────────────

def _scan_gmgn() -> List[dict]:
    """Scan GMGN for watched wallet buys on Robinhood Chain."""
    signals = []
    _refresh_watchlist()
    if not _watch_addresses:
        return signals

    # GMGN supports batch wallet lookup — we check in chunks of 20
    addresses = list(_watch_addresses)
    for i in range(0, len(addresses), 20):
        batch = addresses[i:i+20]
        try:
            url = f"{GMGN_API}/wallet/batch?chain=robinhood&addresses={','.join(batch)}&action=buy&limit=5"
            data = http_json(url)
        except Exception:
            continue

        trades = data if isinstance(data, list) else data.get("data", [])
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            tx = str(trade.get("tx_hash", ""))
            if not tx or not _is_new_fill(tx):
                continue
            wallet = str(trade.get("wallet", "")).lower()
            token = str(trade.get("token", ""))
            if not token:
                continue
            signal = {
                "source": "gmgn",
                "type": "insider_buy",
                "wallet": wallet,
                "handle": _watch_handles.get(wallet, ""),
                "wallet_class": _watch_classes.get(wallet, "sniper"),
                "token": token.lower(),
                "symbol": str(trade.get("symbol", "?")),
                "amount_usd": float(trade.get("amount_usd", 0)),
                "liquidity_usd": float(trade.get("liquidity", 0)),
                "tx_hash": tx,
                "timestamp": int(trade.get("timestamp", time.time())),
                "scout_latency_ms": int((time.time() - float(trade.get("timestamp", time.time()))) * 1000),
            }
            signals.append(signal)
    return signals


# ─── Main loop ───────────────────────────────────────────────────────────

def scout_cycle() -> int:
    """Run one SCOUT cycle. Returns number of signals enqueued."""
    start = time.time()

    trenched_signals = _scan_trenches()
    gmgn_signals = _scan_gmgn()

    # Merge: prefer trenches (faster), dedupe by tx_hash
    seen_txs = set()
    merged = []
    for sig in trenched_signals + gmgn_signals:
        tx = sig.get("tx_hash", "")
        if tx in seen_txs:
            continue
        seen_txs.add(tx)
        merged.append(sig)

    # Enrich with scout timestamp
    scout_ts = time.time()
    for sig in merged:
        sig["_scout_ts"] = scout_ts
        sig["_latency_from_chain_ms"] = int((scout_ts - sig["timestamp"]) * 1000)
        enqueue_signal(sig)

    elapsed = time.time() - start
    n = len(merged)
    if n > 0:
        print(f"  [SCOUT] {n} signals in {elapsed*1000:.0f}ms "
              f"(latency p50: {int(sum(s['scout_latency_ms'] for s in merged)/max(1,n))}ms)")

    # Update desk state
    state = load_desk_state() or {}
    state["scout"] = {
        "last_cycle_ts": scout_ts,
        "signals_found": n,
        "cycle_ms": int(elapsed * 1000),
        "queue_depth": signal_count(),
        "watched_wallets": len(_watch_addresses),
    }
    save_desk_state(state)

    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="poll interval in seconds (0 = once)")
    args = ap.parse_args()

    if args.loop:
        print(f"  [SCOUT] Starting loop every {args.loop}s (Ctrl+C to stop)")
        try:
            while True:
                scout_cycle()
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\n  [SCOUT] Stopped")
    else:
        n = scout_cycle()
        print(json.dumps({"outcome": "ok", "signals_enqueued": n}))
        sys.exit(0 if n >= 0 else 1)
