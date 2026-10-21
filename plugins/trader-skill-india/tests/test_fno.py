import datetime
import io
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

f = load("india-fno-plan", "fnocore")
scan = load("india-fno-scan", "scan")
plan = load("india-fno-plan", "plan")
chain_cli = load("india-fno-chain", "chain")
bt = load("india-fno-backtest", "backtest")
check = load("india-risk", "check")
ticket = load("india-broker", "ticket")
costs = load("india-costs", "costs")

F = os.path.join(FIX, "india-fno")
NIFTY = os.path.join(F, "NIFTY-1d.csv")
REL = os.path.join(F, "RELIANCE-1d.csv")
HDFC = os.path.join(F, "HDFCBANK-1d.csv")
CHAIN = os.path.join(F, "indmoney-chain-NIFTY-20261027.json")
CLOSE = datetime.datetime(2026, 10, 19, 10, 0)   # 15:30 IST, the close the chain was read at


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def bars(spec, start="2026-01-01"):
    d = datetime.date.fromisoformat(start)
    out = []
    for k, (o, h, l, c) in enumerate(spec):
        out.append({"date": (d + datetime.timedelta(days=k)).isoformat(), "open": o, "high": h, "low": l, "close": c,
                    "volume": 0})
    return out


class Indicators(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(f.ema([5.0] * 30, 10)[-1], 5.0)
        self.assertEqual(f.sma([1, 2, 3, 4], 2)[-1], 3.5)
        flat = bars([(100, 102, 98, 100)] * 40)
        self.assertAlmostEqual(f.atr(flat, 14)[-1], 4.0)
        self.assertEqual(f.rsi([float(x) for x in range(1, 40)], 14)[-1], 100.0)

    def test_real_candles_load_and_supertrend(self):
        c = f.load_candles(NIFTY)
        self.assertEqual((len(c), c[0]["date"], c[-1]["close"]), (246, "2025-10-21", 22888.95))
        line, d = f.supertrend(c)
        self.assertIn(d[-1], (1, -1))
        ind = f.indicators(c)
        self.assertAlmostEqual(ind["atr"][-1], 274.86, places=1)

    def test_indmoney_ohlc_json_shape(self):
        doc = {"result": json.dumps({"candles": [{"datetime_ist": "2026-01-%02d 05:30:00" % (i + 1), "open": 10 + i,
                                                  "high": 11 + i, "low": 9 + i, "close": 10.5 + i, "volume": 5}
                                                 for i in range(31)]})}
        c = f.load_candles(doc)
        self.assertEqual(c[0]["date"], "2026-01-01")
        with self.assertRaises(f.FnoError):
            f.load_candles({"candles": [{"datetime_ist": "2026-01-01", "open": 10, "high": 9, "low": 8, "close": 9}] * 31})


class Options(unittest.TestCase):
    def test_put_call_parity_and_iv_round_trip(self):
        F_, K, T, r, s = 22900.0, 23000.0, 8 / 365.0, 0.065, 0.13
        c, p = f.black76(F_, K, T, s, r, "CE"), f.black76(F_, K, T, s, r, "PE")
        self.assertAlmostEqual(c - p, math.exp(-r * T) * (F_ - K), places=6)
        self.assertAlmostEqual(f.implied_vol(c, F_, K, T, r, "CE"), s, places=5)
        self.assertIsNone(f.implied_vol(0.01, F_, 22000, T, r, "CE"))   # below intrinsic

    def test_matches_indmoney_on_the_real_chain(self):
        an = f.chain_analytics(f.parse_chain(CHAIN), CLOSE)
        self.assertAlmostEqual(an["forward"], 22897.5, places=1)
        T = f.years_to_expiry("20261027", CLOSE)
        self.assertAlmostEqual(T * 365, 8.0, places=6)
        iv = f.implied_vol(122.1, an["forward"], 23000, T, 0.065, "CE")
        self.assertLess(abs(100 * iv - 12.49), 0.1)               # INDmoney's VWAP IV for the contract
        g = f.greeks(an["forward"], 23000, T, iv, 0.065, "CE")
        self.assertLess(abs(g["delta"] - 0.41), 0.01)              # INDmoney 0.41
        self.assertLess(abs(g["gamma"] - 0.0009), 0.00005)         # 0.0009
        self.assertLess(abs(g["vega"] - 13.2), 0.2)                # 13.2
        self.assertLess(abs(g["theta"] + 10.07), 0.25)             # -10.07

    def test_chain_analytics_real(self):
        an = f.chain_analytics(f.parse_chain(CHAIN), CLOSE)
        self.assertEqual((an["max_pain"], an["resistance"], an["support"]), (23000.0, 23000.0, 22500.0))
        self.assertEqual(an["pcr_oi"], 0.928)
        self.assertAlmostEqual(an["expected_move_1sd"], 427.9, places=0)
        self.assertEqual(an["top_call_oi_added"][0][0], 23000.0)

    def test_nse_chain_format(self):
        doc = {"records": {"expiryDates": ["27-Oct-2026"], "underlyingValue": 22888.95, "data": [
            {"strikePrice": 22900, "expiryDate": "27-Oct-2026",
             "CE": {"openInterest": 1000, "changeinOpenInterest": 200, "lastPrice": 169.9, "bidprice": 168.6,
                    "askPrice": 169.85, "totalTradedVolume": 50, "impliedVolatility": 0},
             "PE": {"openInterest": 1500, "changeinOpenInterest": -100, "lastPrice": 172.5, "bidprice": 171,
                    "askPrice": 172.45, "totalTradedVolume": 70, "impliedVolatility": 12.6}}]}}
        ch = f.parse_chain(doc, "NIFTY")
        self.assertEqual(ch["expiry"], "20261027")
        self.assertAlmostEqual(ch["strikes"][0]["ce"]["oi_chg_pct"], 25.0)
        self.assertAlmostEqual(ch["strikes"][0]["pe"]["iv"], 0.126)


class Odds(unittest.TestCase):
    def test_no_edge_probability_is_risk_over_risk_plus_reward(self):
        self.assertAlmostEqual(f.no_edge_probability(100, 95, 105), 0.5)
        self.assertAlmostEqual(f.no_edge_probability(100, 95, 110), 1 / 3.0)

    def test_monte_carlo_agrees(self):
        rnd = random.Random(7)
        hits = 0
        n = 3000
        for _ in range(n):
            x = 100.0
            while 95.0 < x < 110.0:
                x *= math.exp(rnd.gauss(-0.5 * 0.01 ** 2, 0.01))   # a martingale price
            hits += x >= 110.0
        self.assertLess(abs(hits / float(n) - 1 / 3.0), 0.03)

    def test_wilson(self):
        lo, hi = f.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.237, places=3)
        self.assertAlmostEqual(hi, 0.763, places=3)


