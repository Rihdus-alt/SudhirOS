import io
import json
import math
import os
import random
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("india-fno-timemachine", "timemachine")
f = tm.f
F = os.path.join(FIX, "india-fno")
NIFTY = os.path.join(F, "NIFTY-1d.csv")
REL = os.path.join(F, "RELIANCE-1d.csv")
CHAIN = os.path.join(F, "indmoney-chain-NIFTY-20261027.json")
CLOSE = f.parse_now("2026-10-19T15:30")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def random_bars(rng, n, start=1000.0, atr=12.0):
    out, pc = [], start
    for _ in range(n):
        o = pc + rng.gauss(0, 0.3) * atr
        cl = o + rng.gauss(0, 0.8) * atr
        h = max(o, cl) + abs(rng.gauss(0, 0.4)) * atr
        l = min(o, cl) - abs(rng.gauss(0, 0.4)) * atr
        out.append((o, h, l, cl, atr * (0.8 + 0.4 * rng.random())))
        pc = cl
    return out


class Engine(unittest.TestCase):
    def test_exit_engine_matches_the_planner_trail_step_for_step(self):
        """run_trade must end every path exactly where fnocore.trail (the planner and the backtest) ends it."""
        k = f.cfg_with({})
        rng = random.Random(3)
        checked = 0
        for n in range(400):
            bars = random_bars(rng, 30)
            long_ = n % 2 == 0
            sgn = 1 if long_ else -1
            e = 1000.0
            risk = 15.0 + rng.random() * 10
            t = {"direction": "long" if long_ else "short", "stop": e - sgn * risk, "tp1": e + sgn * risk,
                 "tp2": e + sgn * 2 * risk, "risk": risk, "frac": 0.5}
            res = tm.run_trade(bars, t, k, 30)
            o0 = bars[0][0]
            if (o0 <= t["stop"] or o0 >= t["tp1"]) if long_ else (o0 >= t["stop"] or o0 <= t["tp1"]):
                self.assertEqual(res["cls"], "gap")
                continue
            pos = {"direction": t["direction"], "entry": o0, "stop": t["stop"], "tp1": t["tp1"], "tp2": t["tp2"],
                   "best": o0, "stage": "open", "bars_held": 0}
            exit_bar = None
            for j, b in enumerate(bars):
                bar = {"date": "d%d" % j, "open": b[0], "high": b[1], "low": b[2], "close": b[3]}
                pos, _ = f.trail(pos, [bar], b[4], k)
                if pos.get("closed"):
                    exit_bar = j
                    break
            if exit_bar is None:
                self.assertEqual(res["cls"], "horizon")
                continue
            self.assertEqual(res["exit_bar"], exit_bar)
            self.assertAlmostEqual(res["exit"], pos["exit"], places=2)
            checked += 1
        self.assertGreater(checked, 250)

    def test_outcomes_partition_every_path(self):
        c = f.load_candles(NIFTY)
        rep = tm.analyse(c, {}, chain=f.parse_chain(CHAIN), now_utc=CLOSE, symbol="NIFTY", paths=2000, lots=2)
        for m, s in rep["methods"].items():
            total = sum(o["count"] for o in s["outcomes"].values())
            self.assertEqual(total, s["n"], m)
            self.assertAlmostEqual(sum(o["p"] for o in s["outcomes"].values()), 1.0, places=3)

    def test_same_seed_same_answer(self):
        c = f.load_candles(NIFTY)
        a = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=1500, methods=("bootstrap",))
        b = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=1500, methods=("bootstrap",))
        self.assertEqual(a["methods"]["bootstrap"], b["methods"]["bootstrap"])


