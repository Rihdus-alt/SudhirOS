import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import _xlsx
from _load import load, FIX

cc = load("india-copycat", "copycat")
F = os.path.join(FIX, "india-copycat")
EQ = os.path.join(F, "nse-equity-list.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cc.main(argv)
    return code, out.getvalue(), err.getvalue()


def maps():
    return cc.load_symbols(None, EQ)


class Isin(unittest.TestCase):
    def test_check_digits(self):
        for good in ("INE002A01018", "INE467B01029", "INE040A01034", "INE101A01026", "US0231351067"):
            self.assertTrue(cc.isin_valid(good), good)
        self.assertFalse(cc.isin_valid("INE002A01019"))

    def test_kinds(self):
        self.assertEqual(cc.isin_kind("INE040A01034")[0], "equity")
        self.assertEqual(cc.isin_kind("US0231351067")[0], "foreign")
        self.assertEqual(cc.isin_kind("INE001X07AB7")[0], "other")
        self.assertEqual(cc.isin_kind("INE002A01019")[0], "invalid")


class Fund(unittest.TestCase):
    def amc(self, name="example-flexicap-2026-09.csv"):
        sheet, rows = cc.read_table(os.path.join(F, name))
        return cc.parse_amc(rows, sheet)

    def test_amc_monthly_portfolio(self):
        port = self.amc()
        self.assertEqual(port["title"], "Example Flexi Cap Fund")
        self.assertEqual(port["as_of"], "2026-09-30")
        rows, skipped = cc.equity_rows(port, maps())
        self.assertEqual([r["symbol"] for r in rows], ["HDFCBANK", "ICICIBANK", "M&M", "TCS", "AXISBANK", "INFY"])
        hdfc = rows[0]
        self.assertEqual(hdfc["quantity"], 15872922)
        self.assertAlmostEqual(hdfc["value_inr"], 113014.48 * 1e5, places=0)   # Rs lakhs -> rupees
        reasons = " ".join(s["reason"] for s in skipped)
        self.assertIn("foreign", reasons)
        self.assertIn("not an equity share", reasons)
        self.assertIn("0% of net assets", reasons)

    def test_same_file_as_xlsx_with_two_schemes(self):
        with open(os.path.join(F, "example-flexicap-2026-09.csv"), newline="") as fh:
            rows = [r for r in csv.reader(fh)]
        with tempfile.TemporaryDirectory() as d:
            x = os.path.join(d, "amc.xlsx")
            _xlsx.write(x, {"Index": [["Scheme list"]], "EXFLEXI": rows, "EXLIQUID": [["Example Liquid Fund"],
                                                                                    ["Name", "ISIN", "% to NAV"]]})
            with self.assertRaises(cc.CopyError):
                cc.read_table(x)                       # two sheets mention ISIN: ask which
            sheet, xr = cc.read_table(x, "Flexi Cap")    # matched by the title inside the sheet
            self.assertEqual(sheet, "EXFLEXI")
            port = cc.parse_amc(xr, sheet)
            self.assertEqual(len(cc.equity_rows(port, maps())[0]), 6)

    def test_diff_month_on_month(self):
        old = cc.equity_rows(self.amc("example-flexicap-2026-08.csv"), maps())[0]
        new = cc.equity_rows(self.amc(), maps())[0]
        d = cc.diff(old, new)
        self.assertEqual([r["symbol"] for r in d["new"]], ["INFY"])
        self.assertEqual([r["symbol"] for r in d["exited"]], ["ITC"])
        self.assertEqual([r["symbol"] for r in d["added"]], ["ICICIBANK"])
        self.assertEqual([r["symbol"] for r in d["trimmed"]], ["AXISBANK"])

    def test_indmoney_real_holdings_the_reel(self):
        with open(os.path.join(F, "indmoney-ppfas-flexicap.json")) as fh:
            port = cc.parse_indmoney_mf(json.load(fh))
        rows, skipped = cc.equity_rows(port, maps())
        w, _ = cc.targets(rows, top=5)
        self.assertEqual([(x["ticker"], x["weight"]) for x in w],
                         [("HDFCBANK", 35.1351), ("ICICIBANK", 23.4234), ("M&M", 17.5676), ("AXISBANK", 12.6126),
                          ("TCS", 11.2613)])
        amazon = [s for s in skipped if s["name"] == "Amazon.com Inc"][0]
        self.assertIn("foreign", amazon["reason"])

    def test_copy_refuses_when_a_big_holding_has_no_symbol(self):
        code, _, err = run(["copy", os.path.join(F, "indmoney-ppfas-flexicap.json"), "--equity-list", EQ,
                            "--symbols-map", "/nonexistent", "--top", "8", "--out", os.devnull])
        self.assertEqual(code, 1)
        self.assertIn("Zydus", err)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "targets.json")
            code, text, err = run(["copy", os.path.join(F, "indmoney-ppfas-flexicap.json"), "--equity-list", EQ,
                                   "--symbols-map", "/nonexistent", "--top", "5", "--out", out])
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(sum(x["weight"] for x in doc["weights"]), 100.0)
            self.assertIn("INDmoney", doc["source"])
            self.assertTrue(any("0.1%" in c for c in doc["caveats"]))

    def test_symbols_map_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "symbols.json")
            with open(m, "w") as fh:
                json.dump({"Zydus Lifesciences Ltd": "ZYDUSLIFE"}, fh)
            by_isin, by_name, _ = cc.load_symbols(m, EQ)
            self.assertEqual(cc.resolve({"name": "Zydus Lifesciences Ltd"}, (by_isin, by_name, {}))[0], "ZYDUSLIFE")


class Investor(unittest.TestCase):
    def test_quarter_on_quarter(self):
        rows = cc.load_investor_rows(os.path.join(F, "investor-holdings.csv"))
        p = cc.investor_portfolio(rows, "example investor", {"ALPHAPIPE": 1250, "BRAVOFOOD": 1890, "DELTAPHAR": 1420})
        self.assertEqual(p["period"], "2026-09-30")
        self.assertEqual([h["symbol"] for h in p["holdings"]], ["ALPHAPIPE", "DELTAPHAR", "BRAVOFOOD"])
        self.assertAlmostEqual(sum(h["pct"] for h in p["holdings"]), 100.0)
        self.assertEqual(p["changes"], {"new": ["DELTAPHAR"], "exited_or_below_1pct": ["CHARLIEINF"],
                                        "added": ["ALPHAPIPE"], "trimmed": []})

    def test_shareholding_pattern_xbrl(self):
        with open(os.path.join(F, "shp-example.xml")) as fh:
            res = cc.parse_shp_xbrl(fh.read())
        self.assertEqual(res["symbol"], "ALPHAPIPE")
        named = {h["name"]: h for h in res["holders"]}
        self.assertEqual(named["Example Investor A"]["shares"], 3000000)
        self.assertAlmostEqual(named["Example Investor A"]["pct"], 2.22)


if __name__ == "__main__":
    unittest.main()
