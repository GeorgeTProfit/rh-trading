"""Wallet discovery + scoring pipeline for the copy-desk.
Polls GMGN and AXIOM (and robinhoodtrenches.com as free fallback),
deduplicates, scores fitness, maintains the ~187-wallet equilibrium.

Zero LLM cost — pure API + math.
"""
from __future__ import annotations
import json, time, urllib.request, urllib.error
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from state import load_wallet_db, save_wallet_db
from config import CONFIG

BASE_URL = "https://robinhoodtrenches.com"
USER_AGENT = "trading-desk-scout/1.0"
WALLET_TTL = 3600 * 24  # re-score wallets daily


@dataclass
class WalletEntry:
    address: str
    handle: str = ""
    chain: str = "robinhood"
    wallet_class: str = "sniper"  # og | sniper | kol
    realized_pnl: float = 0.0
    winrate_14d: float = 0.0
    total_trades: int = 0
    volume: float = 0.0
    score: float = 0.0
    eligible: bool = False
    first_seen: float = 0.0
    last_active: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: dict) -> "WalletEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})


# ─── GMGN API (free tier) ───────────────────────────────────────────────

def fetch_gmgn_trending(chain: str = "robinhood", limit: int = 50) -> list:
    """Fetch trending/active wallets from GMGN API."""
    try:
        url = f"{CONFIG.gmgn_api_base}/v1/trending/{chain}?limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"  [wallet_db] GMGN fetch failed: {e}")
        return []


def fetch_gmgn_wallet_pnl(wallet: str, chain: str = "robinhood") -> Optional[dict]:
    """Fetch a single wallet's PnL stats from GMGN."""
    try:
        url = f"{CONFIG.gmgn_api_base}/v1/wallet/{chain}/{wallet}/pnl"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ─── Robinhood Trenches API (free fallback) ───────────────────────────────

def fetch_trenches_traders(window: str = "7d") -> list:
    """Fetch traders from robinhoodtrenches.com."""
    try:
        url = f"{BASE_URL}/api/traders?window={window}&stocks=false"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [wallet_db] Trenches fetch failed: {e}")
        return []


# ─── Wallet classification + scoring ─────────────────────────────────────

def score_trader_from_trenches(row: dict) -> Tuple[WalletEntry, float]:
    """Score trader from Trenches API row — mirrors trader_copy_strategy.py logic."""
    address = str(row.get("address", "")).lower()
    handle = str(row.get("handle", ""))
    closed = max(0, int(float(str(row.get("closed_trades", "0")))))
    wins = min(closed, max(0, int(float(str(row.get("wins", "0"))))))
    realized = float(str(row.get("realized_pnl", "0")))
    volume = max(0.0, float(str(row.get("volume", "0"))))
    active = bool(row.get("active", True))

    # Beta(5,5) shrunk win rate
    shrunk_wr = (wins + 5.0) / (closed + 10.0) if closed > 0 else 0.5
    realized_margin = max(-1.0, min(1.0, realized / volume)) if volume > 0 else -1.0
    sample_conf = min(1.0, closed / 25.0)

    score = 0.55 * shrunk_wr + 0.25 * max(0.0, realized_margin) + 0.20 * sample_conf
    eligible = active and closed >= 5 and realized > 0 and volume > 0 and shrunk_wr >= 0.50

    # Heuristic wallet class
    wallet_class = "sniper"
    if closed >= 100 and realized > 5000:
        wallet_class = "og"
    if volume > 500_000 and shrunk_wr < 0.55:
        wallet_class = "kol"

    return WalletEntry(
        address=address, handle=handle, chain="robinhood",
        wallet_class=wallet_class, realized_pnl=realized,
        winrate_14d=round(shrunk_wr, 4), total_trades=closed,
        volume=volume, score=round(score, 4), eligible=eligible,
        first_seen=time.time(), last_active=time.time(),
    ), score


# ─── Pipeline ──────────────────────────────────────────────────────────────

