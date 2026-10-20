#!/usr/bin/env python3
"""Trade costs for Indian equities (india-costs). Standard library only, no network.

Every buy and sell on NSE or BSE carries statutory charges on top of the
broker's fee. This works them out line by line, the way a contract note
shows them, so sizing, risk checks and tax use the real rupee cost.

    python3 costs.py buy 50000                       # delivery buy of Rs 50,000 on NSE
    python3 costs.py sell 50000 --exchange BSE
    python3 costs.py roundtrip 50000 --exit 51000    # buy then sell, net P&L, breakeven
    python3 costs.py buy 50000 --product MIS         # intraday
    python3 costs.py buy 50000 --config trader/config.json --json

The lines (rates in DEFAULT_RATES, override any of them under "charges" in
trader/config.json; check them against your own contract note):

  brokerage     your broker's fee: min(flat, pct of value), floored at min;
                many discount brokers charge 0 for delivery
  STT           Securities Transaction Tax: 0.1% on BOTH buy and sell for
                delivery (CNC); 0.025% on the SELL side only for intraday
                (MIS); rounded to the nearest rupee as on a contract note
  exchange      NSE / BSE transaction charge, a percent of turnover
  SEBI fee      Rs 10 per crore of turnover
  stamp duty    BUY side only: 0.015% delivery, 0.003% intraday
  GST           18% on brokerage + exchange charge + SEBI fee (never on STT
                or stamp duty)
  DP charge     a flat depository fee per stock per day on delivery SELLS,
                whatever the quantity (set dp_charge_per_scrip to yours)

STT is not deductible when computing capital gains on listed shares; the
other charges are part of the cost of buying or selling (india-tax uses this).
"""

import argparse
import json
import os
import sys

DEFAULT_RATES = {
    "as_of": "2026-10-20",
    "stt_delivery_pct": 0.1,
    "stt_intraday_sell_pct": 0.025,
    "stamp_delivery_buy_pct": 0.015,
    "stamp_intraday_buy_pct": 0.003,
    "exchange_txn_pct": {"NSE": 0.00297, "BSE": 0.00375},
    "sebi_fee_per_crore": 10.0,
    "gst_pct": 18.0,
    "brokerage_delivery": {"flat": 0.0, "pct": 0.0, "min": 0.0},
    "brokerage_intraday": {"flat": 20.0, "pct": 0.03, "min": 0.0},
    "dp_charge_per_scrip": 15.93,
    "round_stt": True,
}
CRORE = 1e7


class CostError(Exception):
    pass


def rates_from(cfg):
    """DEFAULT_RATES with any overrides from config["charges"] (nested dicts merged)."""
    r = json.loads(json.dumps(DEFAULT_RATES))
    for k, v in ((cfg or {}).get("charges") or {}).items():
        if isinstance(v, dict) and isinstance(r.get(k), dict):
            r[k].update(v)
        else:
            r[k] = v
    return r


def _brokerage(value, rule):
    flat, pct, floor = float(rule.get("flat") or 0), float(rule.get("pct") or 0), float(rule.get("min") or 0)
    by_pct = value * pct / 100.0
    if flat and pct:
        b = min(flat, by_pct)
    else:
        b = flat or by_pct
    return max(b, floor) if (flat or pct) else 0.0


def trade_costs(side, value, exchange="NSE", product="CNC", rates=None, dp=True):
    """Charges on one order of `value` rupees. Returns {line: rupees, "total": rupees}.

    dp: include the depository charge on a delivery sell (once per stock per day)."""
    r = rates or DEFAULT_RATES
    side, product, exchange = str(side).lower(), str(product).upper(), str(exchange).upper()
    if side not in ("buy", "sell"):
        raise CostError("side must be buy or sell, got %r" % side)
    value = float(value)
    if value < 0:
        raise CostError("value must not be negative")
    intraday = product == "MIS"
    brokerage = _brokerage(value, r["brokerage_intraday" if intraday else "brokerage_delivery"])
    if intraday:
        stt = value * float(r["stt_intraday_sell_pct"]) / 100.0 if side == "sell" else 0.0
    else:
        stt = value * float(r["stt_delivery_pct"]) / 100.0
    if r.get("round_stt", True):
        stt = float(int(stt + 0.5))
    txn_pct = r["exchange_txn_pct"]
    txn_pct = float(txn_pct.get(exchange, txn_pct.get("NSE", 0)) if isinstance(txn_pct, dict) else txn_pct)
    exch = value * txn_pct / 100.0
    sebi = value * float(r["sebi_fee_per_crore"]) / CRORE
    stamp = 0.0
    if side == "buy":
        stamp = value * float(r["stamp_intraday_buy_pct" if intraday else "stamp_delivery_buy_pct"]) / 100.0
    gst = (brokerage + exch + sebi) * float(r["gst_pct"]) / 100.0
    dp_fee = float(r.get("dp_charge_per_scrip") or 0) if (dp and side == "sell" and not intraday and value > 0) else 0.0
    out = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp,
           "gst": gst, "dp": dp_fee}
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(sum(out.values()), 2)
    return out


