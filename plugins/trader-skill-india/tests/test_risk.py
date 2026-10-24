import json
import os
import unittest

from _load import load, ROOT, FIX

check = load("india-risk", "check")
size = load("india-budget", "size")
alerts = load("india-alerts", "alerts")
CFG = os.path.join(ROOT, "india-budget", "config.example.json")


def setup():
    with open(os.path.join(FIX, "india-alerts", "indmoney-details-2026-10-19.json")) as fh:
        q = alerts.parse_indmoney(json.load(fh))
    with open(CFG) as fh:
        cfg = json.load(fh)
    with open(os.path.join(ROOT, "india-budget", "targets.example.json")) as fh:
        rows = size.parse_weights(json.load(fh))
    p = {s: v["price"] for s, v in q.items()}
    r = size.size(rows, cfg, prices=p, details=q)
    return size.orders_from(r, cfg), cfg, p, q


def o(sym, side="BUY", qty=1, typ="LIMIT", price=None, product="CNC", **kw):
    d = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": side, "quantity": qty,
         "product": product, "order_type": typ, "validity": "DAY"}
    if price is not None:
        d["price"] = price
    d.update(kw)
    return d


class TheReel(unittest.TestCase):
    def test_five_pass_on_a_fresh_paper_account(self):
        orders, cfg, p, q = setup()
        res = check.check(orders, cfg, None, p, q, today="2026-10-21")
        self.assertEqual((res["pass"], res["block"]), (5, 0))
        self.assertEqual(res["cash_after"], 6304.01)
        self.assertEqual(res["results"][0]["est_charges"], 29.67)

    def test_a_sixth_hdfc_order_is_blocked_four_ways(self):
        orders, cfg, p, q = setup()
        res = check.check(orders + [o("HDFCBANK", qty=3, price=820)], cfg, None, p, q, today="2026-10-21")
        r = res["results"][-1]
        self.assertEqual(r["verdict"], "BLOCK")
        kinds = sorted({x.split(":")[0] for x in r["reasons"]})
        self.assertEqual(kinds, ["band", "cash", "duplicate", "position"])

    def test_fingerprint_matches_the_broker(self):
        orders, cfg, p, q = setup()
        ticket = load("india-broker", "ticket")
        self.assertEqual(check.fingerprint(orders[0]), ticket.fingerprint(orders[0]))
        self.assertNotEqual(check.fingerprint(orders[0]), check.fingerprint(dict(orders[0], quantity=36)))


