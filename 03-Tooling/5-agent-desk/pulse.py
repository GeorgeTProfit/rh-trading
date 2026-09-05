"""PULSE — the macro-mood agent.
Does the whole market permit new entries right now?

Runs on a 10-minute cache. If BTC drops 4% in an hour, or funding flips
sharply negative, or a major exchange goes into maintenance → PULSE goes
cold and the whole desk stands down.

Zynex insight: "This one line of code caught the worst of two double-digit
drawdown days in month two."

Cost: ~$0.003/call (mostly free API, cheap LLM for edge analysis)
"""
from __future__ import annotations
import json, time, urllib.request
from typing import Optional
from config import CONFIG
from state import log_trade, load_pulse_cache, save_pulse_cache

USER_AGENT = "trading-desk-pulse/1.0"

# Free crypto market data APIs
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://api.binance.com/api/v3"


def _http_json(url: str, timeout: int = 15) -> any:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get_btc_price_change() -> dict:
    """Get BTC price and 1h/24h change from CoinGecko (free)."""
    try:
        data = _http_json(
            f"{COINGECKO_BASE}/simple/price?ids=bitcoin&vs_currencies=usd"
            f"&include_24hr_change=true&include_1hr_change=true"
        )
        btc = data.get("bitcoin", {})
        price = float(btc.get("usd", 0))
        ch1h = float(btc.get("usd_1h_change", 0))
        ch24 = float(btc.get("usd_24h_change", 0))
        return {"price": price, "change_1h_pct": ch1h, "change_24h_pct": ch24}
    except Exception as e:
        return {"price": 0, "change_1h_pct": 0, "change_24h_pct": 0, "error": str(e)}


def _get_btc_price_from_binance() -> dict:
    """Alternative BTC price source."""
    try:
        ticker = _http_json(f"{BINANCE_BASE}/ticker/24hr?symbol=BTCUSDT")
        price = float(ticker.get("lastPrice", 0))
        ch = float(ticker.get("priceChangePercent", 0))
        return {"price": price, "change_24h_pct": ch}
    except Exception:
        return {}


def _get_funding_rates() -> Optional[float]:
    """Get perpetual funding rate from Binance (free). Negative funding = bearish."""
    try:
        data = _http_json(f"{BINANCE_BASE}/ticker/fundingRate?symbol=BTCUSDT")
        rate = float(data.get("lastFundingRate", 0))
        return rate
    except Exception:
        return None


def _get_market_dominance() -> Optional[float]:
    """BTC dominance from CoinGecko."""
    try:
        data = _http_json(
            f"{COINGECKO_BASE}/global"
        )
        return float(data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0))
    except Exception:
        return None


