import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

lots = load("india-tax", "lots")
D = datetime.date
EXAMPLE = os.path.join(ROOT, "india-tax", "portfolio.example.json")


def pf(*rows):
    out = {"cash": 0, "positions": []}
    for t, ls in rows:
        out["positions"].append({"ticker": t, "shares": sum(l["shares"] for l in ls), "lots": ls})
    return out


class TheLaw(unittest.TestCase):
    def test_rates_change_on_23_july_2024(self):
        self.assertEqual(lots.rates_on(D(2024, 7, 22)), (15.0, 10.0))
        self.assertEqual(lots.rates_on(D(2024, 7, 23)), (20.0, 12.5))

    def test_exemption_by_financial_year(self):
        self.assertEqual(lots.ltcg_exemption(D(2023, 4, 1)), 100000.0)
        self.assertEqual(lots.ltcg_exemption(D(2024, 4, 1)), 125000.0)
        self.assertEqual(lots.fy_label(D(2026, 3, 31)), "FY 2025-26")
        self.assertEqual(lots.fy_label(D(2026, 4, 1)), "FY 2026-27")

    def test_more_than_twelve_months(self):
        self.assertEqual(lots.term(D(2025, 10, 21), D(2026, 10, 21)), "short")
        self.assertEqual(lots.term(D(2025, 10, 21), D(2026, 10, 22)), "long")
        self.assertEqual(lots.long_term_from(D(2024, 2, 29)), D(2025, 3, 1))

    def test_grandfathering(self):
        g = lots.grandfathered_cost
        self.assertEqual(g(26500, 50, 625, 61800, D(2017, 11, 20)), 31250)   # FMV beats cost
        self.assertEqual(g(26500, 50, 625, 30000, D(2017, 11, 20)), 30000)   # capped at the sale value: no loss
        self.assertEqual(g(26500, 50, 625, 20000, D(2017, 11, 20)), 26500)   # a real loss stays a loss
        self.assertEqual(g(26500, 50, 625, 61800, D(2018, 2, 1)), 26500)     # bought on/after 1 Feb 2018


class Year(unittest.TestCase):
    fy = D(2026, 4, 1)

    def test_set_off_exemption_and_tax(self):
        r = [{"date": "2026-05-01", "short": 50000, "long": 200000}, {"date": "2026-06-01", "short": -10000, "long": -30000}]
        s = lots.fy_summary(r, self.fy)
        self.assertEqual(s["taxable"], {"LTCG @ 12.5%": 45000.0, "STCG @ 20%": 40000.0})
        self.assertEqual(s["est_tax"], 13625.0)
        self.assertEqual(lots.fy_summary(r, self.fy, cess=True)["est_tax"], 14170.0)
        self.assertEqual(s["exemption_left"], 0.0)

    def test_short_term_loss_spills_into_ltcg(self):
        s = lots.fy_summary([{"date": "2026-05-01", "short": 10000, "long": 200000},
                             {"date": "2026-06-01", "short": -50000, "long": 0}], self.fy)
        self.assertEqual(s["est_tax"], 4375.0)

    def test_long_term_loss_never_offsets_stcg(self):
        s = lots.fy_summary([{"date": "2026-05-01", "short": 50000, "long": -80000}], self.fy)
        self.assertEqual(s["est_tax"], 10000.0)
        self.assertEqual(s["carry_forward"]["ltcl"], 80000.0)

    def test_brought_forward_losses(self):
        s = lots.fy_summary([{"date": "2026-05-01", "short": 50000, "long": 0}], self.fy, {"stcl": 20000})
        self.assertEqual(s["est_tax"], 6000.0)

    def test_fy_2024_25_mixed_rates_exemption_on_the_higher_rate_first(self):
        s = lots.fy_summary([{"date": "2024-06-01", "short": 0, "long": 150000},
                             {"date": "2024-09-01", "short": 0, "long": 100000}], D(2024, 4, 1))
        self.assertEqual(s["taxable"], {"LTCG @ 10%": 125000.0})
        self.assertEqual(s["est_tax"], 12500.0)


