import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
PARENT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PARENT))

import exit_manager

TOKEN = "0x90a71817bda6dac8c3a28bbfd877b02d667ae2f9"


class LiveExitLifecycle(unittest.TestCase):
    @patch.object(exit_manager.rh_sell, "execute_sell_plan")
    @patch.object(exit_manager.rh_sell, "build_sell_plan")
    @patch.object(exit_manager.rh_sell, "execute_approval_plan")
    @patch.object(exit_manager.rh_sell, "build_approval_plan")
    def test_approval_failure_stops_before_sell(self, build_approval, execute_approval, build_sell, execute_sell):
        build_sell.return_value = SimpleNamespace(approval_required=True)
        build_approval.return_value = SimpleNamespace(required=True)
        execute_approval.return_value = {"outcome": "reverted", "status": 0}
        result = exit_manager.execute_live_exit(TOKEN, 100)
        self.assertEqual(result["outcome"], "approval_failed")
        execute_sell.assert_not_called()

    @patch.object(exit_manager.rh_sell, "execute_sell_plan")
    @patch.object(exit_manager.rh_sell, "build_sell_plan")
    @patch.object(exit_manager.rh_sell, "execute_approval_plan")
    @patch.object(exit_manager.rh_sell, "build_approval_plan")
    def test_approval_success_rebuilds_quote_before_verified_sell(self, build_approval, execute_approval, build_sell, execute_sell):
        first = SimpleNamespace(approval_required=True)
        second = SimpleNamespace(approval_required=False)
        build_sell.side_effect = [first, second]
        build_approval.return_value = SimpleNamespace(required=True)
        execute_approval.return_value = {"outcome": "ok", "status": 1}
        execute_sell.return_value = {"outcome": "ok", "status": 1, "token_balance_delta": -100, "weth_balance_delta": 10}
        result = exit_manager.execute_live_exit(TOKEN, 100)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(build_sell.call_count, 2)
        execute_sell.assert_called_once_with(second)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class ExitManagerPersistence(unittest.TestCase):
    def setUp(self):
        self.log_patcher = patch.object(exit_manager, "log_trade")
        self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)

    @patch.object(exit_manager, "save_desk_state")
    @patch.object(exit_manager, "load_desk_state", return_value={})
    @patch.object(exit_manager, "save_positions")
    @patch.object(exit_manager, "_batch_price_update", return_value={TOKEN: 0.7})
    @patch.object(exit_manager, "load_positions")
    @patch.object(exit_manager, "execute_live_exit", return_value={"outcome": "reverted"})
    def test_failed_live_exit_keeps_position_open(self, execute, load_positions, _prices, save_positions, *_):
        load_positions.return_value = {"open": {TOKEN: {
            "symbol": "ROBINHOOD", "side": "BUY", "amount_eth": 1.0,
            "token_amount": 1.0, "token_amount_raw": 10**18, "token_decimals": 18,
            "entry_price_eth_per_token": 1.0, "current_price_eth_per_token": 1.0,
            "entry_ts": 1_000.0, "entry_gas_cost_eth": 0.0,
        }}, "closed": []}
        with patch.object(exit_manager.time, "time", return_value=1_001.0):
            events = exit_manager.check_exits(paper_mode=False)
        execute.assert_called_once_with(TOKEN, 10**18)
        saved = save_positions.call_args.args[0]
        self.assertIn(TOKEN, saved["open"])
        self.assertEqual(saved["open"][TOKEN]["exit_status"], "reverted")

class RoutePricedPaperMarks(unittest.TestCase):
    @patch.object(exit_manager.rh_sell, "build_sell_plan")
    def test_price_is_derived_from_executable_weth_quote(self, build_sell):
        build_sell.return_value = SimpleNamespace(quoted_weth_out=2 * 10**18)
        price = exit_manager._fetch_token_price(
            TOKEN, token_amount_raw=4 * 10**18, token_decimals=18, paper_mode=True
        )
        self.assertEqual(price, 0.5)
        self.assertFalse(build_sell.call_args.kwargs["require_balance"])
