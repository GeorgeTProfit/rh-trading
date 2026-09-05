"""HISTORIAN — first LLM in the chain.
One question: does this wallet's history support copying this specific entry?

Evaluates wallet profile, trade pattern, timing, and token context.
Returns a copy_score (0.0-1.0). Below 0.4 → signal dropped, no further LLM calls.

Zynex insight: "Some wallets only work when they're buying alone — the second
another insider joins, the whole pattern breaks."

Cost: ~$0.005/call (deeper analysis) or free with rule-based mode.
"""
from __future__ import annotations
import json, os, time
from typing import Optional
from state import log_trade, load_wallet_db
from config import CONFIG

# ─── Rule-based historian (FREE mode) ───────────────────────────────────
# Zynex's HISTORIAN didn't need an LLM for the basic gate.
# We use heuristics first, LLM only for edge cases.

def _wallet_history_score(
    wallet: str,
    token: str,
    wallet_class: str,
    amount_usd: float,
    hour: int,
    signal: dict,
) -> tuple[float, str, list]:
    """
    Score a wallet-token entry purely on historical data.
    Returns (score, decision, reasons[]).
    """
    db = load_wallet_db()
    wallets = db.get("wallets", {}) if isinstance(db, dict) else {}
    reasons = []

    # Get wallet stats from DB
    entry = wallets.get(wallet, {})
    winrate = float(entry.get("winrate_14d", 0))
    total_trades = int(entry.get("total_trades", 0))
    realized_pnl = float(entry.get("realized_pnl", 0))

    # Class-based scoring
    class_multipliers = {
        "og": 1.3,      # OG operators almost never wrong on narratives
        "sniper": 1.0,  # Neutral baseline
        "kol": 0.7,     # KOL bags are loudest but least reliable
    }
    mult = class_multipliers.get(wallet_class, 1.0)

    # 1. Winrate score
    wr_score = min(1.0, max(0.0, (winrate - 0.40) / 0.40))
    reasons.append(f"wr={winrate:.2%} → {wr_score:.2f} (×{mult:.1f})")

    # 2. Trade count confidence
    if total_trades >= 100:
        conf = 1.0
        reasons.append("high_confidence")
    elif total_trades >= 30:
        conf = 0.7
        reasons.append("medium_confidence")
    elif total_trades >= 5:
        conf = 0.4
        reasons.append("low_confidence")
    else:
        conf = 0.1
        reasons.append("minimal_data")

    # 3. Position size signal
    # Small size = testing the waters, large = conviction
    if amount_usd >= 2000:
        size_score = 1.0
        reasons.append(f"large_buy=${amount_usd:,.0f}")
    elif amount_usd >= 500:
        size_score = 0.6
        reasons.append(f"medium_buy=${amount_usd:,.0f}")
    else:
        size_score = 0.2
        reasons.append(f"small_buy=${amount_usd:,.0f}")

    # 4. Time-based patterns
    # Zynex: "Some wallets print only between 22:00-04:00 UTC"
    if wallet_class == "sniper" and (hour < 6 or hour >= 22):
        time_bonus = 1.2
        reasons.append("sniper_prime_hours")
    elif wallet_class == "og" and 8 <= hour <= 18:
        time_bonus = 1.1
        reasons.append("og_regular_hours")
    else:
        time_bonus = 1.0

    # 5. Cross-wallet correlation check (Zynex insight)
    # Check if multiple watched wallets entered same token recently
    # (We don't have that in real time here — let DESK's orchestrator handle it)

    # Composite score
    base = (wr_score * 0.40 + conf * 0.25 + size_score * 0.35) * mult * time_bonus
    score = max(0.0, min(1.0, base))

    decision = "pass" if score >= CONFIG.historian_threshold else "veto"
    return score, decision, reasons


def evaluate(
    signal: dict,
    use_llm: bool = False,
) -> dict:
    """
    Evaluate a SCOUT signal through HISTORIAN.
    Returns enriched signal with copy_score.
    """
    wallet = signal.get("wallet", "")
    token = signal.get("token", "")
    wallet_class = signal.get("wallet_class", "sniper")
    amount_usd = float(signal.get("amount_usd", 0))
    ts = int(signal.get("timestamp", time.time()))
    hour = time.gmtime(ts).tm_hour if ts else time.gmtime().tm_hour

    start = time.time()
    score, decision, reasons = _wallet_history_score(
        wallet, token, wallet_class, amount_usd, hour, signal
    )
    elapsed_ms = int((time.time() - start) * 1000)

    result = {
        **signal,
        "copy_score": round(score, 4),
        "historian_decision": decision,
        "historian_reasons": reasons,
        "historian_latency_ms": elapsed_ms,
        "historian_mode": "heuristic",
    }

    log_trade({
        "event": "historian_eval",
        "ts": time.time(),
        "wallet": wallet,
        "token": token,
        "symbol": signal.get("symbol", "?"),
        "copy_score": round(score, 4),
        "decision": decision,
        "reasons": reasons,
        "latency_ms": elapsed_ms,
    })

    return result


if __name__ == "__main__":
    import sys
    signal_str = sys.stdin.read()
    signal = json.loads(signal_str)
    result = evaluate(signal, use_llm=False)
    print(json.dumps(result, indent=2, default=str))