class Models(unittest.TestCase):
    def test_risk_neutral_odds_match_the_closed_form(self):
        """Under the implied (martingale) paths, P(TP1 before SL) is risk / (risk + reward): 50% at 1R."""
        k = f.cfg_with({"time_stop_bars": 10 ** 6})
        rng = random.Random(11)
        e, risk = 20000.0, 400.0
        t = {"direction": "long", "stop": e - risk, "tp1": e + risk, "tp2": e + 2 * risk, "risk": risk, "frac": 0.0}
        hits, n = 0, 4000
        for _ in range(n):
            bars = tm.implied_path(rng, e, 250.0, 0.18, 0.0, 400, k)
            res = tm.run_trade(bars, t, k)
            if res["entered"] and res["tp1_bar"] is not None:
                hits += 1
        p = hits / float(n)
        se = math.sqrt(0.25 / n)
        # the stop-first rule on a bar that touches both levels can only lower it, and only a little
        self.assertLess(abs(p - 0.5), 4 * se + 0.02, p)

    def test_a_martingale_has_no_expectancy_before_costs(self):
        k = f.cfg_with({})
        rng = random.Random(5)
        e, risk = 20000.0, 400.0
        t = {"direction": "short", "stop": e + risk, "tp1": e - risk, "tp2": e - 2 * risk, "risk": risk, "frac": 0.5}
        rs = []
        for _ in range(6000):
            res = tm.run_trade(tm.implied_path(rng, e, 250.0, 0.15, 0.2, 40, k), t, k)
            if res["entered"]:
                rs.append(res["r"])
        mu = sum(rs) / len(rs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rs) / (len(rs) - 1))
        self.assertLess(abs(mu), 4 * sd / math.sqrt(len(rs)), mu)

    def test_bootstrap_library_is_drift_free_by_default(self):
        c = f.load_candles(NIFTY)
        ind = f.indicators(c)
        lib = tm.bar_library(c, ind)
        self.assertAlmostEqual(sum(x[3] for x in lib) / len(lib), 0.0, places=9)
        kept = tm.bar_library(c, ind, keep_drift=True)
        self.assertNotAlmostEqual(sum(x[3] for x in kept) / len(kept), 0.0, places=4)
        for g, h, l, cl in lib:
            self.assertGreaterEqual(h + 1e-9, max(g, cl))
            self.assertLessEqual(l - 1e-9, min(g, cl))

    def test_neutral_bootstrap_has_no_price_drift(self):
        c = f.load_candles(NIFTY)
        k = f.cfg_with({})
        ind = f.indicators(c, k)
        a0, c0 = ind["atr"][-1], c[-1]["close"]
        lib = tm.neutralise(tm.bar_library(c, ind), c0, a0, 40, k, 5.0, 1)
        rng = random.Random(9)
        moves = [tm.bootstrap_path(rng, lib, c0, a0, 40, k)[-1][3] - c0 for _ in range(4000)]
        mu = sum(moves) / len(moves)
        sd = math.sqrt(sum((x - mu) ** 2 for x in moves) / (len(moves) - 1))
        self.assertLess(abs(mu), 3.5 * sd / math.sqrt(len(moves)) + 0.02 * a0, (mu, a0))

    def test_history_uncertainty_is_in_the_bootstrap_verdict(self):
        c = f.load_candles(NIFTY)
        rep = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=2000, lots=2, methods=("bootstrap",))
        s = rep["methods"]["bootstrap"]
        self.assertGreater(s["history_se"], s["expectancy_se"])
        self.assertTrue(any("one year of history" in v for v in tm.verdict(rep)))

    def test_replay_verdict_needs_more_than_noise(self):
        rep = {"methods": {"replay": {"entered": 60, "independent": 20, "expectancy_r": 0.012, "expectancy_se": 0.1},
                           "baseline": {"entered": 300, "independent": 40, "expectancy_r": 0.002, "expectancy_se": 0.05}}}
        self.assertIn("no difference this history can show", " ".join(tm.verdict(rep)))
        rep["methods"]["replay"].update(expectancy_r=0.9, expectancy_se=0.05)
        self.assertIn("added something", " ".join(tm.verdict(rep)))

    def test_a_session_opening_past_tp1_fills_it_at_the_open(self):
        k = f.cfg_with({})
        t = {"direction": "long", "stop": 950.0, "tp1": 1050.0, "tp2": 1200.0, "risk": 50.0, "frac": 0.5}
        res = tm.run_trade([(1000, 1010, 995, 1005, 10), (1070, 1080, 1060, 1075, 10)] + [(1075, 1076, 1074, 1075, 10)] * 2,
                           t, k, 4)
        self.assertEqual(res["tp1_px"], 1070)
        pos = {"direction": "long", "entry": 1000, "stop": 950.0, "tp1": 1050.0, "tp2": 1200.0, "best": 1000,
               "stage": "open", "bars_held": 0}
        pos, ev = f.trail(pos, [{"date": "a", "open": 1070, "high": 1080, "low": 1060, "close": 1075}], 10, k)
        self.assertEqual(pos["tp1_fill"], 1070)

    def test_overnight_share_is_measured(self):
        phi = tm.overnight_share(f.load_candles(NIFTY))
        self.assertTrue(0.0 < phi < 0.6, phi)


