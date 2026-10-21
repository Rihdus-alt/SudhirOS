#!/usr/bin/env python3
"""Rebalancer for india-rebalance. Standard library only, no network.

Compares what you hold (trader/portfolio.json at trader/prices.json) with
your target weights (trader/targets.json, capped by your rules) and proposes
the whole-share trades that bring every stock outside the band back to its
target, with the charges counted.

    python3 rebalance.py                      # report only
    python3 rebalance.py --band 5 --write     # write trader/orders.json (proposals)
    python3 rebalance.py --sell-untracked     # also sell holdings that are not in the targets
    python3 rebalance.py --json

The rules:
  value         account = cash + every holding at its price
  targets       weights renormalised to 100 over the stock sleeve, which is the
                account minus your cash buffer; no stock over max_position_pct
                of the account (the excess is shared out, like india-budget)
  drift         current % of the account minus target %, in percentage points
  band          only stocks drifting MORE than the band trade (default 5
                points, or "rebalance_band_pct" in the config); a trade takes
                the stock to the whole number of shares closest to its target
  sells first   sells are listed before buys, so the cash is there (Indian
                delivery sales credit 80% of the proceeds for new buys the
                same day and the rest after settlement on T+1; most brokers
                apply this automatically)
  cash buffer   buys only spend cash above the buffer, after charges; if there
                is not enough, every buy is scaled down by the same share
  costs         a trade whose charges are over max_cost_pct of its value
                (default 1%) is skipped as not worth it
  min / max     trades under min_order_inr are skipped; a trade over
                max_order_inr is cut to the max (run it again after it fills)
  untracked     holdings not in the targets are left alone and listed, unless
                --sell-untracked

Exit code 0 when there are trades to propose, 1 when nothing is outside the band.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys

DEFAULTS = {"cash_buffer_pct": 5, "max_position_pct": 25, "max_order_inr": 50000, "min_order_inr": 500,
            "blocked": [], "rebalance_band_pct": 5, "paper": True, "exchange": "NSE", "max_cost_pct": 1.0,
            "order_type": "LIMIT", "limit_buffer_pct": 0.5}
EPS = 1e-9


class RebalanceError(Exception):
    pass


def _load(folder, name):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), folder, name + ".py")
    if not os.path.exists(path):
        raise RebalanceError("%s/%s.py not found next to india-rebalance: install every india-* folder"
                             % (folder, name))
    spec = importlib.util.spec_from_file_location("india_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = _load("india-costs", "costs")
sizing = _load("india-budget", "size")
inr = costs.inr


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RebalanceError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RebalanceError("%s is not valid JSON: %s" % (path, e))


def norm(sym):
    s = str(sym or "").strip().upper()
    return s[4:] if s.startswith("NSE:") else s


def parse_targets(doc, blocked=()):
    rows = doc.get("weights", doc) if isinstance(doc, dict) else doc
    out = {}
    if isinstance(rows, dict):
        rows = [{"ticker": k, "weight": v} for k, v in rows.items()]
    blocked = {norm(b) for b in blocked or []}
    for r in rows or []:
        t = norm(r.get("ticker") or r.get("symbol"))
        w = float(r.get("weight", 0) or 0)
        if t and w > 0 and t not in blocked:
            out[t] = out.get(t, 0.0) + w
    tot = sum(out.values())
    if not tot:
        raise RebalanceError("no target weights (run india-copycat, or write trader/targets.json)")
    return {t: 100.0 * w / tot for t, w in out.items()}


def capped_targets(weights, sleeve, cap):
    alloc, active, money_left = {}, dict(weights), sleeve
    while active:
        wsum = sum(active.values())
        trial = {t: money_left * w / wsum for t, w in active.items()}
        over = [t for t, v in trial.items() if v > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for t in over:
            alloc[t] = cap
            money_left -= cap
            del active[t]
        if money_left <= EPS:
            for t in active:
                alloc[t] = 0.0
            break
    return alloc


def holdings_of(portfolio):
    out = {}
    for p in (portfolio or {}).get("positions", []):
        t = norm(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not t:
            continue
        n = p.get("shares", p.get("quantity"))
        if n is None:
            n = sum(float(l.get("shares", 0)) for l in p.get("lots", []))
        out[t] = out.get(t, 0) + int(round(float(n)))
    return out


def plan(portfolio, prices, weights, cfg, band=None, sell_untracked=False, details=None):
    """Pure function: the drift table and the proposed whole-share trades."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    rates = costs.rates_from(c)
    details = details or {}
    exch = str(c.get("exchange", "NSE")).upper()
    band = float(c["rebalance_band_pct"] if band is None else band)
    cash = float((portfolio or {}).get("cash", 0) or 0)
    held = holdings_of(portfolio)
    need = sorted(set(held) | set(weights), key=lambda t: (-weights.get(t, 0.0), t))
    missing = [t for t in need if not prices.get(t)]
    if missing:
        raise RebalanceError("no price for %s: refresh trader/prices.json (india-alerts: alerts.py quote %s --write)"
                             % (", ".join(missing), " ".join(missing)))
    value = {t: held.get(t, 0) * prices[t] for t in need}
    total = cash + sum(value.values())
    if total <= 0:
        raise RebalanceError("the account is empty: nothing to rebalance (india-budget sizes a first buy)")
    buf = total * float(c["cash_buffer_pct"]) / 100.0
    cap = total * float(c["max_position_pct"]) / 100.0
    target = capped_targets(weights, total - buf, cap)
    min_o, max_o = float(c.get("min_order_inr") or 0), float(c.get("max_order_inr") or 0)
    max_cost = float(c.get("max_cost_pct") or 0)
    limit = str(c.get("order_type", "LIMIT")).upper() == "LIMIT"
    lbuf = float(c.get("limit_buffer_pct") or 0)

    rows, sells, buys, notes = [], [], [], []
    for t in need:
        tracked = t in weights
        tgt = target.get(t, 0.0)
        cur_pct = 100.0 * value[t] / total
        tgt_pct = 100.0 * tgt / total
        drift = cur_pct - tgt_pct
        row = {"ticker": t, "shares": held.get(t, 0), "price": prices[t], "value": round(value[t], 2),
               "current_pct": round(cur_pct, 2), "target_pct": round(tgt_pct, 2),
               "drift": round(drift, 2) if abs(drift) >= 0.005 else 0.0, "target_inr": round(tgt, 2),
               "tracked": tracked, "action": "hold"}
        rows.append(row)
        if not tracked:
            if sell_untracked and held.get(t, 0) > 0:
                sells.append({"ticker": t, "qty": held[t], "why": "not in your targets; selling the whole position "
                                                                  "(--sell-untracked)"})
                row["action"] = "sell all"
            else:
                row["action"] = "untracked, left alone"
            continue
        if abs(drift) <= band + EPS:
            continue
        want = int(math.floor(min(tgt, cap) / prices[t] + 0.5))
        while want * prices[t] > cap + EPS and want > 0:
            want -= 1
        delta = want - held.get(t, 0)
        why = "drifted %+.1f points from its %.1f%% target (band %s)" % (drift, tgt_pct, _n(band))
        if delta < 0:
            sells.append({"ticker": t, "qty": -delta, "why": why})
            row["action"] = "sell"
        elif delta > 0:
            buys.append({"ticker": t, "qty": delta, "why": why})
            row["action"] = "buy"
        else:
            row["action"] = "hold (one share would overshoot)"

    def worth(t, q, side):
        v = q * prices[t]
        ch = costs.trade_costs(side, v, exch, "CNC", rates)["total"]
        return v, ch

    orders = []
    cash_after = cash
    for s in sells:
        t, q = s["ticker"], s["qty"]
        if max_o and q * prices[t] > max_o + EPS:
            q = int(max_o // prices[t])
            notes.append("%s sell cut to your %s max order; run again after it fills" % (t, inr(max_o)))
        v, ch = worth(t, q, "sell")
        if q <= 0 or v < min_o - EPS:
            notes.append("%s sell of %s is under your %s minimum order: skipped" % (t, inr(v), inr(min_o)))
            continue
        if max_cost and 100.0 * ch / v > max_cost:
            notes.append("%s sell of %s costs %s in charges (over %s%%): skipped" % (t, inr(v), inr(ch), _n(max_cost)))
            continue
        cash_after += v - ch
        o = {"variety": "regular", "exchange": exch, "tradingsymbol": t, "transaction_type": "SELL", "quantity": q,
             "product": "CNC", "order_type": "LIMIT" if limit else "MARKET", "validity": "DAY"}
        if limit:
            o["price"] = sizing.limit_price(prices[t], "sell", lbuf, details.get(t))
        o.update({"why": s["why"], "_inr": round(v, 2), "_charges": round(ch, 2)})
        orders.append(o)

    spendable = max(cash_after - buf, 0.0)
    want_total = sum(worth(b["ticker"], b["qty"], "buy")[0] * (1 + lbuf / 100.0) + worth(b["ticker"], b["qty"], "buy")[1]
                     for b in buys)
    scale = 1.0
    if want_total > spendable + EPS:
        scale = spendable / want_total if want_total else 0.0
        notes.append("buys scaled to %.0f%%: only %s is free above your %s cash buffer"
                     % (100 * scale, inr(spendable), inr(buf)))
    for b in buys:
        t = b["ticker"]
        q = int(math.floor(b["qty"] * scale + EPS))
        if max_o and q * prices[t] > max_o + EPS:
            q = int(max_o // prices[t])
            notes.append("%s buy cut to your %s max order; run again after it fills" % (t, inr(max_o)))
        v, ch = worth(t, q, "buy")
        if q < 1 or v < min_o - EPS:
            notes.append("%s buy of %s does not reach one share or your %s minimum: skipped" % (t, inr(v), inr(min_o)))
            continue
        if max_cost and 100.0 * ch / v > max_cost:
            notes.append("%s buy of %s costs %s in charges (over %s%%): skipped" % (t, inr(v), inr(ch), _n(max_cost)))
            continue
        o = {"variety": "regular", "exchange": exch, "tradingsymbol": t, "transaction_type": "BUY", "quantity": q,
             "product": "CNC", "order_type": "LIMIT" if limit else "MARKET", "validity": "DAY"}
        if limit:
            o["price"] = sizing.limit_price(prices[t], "buy", lbuf, details.get(t))
        o.update({"why": b["why"], "_inr": round(v, 2), "_charges": round(ch, 2)})
        orders.append(o)

    spent = sum(o["_inr"] + o["_charges"] for o in orders if o["transaction_type"] == "BUY")
    return {"account_value": round(total, 2), "cash": round(cash, 2), "cash_buffer": round(buf, 2),
            "cap": round(cap, 2), "band": band, "rows": rows, "orders": orders, "notes": notes,
            "est_charges": round(sum(o["_charges"] for o in orders), 2),
            "cash_after": round(cash_after - spent, 2)}


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def clean(orders):
    return [{k: v for k, v in o.items() if not k.startswith("_")} for o in orders]


def report(res):
    lines = ["Account %s (cash %s, buffer %s, cap %s per stock), band %s points"
             % (inr(res["account_value"]), inr(res["cash"]), inr(res["cash_buffer"]), inr(res["cap"]), _n(res["band"])),
             ""]
    lines.append("%-12s %7s %11s %13s %8s %9s %8s  %s" % ("symbol", "shares", "price", "value", "now %", "target %",
                                                        "drift", "action"))
    for r in res["rows"]:
        lines.append("%-12s %7d %11s %13s %7.2f%% %8.2f%% %+8.2f  %s"
                     % (r["ticker"], r["shares"], inr(r["price"]), inr(r["value"]), r["current_pct"],
                        r["target_pct"], r["drift"], r["action"]))
    lines.append("")
    if res["orders"]:
        lines.append("Proposed orders, sells first (nothing has been placed):")
        for o in res["orders"]:
            lines.append("  %s %d x %s:%s, %s%s, about %s + %s charges"
                         % (o["transaction_type"], o["quantity"], o["exchange"], o["tradingsymbol"], o["order_type"],
                            (" at " + inr(o["price"])) if "price" in o else "", inr(o["_inr"]), inr(o["_charges"])))
        lines.append("Cash after, if all of them fill: about %s." % inr(res["cash_after"]))
    else:
        lines.append("Nothing is outside the band. No trades.")
    for n in res["notes"]:
        lines.append("Note: " + n)
    lines.append("")
    lines.append("Selling can realize a taxable gain: check india-tax (lots.py sell) first. Then india-risk checks "
                 "every order and india-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bring the portfolio back to its target weights (india-rebalance).")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--targets", default="trader/targets.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--band", type=float)
    ap.add_argument("--sell-untracked", action="store_true")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio (your holdings and cash)")
        pdoc = read_json(a.prices, "prices (india-alerts: alerts.py quote --holdings --write)")
        prices = {norm(k): float(v) for k, v in (pdoc.get("prices", pdoc) or {}).items() if isinstance(v, (int, float))}
        weights = parse_targets(read_json(a.targets, "targets"), cfg.get("blocked"))
        res = plan(portfolio, prices, weights, cfg, a.band, a.sell_untracked,
                   {norm(k): v for k, v in (pdoc.get("detail") or {}).items()})
        if a.write and res["orders"]:
            d = os.path.dirname(a.write)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(a.write, "w", encoding="utf-8") as fh:
                json.dump({"created_by": "india-rebalance",
                           "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
                           "status": "proposed", "paper": bool(cfg.get("paper", True)), "currency": "INR",
                           "note": ("Proposals only, sells first. Nothing is placed until india-risk passes each "
                                    "order and you say yes to it in the chat."),
                           "prices_as_of": pdoc.get("as_of", ""), "orders": clean(res["orders"])}, fh, indent=2,
                          ensure_ascii=False)
                fh.write("\n")
    except RebalanceError as e:
        print("rebalance.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        out = dict(res)
        out["orders"] = clean(res["orders"])
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(report(res))
        if a.write and res["orders"]:
            print("Wrote %d proposed order(s) to %s." % (len(res["orders"]), a.write))
    return 0 if res["orders"] else 1


if __name__ == "__main__":
    sys.exit(main())