class Contracts(unittest.TestCase):
    def test_kite_symbols_and_monthly(self):
        lst = ["20261027", "20261103", "20261109", "20261117", "20261123", "20261229"]
        self.assertEqual(f.kite_symbol("NIFTY", "20261027", "FUT"), "NIFTY26OCTFUT")
        self.assertEqual(f.kite_symbol("NIFTY", "20261027", "CE", 23000, f.is_monthly("20261027", lst)), "NIFTY26OCT23000CE")
        self.assertEqual(f.kite_symbol("NIFTY", "20261103", "PE", 22900, f.is_monthly("20261103", lst)), "NIFTY26N0322900PE")
        self.assertTrue(f.is_monthly("20261123", lst))     # the 24th is a holiday: the monthly moved to Monday the 23rd
        self.assertFalse(f.is_monthly("20261117", lst))
        self.assertEqual(f.lot_size_for("NIFTY"), 65)
        with self.assertRaises(f.FnoError):
            f.lot_size_for("RELIANCE")

    def test_fno_charges(self):
        c = costs.fno_costs("sell", 1488110, "FUT")
        self.assertEqual(c["stt"], 744.0)                   # 0.05% sell side from April 2026
        self.assertEqual(costs.fno_costs("buy", 1488110, "FUT")["stt"], 0.0)
        self.assertEqual(costs.fno_costs("buy", 1488110, "FUT")["stamp"], 29.76)
        o = costs.fno_costs("sell", 41600, "OPT")
        self.assertEqual(o["stt"], 62.0)                    # 0.15% of the premium
        self.assertEqual(o["exchange"], 14.78)
        self.assertEqual(costs.fno_costs("buy", 100, "OPT", exercise_intrinsic=10000)["stt"], 15.0)


