#!/usr/bin/env python3
"""Plan one F&O trade with its stop-loss, targets and trailing stop, then track it (india-fno-plan).
Standard library only, no network.

    python3 plan.py new NIFTY --candles NIFTY-1d.csv --direction short --chain nifty-chain.json --instrument option
    python3 plan.py new RELIANCE --candles RELIANCE-1d.csv --direction short --lot 500 --instrument future --write
    python3 plan.py new NIFTY --candles NIFTY-1d.csv --direction long --entry 23050 --capital 1000000 --risk-pct 1
    python3 plan.py trail NIFTY-20261019-1 --candles NIFTY-1d.csv     # move the stop, report SL/TP hits
    python3 plan.py list                                             # open plans and their current stops
    python3 plan.py close NIFTY-20261019-1 --price 22600 --reason "TP1 and trail"

`new` prints the plan and, with --write, writes the ENTRY order to trader/orders.json for india-risk
and india-broker, and saves the plan (stop, targets, trailing rules) to trader/fno/plans.json. The
protective orders come after the entry fills:

  stop-loss   an SL order: trigger at the stop, limit a little beyond it (SL-M is not available on
              NSE options); for an option, the trigger is on the PREMIUM level the plan worked out
  target      a LIMIT exit at TP1 for the TP1 fraction; Zerodha users can put the stop and the
              target in one GTT OCO (one cancels the other)
  trailing    there is no exchange trailing order for F&O in India: run `plan.py trail` after each
              session's close (or on intraday candles) and modify the SL order to the new stop

Levels, sizing and odds come from fnocore.py (see its notes). Defaults are in fnocore.DEFAULTS; set
your own under "fno" in trader/config.json.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fnocore as f  # noqa: E402


def _timemachine():
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-fno-timemachine", "timemachine.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("timemachine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tm = _timemachine()


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_cfg(a):
    doc = read_json(a.config, {}) or {}
    cfg = dict(doc.get("fno") or {})
    if doc.get("charges"):
        cfg["charges"] = doc["charges"]
    if getattr(a, "capital", None):
        cfg["capital"] = a.capital
    if getattr(a, "risk_pct", None):
        cfg["risk_per_trade_pct"] = a.risk_pct
    return cfg


def build(a):
    cfg = load_cfg(a)
    k = f.cfg_with(cfg)
    c = f.load_candles(a.candles)
    if f.load_candles.dropped:
        print("note: left out the unfinished bar of %s (the session had not closed when it was fetched)"
              % ", ".join(f.load_candles.dropped), file=sys.stderr)
    ind = f.indicators(c, cfg)
    i = len(c) - 1
    sym = a.symbol.upper()
    chain = f.parse_chain(a.chain, sym) if a.chain else None
    now = f.parse_now(a.now, chain)
    an = f.chain_analytics(chain, now, k["risk_free_rate"]) if chain else None
    lv = f.levels(c, ind, i, a.direction, cfg, entry=a.entry, resistance=(an or {}).get("resistance"),
                  support=(an or {}).get("support"))
    if a.fut_price:
        # futures trade at a basis to the underlying the candles describe: shift the futures levels by it
        basis = a.fut_price - (a.entry or c[i]["close"])
        lv = dict(lv, fut_basis=round(basis, 2), fut_levels={x: round(lv[x] + basis, 2) for x in ("entry", "stop", "tp1", "tp2")})
    lot = a.lot or (chain or {}).get("lot_size") or f.INDEX_LOTS.get(sym)
    plan = {"symbol": sym, "direction": a.direction, "made_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "bar_date": c[i]["date"], "levels": lv, "chain": {kk: an[kk] for kk in ("expiry", "days_to_expiry",
                                                                                      "atm_iv_pct", "pcr_oi", "max_pain",
                                                                                      "resistance", "support",
                                                                                      "expected_move_1sd")} if an else None}
    if a.instrument in ("future", "both"):
        if not lot:
            raise f.FnoError("no lot size for %s: pass --lot" % sym)
        plan["futures"] = f.futures_sizing(lv, int(lot), lv["entry"], cfg, index=sym in f.INDEX_LOTS)
    if a.instrument in ("option", "both"):
        if not chain:
            raise f.FnoError("an option plan needs --chain (the INDmoney or NSE option chain for %s)" % sym)
        plan["option"] = f.option_plan(lv, chain, cfg, now, lot, sym)
    plan["orders"] = orders_for(plan, chain, k, a)
    if tm is not None and not getattr(a, "no_timemachine", False):
        size = (plan.get("option") or {}).get("lots") or (plan.get("futures") or {}).get("lots") or 1
        plan["time_machine"] = tm.gate(c, cfg, a.direction, None, a.entry, None, None, None, chain, now, lot, size, sym,
                                       getattr(a, "tm_paths", 4000) or 4000)
    return plan


def orders_for(plan, chain, k, a):
    """The entry order (for india-risk and india-broker) and the protective orders to place after it fills."""
    long_ = plan["direction"] == "long"
    lv = plan["levels"]
    out = []
    buf = k["limit_buffer_pct"] / 100.0
    if plan.get("futures") and plan["futures"]["lots"]:
        fu = plan["futures"]
        exp = fut_expiry(a.fut_expiry, chain)
        ts = f.kite_symbol(plan["symbol"], exp, "FUT")
        fl = lv.get("fut_levels") or lv
        px = f.round_tick(fl["entry"] * (1 + buf), 0.1, True) if long_ else f.round_tick(fl["entry"] * (1 - buf), 0.1, False)
        out.append(_order(ts, "BUY" if long_ else "SELL", fu["quantity"], px, "FUT", plan, fu["lot_size"], exp,
                          fl["stop"], fl["tp1"], fl["tp2"], fu["max_loss"],
                          "futures price" if lv.get("fut_levels") else "underlying"))
    if plan.get("option") and plan["option"]["lots"]:
        op = plan["option"]
        monthly = f.is_monthly(op["expiry"], (chain or {}).get("expiry_list") or [op["expiry"]])
        ts = f.kite_symbol(plan["symbol"], op["expiry"], op["kind"], op["strike"], monthly)
        px = f.round_tick(op["entry_premium"] * (1 + buf), 0.05, True)
        out.append(_order(ts, "BUY", op["quantity"], px, "OPT", plan, op["lot_size"], op["expiry"],
                          op["sl_premium"], op["tp1_premium"], op["tp2_premium"], op["max_loss_at_stop"], "premium"))
    return out


def fut_expiry(given, chain):
    """Futures expire monthly. Without --fut-expiry, take the monthly expiry of the chain's month (a NIFTY chain
    is often a weekly); a given date must be a monthly expiry on the chain's list when there is one."""
    elist = sorted({str(x).replace("-", "")[:8] for x in (chain or {}).get("expiry_list") or []})
    if given:
        e = str(given).replace("-", "")[:8]
        if elist and e not in elist:
            raise f.FnoError("--fut-expiry %s is not an expiry of this contract (%s)" % (e, ", ".join(elist)))
        if elist and not f.is_monthly(e, elist):
            raise f.FnoError("--fut-expiry %s is a weekly expiry; futures expire on the month's last expiry" % e)
        return e
    cexp = str((chain or {}).get("expiry") or "").replace("-", "")[:8]
    month = [x for x in elist if x[:6] == cexp[:6]]
    if cexp and month:
        return max(month)
    raise f.FnoError("which futures expiry? pass --fut-expiry YYYYMMDD (the month's last Tuesday on NSE, moved "
                     "earlier for a holiday; the option chain's expiry_list shows it)")


