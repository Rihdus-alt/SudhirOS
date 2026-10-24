import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

rs = load("india-analyst", "results")
F = os.path.join(FIX, "india-analyst")
FILES = [os.path.join(F, "ALPHAPIPE-2026-09-30.xml"), os.path.join(F, "ALPHAPIPE-2026-03-31.xml")]


def quarters(paths=FILES):
    out = []
    for p in paths:
        with open(p) as fh:
            out += rs.quarters_from(rs.parse_xbrl(fh.read()))
    return out


class Results(unittest.TestCase):
    def test_xbrl_periods_and_segments(self):
        with open(FILES[0]) as fh:
            parsed = rs.parse_xbrl(fh.read())
        q = {x["end"]: x for x in rs.quarters_from(parsed)}
        self.assertEqual(sorted(q), ["2025-09-30", "2026-06-30", "2026-09-30"])   # the 6-month YTD is not a quarter
        self.assertEqual(q["2026-09-30"]["revenue"], 12000000000)                 # the segment row is skipped
        self.assertEqual(q["2026-09-30"]["basis"], "consolidated")

    def test_growth_ebitda_ttm_and_flags(self):
        res = rs.analyse(quarters())
        by = {q["end"]: q for q in res["quarters"]}
        sep = by["2026-09-30"]
        self.assertAlmostEqual(sep["revenue_yoy"], 20.0)
        self.assertAlmostEqual(sep["revenue_qoq"], 100.0 * (1200 / 1100 - 1))
        self.assertEqual(sep["ebitda"], 230 * 1e7)                 # 205 + 15 + 40 - 30
        self.assertIsNone(by["2025-09-30"]["revenue_qoq"])        # no June 2025 quarter: no QoQ, not a wrong one
        self.assertTrue(any("exceptional" in f for f in by["2026-03-31"]["flags"]))
        self.assertAlmostEqual(res["ttm"]["revenue"], 4500 * 1e7)
        self.assertAlmostEqual(res["ttm"]["eps_diluted"], 24.92)

    def test_metrics_row_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "metrics.json")
            out = io.StringIO()
            with redirect_stdout(out):
                code = rs.main(["read"] + FILES + [os.path.join(F, "ALPHAPIPE-2026-09-30-standalone.xml"),
                                                   "--metrics", m])
            self.assertEqual(code, 0)
            self.assertNotIn("WARNING", out.getvalue())           # standalone dropped when consolidated exists
            with open(m) as fh:
                row = json.load(fh)["companies"][0]
            self.assertEqual(row["ticker"], "ALPHAPIPE")
            self.assertEqual(row["revenue_growth_yoy"], 20.0)
            self.assertEqual(row["profit_growth_yoy"], 27.1)
            self.assertEqual(row["eps_ttm"], 24.92)

    def test_fetch_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with redirect_stdout(out):
                code = rs.main(["fetch", "ALPHAPIPE", "--offline", F, "--dir", d])
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(d)), ["ALPHAPIPE-30Sep2026.xml", "ALPHAPIPE-31Mar2026.xml"])

    def test_csv_input_in_crores(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "q.csv")
            with open(p, "w") as fh:
                fh.write("period_end,unit,revenue,net_profit,eps_diluted\n2025-09-30,crore,100,10,1\n2026-09-30,crore,125,9,0.9\n")
            res = rs.analyse(rs.load_csv(p))
            self.assertAlmostEqual(res["quarters"][-1]["revenue_yoy"], 25.0)
            self.assertAlmostEqual(res["quarters"][-1]["net_margin"], 7.2)

    def test_csv_with_bracketed_losses_dashes_and_indian_dates(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "q.csv")
            with open(p, "w") as fh:
                fh.write("period_end,unit,revenue,exceptional,net_profit\n30-09-2025,crore,100,-,10\n"
                         "30-Sep-2026,crore,\"1,25\",-,(20)\n")
            qs = rs.load_csv(p)
            self.assertEqual([q["end"] for q in qs], ["2025-09-30", "2026-09-30"])
            self.assertEqual(qs[-1]["net_profit"], -20 * 1e7)
            self.assertNotIn("exceptional", qs[0])
            with open(p, "a") as fh:
                fh.write("Q3,crore,1,1,1\n")
            with self.assertRaises(rs.ResultsError):
                rs.load_csv(p)

    def test_metrics_row_needs_a_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            p, m = os.path.join(d, "q.csv"), os.path.join(d, "metrics.json")
            with open(p, "w") as fh:
                fh.write("period_end,unit,revenue,net_profit\n2025-09-30,crore,100,10\n2026-09-30,crore,125,9\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = rs.main(["read", p, "--metrics", m])
            self.assertEqual(code, 1)
            self.assertIn("--symbol", err.getvalue())
            self.assertFalse(os.path.exists(m))


if __name__ == "__main__":
    unittest.main()
