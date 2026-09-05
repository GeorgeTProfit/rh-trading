import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import evidence


class ClusteredEvidence(unittest.TestCase):
    def test_repeated_wallet_trades_do_not_count_as_independent_clusters(self):
        rows = [
            {"estimated_net_pnl_eth": 0.001, "evidence": {"source_wallet": "A"}},
            {"estimated_net_pnl_eth": 0.001, "evidence": {"source_wallet": "A"}},
            {"estimated_net_pnl_eth": -0.004, "evidence": {"source_wallet": "B"}},
        ]
        summary = evidence.summarize_outcomes(rows, bootstrap_iterations=500)
        self.assertEqual(summary["observations"], 3)
        self.assertEqual(summary["independent_clusters"], 2)
        self.assertLessEqual(summary["expectancy_lower_bound_eth"], 0)

    def test_unverified_sellability_prevents_promotion(self):
        rows = [
            {"estimated_net_pnl_eth": 0.001, "sellability_verified": False,
             "evidence": {"source_wallet": f"w{i}"}}
            for i in range(75)
        ]
        summary = evidence.summarize_outcomes(rows, bootstrap_iterations=100)
        ready = evidence.promotion_readiness(summary, min_observations=75)
        self.assertFalse(ready["ready"])
        self.assertIn("sellability_not_verified", ready["reasons"])


if __name__ == "__main__":
    unittest.main()
