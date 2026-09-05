#!/usr/bin/env python3
"""
RH Chain Momentum Scanner — Strategy A (paper-trade signal feed)

Read-only. Polls Dexscreener for new RH Chain pairs, scores them on momentum
+ safety heuristics, prints the top candidates to stdout. No signing, no
key access, no transaction broadcast — just signals.

Run:
  python3 03-Tooling/rh_scanner.py             # one scan, exit
  python3 03-Tooling/rh_scanner.py --loop 60   # scan every 60s

Writes nothing to disk. Read-only against public APIs.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEXSCREENER = "https://api.dexscreener.com"
UA = "trading-trader-scanner/1.0 (read-only)"

# Strategy A defaults — adjust as you learn
MIN_LIQUIDITY_USD  = 3_000     # ignore pools with < $3k liquidity (rug-prone)
MIN_VOLUME_24H_USD = 10_000    # need at least $10k 24h volume (some action)
MAX_AGE_HOURS      = 48        # only consider pairs created in last 48h
TOP_N              = 10        # print top N candidates
SEARCH_QUERIES     = ["robinhood", "rh", "RHC", "hood"]  # search terms; DS returns 30/pop


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_pairs():
    """Return a deduped set of robinhood-chain pairs from Dexscreener search."""
    seen = {}
    for q in SEARCH_QUERIES:
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
            # Keep the most-popular copy of each pair (highest liquidity)
            prev = seen.get(pid)
            if prev is None or p.get("liquidity", {}).get("usd", 0) > prev.get("liquidity", {}).get("usd", 0):
                seen[pid] = p
    return list(seen.values())


def score(pair, now_ts):
    """Return (score, reasons[]) for a Strategy A momentum candidate."""
    liq = pair.get("liquidity", {}).get("usd") or 0
    vol24 = pair.get("volume", {}).get("h24") or 0
    vol1h = pair.get("volume", {}).get("h1") or 0
    vol5m = pair.get("volume", {}).get("m5") or 0
    ch1h = pair.get("priceChange", {}).get("h1") or 0
    ch24 = pair.get("priceChange", {}).get("h24") or 0
    buys1h = pair.get("txns", {}).get("h1", {}).get("buys") or 0
    sells1h = pair.get("txns", {}).get("h1", {}).get("sells") or 0
    created = pair.get("pairCreatedAt")  # ms epoch
    age_h = ((now_ts - created / 1000) / 3600) if created else 9999
    fdv = pair.get("fdv") or 0
    mcap = pair.get("marketCap") or 0

    reasons = []
    score = 0.0

    # Hard filters (kill rug-prone pairs)
    if liq < MIN_LIQUIDITY_USD:
        return -1, [f"REJECT liq=${liq:,.0f} < ${MIN_LIQUIDITY_USD:,}"]
    if age_h > MAX_AGE_HOURS:
        return -1, [f"REJECT age={age_h:.1f}h > {MAX_AGE_HOURS}h"]
    if vol24 < MIN_VOLUME_24H_USD:
        return -1, [f"REJECT vol24h=${vol24:,.0f} < ${MIN_VOLUME_24H_USD:,}"]

    # Soft signals (additive)
    # Volume acceleration: 5m volume projected to 1h vs actual 1h
    proj1h = vol5m * 12
    if vol1h > 0 and proj1h > vol1h * 1.5:
        score += 3
        reasons.append(f"+vol accel: 5m=${vol5m:,.0f} proj1h=${proj1h:,.0f} vs actual=${vol1h:,.0f}")
    elif vol1h > 0 and proj1h > vol1h * 1.2:
        score += 1.5
        reasons.append(f"+vol warming: 5m proj 1.2x actual 1h")

    # Buy/sell ratio (without giving credit to suspicious 100% buy)
    if buys1h + sells1h >= 10:
        ratio = buys1h / (buys1h + sells1h)
        if 0.55 <= ratio <= 0.85:
            score += 2
            reasons.append(f"+healthy buy ratio {ratio:.0%} (n={buys1h + sells1h})")
        elif ratio > 0.92:
            score -= 1
            reasons.append(f"-buy ratio too clean {ratio:.0%} (possible wash)")

    # Price action: 1h green but not too vertical
    if 5 <= ch1h <= 50:
        score += 2
        reasons.append(f"+sane 1h +{ch1h:.1f}%")
    elif ch1h > 80:
        score += 0.5
        reasons.append(f"~vertical 1h +{ch1h:.1f}% (high rug risk)")

    # 24h change: still positive
    if ch24 > 0:
        score += 1
        reasons.append(f"+24h +{ch24:.1f}%")

    # Liquidity depth bonus
    if liq >= 20_000:
        score += 1
        reasons.append(f"+deep liq ${liq:,.0f}")
    elif liq >= 10_000:
        score += 0.5

    # Freshness bonus
    if age_h <= 6:
        score += 2
        reasons.append(f"+fresh: {age_h:.1f}h old")
    elif age_h <= 24:
        score += 1
        reasons.append(f"+young: {age_h:.1f}h old")

    # FDV sanity: not pre-mined to billions
    if 5_000 <= mcap <= 500_000 and 5_000 <= fdv <= 2_000_000:
        score += 1
        reasons.append(f"+mcap ${mcap:,.0f} / fdv ${fdv:,.0f}")
    elif fdv > 10_000_000:
        score -= 2
        reasons.append(f"-fdv ${fdv:,.0f} too high (diluted)")

    return score, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="seconds between scans (0 = once)")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--min-liq", type=float, default=MIN_LIQUIDITY_USD)
    ap.add_argument("--min-vol24", type=float, default=MIN_VOLUME_24H_USD)
    ap.add_argument("--max-age", type=float, default=MAX_AGE_HOURS)
    args = ap.parse_args()

    while True:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n=== {ts} — RH Chain momentum scan ===")
        try:
            pairs = fetch_pairs()
        except Exception as e:
            print(f"  ERROR fetching pairs: {e}")
            if not args.loop:
                return 1
            time.sleep(args.loop)
            continue
        print(f"  fetched {len(pairs)} unique RH Chain pairs from Dexscreener")

        now = time.time()
        scored = []
        for p in pairs:
            s, reasons = score(p, now)
            if s <= 0:
                continue
            scored.append((s, p, reasons))

        scored.sort(key=lambda x: -x[0])
        top = scored[: args.top]

        if not top:
            print("  no candidates passed filters — nothing to surface")
        else:
            for rank, (s, p, reasons) in enumerate(top, 1):
                bt = p["baseToken"]
                qt = p["quoteToken"]
                liq = p.get("liquidity", {}).get("usd") or 0
                vol24 = p.get("volume", {}).get("h24") or 0
                ch1h = p.get("priceChange", {}).get("h1") or 0
                ch24 = p.get("priceChange", {}).get("h24") or 0
                mcap = p.get("marketCap") or 0
                age_h = (now - p["pairCreatedAt"] / 1000) / 3600
                url = p.get("url", "")
                addr = p.get("baseToken", {}).get("address", "")
                print(f"\n  #{rank}  score={s:.1f}  ${bt['symbol']} / ${qt['symbol']}  on {p.get('dexId','?')}")
                print(f"      contract: {addr}")
                print(f"      liq: ${liq:,.0f}  vol24h: ${vol24:,.0f}  mcap: ${mcap:,.0f}  age: {age_h:.1f}h")
                print(f"      1h: {ch1h:+.1f}%   24h: {ch24:+.1f}%")
                print(f"      dexscreener: {url}")
                print(f"      honeypot: https://trustswap.com/robinhood/honeypot-checker?token={addr}")
                for r in reasons:
                    print(f"        {r}")

        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
