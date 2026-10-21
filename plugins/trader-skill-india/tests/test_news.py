import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load, FIX

news = load("india-news", "news")
F = os.path.join(FIX, "india-news")


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = news.main(argv)
    return code, out.getvalue()


class Tags(unittest.TestCase):
    def test_red_flags(self):
        self.assertEqual(news.tags_for("Resignation of Statutory Auditors")[1], True)
        self.assertEqual(news.tags_for("Disclosure of default on payment of interest")[1], True)
        self.assertEqual(news.tags_for("Credit rating downgraded to BBB")[1], True)
        self.assertEqual(news.tags_for("Outcome of Board Meeting - financial results")[1], False)
        self.assertIn("payout", news.tags_for("Record date for bonus issue")[0])


class Feeds(unittest.TestCase):
    def test_merged_filings_and_relevant_headlines(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["--symbols", "ALPHAPIPE", "--bse", "500000", "--offline", F, "--now", "2026-10-19T06:00:00Z",
                    "--hours", "96", "--state", os.path.join(d, "news.json"), "--prices", "/nonexistent", "--json"]
            code, out = run(argv)
            self.assertEqual(code, 0)
            items = json.loads(out)["items"]
            titles = [i["title"] for i in items]
            self.assertEqual(titles.count("Outcome of Board Meeting"), 1)         # NSE + BSE merged
            merged = [i for i in items if i["title"] == "Outcome of Board Meeting"][0]
            self.assertEqual({merged["source"], merged["also_on"]}, {"NSE", "BSE"})
            self.assertIn("Alpha Pipes shares slide after auditor quits - Example Business Daily", titles)
            self.assertNotIn("Monsoon update lifts fertiliser stocks", titles)    # does not name the company
            self.assertNotIn("Analysts/Institutional Investor Meet/Con. Call Updates", titles)   # older than 96 h
            red = sorted(i["title"] for i in items if i["important"])
            self.assertEqual(red, ["Credit Rating", "Resignation of Statutory Auditors"])
            code, out = run(argv)                                                  # shown once
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
