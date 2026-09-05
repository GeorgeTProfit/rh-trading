#!/usr/bin/env python3
"""
RH Chain Auto-Scanner — Strategy A (Momentum / Trend Following)
Read-only. Continuously polls Dexscreener, filters candidates, scores them,
and writes alerts to a file. No signing, no key access, no transaction broadcast.

Usage:
  python3 03-Tooling/auto_scanner.py             # continuous scanning
  python3 03-Tooling/auto_scanner.py --once       # single pass, exit
  python3 03-Tooling/auto_scanner.py --quiet       # only write to alert file, suppress stdout

Writes:
  alerts.json — append-only alert log with all high-scoring candidates
  scanner.log — running log of scans performed
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEXSCREENER = "https://api.dexscreener.com"
UA = "trading-trader-auto-scanner/1.0 (read-only)"
SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = SCANNER_DIR

DEFAULT_CONFIG = {
    "MIN_LIQUIDITY_USD": 5000,
    "MIN_VOLUME_24H_USD": 20000,
    "MAX_AGE_HOURS": 48,
    "MAX_24H_PUMP_PCT": 800,
    "MIN_MARKET_CAP": 20000,
    "MAX_MARKET_CAP": 2000000,
    "MIN_BUY_SELL_RATIO": 0.45,
    "MAX_BUY_SELL_RATIO": 0.90,
    "MIN_TX_COUNT_1H": 10,
    "SCORE_THRESHOLD": 5.0,
    "TOP_N": 10,
    "SCAN_INTERVAL_SECONDS": 60,
    "ALERT_FILE": "alerts.json",
    "LOG_FILE": "scanner.log",
    "QUIET": False,
}


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_pairs():
    seen = {}
    for q in ["robinhood", "rh", "RHC", "hood"]:
        try:
            data = http_json(f"{DEXSCREENER}/latest/dex/search?q={urllib.parse.quote(q)}")
        except Exception as e:
            print(f"  warn: search {q!r} failed: {e}", file=sys.stderr)
            continue
        for p in data.get("pairs") or []:
            if p.get("chainId") != "robinhood":
                continue
            pid = p.get("pairAddress", "")
            if not pid:
                continue
            prev = seen.get(pid)
            if prev is None or p.get("liquidity", {}).get("usd", 0) > prev.get("liquidity", {}).get("usd", 0):
                seen[pid] = p
    return list(seen.values())


def load_config():
    config_path = os.path.join(SCANNER_DIR, "config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception:
            pass
    return cfg


def score(pair, now_ts, cfg):
    liq = pair.get("liquidity", {}).get("usd") or 0
    vol24 = pair.get("volume", {}).get("h24") or 0
    vol1h = pair.get("volume", {}).get("h1") or 0
    vol5m = pair.get("volume", {}).get("m5") or 0
    ch1h = pair.get("priceChange", {}).get("h1") or 0
    ch24 = pair.get("priceChange", {}).get("h24") or 0
    buys1h = pair.get("txns", {}).get("h1", {}).get("buys") or 0
    sells1h = pair.get("txns", {}).get("h1", {}).get("sells") or 0
    created = pair.get("pairCreatedAt")
    age_h = ((now_ts - created / 1000) / 3600) if created else 9999
    fdv = pair.get("fdv") or 0
    mcap = pair.get("marketCap") or 0

    reasons = []
    score_val = 0.0

    # Hard filters (kill rug-prone pairs)
    if liq < cfg["MIN_LIQUIDITY_USD"]:
        return -1, [f"REJECT liq=${liq:,.0f} < ${cfg['MIN_LIQUIDITY_USD']:,}"]
    if age_h > cfg["MAX_AGE_HOURS"]:
        return -1, [f"REJECT age={age_h:.1f}h > {cfg['MAX_AGE_HOURS']}h"]
    if vol24 < cfg["MIN_VOLUME_24H_USD"]:
        return -1, [f"REJECT vol24h=${vol24:,.0f} < ${cfg['MIN_VOLUME_24H_USD']:,}"]
    if mcap < cfg["MIN_MARKET_CAP"]:
        return -1, [f"REJECT mcap ${mcap:,.0f} < ${cfg['MIN_MARKET_CAP']:,}"]
    if mcap > cfg["MAX_MARKET_CAP"]:
        return -1, [f"REJECT mcap ${mcap:,.0f} > ${cfg['MAX_MARKET_CAP']:,}"]
    if ch24 > cfg["MAX_24H_PUMP_PCT"]:
        return -1, [f"REJECT already pumped {ch24:+.1f}% in 24h"]

    # Soft signals (additive)
    if vol1h > 0 and vol5m > 0:
        proj1h = vol5m * 12
        if proj1h > vol1h * 1.5:
            score_val += 3
            reasons.append(f"+vol accel: 5m=${vol5m:,.0f} proj1h=${proj1h:,.0f} vs actual=${vol1h:,.0f}")
        elif proj1h > vol1h * 1.2:
            score_val += 1.5
            reasons.append(f"+vol warming: 5m proj 1.2x actual 1h")

    total_tx = buys1h + sells1h
    if total_tx >= cfg["MIN_TX_COUNT_1H"]:
        ratio = buys1h / total_tx
        if cfg["MIN_BUY_SELL_RATIO"] <= ratio <= cfg["MAX_BUY_SELL_RATIO"]:
            score_val += 2
            reasons.append(f"+healthy buy ratio {ratio:.0%} (n={total_tx})")
        elif ratio > cfg["MAX_BUY_SELL_RATIO"]:
            score_val -= 1
            reasons.append(f"-buy ratio suspiciously clean {ratio:.0%} (possible wash)")

    if 5 <= ch1h <= 50:
        score_val += 2
        reasons.append(f"+sane 1h +{ch1h:.1f}%")
    elif ch1h > 80:
        score_val += 0.5
        reasons.append(f"~vertical 1h +{ch1h:.1f}% (high rug risk)")

    if ch24 > 0:
        score_val += 1
        reasons.append(f"+24h +{ch24:.1f}%")

    if liq >= 20_000:
        score_val += 1
        reasons.append(f"+deep liq ${liq:,.0f}")
    elif liq >= 10_000:
        score_val += 0.5

    if age_h <= 6:
        score_val += 2
        reasons.append(f"+fresh: {age_h:.1f}h old")
    elif age_h <= 24:
        score_val += 1
        reasons.append(f"+young: {age_h:.1f}h old")

    if 5_000 <= mcap <= 500_000 and 5_000 <= fdv <= 2_000_000:
        score_val += 1
        reasons.append(f"+mcap ${mcap:,.0f} / fdv ${fdv:,.0f}")
    elif fdv > 10_000_000:
        score_val -= 2
        reasons.append(f"-fdv ${fdv:,.0f} too high (diluted)")

    return score_val, reasons


def write_alert(candidate, score_val, reasons, cfg):
    alert_path = os.path.join(OUTPUT_DIR, cfg["ALERT_FILE"])
    alert = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "score": score_val,
        "token_symbol": candidate["baseToken"].get("symbol", "?"),
        "token_address": candidate["baseToken"].get("address", ""),
        "quote_symbol": candidate["quoteToken"].get("symbol", "?"),
        "dex_id": candidate.get("dexId", "?"),
        "liquidity_usd": candidate.get("liquidity", {}).get("usd", 0),
        "volume_24h_usd": candidate.get("volume", {}).get("h24", 0),
        "price_change_1h": candidate.get("priceChange", {}).get("h1", 0),
        "price_change_24h": candidate.get("priceChange", {}).get("h24", 0),
        "market_cap": candidate.get("marketCap", 0),
        "age_hours": (time.time() - candidate["pairCreatedAt"] / 1000) / 3600,
        "reasons": reasons,
        "url_dexscreener": candidate.get("url", ""),
        "url_honeypot_check": f"https://trustswap.com/robinhood/honeypot-checker?token={candidate['baseToken'].get('address', '')}",
    }
    alerts = []
    if os.path.exists(alert_path):
        try:
            with open(alert_path) as f:
                alerts = json.load(f)
        except Exception:
            alerts = []
    alerts.append(alert)
    with open(alert_path, "w") as f:
        json.dump(alerts, f, indent=2)


def write_log(msg, cfg):
    log_path = os.path.join(OUTPUT_DIR, cfg["LOG_FILE"])
    with open(log_path, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass, exit")
    ap.add_argument("--quiet", action="store_true", help="only write to file, suppress stdout")
    args = ap.parse_args()

    cfg = load_config()
    cfg["QUIET"] = args.quiet or cfg.get("QUIET", False)

    write_log("=== Auto-scanner started ===", cfg)

    while True:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            pairs = fetch_pairs()
        except Exception as e:
            if not args.once:
                time.sleep(cfg["SCAN_INTERVAL_SECONDS"])
                continue
            else:
                return 1

        now = time.time()
        scored = []
        for p in pairs:
            s, reasons = score(p, now, cfg)
            if s <= 0:
                continue
            scored.append((s, p, reasons))

        scored.sort(key=lambda x: -x[0])
        top = scored[: cfg["TOP_N"]]

        write_log(f"Scan complete: {len(pairs)} pairs, {len(scored)} passed filters", cfg)

        for rank, (s, p, reasons) in enumerate(top, 1):
            bt = p["baseToken"]
            qt = p["quoteToken"]
            liq = p.get("liquidity", {}).get("usd") or 0
            vol24 = p.get("volume", {}).get("h24") or 0
            ch1h = p.get("priceChange", {}).get("h1") or 0
            ch24 = p.get("priceChange", {}).get("h24") or 0
            mcap = p.get("marketCap") or 0
            age_h = (now - p["pairCreatedAt"] / 1000) / 3600

            token_addr = bt["address"]
            honeypot_url = f"https://trustswap.com/robinhood/honeypot-checker?token={token_addr}"
            dex_url = p.get("url", "")
            
            alert_msg = (
                f"ALERT #{rank}  score={s:.1f}  {bt['symbol']}/{qt['symbol']} on {p.get('dexId','?')}\n"
                f"  contract: {token_addr}\n"
                f"  liq: ${liq:,.0f}  vol24h: ${vol24:,.0f}  mcap: ${mcap:,.0f}  age: {age_h:.1f}h\n"
                f"  1h: {ch1h:+.1f}%   24h: {ch24:+.1f}%\n"
                f"  honeypot-check: {honeypot_url}\n"
                f"  dexscreener: {dex_url}\n"
            )
            for r in reasons:
                alert_msg += f"    {r}\n"

            if not cfg["QUIET"]:
                print(f"\n{ts} {alert_msg.strip()}")
                print("  " + "=" * 70)

            write_alert(p, s, reasons, cfg)

        if args.once:
            write_log("Scanner exiting after --once pass", cfg)
            return 0

        write_log(f"Sleeping {cfg['SCAN_INTERVAL_SECONDS']}s", cfg)
        time.sleep(cfg["SCAN_INTERVAL_SECONDS"])


if __name__ == "__main__":
    sys.exit(main())
