#!/usr/bin/env python3
"""Tests for the Hummingbot-inspired position executor.

Validates:
- TripleBarrier barrier control (stop_loss, take_profit, trailing_stop, time_limit)
- Net PnL calculation (gross - fees - gas - slippage)
- Volatility-adjusted barriers
- PositionManager aggregation
- Partial exits
- Failed exit retries
- CloseType classification
"""
import json
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, '../')
from position_executor import (
    CloseType,
    PositionExecutor,
    PositionExecutorConfig,
    PositionManager,
    TripleBarrierConfig,
    TrailingStopConfig,
    TrackedOrder,
)


class TestCloseType(unittest.TestCase):
    def test_all_close_types_defined(self):
        types = [CloseType.TAKE_PROFIT, CloseType.STOP_LOSS,
                 CloseType.TIME_LIMIT, CloseType.TRAILING_STOP,
                 CloseType.EARLY_STOP, CloseType.EXPIRED,
                 CloseType.INSUFFICIENT_BALANCE, CloseType.FAILED]
        self.assertEqual(len(types), 8)
        for ct in types:
            self.assertIsNotNone(ct.value)
            self.assertIsInstance(ct.value, str)


class TestPositionExecutorEntry(unittest.TestCase):
    def test_entry_creates_running_status(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=0.0000002,
            triple_barrier=TripleBarrierConfig(
                stop_loss=0.15,
                take_profit=0.30,
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        self.assertEqual(executor.status, "OPEN")
        self.assertIsNone(executor.close_type)

        executor.record_entry(
            tx_hash="0x123abc",
            token_amount=5000000.0,
            gas_cost_eth=0.00001,
            actual_fill_price=0.0000002,
        )
        self.assertEqual(executor.status, "RUNNING")
        self.assertIsNotNone(executor._entry_order)
        self.assertTrue(executor._entry_order.is_filled)
        self.assertAlmostEqual(executor.open_filled_amount, 5000000.0)

    def test_entry_price_updates_from_fill(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=0.0000002,
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 5000000.0, 0.00001, 0.00000025)
        self.assertAlmostEqual(executor.entry_price, 0.00000025)
        self.assertAlmostEqual(executor.current_market_price, 0.00000025)


class TestPnLCalculation(unittest.TestCase):
    def setUp(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=0.0000002,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15, take_profit=0.30),
            timestamp=1000.0,
        )
        self.executor = PositionExecutor(config)
        self.executor.record_entry("0x123", 5000000.0, 0.00001, 0.0000002)

    def test_zero_pnl_at_entry(self):
        self.assertAlmostEqual(self.executor.gross_pnl_pct, 0.0)
        self.assertAlmostEqual(self.executor.net_pnl_pct, 0.0, places=3)

    def test_gross_pnl_pct_positive(self):
        self.executor.update_market_price(0.00000025)
        self.assertAlmostEqual(self.executor.gross_pnl_pct, 0.25)

    def test_gross_pnl_pct_negative(self):
        self.executor.update_market_price(0.00000015)
        self.assertAlmostEqual(self.executor.gross_pnl_pct, -0.25)

    def test_net_pnl_accounts_for_gas(self):
        self.executor.update_market_price(0.00000025)
        self.assertLess(self.executor.net_pnl_pct, self.executor.gross_pnl_pct)

    def test_net_pnl_accounts_for_fees(self):
        self.executor.partial_exit(
            token_amount=1000000.0,
            exit_price=0.0000003,
            gas_cost_eth=0.00001,
        )
        self.assertGreater(self.executor._cum_fees_eth, 0)
        self.assertLess(self.executor.net_pnl_pct, self.executor.gross_pnl_pct)

    def test_entry_value_eth(self):
        expected = 5000000.0 * 0.0000002
        self.assertAlmostEqual(self.executor.entry_value_eth, expected)

    def test_current_value_eth(self):
        self.executor.update_market_price(0.00000025)
        expected = 5000000.0 * 0.00000025
        self.assertAlmostEqual(self.executor.current_value_eth, expected)


class TestTripleBarrierStopLoss(unittest.TestCase):
    def test_stop_loss_triggered(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(0.8)
        close_type = executor.control_barriers()
        self.assertEqual(close_type, CloseType.STOP_LOSS)
        self.assertTrue(executor.is_closed)

    def test_stop_loss_not_triggered(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(0.9)
        close_type = executor.control_barriers()
        self.assertIsNone(close_type)
        self.assertFalse(executor.is_closed)

    def test_no_stop_loss_config(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=None),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(0.01)
        close_type = executor.control_barriers()
        self.assertIsNone(close_type)


class TestTripleBarrierTakeProfit(unittest.TestCase):
    def test_take_profit_triggered(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(1.4)
        close_type = executor.control_barriers()
        self.assertEqual(close_type, CloseType.TAKE_PROFIT)
        self.assertTrue(executor.is_closed)

    def test_take_profit_not_triggered(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(1.2)
        close_type = executor.control_barriers()
        self.assertIsNone(close_type)


class TestTrailingStop(unittest.TestCase):
    def test_trailing_stop_activation(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(
                trailing_stop=TrailingStopConfig(
                    activation_price=0.10,
                    trailing_delta=0.05,
                ),
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(1.2)
        close_type = executor.control_barriers()
        self.assertIsNone(close_type)
        self.assertIsNotNone(executor._trailing_stop_trigger)
        self.assertAlmostEqual(executor._trailing_stop_trigger, 0.15)

    def test_trailing_stop_hit(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(
                trailing_stop=TrailingStopConfig(
                    activation_price=0.10,
                    trailing_delta=0.05,
                ),
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(1.2)
        executor._check_trailing_stop()  # triggers activation check
        self.assertIsNotNone(executor._trailing_stop_trigger)
        executor.update_market_price(1.1)
        close_type = executor.control_barriers()
        self.assertEqual(close_type, CloseType.TRAILING_STOP)
        self.assertTrue(executor.is_closed)

    def test_trailing_stop_tracks_up(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(
                trailing_stop=TrailingStopConfig(
                    activation_price=0.10,
                    trailing_delta=0.05,
                ),
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(1.2)
        executor._check_trailing_stop()  # activates
        self.assertAlmostEqual(executor._trailing_stop_trigger, 0.15)
        executor.update_market_price(1.3)
        executor._check_trailing_stop()  # trails up
        executor.update_market_price(1.2)
        close_type = executor.control_barriers()
        self.assertEqual(close_type, CloseType.TRAILING_STOP)


class TestTimeLimit(unittest.TestCase):
    def test_time_limit_expired(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(time_limit=60),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        with patch('position_executor.time') as mock_time:
            mock_time.time.return_value = 1070.0
            close_type = executor.control_barriers()
            self.assertEqual(close_type, CloseType.TIME_LIMIT)
            self.assertTrue(executor.is_closed)

    def test_time_limit_not_expired(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(time_limit=60),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        with patch('position_executor.time') as mock_time:
            mock_time.time.return_value = 1050.0
            close_type = executor.control_barriers()
            self.assertIsNone(close_type)


class TestPartialExit(unittest.TestCase):
    def test_partial_exit_reduces_held_amount(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15, take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        self.assertAlmostEqual(executor.open_filled_amount, 1000.0)

        result = executor.partial_exit(
            token_amount=400.0,
            exit_price=1.2,
            gas_cost_eth=0.00001,
        )
        self.assertTrue(result)
        self.assertAlmostEqual(executor.open_filled_amount, 600.0)
        self.assertIsNotNone(executor._exit_order)

    def test_partial_exit_full_close(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15, take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)

        result = executor.partial_exit(
            token_amount=1000.0,
            exit_price=1.1,
            gas_cost_eth=0.00001,
        )
        self.assertTrue(result)
        self.assertAlmostEqual(executor.open_filled_amount, 0.0)
        self.assertTrue(executor.is_closed)

    def test_partial_exit_invalid_amount(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        self.assertFalse(executor.partial_exit(-100.0, 1.0, 0.00001))
        self.assertFalse(executor.partial_exit(1500.0, 1.0, 0.00001))


class TestFailedExitRetries(unittest.TestCase):
    def test_failed_exit_increments_retries(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)

        executor.record_exit("0xexit1", 1000.0, 1.0, 0.00001, CloseType.STOP_LOSS,
                             error="tx reverted")
        self.assertEqual(len(executor._failed_exits), 1)

    def test_max_retries_closes_as_failed(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            max_retries=3,
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)

        for i in range(3):
            executor.record_exit(f"0xexit{i}", 1000.0, 1.0, 0.00001,
                                 CloseType.STOP_LOSS, error=f"reverted attempt {i}")

        self.assertEqual(executor.close_type, CloseType.FAILED)
        self.assertTrue(executor.is_closed)


class TestVolatilityScaling(unittest.TestCase):
    def test_high_volatility_widens_barriers(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(
                stop_loss=0.15,
                take_profit=0.30,
                trailing_stop=TrailingStopConfig(activation_price=0.10, trailing_delta=0.05),
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        original_sl = config.triple_barrier.stop_loss

        executor.adjust_for_volatility(realized_vol=0.04, target_vol=0.02)
        self.assertGreater(config.triple_barrier.stop_loss, original_sl)
        self.assertAlmostEqual(config.triple_barrier.stop_loss, 0.30)
        self.assertAlmostEqual(config.triple_barrier.take_profit, 0.60)

    def test_low_volatility_no_change(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(
                stop_loss=0.15,
                take_profit=0.30,
            ),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        original_sl = config.triple_barrier.stop_loss

        executor.adjust_for_volatility(realized_vol=0.01, target_vol=0.02)
        self.assertAlmostEqual(config.triple_barrier.stop_loss, original_sl)


class TestStatusReport(unittest.TestCase):
    def test_status_report_contains_key_fields(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15, take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)

        report = executor.status_report()
        self.assertIn("token", report)
        self.assertIn("entry_price", report)
        self.assertIn("current_market_price", report)
        self.assertIn("gross_pnl_pct", report)
        self.assertIn("net_pnl_pct", report)
        self.assertIn("status", report)
        self.assertEqual(report["status"], "RUNNING")

    def test_format_status_closed(self):
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15, take_profit=0.30),
            timestamp=1000.0,
        )
        executor = PositionExecutor(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)
        executor.update_market_price(0.8)
        executor.control_barriers()

        formatted = executor.format_status()
        self.assertIn("CLOSED", formatted)
        self.assertIn("stop_loss", formatted)


class TestPositionManager(unittest.TestCase):
    def test_add_and_get_executor(self):
        manager = PositionManager()
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            timestamp=1000.0,
        )
        executor = manager.add(config)
        self.assertIs(manager.get("0xabc123"), executor)

    def test_remove_executor(self):
        manager = PositionManager()
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            timestamp=1000.0,
        )
        manager.add(config)
        removed = manager.remove("0xabc123")
        self.assertIsNotNone(removed)
        self.assertIsNone(manager.get("0xabc123"))

    def test_update_all_prices(self):
        manager = PositionManager()
        config1 = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST1",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            timestamp=1000.0,
        )
        config2 = PositionExecutorConfig(
            token="0xdef456",
            symbol="TEST2",
            side="BUY",
            amount_eth=0.001,
            entry_price=2.0,
            timestamp=1000.0,
        )
        e1 = manager.add(config1)
        e2 = manager.add(config2)
        e1.record_entry("0x1", 1000.0, 0.00001, 1.0)
        e2.record_entry("0x2", 500.0, 0.00001, 2.0)

        manager.update_all_prices({
            "0xabc123": 1.1,
            "0xdef456": 1.9,
        })
        self.assertAlmostEqual(e1.current_market_price, 1.1)
        self.assertAlmostEqual(e2.current_market_price, 1.9)

    def test_check_all_barriers(self):
        manager = PositionManager()
        config = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        executor = manager.add(config)
        executor.record_entry("0x123", 1000.0, 0.00001, 1.0)

        manager.update_all_prices({"0xabc123": 0.8})
        triggered = manager.check_all_barriers()
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0], executor)
        self.assertEqual(executor.close_type, CloseType.STOP_LOSS)

    def test_aggregate_pnl(self):
        manager = PositionManager()
        config1 = PositionExecutorConfig(
            token="0xabc123",
            symbol="TEST1",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        config2 = PositionExecutorConfig(
            token="0xdef456",
            symbol="TEST2",
            side="BUY",
            amount_eth=0.001,
            entry_price=1.0,
            triple_barrier=TripleBarrierConfig(stop_loss=0.15),
            timestamp=1000.0,
        )
        e1 = manager.add(config1)
        e2 = manager.add(config2)
        e1.record_entry("0x1", 1000.0, 0.00001, 1.0)
        e2.record_entry("0x2", 1000.0, 0.00001, 1.0)

        manager.update_all_prices({"0xabc123": 1.2, "0xdef456": 0.9})

        summary = manager.summary()
        self.assertEqual(summary["total_positions"], 2)
        self.assertEqual(summary["open"], 2)
        self.assertEqual(summary["closed"], 0)


class TestCloseTypeIntegrity(unittest.TestCase):
    def test_close_type_json_serialization(self):
        for ct in CloseType:
            json_str = json.dumps({"close_type": ct.value})
            parsed = json.loads(json_str)
            self.assertEqual(parsed["close_type"], ct.value)

    def test_close_type_from_value(self):
        for ct in CloseType:
            recovered = CloseType(ct.value)
            self.assertEqual(recovered, ct)


if __name__ == "__main__":
    unittest.main()
