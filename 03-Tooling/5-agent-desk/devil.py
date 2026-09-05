"""DEVIL — the adversarial agent. Runs on the stronger model.
The other four work toward approval. DEVIL works toward veto.

Its only job: find a reason NOT to copy this signal, given everything the
others said.

Zynex: "DEVIL performance: 3 near-identical honey pot setups flagged that
would have been the same trap that cost $12,600."

Cost: ~$0.008/call (deep analysis pass)
"""
from __future__ import annotations
import json, time, urllib.request, urllib.parse
from typing import Optional
from state import log_trade
from config import CONFIG

USER_AGENT = "trading-desk-devil/1.0"
HONEYPOT_CHECKERS = [
    "https://api.gopluslabs.io/api/v1/token_security/4663",  # Robinhood Chain
]

# ─── On-chain security checks (free) ─────────────────────────────────────

def _check_honeypot_goplus(token: str) -> dict:
    """Check token security via GoPlus (free, no API key)."""
    try:
        url = f"{HONEYPOT_CHECKERS[0]}?address={token}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if not result:
            return {"honeypot": None, "flags": []}

        flags = []
        is_honeypot = False

        # Check key indicators
        if result.get("is_honeypot", "0") == "1":
            is_honeypot = True
            flags.append("flagged_honeypot")
        if result.get("buy_tax", "0") != "0":
            tax = float(result.get("buy_tax", "0"))
            if tax > 10:
                flags.append(f"buy_tax={tax}%")
                is_honeypot = True
        if result.get("sell_tax", "0") != "0":
            tax = float(result.get("sell_tax", "0"))
            if tax > 10:
                flags.append(f"sell_tax={tax}%")
                is_honeypot = True
        if result.get("cannot_sell_all", "0") == "1":
            flags.append("cannot_sell_all")
            is_honeypot = True
        if result.get("owner_address", ""):
            flags.append("has_owner")
        if result.get("proxy", "0") == "1":
            flags.append("proxy_contract")
        if result.get("is_mintable", "0") == "1":
            flags.append("mintable")
        if result.get("personal_slippage_modifiable", "0") == "1":
            flags.append("slippage_modifiable")
        if result.get("trading_cooldown", "0") == "1":
            flags.append("trading_cooldown")

        return {"honeypot": is_honeypot, "flags": flags, "raw": result}
    except Exception as e:
        return {"honeypot": None, "flags": [f"check_error={e}"]}


def _check_holder_concentration(token: str) -> dict:
    """Check top holder concentration via Dexscreener."""
    try:
        url = f"https://api.dexscreener.com/token-pairs/v1/robinhood/{token}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            pairs = json.loads(r.read())
        if not pairs:
            return {"top_holder_pct": None, "holders": 0, "warning": True,
                    "error": "holder_data_unavailable"}

        pair = pairs[0]
        holders_data = pair.get("holders", {})
        holders = int(holders_data.get("holders", 0))
        top_pct = float(holders_data.get("topHoldersTotalPercent", 0))
        warning = top_pct > 50 if top_pct else False
        return {
            "top_holder_pct": round(top_pct, 2) if top_pct else None,
            "holders": holders,
            "concentrated": warning,
        }
    except Exception as e:
        return {"top_holder_pct": None, "holders": 0, "warning": True,
                "error": str(e)}


# ─── Adversarial analysis ────────────────────────────────────────────────

