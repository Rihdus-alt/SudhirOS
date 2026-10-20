import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

al = load("india-alerts", "alerts")
F = os.path.join(FIX, "india-alerts")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = al.main(argv)
    return code, out.getvalue(), err.getvalue()


class Clock(unittest.TestCase):
    def st(self, ist):
        return al.market_status(al.parse_now(ist))

    def test_dussehra_is_a_holiday(self):
        s = self.st("2026-10-20T10:00")
        self.assertFalse(s["open"])
        self.assertIn("Dussehra", s["reason"])
        self.assertEqual(s["next_open"], "2026-10-21 09:15")

    def test_phases(self):
        self.assertEqual(self.st("2026-10-21T09:05")["phase"], "pre-open")
        self.assertEqual(self.st("2026-10-21T09:10")["phase"], "pre-open matching")
        self.assertTrue(self.st("2026-10-21T09:15")["open"])
        self.assertTrue(self.st("2026-10-21T15:29")["open"])
        self.assertEqual(self.st("2026-10-21T15:45")["phase"], "post-close")
        self.assertFalse(self.st("2026-10-21T15:30")["open"])

    def test_utc_input_and_weekend(self):
        s = al.market_status(al.parse_now("2026-10-21T04:00:00Z"))   # 09:30 IST
        self.assertTrue(s["open"])
        self.assertEqual(self.st("2026-10-24T11:00")["reason"], "weekend")

    def test_all_fifteen_2026_holidays(self):
        self.assertEqual(len(al.NSE_HOLIDAYS), 15)
        for d in al.NSE_HOLIDAYS:
            import datetime
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_muhurat_times_unknown_until_set(self):
        s = self.st("2026-11-08T18:30")
        self.assertFalse(s["open"])
        self.assertIn("Muhurat", s["reason"])
        self.assertEqual(s["next_open"], "2026-11-09 09:15")   # Nov 10 is a holiday, Nov 9 is not
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "calendar.json")
            with open(p, "w") as fh:
                json.dump({"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}, fh)
            cal = al.load_calendar(p)
            s = al.market_status(al.parse_now("2026-11-08T18:30"), cal)
            self.assertTrue(s["open"])

    def test_unknown_year_is_flagged(self):
        self.assertFalse(self.st("2027-01-26T10:00")["calendar_known"])

    def test_last_close(self):
        lc = al.last_close_utc(al.parse_now("2026-10-20T10:00"))   # holiday; last close is Mon 19th 15:30 IST
        self.assertEqual(lc.isoformat(), "2026-10-19T10:00:00")


class Quotes(unittest.TestCase):
    def test_indmoney_import_is_real_data(self):
        with open(os.path.join(F, "indmoney-details-2026-10-19.json")) as fh:
            q = al.parse_indmoney(json.load(fh))
        self.assertEqual(q["HDFCBANK"]["price"], 712)
        self.assertEqual(q["HDFCBANK"]["upper_circuit"], 801.7)
        self.assertEqual(q["M&M"]["lower_circuit"], 2628.9)
        self.assertEqual(q["TCS"]["time"], "2026-10-19T10:29:39Z")
        with open(os.path.join(F, "indmoney-raw-result.json")) as fh:
            raw = al.parse_indmoney(json.load(fh))
        self.assertEqual(sorted(raw), ["HDFCBANK", "INFY", "RELIANCE"])

    def test_yahoo_and_nse_parsers(self):
        q, err = al.get_quotes(["RELIANCE", "M&M", "ZZZZ"], "yahoo", F, pause=0)
        self.assertEqual(q["RELIANCE"]["price"], 1202.0)
        self.assertEqual(q["M&M"]["prev_close"], 2920.9)
        self.assertTrue(any("ZZZZ" in e for e in err))
        n, _ = al.get_quotes(["XYZSMALL"], "nse", F, pause=0)
        self.assertEqual(n["XYZSMALL"]["series"], "BE")
        self.assertEqual(n["XYZSMALL"]["surveillance"], "ASM-LT Stage 1")
        self.assertEqual(n["XYZSMALL"]["upper_circuit"], 53.25)
        self.assertEqual(n["XYZSMALL"]["time"], "2026-10-19T10:30:00Z")

    def test_verify_flags_a_disagreement(self):
        y, _ = al.get_quotes(["RELIANCE", "TCS"], "yahoo", F, pause=0)
        n, _ = al.get_quotes(["RELIANCE", "TCS"], "nse", F, pause=0)
        rows = {r["symbol"]: r for r in al.verify(y, n)}
        self.assertTrue(rows["RELIANCE"]["agree"])
        self.assertFalse(rows["TCS"]["agree"])          # 2460.90 vs 2490: 1.18%

    def test_stale_quote(self):
        y, _ = al.get_quotes(["INFY", "RELIANCE"], "yahoo", F, pause=0)
        now = al.parse_now("2026-10-21T10:00")
        self.assertTrue(al.stale(y["INFY"], now))        # 9 October
        self.assertFalse(al.stale(y["RELIANCE"], now))   # the 19th's close

    def test_symbols(self):
        self.assertEqual(al.norm_symbol("nse:m&m"), "M&M")
        self.assertEqual(al.norm_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(al.yahoo_symbol("BSE:500325"), "500325.BO")
        self.assertIn("M%26M.NS", al.yahoo_url("M&M"))
        with self.assertRaises(al.AlertError):
            al.norm_symbol("not a symbol")


class Rules(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(F, "indmoney-details-2026-10-19.json")) as fh:
            self.q = al.parse_indmoney(json.load(fh))
        with open(os.path.join(os.path.dirname(F), "..", "..", "india-alerts", "alerts.example.json")) as fh:
            self.rules = json.load(fh)["rules"]

    def test_the_reel_after_the_close(self):
        # Axis Bank really fell 4.19% on 2026-10-19; nothing else in the example rules is near its level
        now = al.parse_now("2026-10-19T18:00")
        rings, _ = al.evaluate(self.rules, self.q, al.market_status(now), {}, now)
        self.assertEqual([r["id"] for r in rings], ["axisbank-pct-move-3"])
        self.assertEqual(rings[0]["message"], "AXISBANK moved -4.19% to \u20b91,236 (from \u20b91,290).")

    def test_stop_rings_once_and_bell_once_a_day(self):
        q = dict(self.q)
        q["HDFCBANK"] = dict(q["HDFCBANK"], price=679.5)
        now = al.parse_now("2026-10-21T09:20")
        st = al.market_status(now)
        rings, state = al.evaluate(self.rules, q, st, {}, now)
        ids = [r["id"] for r in rings]
        self.assertIn("hdfcbank-stop-680", ids)
        self.assertIn("opening-bell", ids)
        self.assertIn("₹679.50", [r for r in rings if r["id"] == "hdfcbank-stop-680"][0]["message"])
        rings2, _ = al.evaluate(self.rules, q, st, state, now)
        self.assertEqual(rings2, [])

    def test_near_circuit_and_pct_move(self):
        q = dict(self.q)
        q["TCS"] = dict(q["TCS"], price=2700.0)
        q["AXISBANK"] = dict(q["AXISBANK"], price=1240.0, prev_close=1290.0)
        now = al.parse_now("2026-10-21T11:00")
        rings, _ = al.evaluate(self.rules, q, al.market_status(now), {"rung": {"opening-bell": "2026-10-21T03:50:00Z"}}, now)
        ids = {r["id"] for r in rings}
        self.assertEqual(ids, {"tcs-near-circuit-2", "axisbank-pct-move-3"})

    def test_rearm(self):
        rule = [{"id": "dip", "symbol": "M&M", "type": "below", "price": 2800, "rearm": True}]
        now = al.parse_now("2026-10-21T11:00")
        st = al.market_status(now)
        low = {"M&M": dict(self.q["M&M"], price=2790.0)}
        high = {"M&M": dict(self.q["M&M"], price=2900.0)}
        r1, s = al.evaluate(rule, low, st, {}, now)
        r2, s = al.evaluate(rule, high, st, s, now)
        r3, s = al.evaluate(rule, low, st, s, now)
        self.assertEqual((len(r1), len(r2), len(r3)), (1, 0, 1))


class Cli(unittest.TestCase):
    def test_import_add_check(self):
        with tempfile.TemporaryDirectory() as d:
            prices = os.path.join(d, "prices.json")
            code, out, _ = run(["import", "indmoney", os.path.join(F, "indmoney-details-2026-10-19.json"),
                                "--write", prices, "--now", "2026-10-20T10:00"])
            self.assertEqual(code, 0)
            with open(prices) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["prices"]["MRF"], 121905)
            self.assertEqual(doc["detail"]["HDFCBANK"]["lower_circuit"], 656)
            self.assertIn("₹1,21,905", out)
            rules = os.path.join(d, "alerts.json")
            code, out, _ = run(["add", "--rules", rules, "--symbol", "hdfcbank", "--stop", "720"])
            self.assertIn("hdfcbank-stop-720", out)
            code, out, _ = run(["check", "--rules", rules, "--state", os.path.join(d, "s.json"), "--offline", d,
                                "--prices-in", prices, "--prices-out", os.path.join(d, "p2.json"),
                                "--now", "2026-10-21T10:00"])
            self.assertEqual(code, 0)
            self.assertIn("ALERT HDFCBANK", out)

    def test_clock_cli(self):
        code, out, _ = run(["clock", "--now", "2026-10-20T10:00", "--calendar", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("Dussehra", out)
        self.assertIn("AMO", out)


if __name__ == "__main__":
    unittest.main()
