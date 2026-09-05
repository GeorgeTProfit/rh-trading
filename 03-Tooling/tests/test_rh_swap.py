#!/usr/bin/env python3
"""Unit tests for rh_swap.py.

Run:
  python3 -m unittest tests.test_rh_swap -v
"""
import os
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import rh_swap  # noqa: E402


class NormalizeAddress(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(
            rh_swap.norm_addr("0x8876789976dEcBfCbBbe364623C63652db8C0904"),
            "0x8876789976decbfcbbbe364623c63652db8c0904",
        )

    def test_rejects_non_20_bytes(self):
        with self.assertRaises(ValueError):
            rh_swap.norm_addr("0x1234")

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            rh_swap.norm_addr("0xZZZZ")


class BuildV3Calldata(unittest.TestCase):
    def test_exactInputSingle_layout(self):
        cd = rh_swap.build_v3_exact_input_single(
            token_in="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
            token_out="0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9",
            fee=10000,
            recipient="0x168f71E235f2C81B3f8219f771bF1a94999D662b",
            amount_in_wei=3000000000000000,
            amount_out_min_wei=3000000000000000,
        )
        self.assertTrue(cd.startswith("0x04e45aaf"))
        self.assertEqual(len(cd), 2 + 8 + 7 * 64)

    def test_amount_fields_padded(self):
        cd = rh_swap.build_v3_exact_input_single(
            token_in="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
            token_out="0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9",
            fee=10000,
            recipient="0x168f71E235f2C81B3f8219f771bF1a94999D662b",
            amount_in_wei=int(0.003 * 10**18),
            amount_out_min_wei=1,
        )
        body = cd[2:]
        amount_in = int(body[8 + 4 * 64 : 8 + 5 * 64], 16)
        amount_out = int(body[8 + 5 * 64 : 8 + 6 * 64], 16)
        self.assertEqual(amount_in, int(0.003 * 10**18))
        self.assertEqual(amount_out, 1)


class Denylist(unittest.TestCase):
    def test_blacklist_blocks_rhc(self):
        self.assertTrue(
            rh_swap.is_denied(
                "0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9",
                ["0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"],
            )
        )

    def test_blacklist_case_insensitive(self):
        self.assertTrue(
            rh_swap.is_denied(
                "0x7F04DA8CC451DddFBf80D6Fa3Aae3EE0642F8AB9",
                ["0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"],
            )
        )

    def test_unlisted_token_passes(self):
        self.assertFalse(
            rh_swap.is_denied(
                "0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9",
                ["0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"],
            )
        )


class ClassifyReceipt(unittest.TestCase):
    def test_status_1_is_ok(self):
        r = rh_swap.classify_receipt({"status": "0x1", "blockNumber": "0x100"}, "")
        self.assertEqual(r["outcome"], "ok")

    def test_status_0_is_reverted(self):
        r = rh_swap.classify_receipt({"status": "0x0", "blockNumber": "0x100"}, "")
        self.assertEqual(r["outcome"], "reverted")

    def test_missing_status_is_unknown_pending(self):
        r = rh_swap.classify_receipt({}, "")
        self.assertEqual(r["outcome"], "unknown_pending")

    def test_stdout_status_0_overrides_unmined(self):
        r = rh_swap.classify_receipt({"status": "0x1"}, "status 0 (failed)")
        self.assertEqual(r["outcome"], "reverted")


class SlippageFloor(unittest.TestCase):
    def test_floor_5pct(self):
        self.assertEqual(rh_swap.apply_slippage(1000, 500), 950)

    def test_floor_zero_slippage(self):
        self.assertEqual(rh_swap.apply_slippage(1000, 0), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
