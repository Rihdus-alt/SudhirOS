import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("india-budget", "size")
alerts = load("india-alerts", "alerts")

CFG = os.path.join(ROOT, "india-budget", "config.example.json")
TARGETS = os.path.join(ROOT, "india-budget", "targets.example.json")
REEL = {"HDFCBANK": 35, "ICICIBANK": 18, "M&M": 6, "AXISBANK": 11, "TCS": 5}


def prices():
    with open(os.path.join(FIX, "india-alerts", "indmoney-details-2026-10-19.json")) as fh:
        q = alerts.parse_indmoney(json.load(fh))
    return {s: v["price"] for s, v in q.items()}, {s: v for s, v in q.items()}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rows():
    with open(TARGETS) as fh:
        return size.parse_weights(json.load(fh))


def shares(r):
    return {p["ticker"]: p["shares"] for p in r["positions"]}


class TheReel(unittest.TestCase):
    def setUp(self):
        self.p, self.d = prices()

    def test_whole_shares_at_the_2026_10_19_closes(self):
        r = size.size(rows(), cfg(), prices=self.p, details=self.d)
        self.assertEqual(shares(r), REEL)
        self.assertEqual(r["invested"], 93120.5)
        self.assertEqual(r["cash_buffer"], 5000)
        self.assertEqual(r["cap"], 25000)
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["HDFCBANK", "ICICIBANK"])
        self.assertAlmostEqual(r["est_charges"], 111.44, places=2)
        self.assertAlmostEqual(r["tracking_error_pct"], 1.34, delta=0.015)  # 1.35 from the 2-decimal weights
        self.assertLessEqual(r["max_cash_needed"], r["investable"])

    def test_the_same_from_the_raw_fund_weights(self):
        w = size.parse_weights(size.weights_from_arg("HDFCBANK=7.8,ICICIBANK=5.2,M&M=3.9,AXISBANK=2.8,TCS=2.5"))
        self.assertEqual(shares(size.size(w, cfg(), prices=self.p, details=self.d)), REEL)

    def test_orders_use_broker_field_names(self):
        r = size.size(rows(), cfg(), prices=self.p, details=self.d)
        o = size.orders_from(r, cfg())
        self.assertEqual({k: v for k, v in o[0].items() if k != "why"},
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "HDFCBANK",
                          "transaction_type": "BUY", "quantity": 35, "product": "CNC", "order_type": "LIMIT",
                          "validity": "DAY", "price": 715.55})
        for x in o:
            self.assertIsInstance(x["quantity"], int)
            self.assertLessEqual(x["price"], self.d[x["tradingsymbol"]]["upper_circuit"])

    def test_fill_spends_more_but_stays_inside_the_money(self):
        r = size.size(rows(), cfg(), prices=self.p, details=self.d, fill=True)
        self.assertGreater(r["invested"], 93120.5)
        self.assertLessEqual(r["max_cash_needed"], r["investable"])


class WholeShareMaths(unittest.TestCase):
    def test_nearest_share_not_floor(self):
        # target Rs 1,900 of a Rs 1,000 stock: 2 shares (error 100) beats 1 share (error 900)
        w = size.parse_weights({"A": 50, "B": 50})
        r = size.size(w, {"budget": 4000, "cash_buffer_pct": 5, "max_position_pct": 100, "min_order_inr": 1,
                          "order_type": "MARKET", "market_slippage_pct": 0},
                      prices={"A": 1000, "B": 100})
        self.assertEqual(shares(r)["A"], 2)

    def test_expensive_share_is_dropped_with_the_reason(self):
        p, d = prices()
        w = size.parse_weights({"MRF": 5, "TCS": 5, "INFY": 5})
        r = size.size(w, cfg(budget=50000), prices=p, details=d)
        self.assertNotIn("MRF", shares(r))
        self.assertIn("1,21,905", r["dropped"][0]["reason"])

    def test_charges_make_a_tight_budget_fit(self):
        w = size.parse_weights({"A": 100})
        r = size.size(w, {"budget": 10000, "cash_buffer_pct": 0, "max_position_pct": 100, "min_order_inr": 1,
                          "order_type": "MARKET", "market_slippage_pct": 0}, prices={"A": 1000})
        self.assertEqual(shares(r)["A"], 9)   # 10 shares + charges would exceed Rs 10,000

    def test_limit_price_tick_and_band(self):
        self.assertEqual(size.limit_price(712, "buy", 0.5), 715.55)
        self.assertEqual(size.limit_price(1381, "buy", 0.5), 1387.9)       # 0.10 grid above Rs 1,000
        self.assertEqual(size.limit_price(121905, "buy", 0.5), 122510.0)  # Rs 5 grid above Rs 20,000
        self.assertEqual(size.limit_price(800, "buy", 0.5, {"upper_circuit": 801.7}), 801.7)
        self.assertEqual(size.limit_price(712, "sell", 0.5), 708.45)

    def test_blocked_duplicates_and_no_budget(self):
        p, d = prices()
        r = size.size(rows(), cfg(blocked=["tcs"]), prices=p, details=d)
        self.assertNotIn("TCS", shares(r))
        with self.assertRaises(size.SizeError):
            size.size([{"ticker": "TCS", "name": "", "weight": 1}] * 2, cfg(), prices=p)
        with self.assertRaises(size.SizeError):
            size.size(rows(), {"cash_buffer_pct": 5}, prices=p)

    def test_missing_prices_are_an_error(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(), cfg(), prices={})
        self.assertIn("india-alerts", str(e.exception))


class Cli(unittest.TestCase):
    def test_write_orders_file(self):
        with tempfile.TemporaryDirectory() as d:
            pr = os.path.join(d, "prices.json")
            p, det = prices()
            with open(pr, "w") as fh:
                json.dump({"as_of": "2026-10-19T10:30:00Z", "prices": p, "detail": det}, fh)
            out = os.path.join(d, "trader", "orders.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = size.main(["--targets", TARGETS, "--config", CFG, "--prices", pr, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("not financial advice", buf.getvalue())
            self.assertIn("Tracking error", buf.getvalue())
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual([o["tradingsymbol"] for o in doc["orders"]], list(REEL))


if __name__ == "__main__":
    unittest.main()