class Rules(unittest.TestCase):
    def setUp(self):
        _, self.cfg, self.p, self.q = setup()

    def one(self, order, cfg=None, portfolio=None, details=None):
        res = check.check([order], cfg or self.cfg, portfolio, self.p, details if details is not None else self.q,
                          today="2026-10-21")
        return res["results"][0]

    def test_a_buy_paid_by_an_earlier_sell_waits_for_that_sell(self):
        pf = {"cash": 1000, "positions": [{"ticker": "TCS", "shares": 10}]}
        tcs = self.p["TCS"]
        orders = [o("TCS", "SELL", 5, price=tcs), o("HDFCBANK", qty=3, price=self.p["HDFCBANK"])]
        res = check.check(orders, self.cfg, pf, self.p, self.q, today="2026-10-21")
        sell, buy = res["results"]
        self.assertEqual((sell["verdict"], buy["verdict"]), ("PASS", "PASS"), res)
        self.assertEqual(buy["needs_sells"], [1])
        ticket = load("india-broker", "ticket")
        ok, why, _ = ticket.status_of(orders[1], res, {"orders": []}, "2026-10-21")
        self.assertFalse(ok)
        self.assertIn("SELL order 1", why)
        placed = {"orders": [{"fingerprint": sell["fingerprint"], "date": "2026-10-21", "status": "placed"}]}
        self.assertTrue(ticket.status_of(orders[1], res, placed, "2026-10-21")[0])

    def test_buying_more_of_an_unpriced_holding_is_blocked(self):
        pf = {"cash": 100000, "positions": [{"ticker": "ZZZZ", "shares": 10,
                                             "lots": [{"date": "2026-01-01", "shares": 10, "cost": 5000}]}]}
        r = self.one(o("ZZZZ", qty=1, price=500), portfolio=pf, details={})
        self.assertTrue(any(x.startswith("price: no last price for ZZZZ") for x in r["reasons"]), r["reasons"])

    def test_fractional_quantity(self):
        r = self.one(o("TCS", qty=1.5, price=2470))
        self.assertTrue(any("whole number" in x for x in r["reasons"]))

    def test_fno_intraday_and_mtf_are_off(self):
        self.assertTrue(any(x.startswith("asset") for x in self.one(o("NIFTY26OCT25000CE", qty=75, price=100,
                                                                        product="NRML"))["reasons"]))
        self.assertTrue(any(x.startswith("product: MIS") for x in self.one(o("TCS", price=2470, product="MIS"))["reasons"]))
        self.assertTrue(any(x.startswith("product: MTF") for x in self.one(o("TCS", price=2470, product="MTF"))["reasons"]))

    def test_band_and_paise(self):
        self.assertTrue(any(x.startswith("band") for x in self.one(o("TCS", price=2800))["reasons"]))
        self.assertTrue(any("paise" in x for x in self.one(o("TCS", price=2470.123))["reasons"]))
        r = self.one(o("TCS", price=2470.12))
        self.assertTrue(any("5 paise" in w for w in r["warnings"]))
        self.assertEqual(r["verdict"], "PASS")

    def test_surveillance_and_t2t(self):
        det = dict(self.q)
        det["XYZSMALL"] = {"series": "BE", "surveillance": "ASM-LT Stage 1", "upper_circuit": 53.25, "lower_circuit": 48.25}
        p = dict(self.p, XYZSMALL=48.25)
        res = check.check([o("XYZSMALL", qty=20, price=48.5)], self.cfg, None, p, det, today="2026-10-21")
        self.assertTrue(any(x.startswith("surveillance") for x in res["results"][0]["reasons"]))
        cfg = dict(self.cfg, block_surveillance=False)
        res = check.check([o("XYZSMALL", qty=20, price=48.5)], cfg, None, p, det, today="2026-10-21")
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertTrue(any("trade-to-trade" in w for w in res["results"][0]["warnings"]))

    def test_cannot_sell_what_you_do_not_hold(self):
        pf = {"cash": 1000, "positions": [{"ticker": "TCS", "shares": 2}]}
        self.assertTrue(any(x.startswith("short") for x in self.one(o("TCS", "SELL", 3, price=2450), portfolio=pf)["reasons"]))
        self.assertEqual(self.one(o("TCS", "SELL", 2, price=2450), portfolio=pf)["verdict"], "PASS")

    def test_same_day_buy_and_sell_warning(self):
        pf = {"cash": 100000, "positions": [{"ticker": "TCS", "shares": 5}]}
        res = check.check([o("TCS", "SELL", 2, price=2450), o("TCS", "BUY", 2, price=2470)], self.cfg, pf, self.p,
                          self.q, today="2026-10-21")
        self.assertTrue(any("intraday" in w for w in res["results"][1]["warnings"]))

    def test_daily_limit_and_already_placed(self):
        state = {"orders": [{"status": "placed", "date": "2026-10-21", "symbol": "TCS", "side": "BUY"}]}
        res = check.check([o("TCS", price=2470)], dict(self.cfg, max_orders_per_day=1), None, self.p, self.q,
                          state, "2026-10-21")
        reasons = res["results"][0]["reasons"]
        self.assertTrue(any(x.startswith("duplicate") for x in reasons))
        self.assertTrue(any(x.startswith("daily limit") for x in reasons))

    def test_live_and_stale_warnings(self):
        res = check.check([o("TCS", price=2470)], dict(self.cfg, paper=False), None, self.p, self.q,
                          today="2026-10-21", prices_age_hours=50)
        self.assertTrue(any("LIVE" in w for w in res["warnings"]))
        self.assertTrue(any("hours old" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
