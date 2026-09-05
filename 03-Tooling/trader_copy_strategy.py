#!/usr/bin/env python3
"""Risk-gated shadow signals from robinhoodtrenches.com public data.

This module never signs or broadcasts transactions. It produces paper signals
that still require the independent route and sellability preflight.
"""
from __future__ import annotations
import argparse
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE_URL = "https://robinhoodtrenches.com"
MIN_CLOSED_TRADES = 3
MIN_COPY_USD = 125.0
MIN_LIQUIDITY_USD = 50_000.0
MAX_FILL_TO_LIQUIDITY = 0.05
CONSENSUS_SECONDS = 120
ELITE_SCORE = 0.375
ELITE_BUY_USD = 1_000.0


def _number(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def score_trader(row):
    """Score repeatable realized performance without trusting open-bag marks."""
    closed = max(0, int(_number(row.get("closed_trades"))))
    wins = min(closed, max(0, int(_number(row.get("wins")))))
    realized = _number(row.get("realized_pnl"))
    volume = max(0.0, _number(row.get("volume")))
    shrunk_win_rate = (wins + 5.0) / (closed + 10.0)
    realized_margin = max(-1.0, min(1.0, realized / volume)) if volume else -1.0
    sample_confidence = min(1.0, closed / 25.0)
    score = 0.55 * shrunk_win_rate + 0.25 * max(0.0, realized_margin) + 0.20 * sample_confidence
    eligible = (bool(row.get("active", True)) and closed >= MIN_CLOSED_TRADES
                and realized > 0 and volume > 0 and shrunk_win_rate >= 0.50)
    return {"handle": row.get("handle"), "address": row.get("address"),
            "eligible": eligible, "score": round(score, 8),
            "closed_trades": closed, "wins": wins,
            "shrunk_win_rate": round(shrunk_win_rate, 8),
            "realized_pnl": realized, "realized_margin": round(realized_margin, 8)}


def is_copyable_fill(fill):
    """Apply dashboard-level filters before the mandatory chain preflight."""
    flags = " ".join(str(flag).lower() for flag in (fill.get("flags") or []))
    usd = _number(fill.get("usd"), -1.0)
    liquidity = _number(fill.get("liquidity"), -1.0)
    return (str(fill.get("side", "")).lower() == "buy"
            and bool(fill.get("new_position")) and not bool(fill.get("is_stock"))
            and fill.get("priced") == "cash_leg" and usd >= MIN_COPY_USD
            and liquidity >= MIN_LIQUIDITY_USD
            and usd <= liquidity * MAX_FILL_TO_LIQUIDITY
            and "airdrop" not in flags and "not a real buy" not in flags
            and bool(fill.get("token")) and bool(fill.get("handle")))


def build_signals(fills, ranked, *, now_ts):
    """Build fresh consensus or elite first-buy paper signals."""
    by_token = defaultdict(list)
    for fill in fills:
        handle = str(fill.get("handle", ""))
        rating = ranked.get(handle, {})
        age = now_ts - int(_number(fill.get("ts")))
        if rating.get("eligible") and 0 <= age <= CONSENSUS_SECONDS and is_copyable_fill(fill):
            by_token[str(fill["token"]).lower()].append(fill)
    signals = []
    for token, token_fills in by_token.items():
        latest_by_trader = {}
        for fill in sorted(token_fills, key=lambda item: int(_number(item.get("ts")))):
            latest_by_trader[str(fill["handle"])] = fill
        leaders = sorted(latest_by_trader)
        newest = max(latest_by_trader.values(), key=lambda item: int(_number(item.get("ts"))))
        reason = None
        if len(leaders) >= 2:
            reason = "qualified_consensus"
        elif len(leaders) == 1:
            rating = ranked[leaders[0]]
            if _number(rating.get("score")) >= ELITE_SCORE and _number(newest.get("usd")) >= ELITE_BUY_USD:
                reason = "elite_first_buy"
        if reason:
            signals.append({"token": token, "symbol": newest.get("symbol"),
                            "reason": reason, "leader_count": len(leaders),
                            "leaders": leaders, "latest_ts": int(_number(newest.get("ts"))),
                            "observed_liquidity_usd": _number(newest.get("liquidity")),
                            "paper_only": True, "requires_chain_preflight": True})
    return sorted(signals, key=lambda item: (-item["leader_count"], -item["latest_ts"], item["token"]))


def fetch_json(path, *, timeout=20.0):
    url = urllib.parse.urljoin(BASE_URL, path)
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def snapshot(window="24h", tape_limit=400):
    traders = fetch_json("/api/traders?window=%s&stocks=false" % urllib.parse.quote(window))
    tape = fetch_json("/api/tape?limit=%d&stocks=false" % int(tape_limit))
    overview = fetch_json("/api/overview?window=%s&stocks=false" % urllib.parse.quote(window))
    ranked = {row["handle"]: score_trader(row) for row in traders if row.get("handle")}
    observed_now = max((int(_number(row.get("ts"))) for row in tape), default=int(time.time()))
    return {"source": BASE_URL, "window": window, "observed_ts": observed_now,
            "overview": overview, "trader_count": len(traders),
            "eligible_traders": sorted((row for row in ranked.values() if row["eligible"]),
                                       key=lambda row: (-row["score"], -row["closed_trades"], str(row["handle"]))),
            "signals": build_signals(tape, ranked, now_ts=observed_now)}


def main():
    parser = argparse.ArgumentParser(description="Create Robinhood Trenches paper copy-flow signals")
    parser.add_argument("--window", default="24h")
    parser.add_argument("--tape-limit", type=int, default=400)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = snapshot(args.window, args.tape_limit)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