def _order(ts, side, qty, price, kind, plan, lot, expiry, sl, tp1, tp2, max_loss, level_on):
    plan_id = "%s-%s-%s" % (plan["symbol"], plan["bar_date"].replace("-", "")[:8], kind)
    long_ = side == "BUY"
    sl_limit = max(f.round_tick(sl * (0.99 if long_ else 1.01), 0.05, not long_), 0.05)
    return {"variety": "regular", "exchange": "NFO", "tradingsymbol": ts, "transaction_type": side, "quantity": int(qty),
            "product": "NRML", "order_type": "LIMIT", "price": price, "validity": "DAY",
            "segment": kind, "lot_size": int(lot), "expiry": str(expiry), "underlying": plan["symbol"],
            "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2), "levels_on": level_on,
            "max_loss_inr": round(max_loss, 2), "plan_id": plan_id,
            "then": [
                {"what": "stop-loss", "transaction_type": "SELL" if long_ else "BUY", "order_type": "SL",
                 "trigger_price": round(sl, 2), "price": sl_limit, "quantity": int(qty)},
                {"what": "target TP1 (part)", "transaction_type": "SELL" if long_ else "BUY", "order_type": "LIMIT",
                 "price": round(tp1, 2), "quantity": int(qty // lot // 2 * lot) or int(qty)}],
            "why": "%s view on %s (%s %s), stop %s, TP1 %s, TP2 %s (levels on the %s), max loss %s"
                   % ("bullish" if plan["direction"] == "long" else "bearish", plan["symbol"], side.lower(), ts,
                      round(sl, 2), round(tp1, 2), round(tp2, 2), level_on, f.inr(max_loss))}


def render(plan):
    lv = plan["levels"]
    L = ["%s %s plan on the %s close%s" % (plan["symbol"], plan["direction"].upper(), plan["bar_date"],
                                          "" if lv["ok"] else "  -- NOT WORTH IT: " + "; ".join(lv["notes"]))]
    L.append("  underlying: entry %s, SL %s (%s), TP1 %s (1R), TP2 %s (%.2fR), ATR %s"
             % (f.inr(lv["entry"]), f.inr(lv["stop"]), lv["stop_basis"], f.inr(lv["tp1"]), f.inr(lv["tp2"]),
                lv["reward_r"], lv["atr"]))
    if lv.get("fut_levels"):
        fl = lv["fut_levels"]
        L.append("  futures (basis %+.2f): entry %s, SL %s, TP1 %s, TP2 %s"
                 % (lv["fut_basis"], f.inr(fl["entry"]), f.inr(fl["stop"]), f.inr(fl["tp1"]), f.inr(fl["tp2"])))
    t = lv["trail"]
    L.append("  manage: at TP1 exit %d%% and move SL to entry; trail the rest %.1f x ATR from the best price, "
             "%.1f x ATR after TP2; exit at the close if TP1 is not reached in %d bars"
             % (round(100 * t["tp1_exit_fraction"]), t["after_tp1_atr"], t["after_tp2_atr"], t["time_stop_bars"]))
    L.append("  odds: with no edge, TP1 comes before SL %.0f%% of the time and TP2 %.0f%% (break-even win rates "
             "before costs)" % (100 * lv["no_edge_p_tp1"], 100 * lv["no_edge_p_tp2"]))
    if plan.get("chain"):
        ch = plan["chain"]
        L.append("  chain %s (%s days): ATM IV %s%%, 1-SD move +/-%s, PCR %s, max pain %s, OI resistance %s, support %s"
                 % (ch["expiry"], ch["days_to_expiry"], ch["atm_iv_pct"], ch["expected_move_1sd"], ch["pcr_oi"],
                    ch["max_pain"], ch["resistance"], ch["support"]))
    fu = plan.get("futures")
    if fu:
        if fu["lots"]:
            L.append("  FUTURES: %d lot(s) x %d; max loss at SL %s; reward at TP1 %s; est. margin %s (%s%% of %s)"
                     % (fu["lots"], fu["lot_size"], f.inr(fu["max_loss"]), f.inr(fu["reward_tp1"]),
                        f.inr(fu["est_margin"]), fu["margin_pct_used"], f.inr(fu["notional"])))
        else:
            L.append("  FUTURES: no size: " + fu["why_zero"])
    op = plan.get("option")
    if op:
        L.append("  OPTION: buy %s %d %s (delta %.2f, IV %.1f%%, spread %.1f%%, %s days) at %s"
                 % (op["expiry"], int(op["strike"]), op["kind"], op["delta"], op["iv_pct"], op["spread_pct"], op["dte"],
                    f.inr(op["entry_premium"])))
        L.append("          premium SL %s, TP1 %s, TP2 %s (Black-76 at the underlying's levels after %s / %s / %s days "
                 "of decay; a plain delta estimate puts the SL at %s)"
                 % (f.inr(op["sl_premium"]), f.inr(op["tp1_premium"]), f.inr(op["tp2_premium"]),
                    op["expected_days"]["sl"], op["expected_days"]["tp1"], op["expected_days"]["tp2"],
                    f.inr(op["sl_premium_delta_approx"])))
        if op["lots"]:
            L.append("          %d lot(s) x %d: max loss at SL %s, premium paid %s (the most you can lose), theta %s a "
                     "day per unit" % (op["lots"], op["lot_size"], f.inr(op["max_loss_at_stop"]), f.inr(op["premium_outlay"]),
                                       op["theta_per_day"]))
        else:
            L.append("          no size: " + op["why_zero"])
        for n in op["notes"]:
            L.append("          note: " + n)
    if plan.get("time_machine") and tm is not None:
        L.extend("  " + x for x in tm.gate_line(plan["time_machine"]))
    for o in plan["orders"]:
        if o["quantity"] // o["lot_size"] == 1:
            L.append("  note: with 1 lot of %s there is no partial exit at TP1: either exit it all at TP1, or keep it "
                     "all and only move the stop to entry there, then trail" % o["tradingsymbol"])
        L.append("  ORDER %s %d %s (%d lot%s) LIMIT %s, NRML, then SL trigger %s limit %s, TP1 LIMIT %s"
                 % (o["transaction_type"], o["quantity"], o["tradingsymbol"], o["quantity"] // o["lot_size"],
                    "" if o["quantity"] // o["lot_size"] == 1 else "s", f.inr(o["price"]), f.inr(o["then"][0]["trigger_price"]),
                    f.inr(o["then"][0]["price"]), f.inr(o["then"][1]["price"])))
    L.append("Nothing is placed: india-risk checks the entry order, india-broker asks for your yes. About 91% of "
             "individual F&O traders lost money in FY2024-25 (SEBI). Education and tooling, not financial advice.")
    return "\n".join(L)


def cmd_new(a):
    plan = build(a)
    if a.write and plan["orders"]:
        if not plan["levels"]["ok"] and not a.force:
            raise f.FnoError("the plan is flagged not worth it (%s); nothing written. --force writes it anyway"
                             % "; ".join(plan["levels"]["notes"]))
        g = plan.get("time_machine")
        if g and g["gate"] == "fail" and not a.force:
            raise f.FnoError("the Time Machine finds this trade negative after charges (%s); nothing written. "
                             "--force writes it anyway" % g["reason"])
        write_json(a.orders_out, {"created_by": "india-fno-plan", "created_at": plan["made_at"], "status": "proposed",
                                  "note": "F&O entry order(s). Place the stop-loss as soon as the entry fills.",
                                  "orders": plan["orders"]})
        book = read_json(a.plans, {"plans": []})
        for o in plan["orders"]:
            book["plans"] = [p for p in book["plans"] if p["id"] != o["plan_id"]]
            book["plans"].append({"id": o["plan_id"], "symbol": plan["symbol"], "tradingsymbol": o["tradingsymbol"],
                                  "direction": plan["direction"], "segment": o["segment"], "levels_on": o["levels_on"],
                                  "entry": plan["levels"]["entry"] if o["levels_on"] == "underlying" else o["price"],
                                  "underlying_entry": plan["levels"]["entry"], "stop": plan["levels"]["stop"],
                                  "tp1": plan["levels"]["tp1"], "tp2": plan["levels"]["tp2"],
                                  "premium": {"entry": o["price"], "sl": o["sl"], "tp1": o["tp1"], "tp2": o["tp2"]}
                                  if o["segment"] == "OPT" else None, "quantity": o["quantity"], "lot_size": o["lot_size"],
                                  "from_date": plan["bar_date"], "stage": "planned", "status": "open",
                                  "expiry": o["expiry"],
                                  "best": plan["levels"]["entry"], "bars_held": 0})
        write_json(a.plans, book)
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(render(plan))
        if a.write and plan["orders"]:
            print("Wrote %d entry order(s) to %s and the plan to %s." % (len(plan["orders"]), a.orders_out, a.plans))
    return 0 if plan["levels"]["ok"] else 1


def cmd_trail(a):
    book = read_json(a.plans, {"plans": []})
    p = next((x for x in book["plans"] if x["id"] == a.id), None)
    if not p:
        raise f.FnoError("no plan %s in %s" % (a.id, a.plans))
    if p.get("status") != "open":
        raise f.FnoError("plan %s is %s" % (a.id, p.get("status")))
    c = f.load_candles(a.candles)
    cfg = load_cfg(a)
    ind = f.indicators(c, cfg)
    start = a.since or p.get("last_bar") or p["from_date"]
    idx = [n for n, b in enumerate(c) if b["date"] > start]
    if not idx:
        print("No bars after %s: nothing to update. Stop stays at %s." % (start, f.inr(p["stop"])))
        return 1
    exp = p.get("expiry")
    exp_day = "%s-%s-%s" % (exp[:4], exp[4:6], exp[6:8]) if exp and len(exp) >= 8 else None
    pos = {"direction": p["direction"], "entry": p["underlying_entry"], "stop": p["stop"], "tp1": p["tp1"],
           "tp2": p["tp2"], "best": p.get("best", p["underlying_entry"]), "stage": "open" if p["stage"] in ("planned", "open")
           else p["stage"], "bars_held": p.get("bars_held", 0)}
    events = []
    last = None
    for n in idx:
        if exp_day and c[n]["date"][:10] > exp_day:
            break
        # each bar is judged with the ATR known before it opened
        pos, ev = f.trail(pos, [c[n]], ind["atr"][n - 1] or ind["atr"][n], cfg)
        events += ev
        last = c[n]
        if pos.get("closed"):
            break
    if last is None:
        p.update(status="expired", exit_date=exp_day)
        write_json(a.plans, book)
        print("The contract expired on %s, before any new bar: plan %s marked expired." % (exp_day, p["id"]))
        return 1
    p.update(stop=pos["stop"], best=pos["best"], stage=pos["stage"], bars_held=pos["bars_held"], last_bar=last["date"])
    if pos.get("closed"):
        p.update(status="exit signalled", exit_hint=pos["exit"], exit_date=pos["exit_date"])
    elif exp_day and last["date"][:10] >= exp_day:
        events.append({"date": exp_day, "event": "EXPIRED: the contract settled at the close; roll to the next "
                                                 "month only as a new decision", "price": last["close"]})
        p.update(status="expired", exit_hint=last["close"], exit_date=exp_day)
        pos["closed"] = True
    elif exp_day and p["symbol"] not in f.INDEX_LOTS:
        days = (datetime.date.fromisoformat(exp_day) - datetime.date.fromisoformat(last["date"][:10])).days
        if days <= 4:
            events.append({"date": last["date"], "event": "expiry on %s: stock F&O is settled by DELIVERY; an "
                                                          "open position or in-the-money option becomes shares to "
                                                          "buy or deliver. Exit or roll before then." % exp_day,
                           "price": last["close"]})
    write_json(a.plans, book)
    for e in events:
        print("%s  %s  (%s)" % (e["date"], e["event"], f.inr(e["price"])))
    print("Plan %s: stage %s, stop now %s on the underlying%s." % (p["id"], p["stage"], f.inr(p["stop"]),
                                                                    ", EXIT SIGNALLED" if pos.get("closed") else ""))
    if p["segment"] == "OPT" and not pos.get("closed"):
        print("For the option, move the SL trigger to the premium the option shows when the underlying is at %s "
              "(plan.py does that with a fresh chain: re-run `new` for the levels, or use delta x the move)."
              % f.inr(p["stop"]))
    if not pos.get("closed"):
        print("Modify your SL order to the new stop. Nothing has been sent to a broker.")
    return 0


def cmd_list(a):
    book = read_json(a.plans, {"plans": []})
    rows = [p for p in book["plans"] if a.all or p.get("status") == "open"]
    if not rows:
        print("No open plans.")
        return 1
    for p in rows:
        print("%-26s %-22s %-5s qty %-5d stage %-8s stop %s  TP1 %s  TP2 %s  [%s]"
              % (p["id"], p["tradingsymbol"], p["direction"], p["quantity"], p["stage"], f.inr(p["stop"]),
                 f.inr(p["tp1"]), f.inr(p["tp2"]), p["status"]))
    return 0


def cmd_close(a):
    book = read_json(a.plans, {"plans": []})
    p = next((x for x in book["plans"] if x["id"] == a.id), None)
    if not p:
        raise f.FnoError("no plan %s" % a.id)
    p.update(status="closed", closed_at=datetime.datetime.now().replace(microsecond=0).isoformat(),
             close_price=a.price, close_reason=a.reason or "")
    write_json(a.plans, book)
    print("Closed %s at %s. Record the fills with india-broker (ticket.py fill) so the F&O journal is right."
          % (a.id, f.inr(a.price)))
    return 0


def cmd_journal(a):
    j = read_json(a.journal, None)
    if not j:
        raise f.FnoError("no F&O journal at %s yet (india-broker writes it when F&O orders fill)" % a.journal)
    fy = a.fy
    if not fy:
        t = datetime.date.today()
        fy = "%d-04-01" % (t.year if t.month >= 4 else t.year - 1)
    elif len(fy) == 4:
        fy = "%s-04-01" % fy
    s_ = f.journal_summary(j, fy)
    if a.json:
        print(json.dumps(dict(s_, fy_start=fy), indent=2, ensure_ascii=False))
        return 0
    print("F&O, financial year from %s: %d square-off(s), gross P&L %s, charges %s, net %s"
          % (fy, s_["square_offs"], f.inr(s_["gross_pnl"]), f.inr(s_["charges"]), f.inr(s_["net_pnl"])))
    print("Turnover for tax-audit purposes (sum of absolute P&L per square-off, ICAI Guidance Note): %s"
          % f.inr(s_["turnover"]))
    for ts, p in s_["open_positions"].items():
        print("  open: %s %+d at %s" % (ts, p["qty"], f.inr(p["avg"])))
    print("F&O results are non-speculative business income, taxed at your slab rate in ITR-3; a loss can be set "
          "off against other income except salary and carried forward 8 years against business income. Charges "
          "and STT paid on these trades are business expenses. Not tax advice: confirm with a chartered accountant, "
          "including whether a tax audit applies to you.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan an F&O trade with SL, TP and a trailing stop (india-fno-plan).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("new", help="plan a trade")
    p.add_argument("symbol")
    p.add_argument("--candles", required=True)
    p.add_argument("--direction", required=True, choices=["long", "short"])
    p.add_argument("--instrument", default="both", choices=["future", "option", "both"])
    p.add_argument("--chain", help="option chain JSON (INDmoney or NSE)")
    p.add_argument("--entry", type=float, help="planned entry on the underlying (default: the last close)")
    p.add_argument("--lot", type=int)
    p.add_argument("--fut-expiry", help="futures expiry YYYYMMDD (default: the monthly expiry of the chain's month)")
    p.add_argument("--fut-price", type=float, help="the futures' last price: shifts the futures order's levels by "
                                                   "the basis to the underlying")
    p.add_argument("--capital", type=float)
    p.add_argument("--risk-pct", type=float)
    p.add_argument("--now", help="ISO time for the option clock (no offset = IST); default: the chain's price time")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--write", action="store_true")
    p.add_argument("--force", action="store_true", help="write even a plan flagged not worth it")
    p.add_argument("--orders-out", default="trader/orders.json")
    p.add_argument("--plans", default="trader/fno/plans.json")
    p.add_argument("--no-timemachine", action="store_true",
                   help="skip the Time Machine gate (a plan it finds negative is otherwise not written without --force)")
    p.add_argument("--tm-paths", type=int, default=4000, help="paths per method for the gate")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("trail", help="advance an open plan through new bars")
    p.add_argument("id")
    p.add_argument("--candles", required=True)
    p.add_argument("--since", help="only bars after this date")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--plans", default="trader/fno/plans.json")
    p = sub.add_parser("list", help="open plans")
    p.add_argument("--all", action="store_true")
    p.add_argument("--plans", default="trader/fno/plans.json")
    p = sub.add_parser("close", help="mark a plan closed")
    p.add_argument("id")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--reason")
    p.add_argument("--plans", default="trader/fno/plans.json")
    p = sub.add_parser("journal", help="F&O P&L, charges and turnover for a financial year")
    p.add_argument("--fy", help="the year the FY starts, e.g. 2026 for FY 2026-27")
    p.add_argument("--journal", default="trader/fno/journal.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"new": cmd_new, "trail": cmd_trail, "list": cmd_list, "close": cmd_close,
                "journal": cmd_journal}[a.cmd](a)
    except (f.FnoError, ValueError) as e:
        print("plan.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
