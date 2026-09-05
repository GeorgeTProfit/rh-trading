"""CONTEXT — the narrative agent.
Job: is what they're buying actually alive?

Reads token metadata, holders, socials, DEX volume trajectory, and
crypto Twitter sentiment on the narrative. Can veto a copy_score of 0.9.

Zynex insight: "If an insider you trust just bought a token with a dying
meme and no volume, you skip. Insiders are wrong too."

Cost: ~$0.005/call (API lookups + LLM analysis)
"""
from __future__ import annotations
import json, time, urllib.request, urllib.parse, re
from typing import Optional
from state import log_trade
from config import CONFIG

USER_AGENT = "trading-desk-context/1.0"
DEXSCREENER = "https://api.dexscreener.com"

# ─── On-chain data (free) ────────────────────────────────────────────────

def _fetch_dexscreener_pair(token_address: str) -> Optional[dict]:
    """Fetch token pair data from Dexscreener API."""
    try:
        url = f"{DEXSCREENER}/latest/dex/search?q={urllib.parse.quote(token_address)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        pairs = data.get("pairs", [])
        for pair in pairs:
            base = pair.get("baseToken", {})
            quote = pair.get("quoteToken", {})
            addresses = {str(base.get("address", "")).lower(), str(quote.get("address", "")).lower()}
            if pair.get("chainId") == "robinhood" and token_address.lower() in addresses:
                return pair
        return None
    except Exception as e:
        return None


def _get_token_health(pair: dict) -> dict:
    """Score token health from DEX data."""
    health = {"score": 0.0, "signals": [], "warnings": []}

    liq = float((pair.get("liquidity") or {}).get("usd", 0))
    vol24 = float((pair.get("volume") or {}).get("h24", 0))
    vol1h = float((pair.get("volume") or {}).get("h1", 0))
    vol5m = float((pair.get("volume") or {}).get("m5", 0))
    txns24 = (pair.get("txns") or {}).get("h24", {})
    buys24 = int(txns24.get("buys", 0))
    sells24 = int(txns24.get("sells", 0))
    ch1h = float((pair.get("priceChange") or {}).get("h1", 0))
    ch24 = float((pair.get("priceChange") or {}).get("h24", 0))
    fdv = float(pair.get("fdv", 0))
    age = (time.time() - float(pair.get("pairCreatedAt", 0)) / 1000) / 3600 if pair.get("pairCreatedAt") else 0

    # Liquidity check
    if liq >= 20000:
        health["score"] += 20
        health["signals"].append(f"deep_liq=${liq:,.0f}")
    elif liq >= 5000:
        health["score"] += 10
        health["signals"].append(f"liq=${liq:,.0f}")
    elif liq < 1000:
        health["warnings"].append(f"low_liq=${liq:,.0f}")
        health["score"] -= 20

    # Volume check — alive market
    if vol24 > 50000:
        health["score"] += 15
        health["signals"].append(f"high_vol=${vol24:,.0f}")
    elif vol1h > 5000:
        health["score"] += 10
        health["signals"].append(f"active_1h=${vol1h:,.0f}")
    elif vol1h < 100:
        health["warnings"].append("dead_volume")

    # Volume acceleration (Zynex's pattern)
    if vol5m > 0 and vol1h > 0:
        proj = vol5m * 12
        if proj > vol1h * 1.5:
            health["score"] += 15
            health["signals"].append("volume_accelerating")
        elif proj > vol1h * 1.2:
            health["score"] += 8

    # Buy/sell sanity
    total_txns = buys24 + sells24
    if total_txns > 20:
        ratio = buys24 / total_txns
        if 0.45 <= ratio <= 0.85:
            health["score"] += 10
            health["signals"].append(f"healthy_buy_ratio={ratio:.0%}")
        elif ratio > 0.95:
            health["warnings"].append(f"suspicious_buy_ratio={ratio:.0%}")
            health["score"] -= 15
    else:
        health["warnings"].append("low_tx_count")

    # Price action
    if 0 < ch1h <= 80 and 0 < ch24 <= 500:
        health["score"] += 10
        health["signals"].append(f"positive_1h={ch1h:.1f}%_24h={ch24:.1f}%")
    elif ch1h > 0:
        health["score"] += 5
    elif ch1h < -20:
        health["warnings"].append(f"sharp_1h_drop={ch1h:.1f}%")
        health["score"] -= 10

    # FDV sanity
    if 5000 <= fdv <= 2000000:
        health["score"] += 10
    elif fdv > 10000000:
        health["warnings"].append(f"high_fdv=${fdv:,.0f}")
        health["score"] -= 15

    # Age bonus
    if age <= 2:
        health["score"] += 10
        health["signals"].append(f"fresh={age:.1f}h")
    elif age <= 24:
        health["score"] += 5
    elif age > 168:  # > 1 week, needs volume to stay alive
        if vol24 < 10000:
            health["warnings"].append("old_no_volume")

    health["score"] = max(-100, min(100, health["score"]))
    return health


