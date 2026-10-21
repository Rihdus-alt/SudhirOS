import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

ins = load("india-insider", "insider")
F = os.path.join(FIX, "india-insider")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(argv)
    return code, out.getvalue(), err.getvalue()


def pit():
    with open(os.path.join(F, "pit.json")) as fh:
        return ins.parse_pit(fh.read())


class Pit(unittest.TestCase):
    def test_parse_and_kinds(self):
        rows = {r["person"]: r for r in pit()}
        self.assertEqual(rows["Example Promoter One"]["kind"], "market buy")
        self.assertEqual(rows["Example Director"]["kind"], "market sell")
        self.assertEqual(rows["Example Promoter Three"]["kind"], "pledge created")
        self.assertEqual(rows["Example Employee"]["kind"], "esop")
        self.assertEqual(rows["Example Promoter Four"]["kind"], "off-market")
        one = rows["Example Promoter One"]
        self.assertEqual((one["shares"], one["value_inr"], one["avg_price"]), (50000, 62500000, 1250.0))
        self.assertEqual((one["traded_to"], one["posted"], one["delay_days"]), ("2026-10-07", "2026-10-09", 2))
        self.assertEqual(rows["Example Director"]["delay_days"], 15)

    def test_filters_and_clusters(self):
        rows = pit()
        buys = ins.filter_pit(rows, promoters=True, buys=True, min_value=1e7)
        self.assertEqual(sorted(r["person"] for r in buys), ["Example Promoter One", "Example Promoter Two"])
        cl = ins.clusters(rows)
        self.assertEqual([(c["symbol"], len(c["people"])) for c in cl], [("ALPHAPIPE", 2)])
        self.assertEqual(cl[0]["value_inr"], 87600000)

    def test_cli_late_flag_and_lakh_crore(self):
        code, out, _ = run(["pit", "--offline", F, "--from", "2026-10-01", "--to", "2026-10-19"])
        self.assertEqual(code, 0)
        self.assertIn("late?", out)
        self.assertIn("30,00,000 shares", out)
        self.assertIn("₹5.00 lakh", out)
        self.assertIn("not a forecast", out)


class Deals(unittest.TestCase):
    def test_bulk_csv_and_round_trips(self):
        with open(os.path.join(F, "bulk.csv")) as fh:
            rows = ins.round_trips(ins.parse_deals_csv(fh.read(), "bulk"))
        self.assertEqual(len(rows), 4)
        llp = [r for r in rows if r["client"] == "EXAMPLE TRADING LLP"]
        self.assertTrue(all(r["round_trip"] for r in llp))
        inv = [r for r in rows if r["client"] == "EXAMPLE INVESTOR A"][0]
        self.assertFalse(inv["round_trip"])
        self.assertEqual(inv["value_inr"], 873845000.0)
        self.assertTrue(ins.match_client("EXAMPLE INVESTOR A", ["example investor a"]))
        self.assertFalse(ins.match_client("EXAMPLE INVESTOR B", ["example investor a"]))

    def test_new_alerts_once(self):
        with tempfile.TemporaryDirectory() as d:
            inv = os.path.join(d, "investors.json")
            with open(inv, "w") as fh:
                json.dump({"names": ["example investor a"]}, fh)
            argv = ["new", "--offline", F, "--to", "2026-10-19", "--investors", inv, "--state", os.path.join(d, "s.json"),
                    "--portfolio", "/nonexistent"]
            code, out, _ = run(argv)
            self.assertEqual(code, 0)
            self.assertEqual(out.count("NEW "), 3)    # two promoter buys over Rs 1 crore, one investor bulk deal
            code, out, _ = run(argv)
            self.assertEqual(code, 1)
            self.assertIn("Nothing new", out)


if __name__ == "__main__":
    unittest.main()
