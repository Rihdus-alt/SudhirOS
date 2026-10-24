#!/usr/bin/env python3
"""Dump the Python Time Machine's numbers for the fixtures, so test/core.test.js can hold the JavaScript port to them.

    python3 apps/time-machine/test/ref_dump.py            # writes test/ref.json

Deterministic parts (indicators, setups, levels, the exit engine, the replay, costs, Black-76, the option leg) are
dumped exactly; the stochastic parts (bootstrap, implied) are dumped as a summary from many paths, to be matched
within sampling error."""
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "..", "..", "plugins", "trader-skill-india")
FIX = os.path.join(HERE, "..", "fixtures")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


f = load(os.path.join(PLUGIN, "india-fno-plan", "fnocore.py"), "fnocore")
tm = load(os.path.join(PLUGIN, "india-fno-timemachine", "timemachine.py"), "timemachine")

NIFTY = os.path.join(FIX, "NIFTY-1d.csv")
CHAIN = os.path.join(FIX, "indmoney-chain-NIFTY-20261027.json")
CLOSE = f.parse_now("2026-10-19T15:30")


def main():
    ref = {}
    # ------------------------------------------------ numerics
    pts = [-8.5, -3.0, -1.2, -0.3, 0.0, 0.4, 1.0, 2.5, 6.0]
    ref["ncdf"] = [[x, f.ncdf(x)] for x in pts]
    cases = [(22900.0, 22900.0, 0.02, 0.18, 0.065, "CE"), (22900.0, 22700.0, 0.01, 0.15, 0.065, "PE"),
             (22900.0, 23200.0, 0.05, 0.22, 0.065, "CE"), (22900.0, 22900.0, 0.0, 0.2, 0.065, "CE"),
             (22900.0, 22500.0, 0.003, 0.12, 0.065, "PE"), (74000.0, 74500.0, 0.0273, 0.14, 0.065, "CE")]
    ref["black76"] = [list(c) + [f.black76(*c)] for c in cases]
    ref["implied"] = []
    for F, K, T, sg, r, kind in cases:
        if T <= 0:
            continue
        price = f.black76(F, K, T, sg, r, kind)
        ref["implied"].append([F, K, T, r, kind, price, f.implied_vol(price, F, K, T, r, kind)])
    ref["delta"] = [list(c) + [f.greeks(*c)["delta"]] for c in cases]
    ref["costs"] = []
    for side in ("buy", "sell"):
        for kind in ("FUT", "OPT"):
            for ex in ("NFO", "BFO"):
                for v in (1250.0, 8612.5, 154300.0, 1495000.0):
                    ref["costs"].append([side, v, kind, ex, f.costs.fno_costs(side, v, kind, ex)["total"]])
    ref["trade_costs"] = [[side, v, f.costs.trade_costs(side, v, "NSE", "CNC")["total"]] for side in ("buy", "sell")
                          for v in (5000.0, 87250.0)]
    ref["wilson"] = [[3, 10, f.wilson(3, 10)[0], f.wilson(3, 10)[1]], [0, 7, f.wilson(0, 7)[0], f.wilson(0, 7)[1]]]

    # ------------------------------------------------ the chart
    c = f.load_candles(NIFTY)
    ind = f.indicators(c)
    n = len(c)
    ref["n_candles"] = n
    ref["last_date"] = c[-1]["date"]
    last = {}
    for key in ("ema20", "ema50", "atr", "rsi", "adx", "st_line", "st_dir", "bbw", "vol_avg"):
        last[key] = [[i, ind[key][i]] for i in (60, 100, 150, n - 1)]
    ref["ind"] = last
    sigs = []
    for i in range(60, n):
        for s in f.signals_at(c, ind, i):
            sigs.append([i, s["setup"], s["direction"], s["score"]])
    ref["signals"] = sigs
    lv = []
    for i in (80, 120, 180, n - 1):
        for d in ("long", "short"):
            x = f.levels(c, ind, i, d, None)
            lv.append([i, d, x["entry"], x["stop"], x["tp1"], x["tp2"], x["risk_points"], x["reward_r"], x["ok"], x["stop_basis"]])
            x = f.levels(c, ind, i, d, None, entry=c[i]["close"] * 1.002, resistance=c[i]["close"] * 1.02, support=c[i]["close"] * 0.98)
            lv.append([i, d + "-limit", x["entry"], x["stop"], x["tp1"], x["tp2"], x["risk_points"], x["reward_r"], x["ok"], x["stop_basis"]])
    ref["levels"] = lv
    rd = tm.chart_read(c, ind, None)
    ref["chart_read"] = {k: rd[k] for k in ("close", "trend", "ema50_slope", "adx", "rsi", "supertrend", "atr", "atr_pct",
                                              "atr_percentile", "bb_width_percentile", "realized_vol_20d_pct", "high_52w",
                                              "low_52w", "from_high_pct", "swing_low", "swing_high")}
    ref["overnight_share"] = tm.overnight_share(c)

    # ------------------------------------------------ the exit engine on crafted bars
    k = f.cfg_with(None)
    base = {"direction": "long", "stop": 99.0, "tp1": 101.0, "tp2": 102.0, "risk": 1.0, "frac": 0.5}
    crafted = {
        "tp2_then_trail": [(100, 100.4, 99.5, 100.2, 0.8), (100.2, 101.2, 100.1, 101.0, 0.8), (101.0, 102.3, 100.9, 102.2, 0.8),
                           (102.2, 103.0, 102.0, 102.9, 0.8), (102.9, 103.1, 100.5, 100.8, 0.8)],
        "gap_through_stop": [(100, 100.3, 99.6, 99.9, 0.8), (98.0, 98.5, 97.5, 98.1, 0.8)],
        "stop_first_when_both": [(100, 101.5, 98.5, 100.0, 0.8)],
        "time_stop": [(100, 100.4, 99.6, 100.1, 0.8)] * 12,
        "tp1_gap": [(100, 100.3, 99.7, 100.1, 0.8), (101.6, 101.9, 101.2, 101.7, 0.8), (101.7, 101.9, 100.0, 100.2, 0.8)],
        "no_entry": [(101.4, 101.6, 101.0, 101.5, 0.8)],
    }
    out = {}
    for name, bars in crafted.items():
        for frac in (0.5, 1.0, 0.0):
            for d in ("long", "short"):
                if d == "long":
                    t = dict(base, frac=frac)
                    b = bars
                else:   # mirror around 100
                    t = {"direction": "short", "stop": 101.0, "tp1": 99.0, "tp2": 98.0, "risk": 1.0, "frac": frac}
                    b = [(200 - o, 200 - l, 200 - h, 200 - cl, a) for (o, h, l, cl, a) in bars]
                r = tm.run_trade(b, t, k, None)
                out["%s|%s|%s" % (name, d, frac)] = {kk: (r.get(kk)) for kk in ("cls", "entered", "entry", "exit", "exit_bar", "bars", "tp1_bar", "tp1_px", "tp2_bar", "pts", "r", "mae_r", "mfe_r")}
    lim = dict(base, limit=99.8, frac=1.0)
    out["limit_inside"] = {kk: tm.run_trade([(100.5, 101.5, 99.5, 100.0, 0.8), (100.0, 101.2, 99.9, 101.0, 0.8)], lim, k, None).get(kk)
                           for kk in ("cls", "entry", "exit", "pts")}
    lim2 = dict(base, limit=99.0 + 0.5, frac=1.0)
    out["limit_nofill"] = tm.run_trade([(100.5, 100.9, 99.8, 100.0, 0.8)], lim2, k, None)["cls"]
    ref["run_trade"] = out

    # ------------------------------------------------ the replay
    ch = f.parse_chain(CHAIN)
    an = f.chain_analytics(ch, CLOSE, k["risk_free_rate"])
    tr = tm.trade_from(c, ind, k, None, None, None, None, None, None, an)
    ref["trade"] = {kk: tr[kk] for kk in ("direction", "setup", "entry_plan", "stop", "tp1", "tp2", "risk", "atr", "stop_atr", "tp1_r", "tp2_r", "ok")}
    ref["trade"]["notes"] = tr["notes"]
    rep = {}
    for tag, args in (("short_pullback", dict(direction="short", setup=None)), ("long_any", dict(direction="long", setup=None))):
        t2 = tm.trade_from(c, ind, k, args["direction"], args["setup"], None, None, None, None, None)
        for frac in (0.5, 1.0):
            for cond in (True, False):
                res, still = tm.replay(c, ind, k, t2, frac, 40, cond)
                rep["%s|%s|%s" % (tag, frac, cond)] = {
                    "still": still, "episodes": tm.episodes(res),
                    "rows": [[r["start"], r["cls"], round(r.get("r") or 0.0, 6), r.get("bars"), round(r.get("pts") or 0.0, 6)] for r in res]}
    ref["replay"] = rep

    # ------------------------------------------------ the chain and the option leg
    ref["chain"] = {kk: an.get(kk) for kk in ("forward", "atm_strike", "atm_iv_pct", "expected_move_1sd", "atm_straddle", "pcr_oi", "max_pain",
                                               "call_wall", "put_wall", "resistance", "support", "days_to_expiry")}
    ref["chain_ivs"] = [[x["strike"], (x["ce"] or {}).get("iv"), (x["pe"] or {}).get("iv")] for x in ch["strikes"]]
    op = f.option_plan({"direction": "short", "entry": tr["entry_plan"], "stop": tr["stop"], "tp1": tr["tp1"], "tp2": tr["tp2"], "atr": tr["atr"]},
                       ch, k, CLOSE, None, "NIFTY")
    ref["option"] = {kk: op[kk] for kk in ("expiry", "kind", "strike", "delta", "iv_pct", "spread_pct", "entry_premium", "lot_size", "forward")}
    sessions = tm.trading_days_after(c[-1]["date"], 40, set())
    leg = tm.OptionLeg(op, ch, sessions, CLOSE, k, None)
    ref["leg"] = {"offset": leg.offset, "spread": leg.spread, "T0": leg.T0, "Topen0": leg.T_open0, "basis": leg.basis,
                  "bars_to_expiry": leg.bars_to_expiry,
                  "premium": [[lvl, b, leg.premium(lvl, b)] for lvl in (22600.0, 22888.95, 23100.0) for b in (0, 2)],
                  "entry_premium": [[lvl, leg.entry_premium(lvl)] for lvl in (22800.0, 22888.95, 23000.0)]}
    fake = {"entered": True, "entry": 22880.0, "exit": 22400.0, "exit_bar": 3, "tp1_bar": 1, "tp1_px": 22600.0}
    ref["leg"]["pnl"] = [[fr, leg.pnl(fake, {"frac": fr})] for fr in (0.0, 0.5, 1.0)]
    ref["sessions"] = [d.isoformat() for d in sessions[:8]]
    hol = tm.nse_holidays()
    ref["holidays"] = sorted(hol)
    ref["sessions_h"] = [d.isoformat() for d in tm.trading_days_after(c[-1]["date"], 12, hol)]

    # ------------------------------------------------ the simulations, as summaries
    full = tm.analyse(c, None, chain=f.parse_chain(CHAIN), now_utc=CLOSE, symbol="NIFTY", paths=40000, lots=2, seed=11)
    sim = {}
    for name in ("bootstrap", "implied"):
        s = full["methods"][name]
        sim[name] = {"n": s["n"], "entered": s["entered"], "outcomes": {k2: v["p"] for k2, v in s["outcomes"].items()},
                     "expectancy_r": s["expectancy_r"], "p_profit": s["p_profit"], "avg_win_r": s["avg_win_r"], "avg_loss_r": s["avg_loss_r"],
                     "bars_median": s["bars_median"], "futures": s.get("futures_inr_per_lot"), "option": s.get("option_inr_per_lot"),
                     "history_se": s.get("history_se"), "r_q": s["r_quantiles"], "mae_p90": s["mae_r_p90"]}
    ref["sim"] = sim
    ref["sim_trade_frac"] = full["trade"]["frac"]
    ref["sim_cost_points"] = full["trade"]["cost_points"]
    rp = full["methods"]["replay"]
    ref["sim_replay"] = {"n": rp["n"], "entered": rp["entered"], "independent": rp["independent"], "expectancy_r": rp["expectancy_r"],
                         "outcomes": {k2: v["p"] for k2, v in rp["outcomes"].items()}}
    bl = full["methods"]["baseline"]
    ref["sim_baseline"] = {"n": bl["n"], "independent": bl["independent"], "expectancy_r": bl["expectancy_r"]}
    with open(os.path.join(HERE, "ref.json"), "w") as fh:
        json.dump(ref, fh, indent=1)
    print("wrote", os.path.join(HERE, "ref.json"), "signals:", len(sigs), "replay keys:", len(rep))


if __name__ == "__main__":
    main()