class History(unittest.TestCase):
    def test_replay_never_looks_ahead(self):
        """A past occurrence that finished before bar m reads the same with or without the bars after m."""
        k = f.cfg_with({})
        c = f.load_candles(NIFTY)
        ind = f.indicators(c, k)
        tr = tm.trade_from(c, ind, k, "short")
        full, _ = tm.replay(c, ind, k, tr, 0.5, 40)
        m = len(c) - 60
        cut = c[:m]
        part, _ = tm.replay(cut, f.indicators(cut, k), k, tr, 0.5, 40)
        done = {r["date"]: r for r in full if r["start"] + r.get("exit_bar", 0) < m - 1}
        same = {r["date"]: r for r in part if r["date"] in done}
        self.assertTrue(done)
        for d, r in done.items():
            self.assertIn(d, same)
            self.assertEqual((r["cls"], round(r["r"], 6)), (same[d]["cls"], round(same[d]["r"], 6)), d)

    def test_episodes_count_overlap_once(self):
        rs = [{"start": 10, "exit_bar": 5, "entered": True}, {"start": 11, "exit_bar": 5, "entered": True},
              {"start": 20, "exit_bar": 2, "entered": True}]
        self.assertEqual(tm.episodes(rs), 2)


class Instruments(unittest.TestCase):
    def test_option_leg_prices_the_quote_at_the_chain_clock(self):
        k = f.cfg_with({})
        c = f.load_candles(NIFTY)
        ind = f.indicators(c, k)
        chain = f.parse_chain(CHAIN)
        f.chain_analytics(chain, CLOSE, k["risk_free_rate"])
        lv = f.levels(c, ind, len(c) - 1, "short", k)
        op = f.option_plan(lv, chain, {"capital": 3000000}, CLOSE, None, "NIFTY")
        sessions = tm.trading_days_after(c[-1]["date"], 10, tm.nse_holidays())
        self.assertEqual(sessions[0].isoformat(), "2026-10-21")     # the 20th is Dussehra
        leg = tm.OptionLeg(op, chain, sessions, CLOSE, k)
        self.assertAlmostEqual(leg._model(chain["spot"], leg.T0) + leg.offset, op["entry_premium"], places=6)
        self.assertEqual(leg.bars_to_expiry, 5)        # 21, 22, 23, 26 and expiry on the 27th
        # at expiry intrinsic value (the basis held constant along the path); before it, a sell gets the bid
        self.assertAlmostEqual(leg.premium(chain["spot"] - 2000, 20),
                               max(op["strike"] - (chain["spot"] - 2000 + leg.basis), 0), delta=1e-6)
        mid_like = leg._model(chain["spot"], leg.Ts[0]) + leg.offset
        self.assertAlmostEqual(leg.premium(chain["spot"], 0), mid_like - leg.spread, places=6)
        self.assertGreater(leg.spread, 0)

    def test_cli_text_json_grid_equity_and_errors(self):
        code, out, err = run(["--candles", NIFTY, "--chain", CHAIN, "--now", "2026-10-19T15:30", "--paths", "800",
                              "--lots", "2", "--grid", "--config", "/nonexistent"])
        self.assertEqual(code, 0, err)
        for s in ("TIME MACHINE  NIFTY SHORT", "The chart", "How it ends", "stop-loss before TP1",
                  "option, rupees per lot", "Stop and target sensitivity", "Verdict (bootstrap)"):
            self.assertIn(s, out)
        code, out, err = run(["--candles", NIFTY, "--direction", "long", "--paths", "300", "--json", "--config",
                              "/nonexistent"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["trade"]["direction"], "long")
        self.assertIn("bootstrap", doc["methods"])
        code, out, err = run(["--candles", REL, "--instrument", "equity", "--qty", "20", "--direction", "long",
                              "--paths", "300", "--config", "/nonexistent"])
        self.assertEqual(code, 0, err)
        self.assertIn("shares, rupees for the position", out)
        code, _, err = run(["--candles", REL, "--instrument", "equity", "--direction", "short", "--paths", "300",
                            "--config", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("cannot be held short", err)
        code, _, err = run(["--candles", REL, "--direction", "long", "--sl", "1300", "--paths", "300",
                            "--config", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("wrong side", err)

    def test_limit_entries_fill_only_when_reached(self):
        k = f.cfg_with({})
        t = {"direction": "long", "stop": 950.0, "tp1": 1050.0, "tp2": 1100.0, "risk": 40.0, "frac": 0.5,
             "limit": 990.0}
        filled = tm.run_trade([(1000, 1060, 985, 1040, 10)] + [(1040, 1045, 1035, 1040, 10)] * 3, t, k, 4)
        self.assertEqual(filled["entry"], 990.0)
        self.assertIsNone(filled["tp1_bar"])            # TP1 on the fill session may have come before the fill
        missed = tm.run_trade([(1000, 1010, 995, 1005, 10)] * 3, t, k, 3)
        self.assertEqual(missed["cls"], "nofill")
        better = tm.run_trade([(985, 1000, 980, 995, 10)] * 3, t, k, 3)
        self.assertEqual(better["entry"], 985)

    def test_tp1_part_follows_the_planner_in_whole_lots(self):
        c = f.load_candles(NIFTY)
        for lots, want in ((1, 1.0), (2, 0.5), (3, 1 / 3.0), (5, 0.4)):
            rep = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=200, lots=lots, methods=("bootstrap",))
            self.assertAlmostEqual(rep["trade"]["frac"], want, msg=lots)
        rep = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=200, lots=1, methods=("bootstrap",),
                         trail_whole=True)
        self.assertEqual(rep["trade"]["frac"], 0.0)
        k = f.cfg_with({})
        t = {"direction": "long", "stop": 950.0, "tp1": 1050.0, "tp2": 1100.0, "risk": 50.0, "frac": 1.0}
        res = tm.run_trade([(1000, 1060, 990, 1055, 10)] * 3, t, k, 3)
        self.assertEqual((res["cls"], res["exit"], round(res["r"], 6)), ("tp1_all", 1050.0, 1.0))

    def test_replay_measures_each_past_trade_on_its_own_risk(self):
        c = f.load_candles(NIFTY)
        rep = tm.analyse(c, {}, direction="short", symbol="NIFTY", paths=200, lots=2, methods=("replay",))
        k = f.cfg_with({})
        ind = f.indicators(c, k)
        tr = tm.trade_from(c, ind, k, "short")
        occ, _ = tm.replay(c, ind, k, tr, 0.5, 40)
        cp = rep["trade"]["cost_points"]
        own = [(r["pts"] - cp * r["entry"] / tr["entry_plan"]) / r["risk"] for r in occ if r["entered"]]
        self.assertAlmostEqual(rep["methods"]["replay"]["expectancy_r"], sum(own) / len(own), places=3)

    def test_iv_reprices_the_option_and_bad_inputs_are_refused(self):
        c = f.load_candles(NIFTY)
        ch = f.parse_chain(CHAIN)
        base = tm.analyse(c, {}, chain=ch, now_utc=CLOSE, symbol="NIFTY", paths=600, methods=("bootstrap",))
        crush = tm.analyse(c, {}, chain=f.parse_chain(CHAIN), now_utc=CLOSE, symbol="NIFTY", paths=600,
                           methods=("bootstrap",), iv=8)
        self.assertLess(crush["methods"]["bootstrap"]["option_inr_per_lot"]["expectancy"],
                        base["methods"]["bootstrap"]["option_inr_per_lot"]["expectancy"])
        late = tm.analyse(c + [dict(c[-1], date="2026-10-28")], {}, chain=f.parse_chain(CHAIN), now_utc=CLOSE,
                          symbol="NIFTY", paths=200, methods=("bootstrap",))
        self.assertNotIn("option", late["trade"])
        self.assertTrue(any("before the entry session" in n for n in late["notes"]))
        for kw, want in (({"instrument": "option"}, "needs --chain"), ({"horizon": 0}, "at least 1"),
                         ({"setup": "squeeze", "direction": None}, "did not fire")):
            args = dict(direction="short", symbol="NIFTY", paths=200, methods=("bootstrap",))
            args.update(kw)
            with self.assertRaises(tm.TMError) as e:
                tm.analyse(c, {}, **args)
            self.assertIn(want, str(e.exception))
        with self.assertRaises(tm.TMError):
            tm.analyse(f.load_candles(REL), {}, chain=f.parse_chain(CHAIN), now_utc=CLOSE, direction="long",
                       symbol="RELIANCE", paths=200)
        intraday = [dict(b, date=b["date"] + " 10:15") for b in c]
        with self.assertRaises(tm.TMError):
            tm.analyse(intraday, {}, direction="short", paths=200)

    def test_custom_levels_keep_their_shape_in_the_replay(self):
        k = f.cfg_with({})
        c = f.load_candles(REL)
        ind = f.indicators(c, k)
        e = c[-1]["close"]
        tr = tm.trade_from(c, ind, k, "long", sl=e - 30, tp1=e + 30, tp2=e + 75)
        self.assertTrue(tr["custom_levels"])
        self.assertAlmostEqual(tr["tp2_r"], 2.5)
        self.assertAlmostEqual(tr["stop_atr"], 30 / ind["atr"][-1])
        occ, _ = tm.replay(c, ind, k, tr, 0.0, 40, conditioned=False)
        self.assertTrue(occ)


if __name__ == "__main__":
    unittest.main()


class Gate(unittest.TestCase):
    """Every scanned or planned trade goes through the Time Machine's gate."""

    def test_gate_reads_the_two_simulated_methods(self):
        c = f.load_candles(NIFTY)
        g = tm.gate(c, None, "short", None, None, None, None, None, f.parse_chain(CHAIN, "NIFTY"), CLOSE, None, 2, "NIFTY",
                    3000)
        self.assertEqual(g["gate"], "fail")                    # the implied paths are significantly negative here
        self.assertIn("implied", g["reason"])
        self.assertLess(g["methods"]["implied"]["ci95"][1], 0)
        self.assertIn("option_inr_per_lot", g["methods"]["bootstrap"])
        self.assertEqual(g["replay"]["independent"], 3)
        text = "\n".join(tm.gate_line(g))
        self.assertIn("time machine: FAIL", text)
        self.assertIn("replay of this chart's past", text)

    def test_watch_when_nothing_can_be_told_from_zero_and_pass_needs_a_positive_band(self):
        c = f.load_candles(REL)
        g = tm.gate(c, None, "long", None, None, None, None, None, None, None, 500, 2, "RELIANCE", 3000)
        self.assertIn(g["gate"], ("watch", "fail", "pass"))
        words = [x["word"] for x in g["methods"].values()]
        want = "fail" if "negative" in words else ("pass" if "positive" in words else "watch")
        self.assertEqual(g["gate"], want)                      # the verdict is exactly the rule
        self.assertNotIn("implied", g["methods"])              # no chain, no volatility: the bootstrap alone

    def test_errors_are_not_passes(self):
        c = f.load_candles(NIFTY)
        g = tm.gate(c, None, "long", None, None, 99999.0, None, None, None, None, 65, 1, "NIFTY", 500)   # stop above entry
        self.assertEqual(g["gate"], "error")
        self.assertIn("NOT CHECKED", tm.gate_line(g)[0])
        self.assertEqual(tm.GATE_RANK["error"], max(tm.GATE_RANK.values()))

    def test_scan_runs_the_gate_by_default_and_orders_by_it(self):
        scan = load("india-fno-scan", "scan")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = scan.main(["--candles", NIFTY, REL, "--lots", "RELIANCE=500", "--chain", "NIFTY=" + CHAIN, "--now",
                              "2026-10-19T10:00:00", "--config", "/nonexistent", "--tm-paths", "1500"])
        self.assertEqual(code, 0)
        self.assertIn("time machine: ", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            scan.main(["--candles", NIFTY, "--chain", "NIFTY=" + CHAIN, "--now", "2026-10-19T10:00:00", "--config",
                       "/nonexistent", "--no-timemachine"])
        self.assertNotIn("time machine:", out.getvalue())

    def test_plan_will_not_write_a_trade_the_gate_fails_unless_forced(self):
        import tempfile
        plan = load("india-fno-plan", "plan")
        real = plan.tm.gate
        try:
            plan.tm.gate = lambda *a, **k: {"gate": "fail", "reason": "implied is negative after charges (95% band below zero)",
                                           "methods": {}}
            with tempfile.TemporaryDirectory() as d:
                args = ["new", "RELIANCE", "--candles", REL, "--direction", "long", "--lot", "500", "--instrument", "future",
                        "--fut-expiry", "20261229", "--capital", "3000000", "--config", "/nonexistent", "--write",
                        "--orders-out", os.path.join(d, "o.json"), "--plans", os.path.join(d, "p.json")]
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = plan.main(args)
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn("Time Machine finds this trade negative", err.getvalue())
                self.assertFalse(os.path.exists(os.path.join(d, "o.json")))
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = plan.main(args + ["--force"])
                self.assertEqual(code, 0, err.getvalue())
                self.assertTrue(os.path.exists(os.path.join(d, "o.json")))
                self.assertIn("time machine: FAIL", out.getvalue())
                os.remove(os.path.join(d, "o.json"))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    plan.main(args + ["--no-timemachine"])         # skipped: written as before
                self.assertTrue(os.path.exists(os.path.join(d, "o.json")))
        finally:
            plan.tm.gate = real

    def test_the_real_plan_json_carries_the_gate(self):
        plan = load("india-fno-plan", "plan")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            plan.main(["new", "NIFTY", "--candles", NIFTY, "--direction", "short", "--chain", CHAIN, "--instrument", "option",
                       "--now", "2026-10-19T15:30", "--config", "/nonexistent", "--capital", "3000000", "--json",
                       "--tm-paths", "1500"])
        doc = json.loads(out.getvalue())
        g = doc["time_machine"]
        words = [x["word"] for x in g["methods"].values()]
        self.assertEqual(g["gate"], "fail" if "negative" in words else ("pass" if "positive" in words else "watch"))
        self.assertTrue(g["reason"])
