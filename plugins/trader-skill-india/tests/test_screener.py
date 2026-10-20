import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load, ROOT, FIX

sc = load("india-screener", "screen")
F = os.path.join(FIX, "india-screener")
RULES = os.path.join(ROOT, "india-screener", "rules.example.json")


class Numbers(unittest.TestCase):
    def test_indian_formats(self):
        n = sc.to_number
        self.assertEqual(n("₹1,23,456.7"), 123456.7)
        self.assertEqual(n("4,512 Cr"), 45120000000.0)
        self.assertEqual(n("35 lakh"), 3500000.0)
        self.assertEqual(n("12.5%"), 12.5)
        self.assertEqual(n("(12.5 Cr)"), -125000000.0)
        self.assertIsNone(n("--"))
        self.assertEqual(n("Alpha"), "Alpha")

    def test_crore_columns_stay_in_crores(self):
        rows = {r["ticker"]: r for r in sc.load_metrics(os.path.join(F, "screener-export.csv"))}
        self.assertEqual(rows["ALPHAPIPE"]["market_cap_cr"], 4512.0)
        self.assertEqual(rows["ECHOSOFT"]["market_cap_cr"], 102350.0)
        self.assertEqual(rows["ECHOSOFT"]["net_profit_cr"], -12.5)
        self.assertEqual(rows["CHARLIEINF"]["pledged_pct"], 12.5)
        self.assertEqual(rows["BRAVOFOOD"]["sales_growth_3y"], 11.5)


class Screen(unittest.TestCase):
    def test_the_example_screen(self):
        rows = sc.load_metrics(os.path.join(F, "screener-export.csv"))
        sc.add_prices(rows, os.path.join(F, "prices.json"))
        res = sc.screen(rows, sc.load_rules(RULES))
        passed = [r["ticker"] for r in res if r["passed"]]
        self.assertEqual(sorted(passed), ["ALPHAPIPE", "BRAVOFOOD"])
        fails = {r["ticker"]: r["fails"] for r in res if not r["passed"]}
        self.assertEqual(fails["ECHOSOFT"], ["pe 48.1, needs <= 40"])
        self.assertIn("pledged_pct 12.5, needs <= 5", fails["CHARLIEINF"])

    def test_cli_writes_the_shortlist(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "shortlist.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = sc.main([os.path.join(F, "screener-export.csv"), "--rules", RULES, "--prices",
                                os.path.join(F, "prices.json"), "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("not a recommendation", buf.getvalue())
            with open(out) as fh:
                self.assertEqual(len(json.load(fh)["passed"]), 2)


if __name__ == "__main__":
    unittest.main()
