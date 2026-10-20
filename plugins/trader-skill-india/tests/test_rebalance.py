import json
import os
import unittest

from _load import load, ROOT, FIX

rb = load("india-rebalance", "rebalance")
alerts = load("india-alerts", "alerts")


class Rebalance(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "india-alerts", "indmoney-details-2026-10-19.json")) as fh:
            q = alerts.parse_indmoney(json.load(fh))
        self.p = {s: v["price"] for s, v in q.items()}
        self.d = q
        with open(os.path.join(ROOT, "india-budget", "config.example.json")) as fh:
            self.cfg = json.load(fh)
        with open(os.path.join(ROOT, "india-budget", "targets.example.json")) as fh:
            self.w = rb.parse_targets(json.load(fh))
        self.pf = {"cash": 6769.14, "positions": [{"ticker": "HDFCBANK", "shares": 35}, {"ticker": "ICICIBANK", "shares": 18},
                                                  {"ticker": "M&M", "shares": 6}, {"ticker": "AXISBANK", "shares": 11},
                                                  {"ticker": "TCS", "shares": 5}]}

    def test_the_reel_sits_on_target(self):
        r = rb.plan(self.pf, self.p, self.w, self.cfg, details=self.d)
        self.assertEqual(r["orders"], [])
        self.assertTrue(all(abs(x["drift"]) < 2 for x in r["rows"]))

    def test_new_cash_buys_the_laggards_in_whole_shares(self):
        pf = dict(self.pf, cash=self.pf["cash"] + 50000)
        r = rb.plan(pf, self.p, self.w, self.cfg, details=self.d)
        got = {o["tradingsymbol"]: o["quantity"] for o in r["orders"]}
        self.assertEqual(got, {"HDFCBANK": 17, "ICICIBANK": 9, "M&M": 4})
        self.assertTrue(all(o["product"] == "CNC" and isinstance(o["quantity"], int) for o in r["orders"]))
        self.assertGreater(r["cash_after"], r["cash_buffer"])

    def test_sells_first_and_costly_trades_skipped(self):
        pf = {"cash": 0, "positions": [{"ticker": "HDFCBANK", "shares": 200}, {"ticker": "TCS", "shares": 1}]}
        r = rb.plan(pf, self.p, {"HDFCBANK": 50, "TCS": 50}, dict(self.cfg, max_order_inr=0), details=self.d)
        self.assertEqual(r["orders"][0]["transaction_type"], "SELL")
        self.assertEqual(r["orders"][0]["tradingsymbol"], "HDFCBANK")
        tiny = rb.plan({"cash": 100000, "positions": [{"ticker": "HDFCBANK", "shares": 2}]}, self.p,
                       {"HDFCBANK": 1, "TCS": 99}, dict(self.cfg, min_order_inr=0, max_cost_pct=0.1), details=self.d)
        self.assertTrue(any("charges" in n for n in tiny["notes"]))


if __name__ == "__main__":
    unittest.main()
