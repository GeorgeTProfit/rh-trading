import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
PARENT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PARENT))

import executor
import rh_swap

ROBINHOOD = "0x90A71817bdA6DAC8c3A28bBfd877b02D667ae2f9"
RHC = "0x7F04DA8CC451DddFBf80D6Fa3Aae3EE0642F8AB9"
OTHER = "0x1111111111111111111111111111111111111111"


def fake_plan(token=ROBINHOOD):
    return SimpleNamespace(
        token=token.lower(), wallet=rh_swap.TRENCHES_WALLET.lower(),
        amount_in_wei=2_000_000_000_000_000, quoted_out=50_000 * 10**18,
        token_decimals=18, token_symbol="ROBINHOOD", gas_estimate=150_000,
        max_fee_per_gas=400_000_000, pool="0x" + "22" * 20,
        router=rh_swap.SWAP_ROUTER_02.lower(), fee=10_000,
    )


class LiveTokenScope(unittest.TestCase):
    def test_permanent_denylist_blocks_live(self):
        result = executor.execute_trade({"token": RHC, "symbol": "RHC", "amount_usd": 1000}, paper_mode=False)
        self.assertEqual(result["outcome"], "token_denied")

    def test_unapproved_token_blocks_live(self):
        result = executor.execute_trade({"token": OTHER, "symbol": "OTHER", "amount_usd": 1000}, paper_mode=False)
        self.assertEqual(result["outcome"], "token_not_approved")


class PreflightIdentity(unittest.TestCase):
    @patch.object(executor, "display_plan", return_value={"ok": True})
    @patch.object(executor, "build_plan")
    def test_paper_preflight_uses_trenches_wallet_not_router(self, build, _display):
        build.return_value = fake_plan()
        result = executor._paper_preflight({"token": ROBINHOOD, "amount_usd": 1000})
        self.assertFalse(result.get("error"))
        self.assertEqual(build.call_args.kwargs["wallet"].lower(), rh_swap.TRENCHES_WALLET.lower())


class VerifiedExecution(unittest.TestCase):
    def setUp(self):
        self.log_patcher = patch.object(executor, "log_trade")
        self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)

    @patch.object(executor, "_save_position_manager")
    @patch.object(executor, "_load_position_manager")
    @patch.object(executor, "_save_risk_state")
    @patch.object(executor, "_load_risk_state")
    @patch.object(executor, "execute_plan")
    @patch.object(executor, "_real_preflight")
    def test_live_requires_receipt_and_positive_token_delta(self, preflight, execute, risk, _save_risk, pm, _save_pm):
        state = {
            "strategy_promoted": True, "observations": 150,
            "expectancy_lower_bound_eth": 0.00001,
            "equity_start_day_eth": 1, "equity_peak_eth": 1, "equity_current_eth": 1,
            "realized_pnl_today_eth": 0, "unrealized_pnl_today_eth": 0,
            "trades_today": 0, "last_trade_ts": 0,
            "expected_gross_edge_eth": 0.001, "round_trip_cost_eth": 0.0001,
            "kill_switch_active": False,
            "win_probability": 0.6, "average_win_eth": 0.001, "average_loss_eth": 0.001,
            "stop_fraction": 0.12, "realized_volatility": 0.02,
        }
        risk.return_value = state
        manager = unittest.mock.MagicMock()
        manager.get.return_value = None
        pm.return_value = manager
        preflight.return_value = {"paper": False, "amount_eth": 0.002, "swap_plan": fake_plan(), "plan": {}}
        execute.return_value = {"outcome": "receipt_ok_no_tokens", "status": 1, "token_balance_delta": 0}
        with patch.object(executor.auto_trader, "execution_risk_decision", return_value={"allowed": True, "reasons": []}), \
             patch.dict("os.environ", {"RH_PRIVATE_KEY": "test-only-placeholder"}):
            result = executor.execute_trade({"token": ROBINHOOD, "symbol": "ROBINHOOD", "amount_usd": 1000}, paper_mode=False)
        self.assertEqual(result["outcome"], "receipt_ok_no_tokens")
        _save_pm.assert_not_called()

    @patch.object(executor, "_save_position_manager")
    @patch.object(executor, "_load_position_manager")
    @patch.object(executor, "_paper_preflight")
    def test_paper_trade_opens_evidence_position(self, preflight, pm, save_pm):
        manager = unittest.mock.MagicMock()
        executor_obj = unittest.mock.MagicMock()
        manager.add.return_value = executor_obj
        manager.get.return_value = None
        pm.return_value = manager
        preflight.return_value = {"paper": True, "amount_eth": 0.002, "swap_plan": fake_plan(), "plan": {}}
        result = executor.execute_trade({"token": ROBINHOOD, "symbol": "ROBINHOOD", "amount_usd": 1000}, paper_mode=True)
        self.assertEqual(result["outcome"], "paper_opened")
        executor_obj.record_entry.assert_called_once()
        self.assertEqual(executor_obj._token_amount_raw, 50_000 * 10**18)
        self.assertEqual(executor_obj._token_decimals, 18)
        save_pm.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