class Sales(unittest.TestCase):
    def test_fifo_only_and_terms(self):
        p = pf(("TCS", [{"id": "TCS-1", "date": "2025-01-10", "shares": 10, "cost": 30000},
                        {"id": "TCS-2", "date": "2026-05-10", "shares": 5, "cost": 12000}]))
        sale = lots.simulate_sale(lots.lots_of(p)["TCS"], 12, 2500, D(2026, 10, 21))
        self.assertEqual([(u["id"], u["shares"], u["term"]) for u in sale["lots"]],
                         [("TCS-1", 10, "long"), ("TCS-2", 2, "short")])
        self.assertEqual(sale["long"], -5000.0)
        self.assertEqual(sale["short"], 200.0)

    def test_the_reel_grandfathered_exemption_harvest(self):
        with open(EXAMPLE) as fh:
            p = json.load(fh)
        sale = lots.simulate_sale(lots.lots_of(p)["AXISBANK"], 50, 1236, D(2026, 10, 21), 18.17)
        self.assertEqual(sale["cost"], 31250.0)
        self.assertEqual(sale["long"], 30531.83)
        extra, fy = lots.tax_on_sale(sale, [], D(2026, 4, 1))
        self.assertEqual(extra, 0.0)
        self.assertEqual(fy["exemption_left"], 94468.17)
        h = lots.harvest(p, {"AXISBANK": 1236, "HDFCBANK": 712}, D(2026, 10, 21), [])
        self.assertEqual([(g["ticker"], g["shares"]) for g in h["gains"]], [("AXISBANK", 50)])
        orders = lots.harvest_orders(h)
        self.assertTrue(all(isinstance(o["quantity"], int) for o in orders))

    def test_loss_harvest_saves_tax_against_booked_gains(self):
        p = pf(("ICICIBANK", [{"id": "I-1", "date": "2026-06-01", "shares": 100, "cost": 150000}]))
        h = lots.harvest(p, {"ICICIBANK": 1381}, D(2026, 10, 21), [{"date": "2026-05-01", "short": 30000, "long": 0}])
        c = h["losses"][0]
        self.assertLess(c["loss"], -11000)
        self.assertLess(c["tax_change_this_fy"], -2000)   # about 20% of the loss

    def test_harvest_orders_fit_the_max_order_at_their_limit(self):
        p = pf(("ICICIBANK", [{"id": "I-1", "date": "2026-06-01", "shares": 100, "cost": 150000}]))
        h = lots.harvest(p, {"ICICIBANK": 1381}, D(2026, 10, 21), [{"date": "2026-05-01", "short": 30000, "long": 0}])
        orders = lots.harvest_orders(h, True, {"max_order_inr": 50000, "limit_buffer_pct": 0.5}, p, D(2026, 10, 21))
        o = orders[0]
        self.assertEqual(o["order_type"], "LIMIT")
        self.assertLess(o["price"], 1381)
        self.assertLessEqual(o["quantity"] * o["price"], 50000)
        self.assertEqual(o["quantity"], int(50000 // o["price"]))
        self.assertTrue(any("cut from 100" in n for n in lots.harvest_orders.notes))

    def test_bonus_split_and_bonus_stripping(self):
        p = pf(("HDFCBANK", [{"id": "H-1", "date": "2025-06-10", "shares": 10, "cost": 19000}]))
        self.assertEqual(lots.apply_bonus(p, "HDFCBANK", "1:1", D(2025, 8, 27)), 10)
        ls = lots.lots_of(p)["HDFCBANK"]
        self.assertEqual([(l["kind"], l["cost"], l["date"].isoformat()) for l in ls],
                         [("buy", 19000.0, "2025-06-10"), ("bonus", 0.0, "2025-08-27")])
        sale = lots.simulate_sale(ls, 10, 1000, D(2025, 12, 1))
        self.assertTrue(lots.bonus_strip_warning(ls, sale["lots"], D(2025, 12, 1)))
        q = pf(("TCS", [{"id": "T-1", "date": "2017-06-01", "shares": 10, "cost": 20000, "fmv_2018_01_31": 3000}]))
        lots.apply_split(q, "TCS", "1:5", D(2026, 1, 15))
        l = q["positions"][0]["lots"][0]
        self.assertEqual((l["shares"], l["cost"], l["fmv_2018_01_31"]), (50.0, 20000, 600.0))
        r = pf(("ITC", [{"id": "X-1", "date": "2024-01-01", "shares": 10, "cost": 4000}]))
        self.assertEqual(lots.apply_bonus(r, "ITC", "1:2", D(2024, 6, 1)), 5)   # 1 new for every 2 held

    def test_ex_date_entitlement_and_allotment_date(self):
        p = pf(("ITC", [{"id": "X-1", "date": "2024-05-01", "shares": 10, "cost": 4000},
                        {"id": "X-2", "date": "2024-06-01", "shares": 10, "cost": 4100}]))   # bought ON the ex-date
        self.assertEqual(lots.apply_bonus(p, "ITC", "1:1", D(2024, 6, 1), D(2024, 6, 3)), 10)
        self.assertEqual(p["positions"][0]["lots"][-1]["date"], "2024-06-03")
        q = pf(("TCS", [{"id": "T-1", "date": "2026-01-14", "shares": 10, "cost": 20000},
                        {"id": "T-2", "date": "2026-01-15", "shares": 5, "cost": 2000}]))
        lots.apply_split(q, "TCS", "1:5", D(2026, 1, 15))
        self.assertEqual([l["shares"] for l in q["positions"][0]["lots"]], [50.0, 5])

    def test_a_sale_cuts_lots_by_position_even_with_duplicate_ids(self):
        p = pf(("INFY", [{"id": "INFY-2", "date": "2026-02-05", "shares": 10, "cost": 10000},
                         {"id": "INFY-2", "date": "2026-04-06", "shares": 10, "cost": 12000}]))
        sale = lots.simulate_sale(lots.lots_of(p)["INFY"], 5, 1200, D(2026, 5, 5))
        p, _ = lots.record_sale(p, "INFY", sale, 1200, {"realized": []})
        left = [(l["date"], l["shares"], l["cost"]) for l in p["positions"][0]["lots"]]
        self.assertEqual(left, [("2026-02-05", 5.0, 5000.0), ("2026-04-06", 10, 12000)])

    def test_cli_sell_record_and_fy(self):
        with tempfile.TemporaryDirectory() as d:
            p, s = os.path.join(d, "portfolio.json"), os.path.join(d, "tax.json")
            with open(EXAMPLE) as fh, open(p, "w") as out:
                out.write(fh.read())
            o = io.StringIO()
            with redirect_stdout(o), redirect_stderr(io.StringIO()):
                code = lots.main(["sell", "AXISBANK", "50", "--price", "1236", "--portfolio", p, "--state", s,
                                  "--prices", "/nonexistent", "--today", "2026-10-21", "--record"])
            self.assertEqual(code, 0)
            self.assertIn("grandfathered", o.getvalue())
            self.assertIn("Not tax advice", o.getvalue())
            with open(p) as fh:
                ax = [x for x in json.load(fh)["positions"] if x["ticker"] == "AXISBANK"][0]
            self.assertEqual([l["id"] for l in ax["lots"]], ["AXISBANK-2"])
            o = io.StringIO()
            with redirect_stdout(o):
                lots.main(["fy", "--state", s, "--portfolio", p, "--prices", "/nonexistent", "--today", "2026-10-22"])
            self.assertIn("FY 2026-27", o.getvalue())
            self.assertIn("₹94,468.17", o.getvalue())

    def test_whole_shares_and_missing_lots(self):
        with self.assertRaises(lots.TaxError):
            lots.lots_of({"positions": [{"ticker": "TCS", "shares": 5}]})


if __name__ == "__main__":
    unittest.main()
