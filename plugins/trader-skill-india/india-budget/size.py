#!/usr/bin/env python3
"""Position sizing in whole shares for india-budget. Standard library only, no network.

Turns target weights (the portfolio you want to copy) and your rules (budget,
cash buffer, a cap on any one stock, a minimum order) into whole-share
quantities per stock, then into proposed CNC (delivery) orders in the shape
Indian broker APIs take (Kite's place_order field names). It never places
anything: it writes proposals to trader/orders.json, which india-risk checks
and india-broker shows you one by one.

    python3 size.py                                    # trader/targets.json + trader/config.json + trader/prices.json
    python3 size.py --budget 100000 --write            # write trader/orders.json
    python3 size.py --weights HDFCBANK=7.8,ICICIBANK=5.2,M\\&M=3.9 --prices trader/prices.json
    python3 size.py --fill                             # spend leftover cash even past a target
    python3 size.py --json

Indian exchanges trade whole shares only, so the maths is integer:

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   symbols dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   the excess is shared out by weight until nobody is over
  4. shares        each stock gets the whole number of shares closest to its
                   rupee target (nearest, not floor), then shares are taken
                   back one at a time, where it costs the least accuracy,
                   until everything fits the money: every buy is costed at
                   its worst-case price (the limit, or price + slippage) plus
                   STT, stamp duty, exchange, SEBI and GST charges
  5. drop          a stock that cannot get one share, or whose order is under
                   min_order_inr, is dropped and its weight shared out
  6. --fill        optional: spend what is left one share at a time on the
                   stock furthest below its target, while it fits

The result reports the tracking error (how far the whole-share portfolio is
from the target weights) so you can see what whole shares cost in accuracy.
Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys

DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 5,
    "max_position_pct": 25,
    "max_order_inr": 50000,
    "min_order_inr": 500,
    "exchange": "NSE",
    "product": "CNC",
    "order_type": "LIMIT",
    "limit_buffer_pct": 0.5,
    "market_slippage_pct": 0.5,
    "broker": "manual",
    "paper": True,
    "blocked": [],
}
EPS = 1e-9


class SizeError(Exception):
    pass


def _load_costs():
    """india-costs/costs.py, from the folder next to this skill."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-costs", "costs.py")
    if not os.path.exists(path):
        raise SizeError("india-costs/costs.py not found next to india-budget: install every india-* folder")
    spec = importlib.util.spec_from_file_location("india_costs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = _load_costs()


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def norm(sym):
    s = str(sym).strip().upper()
    return s[4:] if s.startswith("NSE:") else s


def parse_weights(data):
    """targets.json, a {symbol: weight} dict or a list of rows -> [{ticker, name, weight}]."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        rows = [{"ticker": t, "name": "", "weight": w} for t, w in data.items()]
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol") or r.get("tradingsymbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a symbol and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": norm(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.rsplit("=", 1)
        out[norm(t)] = w.strip()
    return out


def load_prices(path):
    """-> ({SYMBOL: price}, {SYMBOL: detail}, as_of)"""
    if not path:
        return {}, {}, None
    data = read_json(path, "prices")
    raw = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in raw.items():
        try:
            out[norm(t)] = float(p)
        except (TypeError, ValueError):
            continue
    detail = {norm(k): v for k, v in (data.get("detail") or {}).items()} if isinstance(data, dict) else {}
    return out, detail, (data.get("as_of") if isinstance(data, dict) else None)


# ---------------------------------------------------------------- the maths

def normalise(rows, blocked=(), top=None):
    blocked = {norm(b) for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact rupee targets with the cap enforced (excess shared out by weight)."""
    alloc, capped = {}, set()
    active = list(rows)
    money = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def conservative_tick(price):
    """A price step that is a multiple of NSE's tick at this price under both the
    flat 5-paise rule and the price-tiered ticks, so a limit price on it is valid."""
    for limit, tick in ((1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00)):
        if price < limit:
            return tick
    return 5.00


def limit_price(price, side, buffer_pct, detail=None, tick=None):
    """A marketable limit: price +/- buffer, on the tick grid, inside the circuit band."""
    tick = tick or (detail or {}).get("tick_size") or conservative_tick(price)
    raw = price * (1 + buffer_pct / 100.0) if side == "buy" else price * (1 - buffer_pct / 100.0)
    steps = raw / tick
    steps = math.floor(steps + EPS) if side == "buy" else math.ceil(steps - EPS)
    lp = round(steps * tick, 2)
    up, lo = (detail or {}).get("upper_circuit"), (detail or {}).get("lower_circuit")
    if side == "buy" and up and lp > up:
        lp = float(up)
    if side == "sell" and lo and lp < lo:
        lp = float(lo)
    return lp


class Coster(object):
    """Worst-case rupees needed to buy q shares of one stock: shares at the
    limit (or price + slippage) plus every charge on that order."""

    def __init__(self, cfg, prices, details):
        self.rates = costs.rates_from(cfg)
        self.exchange = str(cfg.get("exchange", "NSE")).upper()
        self.product = str(cfg.get("product", "CNC")).upper()
        self.limit = str(cfg.get("order_type", "LIMIT")).upper() == "LIMIT"
        self.buf = float(cfg.get("limit_buffer_pct", 0.5) if self.limit else cfg.get("market_slippage_pct", 0.5))
        self.prices, self.details = prices, details
        self.lp = {t: limit_price(p, "buy", self.buf, details.get(t)) for t, p in prices.items()} if self.limit else {}

    def worst_price(self, t):
        return self.lp[t] if self.limit else self.prices[t] * (1 + self.buf / 100.0)

    def need(self, t, q):
        if q <= 0:
            return 0.0
        v = q * self.worst_price(t)
        return v + costs.trade_costs("buy", v, self.exchange, self.product, self.rates)["total"]

    def charges(self, t, q, at=None):
        if q <= 0:
            return 0.0
        v = q * (at if at is not None else self.prices[t])
        return costs.trade_costs("buy", v, self.exchange, self.product, self.rates)["total"]


def integer_shares(targets, prices, cap, money, coster, order, fill=False):
    """Whole shares closest to the rupee targets that fit in `money` (worst case, with charges)."""
    q = {}
    for t in order:
        tgt = min(targets[t], cap)
        n = int(math.floor(tgt / prices[t] + 0.5 + EPS)) if tgt > 0 else 0
        while n > 0 and n * prices[t] > cap + EPS:
            n -= 1
        q[t] = n

    def total():
        return sum(coster.need(t, q[t]) for t in order)

    def err_delta(t, d):
        g = targets[t] - q[t] * prices[t]
        return (g - d * prices[t]) ** 2 - g ** 2

    # take back shares, cheapest in accuracy first, until it fits
    guard = 0
    while total() > money + EPS and guard < 100000:
        cands = [t for t in order if q[t] > 0]
        if not cands:
            break
        t = min(cands, key=lambda t: (err_delta(t, -1) / prices[t], -prices[t]))
        q[t] -= 1
        guard += 1
    # then add shares that make it MORE accurate, while they fit
    improved = True
    while improved:
        improved = False
        best = None
        for t in order:
            if (q[t] + 1) * prices[t] > cap + EPS:
                continue
            d = err_delta(t, +1)
            if d >= -EPS:
                continue
            q[t] += 1
            fits = total() <= money + EPS
            q[t] -= 1
            if fits and (best is None or d < best[1]):
                best = (t, d)
        if best:
            q[best[0]] += 1
            improved = True
    if fill:
        while True:
            best, gap = None, None
            for t in order:
                if (q[t] + 1) * prices[t] > cap + EPS:
                    continue
                q[t] += 1
                fits = total() <= money + EPS
                q[t] -= 1
                g = (targets[t] - q[t] * prices[t]) / prices[t]
                if fits and (best is None or g > gap + EPS):
                    best, gap = t, g
            if best is None:
                break
            q[best] += 1
    return q


def size(rows, cfg, budget=None, prices=None, details=None, top=None, fill=False):
    """Pure function: weights + rules + prices -> the full sizing result (a dict)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct", 5))
    cap_pct = float(c.get("max_position_pct", 25))
    min_order = float(c.get("min_order_inr", 0) or 0)
    max_order = float(c.get("max_order_inr", 0) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = {norm(k): float(v) for k, v in (prices or {}).items() if v}
    details = {norm(k): v for k, v in (details or {}).items()}

    keep, dropped = normalise(rows, c.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")
    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (india-alerts: alerts.py quote %s --write, "
                        "or import the INDmoney connector's quotes)" % (", ".join(missing), " ".join(missing)))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = budget * cap_pct / 100.0
    if max_order:
        cap = min(cap, max_order)
    coster = Coster(c, prices, details)

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        shares = integer_shares(exact, prices, cap, investable, coster, order, fill)
        small = []
        for r in live:
            t = r["ticker"]
            if shares[t] == 0:
                if prices[t] > cap + EPS:
                    why = ("one share costs %s, more than the %s cap per stock"
                           % (costs.inr(prices[t]), costs.inr(cap)))
                else:
                    why = ("one share costs %s; its %s slice rounds to zero whole shares"
                           % (costs.inr(prices[t]), costs.inr(exact[t])))
                small.append((r, why))
            elif shares[t] * prices[t] < min_order - EPS:
                small.append((r, "its order (%s) is under your %s minimum"
                              % (costs.inr(shares[t] * prices[t]), costs.inr(min_order))))
        if not small:
            break
        worst, why = min(small, key=lambda x: (x[0]["source_weight"], x[0]["ticker"]))
        dropped.append({"ticker": worst["ticker"], "reason": why})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("no stock can be bought in whole shares inside these rules; raise the budget, "
                            "the cap, or choose lower-priced stocks")

    positions, charges_total, need_total = [], 0.0, 0.0
    invested_ref = sum(shares[t] * prices[t] for t in shares)
    sleeve = sum(exact.values())
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        value = round(shares[t] * prices[t], 2)
        ch = coster.charges(t, shares[t], coster.worst_price(t))
        need = coster.need(t, shares[t])
        charges_total += ch
        need_total += need
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2), "source_weight": r["source_weight"],
             "target_pct": round(100.0 * exact[t] / sleeve, 2) if sleeve else 0.0,
             "target_inr": round(exact[t], 2), "price": prices[t], "shares": shares[t], "value": value,
             "actual_weight": round(100.0 * value / invested_ref, 2) if invested_ref else 0.0,
             "est_charges": round(ch, 2), "max_cash_needed": round(need, 2), "capped": t in capped}
        if coster.limit:
            p["limit_price"] = coster.lp[t]
        d = details.get(t) or {}
        if d.get("upper_circuit") and prices[t] >= float(d["upper_circuit"]) - EPS:
            p["note"] = "at its upper circuit: a buy may not fill today"
        positions.append(p)
    tracking = round(sum(abs(p["actual_weight"] - p["target_pct"]) for p in positions) / 2.0, 2)
    invested = round(invested_ref, 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "investable": investable,
        "max_position_pct": cap_pct, "cap": round(cap, 2), "min_order_inr": min_order,
        "order_type": "LIMIT" if coster.limit else "MARKET", "buffer_pct": coster.buf,
        "positions": positions, "invested": invested, "est_charges": round(charges_total, 2),
        "max_cash_needed": round(need_total, 2), "idle": round(investable - invested - charges_total, 2),
        "tracking_error_pct": tracking, "dropped": dropped, "fill": bool(fill),
    }


# ---------------------------------------------------------------- orders

def orders_from(result, cfg, source=""):
    """Proposed CNC buy orders, one per position, with Kite place_order field names."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        why = "%d shares, %.2f%% of the stock sleeve (target %.2f%%)" % (p["shares"], p["actual_weight"], p["target_pct"])
        if source:
            why = "%s, copying %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_pct(result["max_position_pct"]), costs.inr(result["budget"]))
        o = {"variety": "regular", "exchange": str(c.get("exchange", "NSE")).upper(), "tradingsymbol": p["ticker"],
             "transaction_type": "BUY", "quantity": int(p["shares"]), "product": str(c.get("product", "CNC")).upper(),
             "order_type": result["order_type"], "validity": "DAY"}
        if result["order_type"] == "LIMIT":
            o["price"] = p["limit_price"]
        o["why"] = why
        out.append(o)
    return out


def write_orders(path, orders, result, cfg, source="", prices_as_of=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "india-budget",
        "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "currency": "INR",
        "note": ("Proposals only. Nothing is placed until india-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "prices_as_of": prices_as_of,
        "orders": orders,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------- report

def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def report(result, orders, source="", prices_as_of=None):
    r = result
    inr = costs.inr
    lines = []
    if source:
        lines.append("Copying: %s" % source)
    if prices_as_of:
        lines.append("Prices as of %s." % prices_as_of)
    lines.append("Budget %s, cash buffer %s%% = %s, investing up to %s, cap %s per stock"
                 % (inr(r["budget"]), _pct(r["cash_buffer_pct"]), inr(r["cash_buffer"]),
                    inr(r["investable"]), inr(r["cap"])))
    lines.append("")
    lines.append("%-12s %8s %8s %8s %11s %7s %12s %8s %11s" % ("symbol", "weight", "target", "actual", "price",
                                                              "shares", "value", "charges", "limit"))
    for p in r["positions"]:
        row = "%-12s %7.2f%% %7.2f%% %7.2f%% %11s %7d %12s %8s %11s" % (
            p["ticker"], p["weight"], p["target_pct"], p["actual_weight"], inr(p["price"]), p["shares"], inr(p["value"]),
            inr(p["est_charges"]), inr(p["limit_price"]) if "limit_price" in p else "market")
        if p["capped"]:
            row += "  capped"
        if p.get("note"):
            row += "  " + p["note"]
        lines.append(row)
    lines.append("")
    lines.append("Invested %s at these prices, est. charges %s, cash buffer %s, left idle about %s."
                 % (inr(r["invested"]), inr(r["est_charges"]), inr(r["cash_buffer"]), inr(max(r["idle"], 0))))
    lines.append("Worst case cash needed (every buy fills at its %s): %s of the %s investable."
                 % ("limit" if r["order_type"] == "LIMIT" else "price + %s%% slippage" % _pct(r["buffer_pct"]),
                    inr(r["max_cash_needed"]), inr(r["investable"])))
    lines.append("weight = the copied weight; target = after the cap; actual = what whole shares buy.")
    lines.append("Tracking error from whole shares: %.2f%% of the sleeve is off its target." % r["tracking_error_pct"])
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (nothing has been placed):")
    for o in orders:
        lines.append("  %s %s %d x %s:%s, %s%s, %s" % (o["transaction_type"], o["product"], o["quantity"], o["exchange"],
                                                   o["tradingsymbol"], o["order_type"],
                                                   (" at " + inr(o["price"])) if "price" in o else "", o["validity"]))
    lines.append("")
    lines.append("Next: india-risk checks every order, then india-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size positions to your budget in whole shares (india-budget).")
    ap.add_argument("--targets", default="trader/targets.json")
    ap.add_argument("--weights", help="inline weights instead of a file: HDFCBANK=7.8,ICICIBANK=5.2")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--top", type=int, help="keep only the N biggest weights")
    ap.add_argument("--prices", default="trader/prices.json", help="prices JSON (india-alerts writes it)")
    ap.add_argument("--fill", action="store_true", help="spend leftover cash even past a target")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        source = ""
        if a.weights:
            rows = parse_weights(weights_from_arg(a.weights))
        else:
            data = read_json(a.targets, "targets file (run india-copycat, or pass --weights)")
            rows = parse_weights(data)
            if isinstance(data, dict):
                source = data.get("source", "")
        prices, details, as_of = load_prices(a.prices) if os.path.exists(a.prices) else ({}, {}, None)
        result = size(rows, cfg, a.budget, prices, details, a.top, a.fill)
        orders = orders_from(result, cfg, source)
        if a.write:
            write_orders(a.write, orders, result, cfg, source, as_of)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source, as_of))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


if __name__ == "__main__":
    sys.exit(main())
