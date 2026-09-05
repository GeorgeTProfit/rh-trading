#!/usr/bin/env python3
"""Tests for swap_collector — event-level on-chain data fetcher.

These tests use a fake RPC so no live chain calls are made.
TDD rule: each test must fail before its helper exists.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import swap_collector  # noqa: E402


V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ROBINHOOD = "0x90a71817bda6dac8c3a28bbfd877b02d667ae2f9"
POOL = "0x13c2080b09eeac9f1ee571fa4e806515bc6a7e6c"


def _raw_log(block: int, tx: str, log_index: int, amount0: int, amount1: int,
             sqrtPriceX96: int = 0, liquidity: int = 0, tick: int = 0,
             removed: bool = False, sender: str = ROBINHOOD, recipient: str = ROBINHOOD) -> Dict[str, Any]:
    """Build a Uniswap v3 Swap log dict matching the real RPC shape."""
    def s256(v: int) -> str:
        return hex(v if v >= 0 else (1 << 256) + v)[2:].rjust(64, "0")
    data = s256(amount0) + s256(amount1) + s256(sqrtPriceX96) + s256(liquidity) + s256(tick)
    return {
        "address": POOL,
        "blockHash": "0x" + "a" * 64,
        "blockNumber": hex(block),
        "transactionHash": tx,
        "transactionIndex": "0x4",
        "logIndex": hex(log_index),
        "removed": removed,
        "topics": [V3_SWAP_TOPIC, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
        "data": "0x" + data,
    }


class DecodeSwapLog(unittest.TestCase):
    def test_decode_returns_signed_int_amounts(self):
        # Pool deltas for the successful buy: WETH enters, ROBINHOOD leaves.
        amount0 = 3 * 10**15
        amount1 = -50723340127373564283458
        log = swap_collector.decode_swap_log(_raw_log(54503198, "0x" + "1" * 64, 74, amount0, amount1))
        self.assertEqual(log["pool"], POOL)
        self.assertEqual(log["block_number"], 54503198)
        self.assertEqual(log["tx_hash"], "0x" + "1" * 64)
        self.assertEqual(log["log_index"], 74)
        self.assertEqual(log["transaction_index"], 4)
        self.assertEqual(log["amount0"], amount0)
        self.assertEqual(log["amount1"], amount1)
        self.assertEqual(log["sender"], ROBINHOOD)
        self.assertEqual(log["recipient"], ROBINHOOD)

    def test_decode_handles_negative_token0(self):
        log = swap_collector.decode_swap_log(_raw_log(1, "0x" + "2" * 64, 0, -1, 100))
        self.assertLess(log["amount0"], 0)
        self.assertGreater(log["amount1"], 0)

    def test_decode_handles_negative_tick(self):
        log = swap_collector.decode_swap_log(_raw_log(1, "0" + "x" + "3" * 64, 0, 1, -100, tick=-12345))
        self.assertEqual(log["tick"], -12345)


class DirectionClassification(unittest.TestCase):
    def test_positive_amount0_means_buying_token1(self):
        side, amount_in, amount_out = swap_collector.classify_direction(1, -100, "WETH", "ROBINHOOD")
        self.assertEqual((side, amount_in, amount_out), ("buy", 1, 100))

    def test_negative_amount0_means_selling_token1(self):
        side, amount_in, amount_out = swap_collector.classify_direction(-1, 100, "WETH", "ROBINHOOD")
        self.assertEqual((side, amount_in, amount_out), ("sell", 100, 1))


class JsonRpcShape(unittest.TestCase):
    def test_get_logs_filter_is_wrapped_in_params_array(self):
        class Rpc:
            def __init__(self):
                self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                return []
        rpc = Rpc()
        swap_collector.fetch_logs_in_range(rpc, 1, 1, address=POOL)
        self.assertEqual(rpc.calls[0][0], "eth_getLogs")
        self.assertIsInstance(rpc.calls[0][1], list)
        self.assertEqual(rpc.calls[0][1][0]["address"], POOL)


class ReorgFilter(unittest.TestCase):
    def test_removed_logs_dropped(self):
        good = _raw_log(100, "0xa" * 64, 0, -1, 100)
        removed = _raw_log(101, "0xb" * 64, 1, -1, 100, removed=True)
        out = swap_collector.filter_logs([good, removed])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["blockNumber"], "0x64")
        self.assertFalse(any(r.get("removed", False) for r in out))


class Pagination(unittest.TestCase):
    def test_walks_in_chunks_with_progress(self):
        seen_ranges: List[int] = []
        def fake_fetch(_rpc, fr, to):
            seen_ranges.append((fr, to))
            return []
        rpc = object()
        swap_collector.fetch_logs_in_range(rpc, 50000, 50010, chunk=4, fetch=fake_fetch)
        self.assertEqual(seen_ranges, [(50000, 50003), (50004, 50007), (50008, 50010)])


class CollectorCli(unittest.TestCase):
    def test_script_has_executable_main(self):
        script = SCRIPT_DIR / "swap_collector.py"
        result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)


class DefaultRpcUrl(unittest.TestCase):
    def test_prefers_alchemy_when_key_set(self):
        import os
        old = os.environ.get("RH_ALCHEMY_KEY")
        os.environ["RH_ALCHEMY_KEY"] = "test-key"
        try:
            url = swap_collector.default_rpc_url()
        finally:
            if old is None:
                os.environ.pop("RH_ALCHEMY_KEY", None)
            else:
                os.environ["RH_ALCHEMY_KEY"] = old
        self.assertIn("alchemy.com", url)
        self.assertIn("test-key", url)

    def test_falls_back_to_public_rpc(self):
        import os
        old = os.environ.get("RH_ALCHEMY_KEY")
        os.environ.pop("RH_ALCHEMY_KEY", None)
        url = swap_collector.default_rpc_url()
        if old is not None:
            os.environ["RH_ALCHEMY_KEY"] = old
        self.assertIn("rpc.mainnet.chain.robinhood.com", url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