class Levels(unittest.TestCase):
    def test_real_nifty_short_is_flagged_by_the_oi_support(self):
        c = f.load_candles(NIFTY)
        ind = f.indicators(c)
        sig = f.signals_at(c, ind, len(c) - 1)
        self.assertEqual([(s["setup"], s["direction"]) for s in sig], [("trend_pullback", "short")])
        lv = f.levels(c, ind, len(c) - 1, "short", support=22500.0)
        self.assertEqual((lv["stop"], lv["tp1"], lv["tp2"]), (23301.25, 22476.65, 22064.36))
        self.assertFalse(lv["ok"])
        self.assertTrue(any("support 22500" in n for n in lv["notes"]))
        self.assertEqual(f.levels(c, ind, len(c) - 1, "short")["ok"], True)

    def test_structure_stop_when_in_range(self):
        c = f.load_candles(REL)
        ind = f.indicators(c)
        lv = f.levels(c, ind, len(c) - 1, "short")
        self.assertIn("swing high", lv["stop_basis"])
        self.assertAlmostEqual(lv["risk_points"] / lv["atr"], 23.66 / 24.29, places=2)

    def test_sizing_in_whole_lots(self):
        c = f.load_candles(NIFTY)
        ind = f.indicators(c)
        lv = f.levels(c, ind, len(c) - 1, "short")
        small = f.futures_sizing(lv, 65, 22888.95, {"capital": 500000})
        self.assertEqual(small["lots"], 0)
        self.assertIn("more than your", small["why_zero"])
        big = f.futures_sizing(lv, 65, 22888.95, {"capital": 3000000})
        self.assertEqual(big["lots"], 1)
        self.assertLessEqual(big["max_loss"], 30000)

    def test_option_plan_real(self):
        c = f.load_candles(NIFTY)
        ind = f.indicators(c)
        lv = f.levels(c, ind, len(c) - 1, "short")
        op = f.option_plan(lv, f.parse_chain(CHAIN), {"capital": 3000000}, CLOSE, None, "NIFTY")
        self.assertEqual((op["kind"], op["strike"], op["lot_size"]), ("PE", 22950.0, 65))
        self.assertLess(op["sl_premium"], op["entry_premium"])
        self.assertGreater(op["tp2_premium"], op["tp1_premium"])
        self.assertEqual(op["lots"], 2)
        self.assertLessEqual(op["max_loss_at_stop"], 30000)


class Trail(unittest.TestCase):
    base = {"direction": "long", "entry": 100.0, "stop": 95.0, "tp1": 105.0, "tp2": 110.0, "best": 100.0,
            "stage": "open", "bars_held": 0}

    def test_tp1_breakeven_trail_and_exit(self):
        b = bars([(100, 106, 99, 105), (105, 112, 104, 111), (111, 113, 108, 109), (109, 109, 100, 101)])
        p, ev = f.trail(self.base, b, 2.0)
        names = " | ".join(e["event"] for e in ev)
        self.assertIn("TP1", names)
        self.assertIn("TP2", names)
        self.assertTrue(p["closed"])
        self.assertGreater(p["exit"], 100.0)                 # stopped out above entry: the trail locked profit

    def test_stop_first_when_a_bar_touches_both(self):
        p, ev = f.trail(self.base, bars([(100, 106, 94, 100)]), 2.0)
        self.assertEqual((p["exit"], ev[0]["event"]), (95.0, "stop"))

    def test_gap_through_the_stop_fills_at_the_open(self):
        p, _ = f.trail(self.base, bars([(90, 92, 88, 91)]), 2.0)
        self.assertEqual(p["exit"], 90)

    def test_stop_never_loosens_and_time_stop(self):
        p, ev = f.trail(self.base, bars([(100, 101, 99, 100)] * 8), 2.0)
        self.assertTrue(p["closed"])
        self.assertIn("time stop", ev[-1]["event"])


