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
        self.assertAlmostEqual(leg.premium(chain["spot"] - 2000, 20), max(op["strike"] - (chain["spot"] - 2000), 0) +
                               0.0, delta=1.0)

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
