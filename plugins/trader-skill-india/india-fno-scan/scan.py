#!/usr/bin/env python3
"""Screen F&O underlyings for trade setups (india-fno-scan). Standard library only, no network.

For each underlying it reads daily (or intraday) candles, checks the setups on the LAST CLOSED bar,
and for every signal prints the full plan: entry, stop-loss, TP1, TP2, the trailing rule, the size in
whole lots for your risk budget, and the setup's own track record on that underlying (a backtest over
the same candles, same rules, costs included). With an option chain it also checks the room to the
OI walls and suggests the option strike with premium levels.

    python3 scan.py --candles data/NIFTY-1d.csv data/RELIANCE-1d.csv --lots NIFTY=65 RELIANCE=500
    python3 scan.py --candles data/*.csv --chain NIFTY=data/nifty-chain.json --capital 500000 --risk-pct 1
    python3 scan.py --candles data/*.csv --proven-only        # only setups with a positive track record here
    python3 scan.py --candles data/*.csv --json --write trader/fno/scan.json

Candle files: SYMBOL-<anything>.csv (date,open,high,low,close,volume) or the INDmoney connector's
get_indian_stocks_ohlc output saved as SYMBOL-<anything>.json. The symbol is the part before the
first "-" or ".".

Setups (closed bars only, no look-ahead):
  trend_pullback   EMA20 above a rising EMA50, a dip to EMA20 that closes back above it, RSI 40-60
                   (mirror for shorts)
  breakout         a close beyond the prior 20-day high/low with ADX >= 20
  supertrend_flip  Supertrend(10,3) changes direction with the close on the same side of EMA50
  squeeze          Bollinger width at a 60-bar low, then a close outside the bands
Score: 1 + 1 if ADX >= 25 + 0.5 if volume is 1.5x its 20-day average (+0.5 more for a breakout).

A signal is not a prediction. Its track record says how this exact rule did here before, with a 95%
confidence interval that is wide when trades are few.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys


def _core():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-fno-plan", "fnocore.py")
    if not os.path.exists(path):
        sys.exit("scan.py: india-fno-plan/fnocore.py not found: install every india-* folder")
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


def parse_pairs(items, conv=str):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise f.FnoError("expected SYMBOL=VALUE, got %r" % it)
        k, v = it.split("=", 1)
        out[k.strip().upper()] = conv(v.strip())
    return out


def track_record(c, setup, direction, cfg, cost_points):
    trades = [t for t in f.backtest(c, cfg, enabled=(setup,), cost_points=cost_points) if t["direction"] == direction]
    return f.stats(trades)


def scan_one(sym, c, cfg, lot=None, chain=None, now_utc=None, enabled=f.SETUPS, history=True):
    ind = f.indicators(c, cfg)
    i = len(c) - 1
    sigs = f.signals_at(c, ind, i, enabled)
    out = []
    an = None
    if chain:
        an = f.chain_analytics(chain, now_utc, f.cfg_with(cfg)["risk_free_rate"])
    for s in sigs:
        res = an["resistance"] if an else None
        sup = an["support"] if an else None
        lv = f.levels(c, ind, i, s["direction"], cfg, resistance=res, support=sup)
        item = {"symbol": sym, "date": c[i]["date"], "setup": s["setup"], "direction": s["direction"],
                "score": s["score"], "why": s["why"], "close": c[i]["close"], "levels": lv, "bars": len(c)}
        cost_points = 0.0
        if lot:
            item["futures"] = f.futures_sizing(lv, lot, c[i]["close"], cfg, index=sym in f.INDEX_LOTS)
            cost_points = item["futures"]["roundtrip_charges_per_lot"] / lot
        if an:
            item["chain"] = {k: an[k] for k in ("expiry", "days_to_expiry", "atm_iv_pct", "expected_move_1sd",
                                                "pcr_oi", "max_pain", "resistance", "support")}
            try:
                item["option"] = f.option_plan(lv, chain, cfg, now_utc, lot, sym)
            except f.FnoError as e:
                item["option_error"] = str(e)
        if history:
            item["track_record"] = track_record(c, s["setup"], s["direction"], cfg, cost_points)
        out.append(item)
    return out


def fmt(x):
    return f.inr(x)


def render(items, proven_only=False):
    lines = []
    shown = 0
    for it in sorted(items, key=lambda x: (-x["score"], x["symbol"])):
        tr = it.get("track_record") or {}
        proven = tr.get("trades", 0) >= 5 and (tr.get("expectancy_r") or 0) > 0
        if proven_only and not proven:
            continue
        shown += 1
        lv = it["levels"]
        lines.append("%s %s%s  [%s, score %.1f]  close %s on %s"
                     % (it["symbol"], it["direction"].upper(), "" if lv["ok"] else " (NOT WORTH IT)", it["setup"],
                        it["score"], fmt(it["close"]), it["date"]))
        lines.append("  why: " + it["why"])
        lines.append("  entry %s | SL %s (%s, %s points) | TP1 %s (1R) | TP2 %s (%.2fR)"
                     % (fmt(lv["entry"]), fmt(lv["stop"]), lv["stop_basis"], lv["risk_points"], fmt(lv["tp1"]),
                        fmt(lv["tp2"]), lv["reward_r"]))
        t = lv["trail"]
        lines.append("  trail: at TP1 book %d%% and move SL to entry; then trail %.1f x ATR from the best price "
                     "(%.1f x ATR after TP2); exit if no TP1 in %d bars; ATR now %.2f"
                     % (round(100 * t["tp1_exit_fraction"]), t["after_tp1_atr"], t["after_tp2_atr"],
                        t["time_stop_bars"], lv["atr"]))
        lines.append("  no-edge odds of TP1 before SL: %.0f%% (that is the break-even win rate before costs)"
                     % (100 * lv["no_edge_p_tp1"]))
        for n in lv["notes"]:
            lines.append("  note: " + n)
        fu = it.get("futures")
        if fu:
            if fu["lots"]:
                lines.append("  futures: %d lot(s) x %d = %d, max loss at SL %s (incl. %s charges a lot), est. margin %s"
                             % (fu["lots"], fu["lot_size"], fu["quantity"], fmt(fu["max_loss"]),
                                fmt(fu["roundtrip_charges_per_lot"]), fmt(fu["est_margin"])))
            else:
                lines.append("  futures: no size: " + fu["why_zero"])
        op = it.get("option")
        if op:
            lines.append("  option: %s %s %s (delta %.2f, IV %.1f%%, %s days), buy at %s, SL %s, TP1 %s, TP2 %s"
                         % (op["expiry"], int(op["strike"]), op["kind"], op["delta"], op["iv_pct"], op["dte"],
                            fmt(op["entry_premium"]), fmt(op["sl_premium"]), fmt(op["tp1_premium"]),
                            fmt(op["tp2_premium"])))
            if op["lots"]:
                lines.append("          %d lot(s) x %d, max loss at SL %s, all-in worst case %s"
                             % (op["lots"], op["lot_size"], fmt(op["max_loss_at_stop"]), fmt(op["worst_case_loss"])))
            else:
                lines.append("          no size: " + op["why_zero"])
            for n in op["notes"]:
                lines.append("  option note: " + n)
        if it.get("option_error"):
            lines.append("  option: " + it["option_error"])
        ch = it.get("chain")
        if ch:
            lines.append("  chain: PCR %s, max pain %s, OI resistance %s, support %s, ATM IV %s%%, 1-SD move to expiry "
                         "+/-%s" % (ch["pcr_oi"], int(ch["max_pain"]), ch["resistance"], ch["support"], ch["atm_iv_pct"],
                                    ch["expected_move_1sd"]))
        if tr.get("trades"):
            lo, hi = tr["win_rate_95ci"]
            lines.append("  track record here (%s %s over the %d bars given): %d trades, win %.0f%% (95%% CI %.0f-%.0f%%), "
                         "expectancy %+.2fR, profit factor %s%s"
                         % (it["setup"], it["direction"], it.get("bars", 0), tr["trades"], 100 * tr["win_rate"],
                            100 * lo, 100 * hi, tr["expectancy_r"],
                            tr["profit_factor"] if tr["profit_factor"] is not None else "n/a (no losses)",
                            "" if proven else "  <- NOT a proven edge here (needs 5+ trades and positive expectancy)"))
        elif "track_record" in it:
            lines.append("  track record here: no earlier trades of this setup in the data")
        lines.append("")
    if not shown:
        lines.append("No setup fired on the last closed bar%s." % (" with a positive track record" if proven_only else ""))
    lines.append("SEBI's study: about 91% of individual F&O traders lost money in FY2024-25. A signal is a rule "
                 "firing, not a forecast. Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Screen F&O underlyings for setups with SL, TP and trailing (india-fno-scan).")
    ap.add_argument("--candles", nargs="+", required=True, help="candle files, one per underlying")
    ap.add_argument("--chain", nargs="*", default=[], help="SYMBOL=chain.json (INDmoney or NSE option chain)")
    ap.add_argument("--lots", nargs="*", default=[], help="SYMBOL=LOT for stocks (indices have defaults)")
    ap.add_argument("--setups", nargs="*", default=list(f.SETUPS), choices=list(f.SETUPS))
    ap.add_argument("--config", default="trader/config.json", help="reads the \"fno\" section")
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk-pct", type=float)
    ap.add_argument("--now", help="ISO time (UTC) for option time to expiry; default now")
    ap.add_argument("--proven-only", action="store_true", help="only signals whose setup made money here before")
    ap.add_argument("--no-history", action="store_true", help="skip the per-signal backtest")
    ap.add_argument("--write", nargs="?", const="trader/fno/scan.json", metavar="PATH")
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
        if a.capital:
            cfg["capital"] = a.capital
        if a.risk_pct:
            cfg["risk_per_trade_pct"] = a.risk_pct
        lots = parse_pairs(a.lots, int)
        chains = parse_pairs(a.chain)
        now = (datetime.datetime.fromisoformat(a.now.replace("Z", "")) if a.now else None)
        items, errors = [], []
        for path in a.candles:
            sym = symbol_of(path)
            try:
                c = f.load_candles(path)
                chain = f.parse_chain(chains[sym], sym) if sym in chains else None
                lot = lots.get(sym) or (chain or {}).get("lot_size") or f.INDEX_LOTS.get(sym)
                items += scan_one(sym, c, cfg, lot, chain, now, tuple(a.setups), not a.no_history)
            except f.FnoError as e:
                errors.append("%s: %s" % (sym, e))
    except f.FnoError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump({"created_at": datetime.datetime.now().replace(microsecond=0).isoformat(), "signals": items,
                       "errors": errors}, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps({"signals": items, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        print(render(items, a.proven_only))
        for e in errors:
            print("error: " + e)
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