def _adversarial_checks(
    signal: dict,
    historian_result: dict,
    context_result: dict,
    pulse_result: dict,
) -> tuple[float, list[str], list[str]]:
    """
    Run adversarial checks against the combined signal.
    Returns (veto_reason_score, veto_reasons, warnings).
    0.0 = total veto, 1.0 = no issues found.
    """
    veto_reasons = []
    warnings = []
    score = 1.0

    token = signal.get("token", "")
    wallet_class = signal.get("wallet_class", "sniper")
    amount_usd = float(signal.get("amount_usd", 0))
    copy_score = historian_result.get("copy_score", 0)
    narrative_score = context_result.get("narrative_score", 0)
    go_signal = pulse_result.get("pulse_go_signal", 0.5)
    context_warnings = context_result.get("context_info", {}).get("health_warnings", [])
    context_signals = context_result.get("context_info", {}).get("health_signals", [])

    # 1. Honeypot check
    security = _check_honeypot_goplus(token)
    if security.get("honeypot") is None:
        veto_reasons.append("SECURITY_DATA_UNAVAILABLE")
        score = 0.0
    elif security.get("honeypot") is True:
        veto_reasons.append(f"HONEYPOT_DETECTED: {', '.join(security['flags'])}")
        score -= 0.6
    elif security.get("flags"):
        flags = [f for f in security["flags"] if not f.startswith("check_error")]
        if flags:
            warnings.extend([f"security:{f}" for f in flags])
            score -= 0.15 * len(flags)

    # 2. Holder concentration
    holders = _check_holder_concentration(token)
    if holders.get("error") or holders.get("warning") and holders.get("top_holder_pct") is None:
        veto_reasons.append("HOLDER_DATA_UNAVAILABLE")
        score = 0.0
    elif holders.get("concentrated"):
        veto_reasons.append(f"TOP_HOLDER_CONCENTRATION={holders['top_holder_pct']}%")
        score -= 0.3
    elif holders.get("holders", 0) < 10:
        warnings.append(f"low_holders={holders['holders']}")
        score -= 0.1

    # 3. Sniper wallet + low liquidity = high rug risk
    if wallet_class == "sniper" and amount_usd > 1000:
        liq = float(signal.get("liquidity_usd", 0))
        if liq < 10000:
            veto_reasons.append(f"SNIPER_BIG_BUY_LOW_LIQ: ${amount_usd:,.0f} in ${liq:,.0f} pool")
            score -= 0.25

    # 4. Copy_score vs narrative_score mismatch
    if copy_score > 0.8 and narrative_score < 0.3:
        veto_reasons.append(
            f"SCORE_MISMATCH: copy={copy_score:.2f} narrative={narrative_score:.2f} "
            f"— high wallet trust but token is dead"
        )
        score -= 0.35

    # 5. Context warnings penalty
    severe_warnings = [w for w in context_warnings
                       if any(kw in w for kw in ["dead", "honeypot", "suspicious"])]
    if severe_warnings:
        veto_reasons.append(f"CONTEXT_WARNINGS: {severe_warnings}")
        score -= 0.3 * len(severe_warnings)

    # 6. Macro headwinds
    if go_signal < CONFIG.pulse_go_minimum:
        veto_reasons.append(f"MACRO_STAND_DOWN: go_signal={go_signal:.2f}")
        score -= 0.4
    elif go_signal < 0.5:
        warnings.append(f"macro_caution:go_signal={go_signal:.2f}")
        score -= 0.1

    # 7. Cross-correlation: lone sniper buy on dead volume
    if wallet_class == "sniper" and narrative_score < 0.4 and amount_usd > 500:
        if "low_tx_count" in context_warnings or "dead_volume" in context_warnings:
            veto_reasons.append("LONE_SNIPER_DEAD_TOKEN: possible honey pot setup")
            score -= 0.3

    score = max(0.0, min(1.0, score))
    return score, veto_reasons, warnings


def evaluate(
    signal: dict,
    historian_result: dict,
    context_result: dict,
    pulse_result: dict,
) -> dict:
    """
    DEVIL evaluates the combined signal for reasons to veto.
    Returns enriched signal with devil_score and final recommendation.
    """
    start = time.time()
    vet_score, veto_reasons, warnings = _adversarial_checks(
        signal, historian_result, context_result, pulse_result
    )
    elapsed_ms = int((time.time() - start) * 1000)

    # Veto if score drops below threshold
    final_veto = vet_score < 0.5 or len(veto_reasons) >= 2

    result = {
        **signal,
        "devil_score": round(vet_score, 4),
        "devil_veto": final_veto,
        "devil_veto_reasons": veto_reasons,
        "devil_warnings": warnings,
        "devil_latency_ms": elapsed_ms,
    }

    log_trade({
        "event": "devil_eval",
        "ts": time.time(),
        "token": signal.get("token", ""),
        "symbol": signal.get("symbol", "?"),
        "devil_score": round(vet_score, 4),
        "veto": final_veto,
        "veto_reasons": veto_reasons,
        "warnings": warnings,
        "latency_ms": elapsed_ms,
    })

    return result


if __name__ == "__main__":
    import sys
    data = json.loads(sys.stdin.read())
    result = evaluate(
        data.get("signal", {}),
        data.get("historian", {}),
        data.get("context", {}),
        data.get("pulse", {}),
    )
    print(json.dumps(result, indent=2, default=str))
