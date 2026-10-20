import io
import unittest
from contextlib import redirect_stdout

from _load import load

costs = load("india-costs", "costs")


class Charges(unittest.TestCase):
    def test_delivery_buy_line_by_line(self):
        c = costs.trade_costs("buy", 24920)
        self.assertEqual(c["stt"], 25.0)            # 0.1% = 24.92, rounded to the rupee
        self.assertEqual(c["stamp"], 3.74)          # 0.015% on the buy
        self.assertEqual(c["exchange"], 0.74)       # NSE 0.00297%
        self.assertEqual(c["sebi"], 0.02)           # Rs 10 a crore
        self.assertEqual(c["gst"], 0.14)            # 18% of brokerage + exchange + SEBI
        self.assertEqual(c["dp"], 0.0)
        self.assertEqual(c["brokerage"], 0.0)
        self.assertAlmostEqual(c["total"], 29.64, places=2)

    def test_delivery_sell_has_dp_and_no_stamp(self):
        c = costs.trade_costs("sell", 26000)
        self.assertEqual(c["stamp"], 0.0)
        self.assertEqual(c["dp"], 15.93)
        self.assertEqual(c["stt"], 26.0)
        c2 = costs.trade_costs("sell", 26000, dp=False)
        self.assertEqual(c2["dp"], 0.0)

    def test_intraday_stt_is_sell_side_only(self):
        self.assertEqual(costs.trade_costs("buy", 100000, product="MIS")["stt"], 0.0)
        self.assertEqual(costs.trade_costs("sell", 100000, product="MIS")["stt"], 25.0)
        self.assertEqual(costs.trade_costs("buy", 100000, product="MIS")["brokerage"], 20.0)

    def test_bse_and_broker_overrides(self):
        r = costs.rates_from({"charges": {"brokerage_delivery": {"flat": 20, "pct": 0.1}, "dp_charge_per_scrip": 18.5}})
        c = costs.trade_costs("sell", 10000, "BSE", "CNC", r)
        self.assertEqual(c["brokerage"], 10.0)       # min(Rs 20, 0.1% of 10,000)
        self.assertEqual(c["dp"], 18.5)
        self.assertEqual(c["exchange"], 0.38)        # BSE 0.00375%
        self.assertEqual(costs.DEFAULT_RATES["dp_charge_per_scrip"], 15.93)  # defaults untouched

    def test_roundtrip_breakeven(self):
        rt = costs.roundtrip(24920, 26000)
        self.assertEqual(rt["gross"], 1080.0)
        self.assertAlmostEqual(rt["net"], 1080 - rt["costs"], places=2)
        self.assertGreater(rt["breakeven_move_pct"], 0.25)
        self.assertLess(rt["breakeven_move_pct"], 0.35)

    def test_indian_digit_grouping(self):
        self.assertEqual(costs.inr(100000), "₹1,00,000")
        self.assertEqual(costs.inr(121905), "₹1,21,905")
        self.assertEqual(costs.inr(12345678.5), "₹1,23,45,678.50")
        self.assertEqual(costs.inr(-950), "-₹950")

    def test_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(costs.main(["roundtrip", "24920", "--exit", "26000", "--config", "/nonexistent"]), 0)
        self.assertIn("Breakeven", out.getvalue())
        self.assertIn("contract note", out.getvalue())


if __name__ == "__main__":
    unittest.main()
