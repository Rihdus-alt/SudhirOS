import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("india-broker", "ticket")
check = load("india-risk", "check")
size = load("india-budget", "size")
alerts = load("india-alerts", "alerts")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Yes(unittest.TestCase):
    order = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "HDFCBANK", "transaction_type": "BUY",
             "quantity": 35, "product": "CNC", "order_type": "LIMIT", "validity": "DAY", "price": 715.55}
    batch = ["HDFCBANK", "ICICIBANK", "M&M", "AXISBANK", "TCS"]

    def ok(self, reply):
        return ticket.parse_yes(reply, self.order, self.batch)[0]

    def test_explicit_yes(self):
        for r in ("yes", "Yes", "y", "haan", "Haan, 35 HDFCBANK", "ji haan", "yes 715.55", "yes, buy HDFCBANK"):
            self.assertTrue(self.ok(r), r)

    def test_not_a_yes(self):
        for r in ("ok", "sure", "go", "yes to all", "haan sab", "yes but wait", "yes?", "", "yes ICICIBANK",
                  "yes 40", "nahi", "yes, nahi"):
            self.assertFalse(self.ok(r), r)

    def test_plain_ticket(self):
        self.assertEqual(ticket.plain(self.order), "BUY 35 shares of HDFCBANK on NSE, delivery (CNC), limit "
                                                   "₹715.55, valid for today")


class PaperFlow(unittest.TestCase):
    def test_the_reel_end_to_end_on_paper(self):
        with tempfile.TemporaryDirectory() as d:
            t = lambda *p: os.path.join(d, "trader", *p)
            os.makedirs(t("state"))
            with open(os.path.join(ROOT, "india-budget", "config.example.json")) as fh:
                cfg = json.load(fh)
            with open(t("config.json"), "w") as fh:
                json.dump(cfg, fh)
            with open(os.path.join(FIX, "india-alerts", "indmoney-details-2026-10-19.json")) as fh:
                q = alerts.parse_indmoney(json.load(fh))
            with open(t("prices.json"), "w") as fh:
                json.dump({"prices": {s: v["price"] for s, v in q.items()}, "detail": q}, fh)
            code, _, err = run(size, ["--targets", os.path.join(ROOT, "india-budget", "targets.example.json"),
                                      "--config", t("config.json"), "--prices", t("prices.json"), "--write", t("orders.json")])
            self.assertEqual(code, 0, err)
            common = ["--orders", t("orders.json"), "--config", t("config.json"), "--state", t("state", "broker.json"),
                      "--today", "2026-10-21"]
            # not ready before the risk check
            code, out, _ = run(ticket, ["show"] + common + ["--risk", t("state", "risk.json")])
            self.assertEqual(code, 1)
            self.assertIn("NOT READY", out)
            code, out, _ = run(check, ["--orders", t("orders.json"), "--config", t("config.json"), "--prices",
                                       t("prices.json"), "--portfolio", t("portfolio.json"), "--state",
                                       t("state", "broker.json"), "--out", t("state", "risk.json"), "--today",
                                       "2026-10-21"])
            self.assertEqual(code, 0, out)
            common += ["--risk", t("state", "risk.json")]
            code, out, _ = run(ticket, ["show", "--index", "1"] + common)
            self.assertIn("READY", out)
            self.assertIn("PAPER", out)
            code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", "yes to all"] + common)
            self.assertEqual(code, 1)
            self.assertIn("Not recorded", out)
            code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", "haan, 35 HDFCBANK"] + common)
            self.assertEqual(code, 0)
            code, out, _ = run(ticket, ["paper-fill", "--index", "1", "--prices", t("prices.json"), "--portfolio",
                                        t("portfolio.json")] + common)
            self.assertEqual(code, 0, out)
            self.assertIn("₹75,050.36", out)
            with open(t("portfolio.json")) as fh:
                lot = json.load(fh)["positions"][0]["lots"][0]
            self.assertEqual(lot, {"id": "HDFCBANK-1", "date": "2026-10-21", "shares": 35, "cost": 24924.64,
                                   "price": 712.0, "charges": 29.64, "stt": 25.0})
            # a second yes for the same order is refused: it is DONE
            code, _, err = run(ticket, ["approve", "--index", "1", "--reply", "yes"] + common)
            self.assertEqual(code, 1)
            self.assertIn("DONE", err)
            code, out, _ = run(ticket, ["log", "--state", t("state", "broker.json"), "--today", "2026-10-21"])
            self.assertIn("1 placed today", out)

    def test_sell_fill_is_fifo_and_logged_for_tax(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "trader", "portfolio.json")
            os.makedirs(os.path.dirname(pf))
            with open(pf, "w") as fh:
                json.dump({"cash": 0, "positions": [{"ticker": "TCS", "shares": 15, "lots": [
                    {"id": "TCS-1", "date": "2025-01-10", "shares": 10, "cost": 30000},
                    {"id": "TCS-2", "date": "2026-05-10", "shares": 5, "cost": 12000}]}]}, fh)
            code, out, err = run(ticket, ["fill", "--symbol", "TCS", "--side", "sell", "--qty", "12", "--price", "2500",
                                          "--charges", "45.93", "--stt", "30", "--date", "2026-10-21",
                                          "--portfolio", pf])
            self.assertEqual(code, 0, err)
            with open(pf) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["positions"][0]["lots"], [{"id": "TCS-2", "date": "2026-05-10", "shares": 3.0,
                                                            "cost": 7200.0}])
            self.assertAlmostEqual(doc["cash"], 12 * 2500 - 45.93, places=2)
            with open(os.path.join(d, "trader", "state", "tax.json")) as fh:
                rec = json.load(fh)["realized"][0]
            self.assertEqual([u["id"] for u in rec["lots"]], ["TCS-1", "TCS-2"])
            self.assertEqual(rec["lots"][0]["term"], "long")
            self.assertEqual(rec["lots"][1]["term"], "short")


if __name__ == "__main__":
    unittest.main()
