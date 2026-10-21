#!/usr/bin/env python3
"""Backtest the F&O setups and exits on history (india-fno-backtest). Standard library only, no network.

The honest measure of "accuracy": replay exactly the rules india-fno-scan and india-fno-plan use,
bar by bar, and count what happened.

    python3 backtest.py NIFTY-1d.csv RELIANCE-1d.csv HDFCBANK-1d.csv --lots RELIANCE=500 HDFCBANK=550
    python3 backtest.py data/*.csv --setups trend_pullback --split 0.3      # 30% held out
    python3 backtest.py NIFTY-1d.csv --trades                               # every trade
    python3 backtest.py data/*.csv --json

Rules of the replay (no look-ahead, no flattering assumptions):
  - a setup is evaluated on bar i's close using bars up to i only
  - the stop and targets are fixed then; the order fills at bar i+1's OPEN, and the trade is skipped
    if that open is already beyond the stop or TP1 (a gap)
  - a bar that touches both the stop and a target counts as the stop (we cannot see inside the bar)
  - a gap through the stop fills at the open, not at the stop
  - TP1 books tp1_exit_fraction at 1R and moves the stop to entry; the rest trails (chandelier),
    with a time stop if TP1 does not come within time_stop_bars
  - one position per underlying at a time; round-trip futures charges (STT 0.05% on the sell side,
    exchange, stamp duty, SEBI, GST, brokerage) are taken off every trade
  - trades still open at the end are listed but not counted

What it reports: trades, win rate with its 95% (Wilson) confidence interval, average win and loss in
R, the break-even win rate at that payoff, expectancy per trade in R, its t-statistic, profit factor,
total R and the worst drawdown in R. With --split, the last part of every series is held out and
reported separately: a rule that only works in-sample does not work.

Limits: these are tests on the underlying's candles. Options add theta, IV changes and spreads that
candles do not contain; futures add rollover at each expiry and a basis to spot. A year of daily bars
gives few trades: read the confidence interval before the win rate.
"""

import argparse
import importlib.util
import json
import os
import sys


