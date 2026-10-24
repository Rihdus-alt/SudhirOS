#!/usr/bin/env python3
"""Capital gains on listed Indian shares for india-tax. Standard library only, no network.

Not tax advice: it does the arithmetic of the Income-tax Act on the lots you
give it. A chartered accountant decides what applies to you.

    python3 lots.py report --lots                    # every lot: gain, STCG or LTCG, LTCG date
    python3 lots.py sell INFY 10 --price 1130.70     # what a sale would realize (FIFO), with tax
    python3 lots.py sell INFY 10 --price 1130.70 --record   # it filled: update lots, log it
    python3 lots.py fy                               # this financial year: gains, set-off, est. tax
    python3 lots.py harvest                          # loss harvesting + LTCG exemption harvesting
    python3 lots.py harvest --write                  # write sells to trader/orders.json as proposals
    python3 lots.py bonus HDFCBANK --ratio 1:1 --date 2025-08-27   # corporate actions
    python3 lots.py split TCS --ratio 1:2 --date 2026-01-15

Lots live in trader/portfolio.json:

    {"cash": 6304.01, "positions": [{"ticker": "INFY", "shares": 10, "lots": [
        {"id": "INFY-1", "date": "2017-06-15", "shares": 10, "cost": 9500.0,
         "fmv_2018_01_31": 1170.0}]}]}

"cost" is the lot's total cost of acquisition in rupees: price x shares plus
buy-side charges (brokerage, stamp duty, exchange, SEBI, GST) but NOT STT,
which the Act does not allow as a deduction on these gains.

The rules applied (listed equity shares on which STT was paid, residents):
  FIFO          demat shares are sold first-in, first-out (CBDT Circular
                768 of 1998); there is no choosing lots
  term          long term (LTCG) if held MORE than 12 months, else short
                term (STCG)
  rates         sales before 2024-07-23: STCG 15% (s.111A), LTCG 10%
                (s.112A); sales on or after: STCG 20%, LTCG 12.5%
  exemption     s.112A: the first Rs 1,00,000 of LTCG a financial year is
                not taxed, Rs 1,25,000 from FY 2024-25 on
  grandfather   lots bought before 2018-02-01: cost = the higher of actual
                cost and the lower of (FMV on 2018-01-31, sale value);
                FMV is the HIGHEST price quoted on 31 January 2018
  set-off       short-term losses offset STCG then LTCG; long-term losses
                offset LTCG only; unused losses carry forward 8 years
                (enter brought-forward losses under "carried_forward" in
                trader/state/tax.json)
  bonus         bonus shares cost zero and are held from their allotment
                date; a split keeps the original date and cost
  no wash sale  India has no wash-sale rule, but a same-day sell and re-buy
                is netted by the broker into an intraday (speculative)
                trade and is NOT a sale of your old shares: re-buy on the
                next trading day at the earliest
The estimate is at the special rates before surcharge and 4% cess (--cess
adds the cess). It ignores the basic exemption limit and rebates.
"""

import argparse
import datetime
import json
import math
import os
import sys

EPS = 1e-9
RATE_CHANGE = datetime.date(2024, 7, 23)
GRANDFATHER_BEFORE = datetime.date(2018, 2, 1)