class Backtest(unittest.TestCase):
    def test_no_look_ahead(self):
        c = f.load_candles(NIFTY)
        full = f.backtest(c)
        cut = f.backtest(c[:180])
        done = [t for t in full if t["exit_date"] < c[179]["date"]]
        self.assertEqual([t["entry_date"] for t in done], [t["entry_date"] for t in cut if not t["open_at_end"]][:len(done)])

    def test_real_results_regression(self):
        allt = []
        for path, lot in ((NIFTY, 65), (REL, 500), (HDFC, 550)):
            c = f.load_candles(path)
            px = c[-1]["close"]
            cp = (costs.fno_costs("buy", px * lot, "FUT")["total"] + costs.fno_costs("sell", px * lot, "FUT")["total"]) / lot
            allt += f.backtest(c, cost_points=cp)
        st = f.stats(allt)
        self.assertEqual(st["trades"], 49)
        self.assertAlmostEqual(st["expectancy_r"], -0.055, places=2)
        self.assertAlmostEqual(st["max_drawdown_r"], -8.24, delta=0.01)
        self.assertLess(st["win_rate"], st["breakeven_win_rate"])

    def test_r_is_planned_risk_and_tp1_books_its_real_price(self):
        c = f.load_candles(NIFTY)
        close = {b["date"]: b["close"] for b in c}
        trades = [t for t in f.backtest(c) if not t["open_at_end"]]
        checked = 0
        for t in trades:
            sgn = 1 if t["direction"] == "long" else -1
            risk = abs(close[t["signal_date"]] - t["stop"])
            if any(e.startswith("TP1") for e in t["events"]):
                want = 0.5 * sgn * (t["tp1"] - t["entry"]) / risk + 0.5 * sgn * (t["exit"] - t["entry"]) / risk
                checked += 1
            else:
                want = sgn * (t["exit"] - t["entry"]) / risk
            self.assertAlmostEqual(t["r"], want, delta=0.002, msg=t["entry_date"])
        self.assertGreater(checked, 0)

    def test_drawdown_does_not_depend_on_file_order(self):
        a = f.backtest(f.load_candles(NIFTY))
        b = f.backtest(f.load_candles(REL))
        self.assertEqual(f.stats(a + b)["max_drawdown_r"], f.stats(b + a)["max_drawdown_r"])

    def test_cli_split(self):
        code, out, _ = run(bt, [NIFTY, REL, HDFC, "--lots", "RELIANCE=500", "HDFCBANK=550", "--split", "0.3",
                                "--config", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("out-of-sample", out)
        self.assertIn("Verdict:", out)


class Cli(unittest.TestCase):
    def test_chain_cli(self):
        code, out, _ = run(chain_cli, [CHAIN, "--now", "2026-10-19T15:30", "--iv", "23000CE=122.1"])
        self.assertEqual(code, 0)
        self.assertIn("max pain 23000", out)
        self.assertIn("23000 CE at ₹122.10: IV 12.44%", out)

    def test_scan_cli(self):
        code, out, _ = run(scan, ["--candles", NIFTY, REL, HDFC, "--lots", "RELIANCE=500", "HDFCBANK=550", "--chain",
                                  "NIFTY=" + CHAIN, "--now", "2026-10-19T10:00:00", "--config", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("NIFTY SHORT (NOT WORTH IT)", out)
        self.assertIn("track record here", out)
        self.assertIn("91%", out)
        code, out, _ = run(scan, ["--candles", NIFTY, "--proven-only", "--config", "/nonexistent", "--no-history"])
        self.assertIn("No setup fired", out)

    def test_plan_risk_broker_journal_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            t = lambda *p: os.path.join(d, "trader", *p)
            os.makedirs(t("state"))
            cfg = {"budget": 3000000, "paper": True, "allow_fno": True, "fno": {"capital": 3000000}}
            with open(t("config.json"), "w") as fh:
                json.dump(cfg, fh)
            code, out, err = run(plan, ["new", "NIFTY", "--candles", NIFTY, "--direction", "short", "--chain", CHAIN,
                                        "--instrument", "option", "--now", "2026-10-19T15:30", "--config", t("config.json"),
                                        "--write", "--orders-out", t("orders.json"), "--plans", t("fno", "plans.json")])
            self.assertEqual(code, 1, err)                   # flagged not worth it: nothing written
            self.assertIn("not worth it", err)
            code, out, err = run(plan, ["new", "NIFTY", "--candles", NIFTY, "--direction", "short", "--chain", CHAIN,
                                        "--instrument", "option", "--now", "2026-10-19T15:30", "--config", t("config.json"),
                                        "--write", "--force", "--orders-out", t("orders.json"),
                                        "--plans", t("fno", "plans.json")])
            with open(t("orders.json")) as fh:
                order = json.load(fh)["orders"][0]
            self.assertEqual((order["tradingsymbol"], order["quantity"], order["product"]), ("NIFTY26OCT22950PE", 130, "NRML"))
            with open(t("prices.json"), "w") as fh:
                json.dump({"prices": {"NIFTY26OCT22950PE": 198.65}}, fh)
            common = ["--orders", t("orders.json"), "--config", t("config.json"), "--state", t("state", "broker.json"),
                      "--today", "2026-10-20"]
            code, out, _ = run(check, ["--orders", t("orders.json"), "--config", t("config.json"), "--prices",
                                       t("prices.json"), "--portfolio", t("portfolio.json"), "--state",
                                       t("state", "broker.json"), "--out", t("state", "risk.json"), "--today", "2026-10-20"])
            self.assertEqual(code, 0, out)
            common += ["--risk", t("state", "risk.json")]
            code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", "yes"] + common)
            self.assertEqual(code, 0)
            code, out, err = run(ticket, ["paper-fill", "--index", "1", "--prices", t("prices.json"), "--portfolio",
                                          t("portfolio.json")] + common)
            self.assertEqual(code, 0, err)
            self.assertIn("Now protect it", out)
            code, out, err = run(ticket, ["fill", "--symbol", "NIFTY26OCT22950PE", "--side", "SELL", "--qty", "130",
                                          "--price", "320", "--date", "2026-10-22", "--portfolio", t("portfolio.json"),
                                          "--config", t("config.json")])
            self.assertEqual(code, 0, err)
            with open(t("portfolio.json")) as fh:
                self.assertAlmostEqual(json.load(fh)["cash"], 3015637.17, places=2)
            code, out, _ = run(plan, ["journal", "--fy", "2026", "--journal", t("fno", "journal.json")])
            self.assertIn("₹15,775.50", out)
            self.assertIn("business income", out)

    def test_trail_cli_stops_at_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            early = os.path.join(d, "RELIANCE-early.csv")
            with open(REL) as fh, open(early, "w") as out:
                out.writelines(fh.readlines()[:220])
            plans = os.path.join(d, "plans.json")
            code, _, err = run(plan, ["new", "RELIANCE", "--candles", early, "--direction", "short", "--lot", "500",
                                      "--instrument", "future", "--fut-expiry", "20260929", "--capital", "3000000",
                                      "--config", "/nonexistent", "--write", "--orders-out", os.path.join(d, "o.json"),
                                      "--plans", plans])
            self.assertEqual(code, 0, err)
            code, out, _ = run(plan, ["trail", "RELIANCE-20260908-FUT", "--candles", REL, "--plans", plans,
                                      "--config", "/nonexistent"])
            self.assertIn("TP1", out)
            self.assertIn("EXPIRED", out)
            with open(plans) as fh:
                p = json.load(fh)["plans"][0]
            self.assertEqual((p["last_bar"], p["status"]), ("2026-09-29", "expired"))


class RiskFno(unittest.TestCase):
    cfg = {"budget": 1000000, "paper": True, "allow_fno": True, "fno": {"capital": 1000000}}

    def o(self, **kw):
        d = {"variety": "regular", "exchange": "NFO", "tradingsymbol": "NIFTY26OCT22950PE", "transaction_type": "BUY",
             "quantity": 65, "product": "NRML", "order_type": "LIMIT", "price": 199.25, "validity": "DAY",
             "lot_size": 65, "expiry": "20261027", "underlying": "NIFTY", "sl": 120.0, "levels_on": "premium"}
        d.update(kw)
        return d

    def one(self, order, cfg=None, today="2026-10-20", details=None):
        return check.check([order], cfg or self.cfg, None, {}, details or {}, None, today)["results"][0]

    def test_a_good_order_passes(self):
        r = self.one(self.o())
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_each_rule_blocks(self):
        cases = [
            (self.o(), dict(self.cfg, allow_fno=False), "product: futures and options are off"),
            (self.o(quantity=100), None, "lots: quantity 100"),
            (self.o(quantity=650), None, "lots: 10 lots"),
            (self.o(sl=None), None, "stop-loss: every F&O entry"),
            (self.o(sl=250), None, "wrong side"),
            (self.o(transaction_type="SELL"), None, "short option"),
            (self.o(), None, "expiry: today", ),
            (self.o(quantity=325, sl=50.0), None, "risk: the loss at the stop"),
            (self.o(tradingsymbol="NIFTY23000"), None, "asset:"),
        ]
        for order, cfg, want in cases:
            today = "2026-10-27" if want.startswith("expiry") else "2026-10-20"
            r = self.one(order, cfg, today)
            self.assertEqual(r["verdict"], "BLOCK", want)
            self.assertTrue(any(want in x for x in r["reasons"]), (want, r["reasons"]))

    def test_closing_sell_and_exec_band(self):
        self.assertEqual(self.one(self.o(transaction_type="SELL", closing=True, sl=None))["verdict"], "PASS")
        r = self.one(self.o(), details={"NIFTY26OCT22950PE": {"high_exec_band": 190.0, "low_exec_band": 120.0}})
        self.assertTrue(any(x.startswith("band") for x in r["reasons"]))

    def test_stock_future_delivery_warning(self):
        r = self.one(self.o(tradingsymbol="RELIANCE26OCTFUT", underlying="RELIANCE", lot_size=500, quantity=500,
                            price=1198.3, sl=1225.66, transaction_type="SELL", levels_on="underlying",
                            max_loss_inr=12217.6), today="2026-10-24")
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("DELIVERY" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
