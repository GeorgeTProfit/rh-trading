import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import risk_accounting


class RiskAccounting(unittest.TestCase):
    def setUp(self):
        self.state = {
            "equity_start_day_eth": 0.02,
            "equity_current_eth": 0.02,
            "equity_peak_eth": 0.02,
            "realized_pnl_today_eth": 0.0,
            "unrealized_pnl_today_eth": 0.0,
            "trades_today": 0,
            "risk_day_utc": "2026-09-05",
        }
        self.now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc).timestamp()

    def test_verified_entry_books_gas_and_turnover(self):
        result = risk_accounting.apply_entry(self.state, gas_cost_eth=0.0001, now_ts=self.now)
        self.assertAlmostEqual(result["equity_current_eth"], 0.0199)
        self.assertAlmostEqual(result["realized_pnl_today_eth"], -0.0001)
        self.assertEqual(result["trades_today"], 1)

    def test_verified_exit_books_proceeds_basis_and_exit_cost(self):
        after_entry = risk_accounting.apply_entry(self.state, gas_cost_eth=0.0001, now_ts=self.now)
        result = risk_accounting.apply_exit(
            after_entry, proceeds_eth=0.0012, cost_basis_eth=0.001,
            exit_cost_eth=0.00005, now_ts=self.now + 60,
        )
        self.assertAlmostEqual(result["realized_pnl_today_eth"], 0.00005)
        self.assertAlmostEqual(result["equity_current_eth"], 0.02005)
        self.assertAlmostEqual(result["drawdown_fraction"], 0.0)

    def test_utc_day_roll_resets_daily_counters_not_equity(self):
        next_day = datetime(2026, 9, 6, 0, 1, tzinfo=timezone.utc).timestamp()
        result = risk_accounting.apply_entry(self.state, gas_cost_eth=0.0001, now_ts=next_day)
        self.assertEqual(result["risk_day_utc"], "2026-09-06")
        self.assertEqual(result["trades_today"], 1)
        self.assertAlmostEqual(result["equity_start_day_eth"], 0.02)


if __name__ == "__main__":
    unittest.main()
