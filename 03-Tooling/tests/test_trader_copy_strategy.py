#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import trader_copy_strategy as s


def trader(handle, *, closed=10, wins=6, realized=1000, volume=10000,
           worst=-200, open_pnl=0, active=True):
    return {"handle": handle, "address": "0x" + "1"*40, "closed_trades": closed,
            "wins": wins, "realized_pnl": realized, "volume": volume,
            "worst_trade": worst, "unrealized_pnl": open_pnl, "active": active}


def fill(handle, token, ts, *, usd=1000, first=1, liquidity=100000,
         side="buy", flags=None, priced="cash_leg"):
    return {"id": ts, "ts": ts, "tx": "0x" + str(ts).rjust(64, "0"),
            "side": side, "usd": usd, "new_position": first, "is_stock": 0,
            "priced": priced, "handle": handle, "wallet": "0x" + "2"*40,
            "token": token, "symbol": "MEME", "liquidity": liquidity,
            "flags": flags or []}


class TraderSelection(unittest.TestCase):
    def test_requires_realized_history_not_open_bag_marks(self):
        self.assertFalse(s.score_trader(trader("bag", realized=-100, open_pnl=100000))["eligible"])

    def test_rejects_tiny_sample(self):
        self.assertFalse(s.score_trader(trader("lucky", closed=1, wins=1))["eligible"])

    def test_accepts_repeatable_realized_performance(self):
        result = s.score_trader(trader("solid", closed=20, wins=13, realized=4000, volume=20000, worst=-500))
        self.assertTrue(result["eligible"])
        self.assertGreater(result["score"], 0.5)


class FillSafety(unittest.TestCase):
    def test_rejects_airdrop_flag(self):
        self.assertFalse(s.is_copyable_fill(fill("solid", "0x"+"a"*40, 1, flags=["not a real buy (airdropped)"])))

    def test_rejects_non_first_buy(self):
        self.assertFalse(s.is_copyable_fill(fill("solid", "0x"+"a"*40, 1, first=0)))

    def test_rejects_low_liquidity(self):
        self.assertFalse(s.is_copyable_fill(fill("solid", "0x"+"a"*40, 1, liquidity=49999)))


class ConsensusSignals(unittest.TestCase):
    def setUp(self):
        self.token = "0x" + "a"*40
        self.ranked = {"alpha": {"eligible": True, "score": 0.70},
                       "beta": {"eligible": True, "score": 0.65},
                       "weak": {"eligible": False, "score": 0.20}}

    def test_two_qualified_first_buys_create_consensus(self):
        fills=[fill("alpha", self.token, 1000), fill("beta", self.token, 1080)]
        signals=s.build_signals(fills, self.ranked, now_ts=1100)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["reason"], "qualified_consensus")
        self.assertEqual(signals[0]["leader_count"], 2)

    def test_single_elite_large_buy_can_signal(self):
        ranked={"elite": {"eligible": True, "score": 0.80}}
        signals=s.build_signals([fill("elite", self.token, 1000, usd=2500)], ranked, now_ts=1050)
        self.assertEqual(signals[0]["reason"], "elite_first_buy")

    def test_stale_consensus_does_not_signal(self):
        fills=[fill("alpha", self.token, 1000), fill("beta", self.token, 1080)]
        self.assertEqual(s.build_signals(fills, self.ranked, now_ts=1300), [])

    def test_duplicate_trader_does_not_count_twice(self):
        fills=[fill("alpha", self.token, 1000), fill("alpha", self.token, 1010)]
        ranked={"alpha": {"eligible": True, "score": 0.30}}
        self.assertEqual(s.build_signals(fills, ranked, now_ts=1020), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