def _get_gas_price_rh() -> Optional[int]:
    """Get Robinhood Chain gas price (simple proxy for network activity)."""
    try:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}).encode()
        req = urllib.request.Request(
            CONFIG.rpc_url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            wei = int(data.get("result", "0"), 16)
            return wei
    except Exception:
        return None


# ─── Macro mood scoring ─────────────────────────────────────────────────

def score_market() -> dict:
    """
    Evaluate overall market conditions.
    Returns dict with go_signal (0.0-1.0) and reasons.
    """
    # Check cache
    cache = load_pulse_cache()
    cache_ts = cache.get("cached_at", 0) if isinstance(cache, dict) else 0
    now = time.time()
    if now - cache_ts < CONFIG.pulse_cache_ttl:
        cached = cache.get("cached_result", cache)
        if cached.get("data_quality_ok") is True:
            return cached
        return {**cached, "go_signal": 0.0, "phase": "red",
                "data_quality_ok": False,
                "warnings": list(cached.get("warnings", [])) + ["macro_data_unavailable"]}

    start = time.time()
    reasons = []
    warnings = []

    # 1. BTC trend (highest weight)
    btc = _get_btc_price_change()
    if not btc.get("price"):
        btc = _get_btc_price_from_binance()
    btc_price = btc.get("price", 0)
    btc_1h = btc.get("change_1h_pct", 0)
    btc_24h = btc.get("change_24h_pct", 0)

    if btc_price > 0:
        reasons.append(f"BTC=${btc_price:,.0f}")
    if btc_1h != 0:
        if btc_1h < CONFIG.btc_drop_alert_pct:
            warnings.append(f"btc_dropped_1h={btc_1h:.1f}%")
        elif btc_1h > 0:
            reasons.append(f"btc_up_1h={btc_1h:.1f}%")
    if btc_24h != 0:
        if btc_24h < -8:
            warnings.append(f"btc_down_24h={btc_24h:.1f}%")
        elif btc_24h > 3:
            reasons.append(f"btc_up_24h={btc_24h:.1f}%")

    # 2. Funding rates
    funding = _get_funding_rates()
    if funding is not None:
        if funding < CONFIG.perp_funding_sharp:
            warnings.append(f"negative_funding={funding:.6f}")
        else:
            reasons.append(f"funding={funding:.6f}")

    # 3. BTC dominance
    dominance = _get_market_dominance()
    if dominance is not None:
        if dominance > 62:
            warnings.append(f"high_btc_dominance={dominance:.1f}%_altcoin_pressure")
        elif dominance < 40:
            reasons.append(f"low_btc_dominance={dominance:.1f}%_alt_season")
        else:
            reasons.append(f"btc_dominance={dominance:.1f}%")

    # 4. Robinhood Chain gas — proxy for network activity
    gas = _get_gas_price_rh()
    if gas:
        gwei = gas / 1e9
        if gwei > 100:
            warnings.append(f"high_gas={gwei:.1f}gwei")
        elif gwei > 30:
            reasons.append(f"active_network_gas={gwei:.1f}gwei")
        else:
            reasons.append(f"low_gas={gwei:.1f}gwei")

    # BTC and chain gas are mandatory. Missing data forces a stand-down.
    data_quality_ok = btc_price > 0 and gas is not None
    if not data_quality_ok:
        warnings.append("macro_data_unavailable")
    base = 0.7 if data_quality_ok else 0.0
    for w in warnings:
        if "btc_dropped" in w:
            base -= 0.3
        elif "negative_funding" in w:
            base -= 0.15
        elif "high_btc_dominance" in w:
            base -= 0.1
        elif "high_gas" in w:
            base -= 0.05

    for r in reasons:
        if "btc_up_1h" in r and float(r.split("=")[-1].replace("%", "")) > 2:
            base += 0.1
        if "alt_season" in r:
            base += 0.15

    go_signal = max(0.0, min(1.0, base))
    elapsed_ms = int((time.time() - start) * 1000)

    # Market phase classification
    if go_signal >= 0.7:
        phase = "green"
    elif go_signal >= 0.4:
        phase = "yellow"
    elif go_signal >= 0.2:
        phase = "orange"
    else:
        phase = "red"

    result = {
        "go_signal": round(go_signal, 4),
        "phase": phase,
        "btc_price": btc_price,
        "btc_change_1h_pct": round(btc_1h, 2),
        "btc_change_24h_pct": round(btc_24h, 2),
        "funding_rate": funding,
        "btc_dominance_pct": dominance,
        "gas_gwei": round(gas / 1e9, 1) if gas else None,
        "data_quality_ok": data_quality_ok,
        "warnings": warnings,
        "reasons": reasons,
        "pulse_latency_ms": elapsed_ms,
        "cached_at": time.time(),
    }

    # Cache
    cache_data = {"cached_at": time.time(), "cached_result": result}
    save_pulse_cache(cache_data)

    return result


def evaluate(signal: dict) -> dict:
    """
    Evaluate a signal through PULSE — macro mood.
    Signal passes only if go_signal >= CONFIG.pulse_go_minimum.
    """
    market = score_market()
    go_signal = market["go_signal"]
    stand_down = go_signal < CONFIG.pulse_go_minimum

    result = {
        **signal,
        **{f"pulse_{k}": v for k, v in market.items() if k != "cached_at"},
        "pulse_stand_down": stand_down,
        "pulse_go_signal": go_signal,
    }

    log_trade({
        "event": "pulse_eval",
        "ts": time.time(),
        "token": signal.get("token", ""),
        "symbol": signal.get("symbol", "?"),
        "go_signal": round(go_signal, 4),
        "phase": market["phase"],
        "stand_down": stand_down,
        "btc_change_1h_pct": market["btc_change_1h_pct"],
        "warnings": market["warnings"],
    })

    return result


if __name__ == "__main__":
    import sys
    signal_str = sys.stdin.read() if not sys.stdin.isatty() else "{}"
    signal = json.loads(signal_str) if signal_str else {}
    result = evaluate(signal)
    print(json.dumps(result, indent=2, default=str))
