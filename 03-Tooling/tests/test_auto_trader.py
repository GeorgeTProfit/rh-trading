#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import auto_trader

ROBINHOOD = "0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9"
RHC = "0x7F04DA8CC451DddFBf80D6Fa3Aae3EE0642F8AB9"
OTHER = "0x1111111111111111111111111111111111111111"


class TokenApprovalScope(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "honeypot_required": True,
            "approved_tokens": [ROBINHOOD.lower()],
        }

    def test_approved_token_bypasses_manual_gate(self):
        self.assertTrue(auto_trader.token_is_approved(ROBINHOOD, self.cfg))
        self.assertFalse(auto_trader.requires_honeypot_gate(ROBINHOOD, self.cfg, True))

    def test_unapproved_token_stays_gated(self):
        self.assertFalse(auto_trader.token_is_approved(OTHER, self.cfg))
        self.assertTrue(auto_trader.requires_honeypot_gate(OTHER, self.cfg, True))

    def test_paper_mode_does_not_require_live_gate(self):
        self.assertFalse(auto_trader.requires_honeypot_gate(OTHER, self.cfg, False))


class DenylistPrecedence(unittest.TestCase):
    def test_rhc_is_denied_even_if_approved(self):
        cfg = {
            "blacklist": [RHC.lower()],
            "approved_tokens": [RHC.lower()],
        }
        self.assertTrue(auto_trader.token_is_denied(RHC, cfg))


class OutcomeMapping(unittest.TestCase):
    def test_only_verified_ok_is_positionable(self):
        self.assertTrue(auto_trader.is_verified_purchase({"outcome": "ok", "token_balance_delta": 1}))
        self.assertFalse(auto_trader.is_verified_purchase({"outcome": "reverted"}))
        self.assertFalse(auto_trader.is_verified_purchase({"outcome": "unknown_pending"}))
        self.assertFalse(auto_trader.is_verified_purchase({"outcome": "ok", "token_balance_delta": 0}))


class QuantRiskPrecedence(unittest.TestCase):
    def test_live_mode_blocks_missing_promotion_evidence(self):
        decision = auto_trader.execution_risk_decision({}, live_mode=True, now_ts=1000)
        self.assertFalse(decision["allowed"])
        self.assertIn("strategy_not_promoted", decision["reasons"])

    def test_paper_mode_remains_available_for_evidence_collection(self):
        decision = auto_trader.execution_risk_decision({}, live_mode=False, now_ts=1000)
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reasons"], [])

    def test_live_size_is_zero_without_validated_edge(self):
        self.assertEqual(auto_trader.live_position_size({}, auto_trader.DEFAULT_CONFIG), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