class TaxError(Exception):
    pass


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TaxError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TaxError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def day(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        raise TaxError("not a date (YYYY-MM-DD): %r" % s)


def inr(x):
    x = float(x)
    sign = "-" if x < -0.004 else ""
    whole, frac = ("%.2f" % abs(x)).split(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups)
    return "%s₹%s%s" % (sign, whole, "" if frac == "00" else "." + frac)


def fmt_qty(x):
    s = ("%.4f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


def norm(sym):
    s = str(sym or "").strip().upper()
    return s[4:] if s.startswith("NSE:") else s


# ---------------------------------------------------------------- the law, as arithmetic

def fy_start(d):
    """The first day of the Indian financial year (1 April) that contains d."""
    return datetime.date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def fy_label(d):
    s = fy_start(d).year
    return "FY %d-%02d" % (s, (s + 1) % 100)


def rates_on(sold):
    """(stcg_rate, ltcg_rate) in percent for a sale on this date."""
    return (20.0, 12.5) if sold >= RATE_CHANGE else (15.0, 10.0)


def ltcg_exemption(fy_first_day):
    return 125000.0 if fy_first_day >= datetime.date(2024, 4, 1) else 100000.0


def add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def long_term_from(acquired):
    """The first sale date that counts as held MORE than 12 months."""
    return add_months(acquired, 12) + datetime.timedelta(days=1)


def term(acquired, sold):
    return "long" if sold >= long_term_from(acquired) else "short"


def grandfathered_cost(lot_cost, shares, fmv, sale_value, acquired):
    """s.55(2)(ac): cost of a pre-2018-02-01 lot for LTCG."""
    if acquired >= GRANDFATHER_BEFORE or fmv is None:
        return lot_cost
    fmv_total = float(fmv) * shares
    return max(lot_cost, min(fmv_total, sale_value))


# ---------------------------------------------------------------- lots

def lots_of(portfolio):
    """{symbol: [lot, ...]} oldest first (FIFO order)."""
    out = {}
    for p in (portfolio or {}).get("positions", []):
        t = norm(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not t:
            continue
        lots = []
        for i, l in enumerate(p.get("lots", []), 1):
            sh, cost = float(l.get("shares", 0)), float(l.get("cost", 0))
            if sh <= EPS:
                continue
            lots.append({"id": l.get("id") or "%s-%d" % (t, i), "date": day(l.get("date")), "shares": sh,
                         "cost": cost, "per_share": cost / sh, "fmv": l.get("fmv_2018_01_31"),
                         "kind": l.get("kind", "buy"), "seq": i})
        if not lots and float(p.get("shares", 0) or 0) > 0:
            raise TaxError("%s has shares but no lots; add each purchase (date, shares, cost) to "
                           "trader/portfolio.json from your broker's tradebook" % t)
        lots.sort(key=lambda l: (l["date"], l["seq"]))
        out[t] = lots
    return out


def need_fmv(lots):
    return [l["id"] for l in lots if l["date"] < GRANDFATHER_BEFORE and l["fmv"] is None]


def fifo(lots, shares):
    held = sum(l["shares"] for l in lots)
    if shares > held + EPS:
        raise TaxError("selling %s shares but only %s are held" % (fmt_qty(shares), fmt_qty(held)))
    out, left = [], shares
    for l in lots:
        if left <= EPS:
            break
        take = min(left, l["shares"])
        out.append((l, take))
        left -= take
    return out


def simulate_sale(lots, shares, price, sold, sell_charges=0.0):
    """Gains by lot. sell_charges: transfer expenses (no STT) for the whole sale."""
    used, res = [], {"proceeds": 0.0, "cost": 0.0, "short": 0.0, "long": 0.0}
    gross = price * shares
    for l, take in fifo(lots, shares):
        part = take / shares if shares else 0.0
        proceeds = price * take - sell_charges * part
        actual = l["per_share"] * take
        tm = term(l["date"], sold)
        cost = grandfathered_cost(actual, take, l["fmv"], price * take, l["date"]) if tm == "long" else actual
        used.append({"id": l["id"], "seq": l.get("seq"), "acquired": l["date"].isoformat(), "kind": l["kind"],
                     "shares": round(take, 6),
                     "cost": round(cost, 2), "actual_cost": round(actual, 2), "proceeds": round(proceeds, 2),
                     "gain": round(proceeds - cost, 2), "term": tm,
                     "grandfathered": cost > actual + 0.005})
        res["proceeds"] += proceeds
        res["cost"] += cost
        res[tm] += proceeds - cost
    res = {k: round(v, 2) for k, v in res.items()}
    res["gain"] = round(res["proceeds"] - res["cost"], 2)
    res["gross"] = round(gross, 2)
    res["sell_charges"] = round(sell_charges, 2)
    res["lots"] = used
    res["sold"] = sold.isoformat()
    res["stcg_rate"], res["ltcg_rate"] = rates_on(sold)
    return res


def unrealized(portfolio, prices, today):
    rows, totals, missing = [], [], []
    for t, lots in sorted(lots_of(portfolio).items()):
        p = prices.get(t)
        if not p:
            missing.append(t)
            continue
        tot = {"ticker": t, "shares": 0.0, "cost": 0.0, "value": 0.0, "short": 0.0, "long": 0.0}
        for l in lots:
            value = l["shares"] * p
            tm = term(l["date"], today)
            cost = grandfathered_cost(l["cost"], l["shares"], l["fmv"], value, l["date"]) if tm == "long" else l["cost"]
            g = value - cost
            rows.append({"ticker": t, "id": l["id"], "date": l["date"].isoformat(), "kind": l["kind"],
                         "shares": l["shares"], "cost": round(cost, 2), "price": p, "value": round(value, 2),
                         "gain": round(g, 2), "term": tm, "ltcg_from": long_term_from(l["date"]).isoformat()})
            tot["shares"] += l["shares"]
            tot["cost"] += cost
            tot["value"] += value
            tot[tm] += g
        tot["gain"] = round(tot["value"] - tot["cost"], 2)
        tot["pct"] = round(100.0 * tot["gain"] / tot["cost"], 2) if tot["cost"] else 0.0
        for k in ("cost", "value", "short", "long"):
            tot[k] = round(tot[k], 2)
        totals.append(tot)
    return rows, totals, missing


# ---------------------------------------------------------------- a financial year

def fy_summary(realized, fy_first_day, carried=None, cess=False):
    """Set-off and estimated tax for one financial year.

    realized: [{"date", "short", "long"}] sales; gains by rate bucket.
    carried: {"stcl": x, "ltcl": y} losses brought forward (positive numbers)."""
    nxt = datetime.date(fy_first_day.year + 1, 4, 1)
    buckets = {}  # (term, rate) -> net amount
    count = 0
    for r in realized or []:
        d = day(r.get("date"))
        if not fy_first_day <= d < nxt:
            continue
        count += 1
        st, lt = rates_on(d)
        for tm, rate in (("short", st), ("long", lt)):
            v = float(r.get(tm, 0) or 0)
            if abs(v) > EPS:
                buckets[(tm, rate)] = buckets.get((tm, rate), 0.0) + v
    gains = {k: v for k, v in buckets.items()}
    stcl = -sum(v for (tm, _), v in gains.items() if tm == "short" and v < 0)
    ltcl = -sum(v for (tm, _), v in gains.items() if tm == "long" and v < 0)
    pos = {k: v for k, v in gains.items() if v > 0}
    carried = carried or {}
    bf_st, bf_lt = float(carried.get("stcl", 0) or 0), float(carried.get("ltcl", 0) or 0)

    def absorb(loss, keys):
        for k in sorted(keys, key=lambda k: -k[1]):  # highest rate first
            if loss <= EPS:
                break
            take = min(loss, pos[k])
            pos[k] -= take
            loss -= take
        return loss

    st_keys = [k for k in pos if k[0] == "short"]
    lt_keys = [k for k in pos if k[0] == "long"]
    left_stcl = absorb(stcl, st_keys)
    left_stcl = absorb(left_stcl, lt_keys)
    left_ltcl = absorb(ltcl, lt_keys)
    left_bf_st = absorb(absorb(bf_st, st_keys), lt_keys)
    left_bf_lt = absorb(bf_lt, lt_keys)
    exemption = ltcg_exemption(fy_first_day)
    ex_left = exemption
    for k in sorted(lt_keys, key=lambda k: -k[1]):
        use = min(ex_left, pos[k])
        pos[k] -= use
        ex_left -= use
    tax = sum(v * k[1] / 100.0 for k, v in pos.items())
    if cess:
        tax *= 1.04
    ltcg_net = sum(v for k, v in gains.items() if k[0] == "long")
    return {
        "fy": fy_label(fy_first_day), "sales": count,
        "stcg": round(sum(v for k, v in gains.items() if k[0] == "short" and v > 0), 2),
        "stcl": round(stcl, 2),
        "ltcg": round(sum(v for k, v in gains.items() if k[0] == "long" and v > 0), 2),
        "ltcl": round(ltcl, 2),
        "brought_forward": {"stcl": bf_st, "ltcl": bf_lt},
        "exemption_112a": exemption, "exemption_used": round(exemption - ex_left, 2),
        "exemption_left": round(ex_left, 2),
        "taxable": {"%s @ %s%%" % ("STCG" if k[0] == "short" else "LTCG", _r(k[1])): round(v, 2)
                    for k, v in sorted(pos.items()) if v > 0.005},
        "est_tax": round(tax, 2), "with_cess": bool(cess),
        "carry_forward": {"stcl": round(left_stcl + left_bf_st, 2), "ltcl": round(left_ltcl + left_bf_lt, 2)},
        "ltcg_net": round(ltcg_net, 2),
    }


def _r(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def tax_on_sale(sale, realized, fy_first_day, carried=None, cess=False):
    """Extra tax this sale would add to the year's estimate (negative = it saves tax)."""
    before = fy_summary(realized, fy_first_day, carried, cess)
    after = fy_summary(list(realized or []) + [{"date": sale["sold"], "short": sale["short"], "long": sale["long"]}],
                       fy_first_day, carried, cess)
    return round(after["est_tax"] - before["est_tax"], 2), after


# ---------------------------------------------------------------- harvesting

def harvest(portfolio, prices, today, realized, carried=None, min_loss=1000.0, sell_cost_pct=0.12,
            gain_harvest=True):
    """Loss harvesting and LTCG exemption harvesting candidates, FIFO-aware."""
    fy0 = fy_start(today)
    year = fy_summary(realized, fy0, carried)
    losses, gains = [], []
    for t, lots in sorted(lots_of(portfolio).items()):
        p = prices.get(t)
        if not p:
            continue
        # FIFO: you can only sell from the oldest lot forward, so walk the prefix
        n, best = 0.0, None
        for l in lots:
            n += l["shares"]
            sale = simulate_sale(lots, n, p, today, p * n * sell_cost_pct / 100.0)
            if sale["gain"] < -min_loss and (best is None or sale["gain"] < best["gain"]):
                best = dict(sale, shares=n)
        if best:
            saving, _ = tax_on_sale(best, realized, fy0, carried)
            losses.append({"ticker": t, "price": p, "shares": round(best["shares"], 6),
                           "lots": [u["id"] for u in best["lots"]], "loss": best["gain"], "short": best["short"],
                           "long": best["long"], "tax_change_this_fy": saving,
                           "rebuy_from": (today + datetime.timedelta(days=1)).isoformat()})
    if gain_harvest and year["exemption_left"] > 0:
        room = max(0.0, year["exemption_left"])
        for t, lots in sorted(lots_of(portfolio).items()):
            p = prices.get(t)
            if not p or room <= 1:
                continue
            n, pick = 0.0, None
            for l in lots:
                if term(l["date"], today) != "long":
                    break
                for k in range(int(l["shares"]), 0, -1):
                    sale = simulate_sale(lots, n + k, p, today, p * (n + k) * sell_cost_pct / 100.0)
                    if sale["short"] == 0 and 0 < sale["long"] <= room:
                        if pick is None or sale["long"] > pick["long"]:
                            pick = dict(sale, shares=n + k)
                        break
                n += l["shares"]
            if pick and pick["long"] > 1:
                gains.append({"ticker": t, "price": p, "shares": round(pick["shares"], 6),
                              "lots": [u["id"] for u in pick["lots"]], "ltcg": pick["long"],
                              "rebuy_from": (today + datetime.timedelta(days=1)).isoformat()})
                room -= pick["long"]
    losses.sort(key=lambda c: c["loss"])
    return {"fy": year, "losses": losses, "gains": gains}


def _limit_price(price, buf):
    """A sell limit buf% under the last price on NSE's tick grid (india-budget's rule, when it is installed)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "india-budget", "size.py")
    if os.path.exists(path):
        import importlib.util
        spec = importlib.util.spec_from_file_location("india_size", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.limit_price(price, "sell", buf, None)
    return math.floor(price * (1 - buf / 100.0) * 20 + EPS) / 20.0


def harvest_orders(h, include_gains=True, cfg=None, portfolio=None, today=None):
    """Sell proposals for the harvest candidates, in india-risk's terms: a LIMIT (unless the config says
    MARKET) and no bigger than max_order_inr at that limit. A capped FIFO prefix is simulated again and kept
    only if it still books a loss (a loss harvest) or only long-term gain (an exemption harvest)."""
    cfg = cfg or {}
    max_o = float(cfg.get("max_order_inr") or 0)
    limit = str(cfg.get("order_type", "LIMIT")).upper() == "LIMIT"
    buf = float(cfg.get("limit_buffer_pct", 0.5) or 0)
    lots = lots_of(portfolio) if portfolio else {}
    cands = [("loss", c) for c in h["losses"]] + ([("gain", c) for c in h["gains"]] if include_gains else [])
    out, notes = [], []
    for kind, c in cands:
        t, q, px = c["ticker"], int(round(c["shares"])), float(c["price"])
        lp = _limit_price(px, buf) if limit else px
        cut = ""
        if max_o and q * lp > max_o + EPS:
            q2 = int(max_o // lp)
            sale = simulate_sale(lots[t], q2, px, today, estimate_sell_charges(q2 * px)) \
                if (q2 > 0 and lots.get(t) and today) else None
            if not sale or (kind == "loss" and sale["gain"] >= 0) or (kind == "gain" and abs(sale["short"]) > EPS):
                notes.append("%s: within your %s max order the FIFO sale no longer books %s; not proposed"
                             % (t, inr(max_o), "a loss" if kind == "loss" else "only long-term gain"))
                continue
            cut = " Cut from %d to %d shares by your %s max order; sell the rest on a later day." % (q, q2, inr(max_o))
            notes.append("%s: cut from %d to %d shares by your %s max order" % (t, q, q2, inr(max_o)))
            q = q2
        if kind == "loss":
            why = ("tax-loss harvest: sell %s (lots %s, FIFO) to book an estimated %s loss; buy back on or "
                   "after %s, never the same day. Not tax advice." % (fmt_qty(c["shares"]), ", ".join(c["lots"]),
                                                                      inr(c["loss"]), c["rebuy_from"]))
        else:
            why = ("LTCG exemption harvest: sell %s (lots %s, FIFO) to book about %s of long-term gain "
                   "inside this year's s.112A exemption, then buy back on or after %s to reset the cost. "
                   "The new shares start a new 12-month holding period. Not tax advice."
                   % (fmt_qty(c["shares"]), ", ".join(c["lots"]), inr(c["ltcg"]), c["rebuy_from"]))
        o = {"variety": "regular", "exchange": "NSE", "tradingsymbol": t, "transaction_type": "SELL", "quantity": q,
             "product": "CNC", "order_type": "LIMIT" if limit else "MARKET", "validity": "DAY", "why": why + cut}
        if limit:
            o["price"] = lp
        out.append(o)
    harvest_orders.notes = notes
    return out


harvest_orders.notes = []


# ---------------------------------------------------------------- corporate actions, recording

def _ratio(text):
    try:
        a, b = [float(x) for x in str(text).split(":")]
    except ValueError:
        raise TaxError("--ratio wants A:B, e.g. 1:1 for a bonus or 1:5 for a split")
    if a <= 0 or b <= 0:
        raise TaxError("ratio parts must be positive")
    return a, b


def apply_bonus(portfolio, ticker, ratio, when, allotted=None):
    """Bonus "N:H" (N new shares for every H held, the way companies announce it). when: the ex-date (the record
    date under T+1): shares bought on or after it are not entitled. The new zero-cost lot is dated the allotment
    day (allotted, default the ex-date), which starts its holding period."""
    a, b = _ratio(ratio)
    t = norm(ticker)
    allotted = allotted or when
    for p in portfolio.get("positions", []):
        if norm(p.get("ticker") or p.get("symbol")) != t:
            continue
        held = sum(float(l["shares"]) for l in p.get("lots", []) if day(l["date"]) < when)
        extra = int(held * a / b + EPS)
        if extra <= 0:
            raise TaxError("no %s shares held on %s to earn a bonus" % (t, when))
        p["lots"].append({"id": "%s-B%s" % (t, allotted.strftime("%Y%m%d")), "date": allotted.isoformat(),
                          "shares": extra, "cost": 0.0, "kind": "bonus", "ex_date": when.isoformat()})
        p["shares"] = round(float(p.get("shares", 0)) + extra, 6)
        return extra
    raise TaxError("no %s in the portfolio" % t)


def apply_split(portfolio, ticker, ratio, when):
    """A old shares become B new: every lot's shares x B/A, cost and date unchanged; FMV per share / (B/A).
    when: the ex-date; a lot bought on or after it was bought at the post-split price and is left alone."""
    a, b = _ratio(ratio)
    t = norm(ticker)
    f = b / a
    for p in portfolio.get("positions", []):
        if norm(p.get("ticker") or p.get("symbol")) != t:
            continue
        for l in p.get("lots", []):
            if day(l["date"]) < when:
                l["shares"] = round(float(l["shares"]) * f, 6)
                if l.get("fmv_2018_01_31") is not None:
                    l["fmv_2018_01_31"] = round(float(l["fmv_2018_01_31"]) / f, 4)
        p["shares"] = round(sum(float(l["shares"]) for l in p["lots"]), 6)
        return p["shares"]
    raise TaxError("no %s in the portfolio" % t)


def record_sale(portfolio, ticker, sale, price, state):
    t = norm(ticker)
    # match the lots a sale used by their position in the portfolio (lots_of's seq), not by id: two lots can
    # carry the same id in a hand-edited or older portfolio, and cutting both would lose shares
    by_seq = all(u.get("seq") for u in sale["lots"])
    taken = {(u["seq"] if by_seq else u["id"]): u for u in sale["lots"]}
    for p in portfolio.get("positions", []):
        if norm(p.get("ticker") or p.get("symbol")) != t:
            continue
        new = []
        for i, l in enumerate(p.get("lots", []), 1):
            lid = l.get("id") or "%s-%d" % (t, i)
            l["id"] = lid
            key = i if by_seq else lid
            if key in taken:
                sh = float(l["shares"])
                take = taken[key]["shares"]
                l["cost"] = round(float(l["cost"]) * (1 - take / sh), 2) if sh else 0.0
                l["shares"] = round(sh - take, 6)
            if float(l["shares"]) > EPS:
                new.append(l)
        p["lots"] = new
        p["shares"] = round(sum(float(l["shares"]) for l in new), 6)
    portfolio["positions"] = [p for p in portfolio.get("positions", []) if float(p.get("shares", 0)) > EPS]
    portfolio["cash"] = round(float(portfolio.get("cash", 0)) + sale["proceeds"], 2)
    state.setdefault("realized", []).append({
        "date": sale["sold"], "symbol": t, "shares": round(sum(u["shares"] for u in sale["lots"]), 6),
        "price": price, "proceeds": sale["proceeds"], "cost": sale["cost"], "gain": sale["gain"],
        "short": sale["short"], "long": sale["long"], "sell_charges": sale.get("sell_charges", 0.0),
        "lots": sale["lots"]})
    return portfolio, state


def bonus_strip_warning(lots, used, sold):
    """s.94(8): original shares bought within 3 months before a bonus and sold at a loss within 9
    months after it: the loss is ignored (it becomes the bonus shares' cost)."""
    out = []
    bonuses = [l for l in lots if l["kind"] == "bonus"]
    for u in used:
        if u["gain"] >= 0 or u["kind"] == "bonus":
            continue
        acq = day(u["acquired"])
        for b in bonuses:
            if add_months(b["date"], -3) <= acq <= b["date"] and sold <= add_months(b["date"], 9):
                out.append("lot %s was bought within 3 months before the %s bonus and is sold at a loss within 9 "
                           "months after it: s.94(8) bonus stripping ignores that loss" % (u["id"], b["date"]))
    return out


def estimate_sell_charges(value):
    """Sell-side transfer expenses except STT, from india-costs when it is installed next to this skill."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "india-costs", "costs.py")
    if not os.path.exists(path):
        return 0.0
    import importlib.util
    spec = importlib.util.spec_from_file_location("india_costs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = mod.trade_costs("sell", value)
    return round(c["total"] - c["stt"], 2)


# ---------------------------------------------------------------- CLI

NOT_ADVICE = ("Not tax advice: arithmetic on your lots at the s.111A / s.112A rates, before surcharge, cess "
              "(unless --cess), the basic exemption limit and rebates. Check with a chartered accountant.")


def _inputs(a):
    portfolio = read_json(a.portfolio, "portfolio (lots)")
    pdoc = read_json(a.prices, "prices", required=False) or {}
    prices = {norm(k): float(v) for k, v in (pdoc.get("prices", pdoc) or {}).items() if isinstance(v, (int, float))}
    state = read_json(a.state, "tax state", required=False) or {"realized": []}
    today = day(a.today) if a.today else datetime.date.today()
    return portfolio, prices, state, today


def _carried(state, today):
    cf = state.get("carried_forward") or {}
    return cf.get(fy_label(today), cf) if isinstance(cf, dict) else {}


def cmd_report(a):
    portfolio, prices, state, today = _inputs(a)
    rows, totals, missing = unrealized(portfolio, prices, today)
    if a.json:
        print(json.dumps({"lots": rows, "positions": totals, "missing_prices": missing,
                          "fy": fy_summary(state.get("realized"), fy_start(today), _carried(state, today))},
                         indent=2, ensure_ascii=False))
        return 0
    print("%-12s %9s %13s %13s %13s %8s" % ("symbol", "shares", "cost", "value", "gain", "gain %"))
    for t in totals:
        print("%-12s %9s %13s %13s %13s %7.2f%%" % (t["ticker"], fmt_qty(t["shares"]), inr(t["cost"]),
                                                   inr(t["value"]), inr(t["gain"]), t["pct"]))
    if a.lots:
        print("")
        print("%-16s %-11s %8s %12s %12s %5s %s" % ("lot", "bought", "shares", "cost", "gain", "term", "LTCG from"))
        for r in rows:
            print("%-16s %-11s %8s %12s %12s %5s %s%s" % (r["id"], r["date"], fmt_qty(r["shares"]), inr(r["cost"]),
                                                         inr(r["gain"]), "LTCG" if r["term"] == "long" else "STCG",
                                                         r["ltcg_from"], "  bonus" if r["kind"] == "bonus" else ""))
    for t, lots in lots_of(portfolio).items():
        ids = need_fmv(lots)
        if ids:
            print("Lots bought before 2018-02-01 need \"fmv_2018_01_31\" (the highest price on 31 Jan 2018) for "
                  "grandfathering: %s" % ", ".join(ids))
    for t in missing:
        print("No price for %s: refresh trader/prices.json (india-alerts)" % t)
    fy = fy_summary(state.get("realized"), fy_start(today), _carried(state, today))
    print("")
    print("%s so far (recorded sales): STCG %s, STCL %s, LTCG %s, LTCL %s; s.112A exemption left %s; est. tax %s."
          % (fy["fy"], inr(fy["stcg"]), inr(fy["stcl"]), inr(fy["ltcg"]), inr(fy["ltcl"]), inr(fy["exemption_left"]),
             inr(fy["est_tax"])))
    print(NOT_ADVICE)
    return 0


def cmd_sell(a):
    portfolio, prices, state, today = _inputs(a)
    t = norm(a.ticker)
    lots = lots_of(portfolio).get(t)
    if not lots:
        raise TaxError("no lots for %s in %s" % (t, a.portfolio))
    price = a.price or prices.get(t)
    if not price:
        raise TaxError("no price for %s: pass --price" % t)
    if abs(a.shares - round(a.shares)) > EPS:
        raise TaxError("Indian exchanges trade whole shares; %s is not whole" % a.shares)
    charges = a.charges
    if charges is None:
        charges = estimate_sell_charges(float(a.shares) * float(price))
    sale = simulate_sale(lots, float(a.shares), float(price), today, charges)
    extra, fy = tax_on_sale(sale, state.get("realized"), fy_start(today), _carried(state, today), a.cess)
    warns = bonus_strip_warning(lots, sale["lots"], today)
    missing = [u["id"] for u in sale["lots"] if u["term"] == "long" and day(u["acquired"]) < GRANDFATHER_BEFORE
               and not [l for l in lots if l["id"] == u["id"] and l["fmv"] is not None]]
    if a.json:
        print(json.dumps(dict(sale, tax_change_this_fy=extra, fy_after=fy, warnings=warns,
                              missing_fmv=missing), indent=2, ensure_ascii=False))
    else:
        print("Selling %s %s at %s on %s (FIFO), sell charges %s:" % (fmt_qty(a.shares), t, inr(price), today,
                                                                     inr(sale["sell_charges"])))
        for u in sale["lots"]:
            print("  lot %-14s bought %s  %s shares  cost %s%s  proceeds %s  %s %s"
                  % (u["id"], u["acquired"], fmt_qty(u["shares"]), inr(u["cost"]),
                     " (grandfathered)" if u["grandfathered"] else "", inr(u["proceeds"]),
                     "LTCG" if u["term"] == "long" else "STCG", inr(u["gain"])))
        print("Total: proceeds %s, cost %s, %s %s (STCG %s at %s%%, LTCG %s at %s%%)."
              % (inr(sale["proceeds"]), inr(sale["cost"]), "gain" if sale["gain"] >= 0 else "loss", inr(sale["gain"]),
                 inr(sale["short"]), _r(sale["stcg_rate"]), inr(sale["long"]), _r(sale["ltcg_rate"])))
        print("This sale changes %s's estimated tax by %s (s.112A exemption left after it: %s)."
              % (fy["fy"], inr(extra), inr(fy["exemption_left"])))
        for w in warns:
            print("WARNING: " + w)
        if missing:
            print("WARNING: lots %s were bought before 2018-02-01 and have no FMV: their gain is overstated."
                  % ", ".join(missing))
        if sale["gain"] < 0:
            print("No wash-sale rule in India, but buy back on the next trading day at the earliest: a same-day "
                  "sell and buy is netted into an intraday trade.")
        print(NOT_ADVICE)
    if a.record:
        portfolio, state = record_sale(portfolio, t, sale, float(price), state)
        stt = a.stt if a.stt is not None else float(round(0.001 * float(a.shares) * float(price)))
        portfolio["cash"] = round(float(portfolio["cash"]) - stt, 2)   # the proceeds above exclude STT
        write_json(a.portfolio, portfolio)
        write_json(a.state, state)
        if not a.json:
            print("Recorded: lots updated in %s, sale logged in %s." % (a.portfolio, a.state))
    return 0


def cmd_fy(a):
    portfolio, prices, state, today = _inputs(a)
    when = day(a.fy + "-04-01") if a.fy else fy_start(today)
    fy = fy_summary(state.get("realized"), when, _carried(state, when), a.cess)
    if a.json:
        print(json.dumps(fy, indent=2, ensure_ascii=False))
        return 0
    print("%s: %d recorded sale(s)." % (fy["fy"], fy["sales"]))
    print("  STCG %s, STCL %s, LTCG %s, LTCL %s; brought forward STCL %s, LTCL %s"
          % (inr(fy["stcg"]), inr(fy["stcl"]), inr(fy["ltcg"]), inr(fy["ltcl"]),
             inr(fy["brought_forward"]["stcl"]), inr(fy["brought_forward"]["ltcl"])))
    print("  s.112A exemption %s: used %s, left %s" % (inr(fy["exemption_112a"]), inr(fy["exemption_used"]),
                                                         inr(fy["exemption_left"])))
    for k, v in fy["taxable"].items():
        print("  taxable %-14s %s" % (k, inr(v)))
    print("  estimated tax %s%s" % (inr(fy["est_tax"]), " incl. 4% cess" if fy["with_cess"] else " (before surcharge and cess)"))
    if fy["carry_forward"]["stcl"] or fy["carry_forward"]["ltcl"]:
        print("  to carry forward (file your ITR on time to keep them): STCL %s, LTCL %s"
              % (inr(fy["carry_forward"]["stcl"]), inr(fy["carry_forward"]["ltcl"])))
    print(NOT_ADVICE)
    return 0


def cmd_harvest(a):
    portfolio, prices, state, today = _inputs(a)
    h = harvest(portfolio, prices, today, state.get("realized"), _carried(state, today), a.min_loss,
                a.sell_cost_pct, not a.no_gains)
    cfg = read_json(a.config, "config", required=False) or {}
    props = harvest_orders(h, not a.no_gains, cfg, portfolio, today)
    if a.write and props:
        write_json(a.write, {
            "created_by": "india-tax", "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "status": "proposed", "currency": "INR",
            "note": ("Harvesting proposals, not tax advice. Nothing is placed until india-risk passes each order "
                     "and you say yes to it in the chat. Re-buys are separate decisions, on a later day."),
            "orders": props})
    if a.json:
        print(json.dumps(dict(h, orders=props), indent=2, ensure_ascii=False))
        return 0 if props else 1
    fy = h["fy"]
    print("%s: realized STCG %s, LTCG %s; s.112A exemption left %s." % (fy["fy"], inr(fy["stcg"]), inr(fy["ltcg"]),
                                                                         inr(fy["exemption_left"])))
    if not h["losses"] and not h["gains"]:
        print("Nothing to harvest: no FIFO sale books a loss over %s, and no long-term gain fits the exemption."
              % inr(a.min_loss))
        print(NOT_ADVICE)
        return 1
    for c in h["losses"]:
        print("LOSS  %-12s sell %s (lots %s) at %s: loss %s (STCG %s, LTCG %s); this FY's est. tax changes by %s"
              % (c["ticker"], fmt_qty(c["shares"]), ", ".join(c["lots"]), inr(c["price"]), inr(c["loss"]),
                 inr(c["short"]), inr(c["long"]), inr(c["tax_change_this_fy"])))
    for c in h["gains"]:
        print("GAIN  %-12s sell %s (lots %s) at %s: books %s LTCG inside the exemption, tax-free; re-buy from %s "
              "to raise your cost" % (c["ticker"], fmt_qty(c["shares"]), ", ".join(c["lots"]), inr(c["price"]),
                                     inr(c["ltcg"]), c["rebuy_from"]))
    for n in harvest_orders.notes:
        print("NOTE: " + n)
    print("Re-buy on the next trading day at the earliest (a same-day sell and buy is netted into an intraday "
          "trade). Harvesting only to create a loss can be questioned under the anti-avoidance rules; book it as a "
          "real sale. Charges (about %.2f%% a side) are counted in these numbers." % a.sell_cost_pct)
    if a.write:
        print("Wrote %d proposed sell order(s) to %s. india-risk checks them next." % (len(props), a.write))
    print(NOT_ADVICE)
    return 0


def cmd_action(a):
    portfolio = read_json(a.portfolio, "portfolio (lots)")
    when = day(a.date)
    if a.cmd == "bonus":
        allotted = day(a.allotment) if a.allotment else when
        extra = apply_bonus(portfolio, a.ticker, a.ratio, when, allotted)
        msg = "Added %d bonus shares of %s dated %s at zero cost." % (extra, norm(a.ticker), allotted)
    else:
        total = apply_split(portfolio, a.ticker, a.ratio, when)
        msg = "Split %s %s: now %s shares, same cost and purchase dates." % (norm(a.ticker), a.ratio, fmt_qty(total))
    if not a.dry_run:
        write_json(a.portfolio, portfolio)
    print(msg + ("" if not a.dry_run else " (dry run, nothing written)"))
    print("Check the new share count against your demat holdings statement.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Capital gains on listed Indian shares (india-tax). Not tax advice.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--prices", default="trader/prices.json")
        p.add_argument("--state", default="trader/state/tax.json")
        p.add_argument("--today", help="YYYY-MM-DD (default: today)")
        p.add_argument("--cess", action="store_true", help="add 4%% health and education cess")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="unrealized gains by lot")
    common(p)
    p.add_argument("--lots", action="store_true")
    p = sub.add_parser("sell", help="what a FIFO sale would realize, and its tax")
    common(p)
    p.add_argument("ticker")
    p.add_argument("shares", type=float)
    p.add_argument("--price", type=float)
    p.add_argument("--charges", type=float, help="sell-side charges except STT, from the contract note "
                                                 "(default: estimated with india-costs)")
    p.add_argument("--record", action="store_true")
    p.add_argument("--stt", type=float, help="STT on the sale, from the contract note (default 0.1%%, rounded)")
    p = sub.add_parser("fy", help="this financial year: set-off, exemption, estimated tax")
    common(p)
    p.add_argument("--fy", help="the starting year, e.g. 2025 for FY 2025-26")
    p = sub.add_parser("harvest", help="loss harvesting and LTCG exemption harvesting")
    common(p)
    p.add_argument("--min-loss", type=float, default=1000.0)
    p.add_argument("--sell-cost-pct", type=float, default=0.12, help="charges per side as %% of value")
    p.add_argument("--no-gains", action="store_true", help="skip LTCG exemption harvesting")
    p.add_argument("--config", default="trader/config.json", help="max_order_inr, order_type, limit_buffer_pct")
    p.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH")
    for name in ("bonus", "split"):
        p = sub.add_parser(name, help="record a %s" % name)
        p.add_argument("ticker")
        p.add_argument("--ratio", required=True,
                       help="bonus new:held as announced (1:1, 1:2); split old:new shares (1:5 for 10 -> 2 face value)")
        p.add_argument("--date", required=True, help="ex-date YYYY-MM-DD (the record date under T+1): shares "
                       "bought on or after it are not entitled")
        if name == "bonus":
            p.add_argument("--allotment", help="allotment date YYYY-MM-DD, which dates the bonus lot (default: "
                           "--date)")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"report": cmd_report, "sell": cmd_sell, "fy": cmd_fy, "harvest": cmd_harvest,
                "bonus": cmd_action, "split": cmd_action}[a.cmd](a)
    except TaxError as e:
        print("lots.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