def roundtrip(entry_value, exit_value, exchange="NSE", product="CNC", rates=None):
    buy = trade_costs("buy", entry_value, exchange, product, rates)
    sell = trade_costs("sell", exit_value, exchange, product, rates)
    gross = exit_value - entry_value
    total = round(buy["total"] + sell["total"], 2)
    # breakeven: the exit value whose sell costs plus the buy costs equal the gain
    lo, hi = entry_value, entry_value * 1.1 + 1000
    for _ in range(80):
        mid = (lo + hi) / 2.0
        net = mid - entry_value - buy["total"] - trade_costs("sell", mid, exchange, product, rates)["total"]
        if net >= 0:
            hi = mid
        else:
            lo = mid
    return {"buy": buy, "sell": sell, "gross": round(gross, 2), "costs": total,
            "net": round(gross - total, 2), "breakeven_exit_value": round(hi, 2),
            "breakeven_move_pct": round(100.0 * (hi / entry_value - 1), 4) if entry_value else None}


# ---------------------------------------------------------------- report

def inr(x):
    """Indian grouping: 1234567.5 -> Rs 12,34,567.50"""
    x = float(x)
    sign = "-" if x < -0.004 else ""
    whole, frac = ("%.2f" % abs(x)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    s = "%s₹%s" % (sign, whole)
    return s if frac == "00" else s + "." + frac


LABELS = (("brokerage", "Brokerage"), ("stt", "STT"), ("exchange", "Exchange txn charge"),
          ("sebi", "SEBI fee"), ("stamp", "Stamp duty"), ("gst", "GST (18%)"), ("dp", "DP charge"))


def render(side, value, c, exchange, product):
    lines = ["%s %s on %s (%s)" % (side.upper(), inr(value), exchange, product)]
    for k, label in LABELS:
        lines.append("  %-20s %12s" % (label, inr(c[k])))
    lines.append("  %-20s %12s  (%.3f%% of the order)" % ("Total charges", inr(c["total"]),
                                                       100.0 * c["total"] / value if value else 0))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Charges on an Indian equity trade (india-costs).")
    ap.add_argument("what", choices=["buy", "sell", "roundtrip"])
    ap.add_argument("value", type=float, help="order value in rupees (price x quantity)")
    ap.add_argument("--exit", type=float, help="roundtrip: the exit value in rupees")
    ap.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    ap.add_argument("--product", default="CNC", choices=["CNC", "MIS"], help="CNC delivery (default) or MIS intraday")
    ap.add_argument("--config", default="trader/config.json", help="reads \"charges\" overrides")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cfg = {}
    if a.config and os.path.exists(a.config):
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    rates = rates_from(cfg)
    try:
        if a.what == "roundtrip":
            res = roundtrip(a.value, a.exit if a.exit is not None else a.value, a.exchange, a.product, rates)
            if a.json:
                print(json.dumps(res, indent=2))
            else:
                print(render("buy", a.value, res["buy"], a.exchange, a.product))
                print(render("sell", a.exit if a.exit is not None else a.value, res["sell"], a.exchange, a.product))
                print("Gross %s, charges %s, net %s." % (inr(res["gross"]), inr(res["costs"]), inr(res["net"])))
                print("Breakeven: the position must be worth %s (+%.3f%%) before you sell to cover the charges."
                      % (inr(res["breakeven_exit_value"]), res["breakeven_move_pct"]))
        else:
            c = trade_costs(a.what, a.value, a.exchange, a.product, rates)
            print(json.dumps(c, indent=2) if a.json else render(a.what, a.value, c, a.exchange, a.product))
    except CostError as e:
        print("costs.py: %s" % e, file=sys.stderr)
        return 1
    if not a.json:
        print("Rates as of %s; your contract note is the final word. Income tax on gains is separate "
              "(india-tax)." % rates.get("as_of"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