def _core():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-fno-plan", "fnocore.py")
    if not os.path.exists(path):
        sys.exit("backtest.py: india-fno-plan/fnocore.py not found: install every india-* folder")
    spec = importlib.util.spec_from_file_location("fnocore", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f = _core()


def symbol_of(path):
    base = os.path.basename(path)
    for sep in ("-", "_", "."):
        base = base.split(sep)[0]
    return base.upper()


def run(files, cfg, setups, lots, split=0.0):
    per, all_in, all_out, skipped = {}, [], [], []
    for path in files:
        sym = symbol_of(path)
        if f.not_candles(path):
            skipped.append("%s: not a candle file" % path)
            continue
        try:
            c = f.load_candles(path)
        except f.FnoError as e:
            skipped.append("%s: %s" % (path, e))
            continue
        lot = lots.get(sym) or f.INDEX_LOTS.get(sym)
        px = c[-1]["close"]
        cost_points, charges_lot, assumed = f.roundtrip_points(px, lot, cfg)
        cut = int(len(c) * (1 - split)) if split else len(c) - 1
        tin = f.backtest(c, cfg, setups, cost_points, end=cut if split else None)
        tout = f.backtest(c, cfg, setups, cost_points, start=max(cut, 60)) if split else []
        for t in tin + tout:
            t["symbol"] = sym
        per[sym] = {"bars": len(c), "from": c[0]["date"], "to": c[-1]["date"], "cost_points": round(cost_points, 2),
                    "lot": lot, "charges_lot": charges_lot, "lot_assumed": assumed, "in_sample": f.stats(tin), "out_of_sample": f.stats(tout) if split else None,
                    "trades": tin + tout, "split_date": c[cut]["date"] if split else None}
        all_in += tin
        all_out += tout
    if not per:
        raise f.FnoError("no candle files among the %d given (%s)" % (len(files), "; ".join(skipped)))
    by_setup = {}
    for s in setups:
        by_setup[s] = {"in_sample": f.stats([t for t in all_in if t["setup"] == s]),
                       "out_of_sample": f.stats([t for t in all_out if t["setup"] == s]) if split else None}
    return {"per_symbol": per, "pooled_in_sample": f.stats(all_in),
            "pooled_out_of_sample": f.stats(all_out) if split else None, "by_setup": by_setup, "skipped": skipped}


def line(label, st):
    if not st or not st.get("trades"):
        return "%-28s no closed trades" % label
    lo, hi = st["win_rate_95ci"]
    return ("%-28s %3d trades  win %3.0f%% (95%% CI %2.0f-%3.0f%%, needs %3.0f%%)  avg +%.2fR / -%.2fR  "
            "expectancy %+.2fR (t %s)  PF %s  total %+.1fR  max DD %.1fR"
            % (label, st["trades"], 100 * st["win_rate"], 100 * lo, 100 * hi,
               100 * (st["breakeven_win_rate"] or 0), st["avg_win_r"], st["avg_loss_r"], st["expectancy_r"],
               st["t_stat"] if st["t_stat"] is not None else "n/a",
               st["profit_factor"] if st["profit_factor"] is not None else "n/a", st["total_r"], st["max_drawdown_r"]))


def verdict(st):
    if not st or st.get("trades", 0) < 20:
        return "too few trades to judge (under 20)"
    if st["t_stat"] is not None and st["t_stat"] >= 2 and st["expectancy_r"] > 0:
        return "positive and statistically meaningful (t >= 2)"
    if st["expectancy_r"] > 0:
        return "positive but not statistically meaningful (could be luck)"
    return "no edge: expectancy at or below zero after costs"


def render(res, show_trades=False, split=0.0):
    L = []
    for sym, p in res["per_symbol"].items():
        L.append("%s: %d bars %s to %s, costs %s points a round trip%s%s"
                 % (sym, p["bars"], p["from"], p["to"], p["cost_points"],
                    (" (no lot size given: charges worked out on a %d-unit, Rs 15 lakh contract; pass --lots)"
                     % p["charges_lot"]) if p.get("lot_assumed") else "",
                    (", held out from %s" % p["split_date"]) if split else ""))
        L.append("  " + line("in-sample" if split else "all", p["in_sample"]))
        if split:
            L.append("  " + line("out-of-sample", p["out_of_sample"]))
        if show_trades:
            for t in p["trades"]:
                L.append("    %s %-15s %-5s in %s @ %.2f  SL %.2f TP1 %.2f TP2 %.2f  out %s @ %.2f  %+.2fR%s"
                         % (t["symbol"], t["setup"], t["direction"], t["entry_date"], t["entry"], t["stop"], t["tp1"],
                            t["tp2"], t["exit_date"], t["exit"], t["r"], "  (open)" if t["open_at_end"] else ""))
    L.append("")
    L.append("By setup, all symbols:")
    for s, v in res["by_setup"].items():
        L.append("  " + line(s, v["in_sample"]))
        if split:
            L.append("  " + line("  out-of-sample", v["out_of_sample"]))
    L.append("")
    L.append(line("POOLED" + (" in-sample" if split else ""), res["pooled_in_sample"]))
    if split:
        L.append(line("POOLED out-of-sample", res["pooled_out_of_sample"]))
    for sk in res.get("skipped") or []:
        L.append("skipped " + sk)
    L.append("Verdict: %s." % verdict(res["pooled_out_of_sample"] if split else res["pooled_in_sample"]))
    L.append("Past results on candles are not future results, and options behave differently from candles. "
             "Education and tooling, not financial advice.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the F&O setups and exits (india-fno-backtest).")
    ap.add_argument("files", nargs="+", help="candle files, SYMBOL-*.csv or INDmoney OHLC JSON")
    ap.add_argument("--lots", nargs="*", default=[], help="SYMBOL=LOT for stocks, for the charges")
    ap.add_argument("--setups", nargs="*", default=list(f.SETUPS), choices=list(f.SETUPS))
    ap.add_argument("--split", type=float, default=0.0, help="fraction held out at the end (e.g. 0.3)")
    ap.add_argument("--config", default="trader/config.json", help="reads the \"fno\" section")
    ap.add_argument("--trades", action="store_true", help="list every trade")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = {}
        if a.config and os.path.exists(a.config):
            with open(a.config, encoding="utf-8") as fh:
                doc = json.load(fh)
            cfg = dict(doc.get("fno") or {})
            if doc.get("charges"):
                cfg["charges"] = doc["charges"]
        lots = {}
        for it in a.lots:
            k, v = it.split("=", 1)
            lots[k.upper()] = int(v)
        if not 0 <= a.split < 0.9:
            raise f.FnoError("--split is a fraction between 0 and 0.9")
        res = run(a.files, cfg, tuple(a.setups), lots, a.split)
    except (f.FnoError, ValueError, OSError) as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(render(res, a.trades, a.split))
    return 0


if __name__ == "__main__":
    sys.exit(main())
