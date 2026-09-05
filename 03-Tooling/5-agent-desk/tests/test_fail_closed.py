import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import context
import devil
import pulse


class ExternalDataFailClosed(unittest.TestCase):
    @patch.object(context, "log_trade")
    @patch.object(context, "_fetch_dexscreener_pair", return_value=None)
    def test_missing_exact_token_pair_vetoes_context(self, fetch_pair, log):
        result = context.evaluate({"token": "0x" + "1" * 40, "symbol": "X"})
        self.assertTrue(result["context_veto"])
        self.assertFalse(result["context_data_quality_ok"])

    @patch.object(pulse, "save_pulse_cache")
    @patch.object(pulse, "load_pulse_cache", return_value={})
    @patch.object(pulse, "_get_gas_price_rh", return_value=None)
    @patch.object(pulse, "_get_market_dominance", return_value=None)
    @patch.object(pulse, "_get_funding_rates", return_value=None)
    @patch.object(pulse, "_get_btc_price_from_binance", return_value={})
    @patch.object(pulse, "_get_btc_price_change", return_value={"price": 0, "change_1h_pct": 0, "change_24h_pct": 0})
    def test_missing_macro_and_chain_data_forces_standdown(self, *mocks):
        result = pulse.score_market()
        self.assertEqual(result["go_signal"], 0.0)
        self.assertFalse(result["data_quality_ok"])

    @patch.object(devil, "_check_holder_concentration", return_value={"top_holder_pct": None, "holders": 0, "warning": True, "error": "unavailable"})
    @patch.object(devil, "_check_honeypot_goplus", return_value={"honeypot": None, "flags": ["check_error=unavailable"]})
    def test_unknown_security_state_is_a_hard_veto(self, security, holders):
        score, reasons, warnings = devil._adversarial_checks(
            {"token": "0x" + "1" * 40, "wallet_class": "sniper", "amount_usd": 10},
            {"copy_score": 1}, {"narrative_score": 1, "context_info": {}},
            {"pulse_go_signal": 1},
        )
        self.assertEqual(score, 0.0)
        self.assertTrue(any("SECURITY_DATA_UNAVAILABLE" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
