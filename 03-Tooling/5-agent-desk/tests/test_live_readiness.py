import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
PARENT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PARENT))

import config
import desk
import scout


class ThresholdPolicy(unittest.TestCase):
    def test_strategy_thresholds_are_halved_but_safety_triggers_are_not(self):
        self.assertEqual(config.CONFIG.historian_threshold, 0.20)
        self.assertEqual(config.CONFIG.composite_threshold, 0.31)
        self.assertEqual(config.CONFIG.elite_threshold, 0.40)
        self.assertEqual(config.CONFIG.pulse_go_minimum, 0.15)
        self.assertEqual(config.CONFIG.btc_drop_alert_pct, -4.0)
        import executor
        self.assertEqual(executor._policy()["min_edge_observations"], 75)


class SignalFreshness(unittest.TestCase):
    def test_stale_signal_is_rejected(self):
        signal = {"timestamp": 1_000}
        self.assertFalse(desk.signal_is_fresh(signal, now_ts=1_301, max_age_seconds=300))

    def test_boundary_signal_is_accepted(self):
        signal = {"timestamp": 1_000}
        self.assertTrue(desk.signal_is_fresh(signal, now_ts=1_300, max_age_seconds=300))

    def test_future_signal_is_rejected(self):
        signal = {"timestamp": 2_000}
        self.assertFalse(desk.signal_is_fresh(signal, now_ts=1_000, max_age_seconds=300))


class PersistentDedupe(unittest.TestCase):
    def test_fill_remains_seen_after_memory_cache_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen_path = Path(tmp) / "seen.json"
            with patch.object(scout, "SEEN_FILLS_PATH", seen_path):
                scout._seen_fills.clear()
                scout._seen_timestamps.clear()
                self.assertTrue(scout._is_new_fill("0xtx", now_ts=1_000))
                scout._seen_fills.clear()
                scout._seen_timestamps.clear()
                self.assertFalse(scout._is_new_fill("0xtx", now_ts=1_001))


if __name__ == "__main__":
    unittest.main(verbosity=2)

class PaperEvidenceCycle(unittest.TestCase):
    def test_trade_signal_runs_executor_in_paper_mode(self):
        signal = {"timestamp": 1_000, "token": "0x" + "11" * 20, "symbol": "T", "amount_usd": 1000}
        enriched = {**signal, "pipeline_veto": False, "desk_action": "TRADE", "composite_score": 0.5}
        with patch.object(desk, "wallet_count", return_value=1), \
             patch.object(desk, "scout_cycle", return_value=1), \
             patch.object(desk, "dequeue_signals", return_value=[signal]), \
             patch.object(desk, "signal_is_fresh", return_value=True), \
             patch.object(desk, "process_signal", return_value=enriched), \
             patch.object(desk, "execute_trade", return_value={"outcome": "paper_opened"}) as execute, \
             patch.object(desk, "check_exits", return_value=[]), \
             patch.object(desk, "load_desk_state", return_value={}), \
             patch.object(desk, "save_desk_state"):
            desk.desk_cycle(paper_mode=True)
        execute.assert_called_once_with(enriched, paper_mode=True)

    def test_existing_queue_is_processed_when_scout_finds_nothing(self):
        signal = {"timestamp": time.time(), "token": "0x" + "22" * 20,
                  "symbol": "Q", "amount_usd": 1000, "tx_hash": "0xq"}
        enriched = {**signal, "pipeline_veto": False, "desk_action": "TRADE",
                    "composite_score": 0.5}
        with patch.object(desk, "wallet_count", return_value=1), \
             patch.object(desk, "scout_cycle", return_value=0), \
             patch.object(desk, "dequeue_signals", return_value=[signal]), \
             patch.object(desk, "signal_is_fresh", return_value=True), \
             patch.object(desk, "process_signal", return_value=enriched), \
             patch.object(desk, "execute_trade", return_value={"outcome": "paper_opened"}) as execute, \
             patch.object(desk, "check_exits", return_value=[]), \
             patch.object(desk, "load_desk_state", return_value={}), \
             patch.object(desk, "save_desk_state"):
            desk.desk_cycle(paper_mode=True)
        execute.assert_called_once_with(enriched, paper_mode=True)

class CrashSafeQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = config.CONFIG.signal_queue_path
        config.CONFIG.signal_queue_path = str(Path(self.tmp.name) / "queue.json")
        self.addCleanup(setattr, config.CONFIG, "signal_queue_path", self.old_path)

    def test_duplicate_transaction_is_enqueued_once(self):
        import state
        state.enqueue_signal({"tx_hash": "0xabc", "timestamp": 100})
        state.enqueue_signal({"tx_hash": "0xabc", "timestamp": 100})
        self.assertEqual(state.signal_count(), 1)

    def test_stale_backlog_is_purged_without_blocking_fresh_signal(self):
        import state
        for index in range(20):
            state.enqueue_signal({"tx_hash": f"0xold{index}", "timestamp": 100})
        state.enqueue_signal({"tx_hash": "0xfresh", "timestamp": 995})
        removed = state.purge_invalid_signals(now_ts=1000, max_age_seconds=300)
        self.assertEqual(len(removed), 20)
        claimed = state.dequeue_signals(limit=1, now_ts=1000)
        self.assertEqual(claimed[0]["tx_hash"], "0xfresh")
        state.ack_signal(claimed[0])

    def test_unacked_claim_is_recovered_after_lease(self):
        import state
        state.enqueue_signal({"tx_hash": "0xabc", "timestamp": 100})
        claimed = state.dequeue_signals(limit=1, now_ts=100, lease_seconds=300)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(state.dequeue_signals(limit=1, now_ts=200, lease_seconds=300), [])
        recovered = state.dequeue_signals(limit=1, now_ts=401, lease_seconds=300)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["tx_hash"], "0xabc")
        state.ack_signal(recovered[0])
        self.assertEqual(state.signal_count(), 0)

class WalletEligibility(unittest.TestCase):
    def test_scout_list_excludes_ineligible_wallets(self):
        import wallet_db
        db = {"wallets": {
            "0x1": {"address": "0x1", "eligible": True},
            "0x2": {"address": "0x2", "eligible": False},
        }}
        with patch.object(wallet_db, "load_wallet_db", return_value=db):
            self.assertEqual(wallet_db.get_active_wallet_list(), [{"address": "0x1", "eligible": True}])
            self.assertEqual(wallet_db.wallet_count(), 1)