def refresh_wallet_db() -> dict:
    """Full wallet pipeline — runs every 4 hours via cron."""
    print("  [wallet_db] Refreshing wallet database...")
    existing_db = load_wallet_db()
    if not isinstance(existing_db, dict):
        existing_db = {"wallets": {}, "_meta": {"refresh_count": 0, "last_refresh": 0}}
    existing = existing_db.get("wallets", {})

    # Fetch from all sources
    new_entries: Dict[str, WalletEntry] = {}

    # 1) Robinhood Trenches traders
    traders = fetch_trenches_traders("7d")
    print(f"  [wallet_db]   Trenches: {len(traders)} traders")
    for row in traders:
        if not row.get("address"):
            continue
        addr = str(row["address"]).lower()
        entry, score = score_trader_from_trenches(row)
        if entry.eligible or score > 0.4:
            new_entries[addr] = entry

    # 2) GMGN trending wallets
    gmgn_data = fetch_gmgn_trending("robinhood", 100)
    print(f"  [wallet_db]   GMGN: {len(gmgn_data)} wallets")
    for w in gmgn_data:
        addr = str(w.get("address", "")).lower()
        if not addr or addr in new_entries:
            continue
        pnl_data = fetch_gmgn_wallet_pnl(addr)
        total_pnl = float(str(pnl_data.get("total_pnl", 0))) if pnl_data else 0
        winrate = float(str(pnl_data.get("winrate", 0))) if pnl_data else 0
        trades = int(float(str(pnl_data.get("total_trades", 0)))) if pnl_data else 0

        # Heuristic scoring for GMGN data
        eligible = winrate >= 0.40 and trades >= 3
        score = winrate * 0.6 + min(1.0, total_pnl / 5000) * 0.2 + min(1.0, trades / 50) * 0.2
        wallet_class = "sniper"
        if trades >= 50 and total_pnl > 2000:
            wallet_class = "og"
        if total_pnl < 0:
            wallet_class = "kol"
            score *= 0.5

        new_entries[addr] = WalletEntry(
            address=addr, handle=str(w.get("handle", "")),
            chain="robinhood", wallet_class=wallet_class,
            realized_pnl=total_pnl, winrate_14d=round(winrate, 4),
            total_trades=trades, volume=float(str(w.get("volume", 0))),
            score=round(score, 4), eligible=eligible,
            first_seen=time.time(), last_active=time.time(),
        )

    # 3) Merge with existing — keep existing metadata, prefer higher score
    merged = {}
    for addr, entry in new_entries.items():
        merged[addr] = entry
    for addr, existing_entry_data in existing.items():
        if addr not in merged:
            existing_entry = (
                WalletEntry.from_dict(existing_entry_data)
                if isinstance(existing_entry_data, dict)
                else existing_entry_data
            )
            # Keep existing wallets that haven't gone too stale
            age = time.time() - existing_entry.last_active
            if age < WALLET_TTL * 3:
                merged[addr] = existing_entry

    # 4) Cull underperformers (Zynex: drop below 40% winrate for 7 days)
    now = time.time()
    culled = 0
    for addr in list(merged.keys()):
        entry = merged[addr]
        if entry.winrate_14d < CONFIG.drop_winrate_7d:
            age = now - entry.last_active
            if age > 86400 * 3:  # give 3 days grace
                del merged[addr]
                culled += 1

    # 5) Maintain equilibrium around 187
    sorted_wallets = sorted(merged.values(), key=lambda w: -w.score)
    target = min(len(sorted_wallets), 250)
    final = {}
    for entry in sorted_wallets[:target]:
        final[entry.address] = entry.to_dict()

    db = {
        "wallets": final,
        "_meta": {
            "refresh_count": existing_db.get("_meta", {}).get("refresh_count", 0) + 1,
            "last_refresh": time.time(),
            "total_wallets": len(final),
            "eligible_count": sum(1 for w in final.values() if w.get("eligible")),
            "culled": culled,
        },
    }
    save_wallet_db(db)
    print(f"  [wallet_db]   Saved {len(final)} wallets ({sum(1 for w in final.values() if w.get('eligible'))} eligible, {culled} culled)")
    return db


def get_active_wallet_list() -> list[dict]:
    """Return the working wallet list for SCOUT."""
    db = load_wallet_db()
    if not isinstance(db, dict):
        return []
    wallets = db.get("wallets", {})
    return [wallet for wallet in wallets.values() if wallet.get("eligible") is True]


def wallet_count() -> int:
    return len(get_active_wallet_list())


if __name__ == "__main__":
    refresh_wallet_db()