def _classify_meme_health(health: dict) -> tuple[str, float]:
    """Classify meme health based on DEX signals."""
    s = health["score"]
    warnings = len(health["warnings"])
    signals = len(health["signals"])

    if s >= 50 and warnings == 0:
        return "strong", 0.9
    elif s >= 25:
        return "moderate", 0.7
    elif s >= 0:
        return "weak", 0.5
    elif s >= -25:
        return "dying", 0.3
    else:
        return "dead", 0.1


# ─── Main evaluation ─────────────────────────────────────────────────────

def evaluate(signal: dict) -> dict:
    """
    Evaluate a signal through CONTEXT — token narrative + health check.
    Returns enriched signal with narrative_score.
    """
    token = signal.get("token", "")
    symbol = signal.get("symbol", "?")
    start = time.time()

    # Fetch on-chain data
    pair = _fetch_dexscreener_pair(token)
    if pair:
        health = _get_token_health(pair)
        status, narrative_score = _classify_meme_health(health)
        token_name = str(pair.get("baseToken", {}).get("name", symbol))
        token_symbol = str(pair.get("baseToken", {}).get("symbol", symbol))

        # Extract dex info
        dex_id = str(pair.get("dexId", "?"))
        pair_url = str(pair.get("url", ""))
        token_age_h = (time.time() - float(pair.get("pairCreatedAt", 0)) / 1000) / 3600
        mcap = float(pair.get("marketCap", 0))
        fdv = float(pair.get("fdv", 0))

        context_info = {
            "token_name": token_name,
            "token_symbol": token_symbol,
            "dex": dex_id,
            "pair_url": pair_url,
            "token_age_hours": round(token_age_h, 1),
            "market_cap": mcap,
            "fdv": fdv,
            "health_score": health["score"],
            "health_status": status,
            "health_signals": health["signals"],
            "health_warnings": health["warnings"],
        }
    else:
        # No DEX data — token is too new or unknown
        narrative_score = 0.0
        status = "unavailable"
        context_info = {
            "token_name": symbol,
            "token_symbol": symbol,
            "dex": "unknown",
            "health_status": "unknown",
            "health_score": 0,
            "health_warnings": ["no_dex_data"],
        }

    elapsed_ms = int((time.time() - start) * 1000)

    data_quality_ok = pair is not None
    # Unknown/wrong-chain market data is a hard veto.
    veto = not data_quality_ok or narrative_score < 0.2

    result = {
        **signal,
        "narrative_score": round(narrative_score, 4),
        "context_status": status,
        "context_info": context_info,
        "context_veto": veto,
        "context_data_quality_ok": data_quality_ok,
        "context_latency_ms": elapsed_ms,
    }

    log_trade({
        "event": "context_eval",
        "ts": time.time(),
        "token": token,
        "symbol": symbol,
        "narrative_score": round(narrative_score, 4),
        "status": status,
        "veto": veto,
        "latency_ms": elapsed_ms,
        "health_score": context_info.get("health_score", 0),
        "warnings": context_info.get("health_warnings", []),
    })

    return result


if __name__ == "__main__":
    import sys
    signal_str = sys.stdin.read()
    signal = json.loads(signal_str)
    result = evaluate(signal)
    print(json.dumps(result, indent=2, default=str))
