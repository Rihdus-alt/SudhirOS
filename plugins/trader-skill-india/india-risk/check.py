#!/usr/bin/env python3
"""Pre-trade risk check for india-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json), prices
and exchange limits (trader/prices.json). Each order gets PASS or BLOCK with
the reasons. Orders are checked in file order as if each PASS had filled, so
five buys that are fine one by one but break the cash buffer together are
caught. Charges (STT, stamp duty, exchange, SEBI, GST, DP) are counted.

    python3 check.py                       # the trader/ files, writes trader/state/risk.json
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write            # check without recording the verdicts

What blocks an order:

  format        missing symbol, exchange not NSE or BSE, transaction_type not
                BUY or SELL, quantity not a whole number of shares (Indian
                exchanges have no fractional shares), unknown product, order
                type, validity or variety, a LIMIT without a price, an SL
                without a trigger, a price not in whole paise
  product       MIS (intraday) unless "allow_intraday", MTF unless "allow_mtf",
                NRML (futures and options) unless "allow_fno"
  asset         a futures or options contract unless "allow_fno"; anything
                that is not an NSE symbol or a 6-digit BSE code
  blocked       a symbol on your "blocked" list
  band          a BUY limit above the day's upper circuit, a SELL limit below
                the lower circuit (the exchange rejects these)
  surveillance  the stock is under ASM / GSM / ESM surveillance (from the NSE
                quote) while "block_surveillance" is true (the default);
                MIS on a trade-to-trade (BE) stock
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy plus its charges would dip into your cash buffer, or
                need margin
  short         selling more than you hold: delivery shares you do not own
                cannot be sold (no retail short selling in CNC)
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   more than max_orders_per_day placed today

Warnings do not block, but say them: a LIVE account, a price at the circuit,
a narrow price band, a T2T or ASM stock, stale prices, a MARKET order.

Exit code 0 when every order passes, 1 when any order is blocked.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 5, "max_position_pct": 25, "max_order_inr": 50000,
    "min_order_inr": 500, "max_orders_per_day": 10, "paper": True, "blocked": [],
    "allow_fno": False, "allow_intraday": False, "allow_mtf": False, "allow_short": False,
    "block_surveillance": True, "market_slippage_pct": 0.5, "max_price_age_hours": 30,
}
ORDER_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                "order_type", "price", "trigger_price", "validity", "disclosed_quantity", "tag")
PRODUCTS = {"CNC", "MIS", "MTF", "NRML"}
TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALIDITIES = {"DAY", "IOC"}
VARIETIES = {"regular", "amo"}
EQUITY = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
BSE_CODE = re.compile(r"^\d{6}$")
DERIVATIVE = re.compile(r"^[A-Z&\-]+\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|[1-9OND]\d{2})"
                        r"[\d.]*(CE|PE|FUT)$")
PAISA = 0.005


class RiskError(Exception):
    pass


def _load_costs():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-costs", "costs.py")
    if not os.path.exists(path):
        raise RiskError("india-costs/costs.py not found next to india-risk: install every india-* folder")
    spec = importlib.util.spec_from_file_location("india_costs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = _load_costs()
inr = costs.inr


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in india-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm(sym):
    s = str(sym or "").strip().upper()
    return s[4:] if s.startswith("NSE:") else s


def account(cfg, portfolio, prices):
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number (rupees)")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = norm(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not t:
            continue
        n = _f(p.get("shares", p.get("quantity", p.get("qty"))))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def placed_today(state, today):
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and str(rec.get("date") or str(rec.get("at", ""))[:10]) == today:
            out.append(rec)
    return out


def _is_int(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    f = _f(v)
    return f is not None and abs(f - round(f)) < 1e-9


def check(orders, cfg, portfolio=None, prices=None, details=None, state=None, today=None, prices_age_hours=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    rates = costs.rates_from(c)
    prices = {norm(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    details = {norm(k): v for k, v in (details or {}).items()}
    today = today or datetime.date.today().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {norm(b) for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    placed_keys = {(norm(r.get("symbol")), str(r.get("side", "")).upper()) for r in done_today}
    seen, results = set(), []
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    if prices_age_hours is not None and prices_age_hours > float(c.get("max_price_age_hours") or 30):
        top_warn.append("prices are %.0f hours old: refresh them (india-alerts) before trusting the sizes"
                        % prices_age_hours)
    sold_today = set()

    for i, o in enumerate(orders, 1):
        reasons, warns = [], []
        sym = norm(o.get("tradingsymbol") or o.get("symbol"))
        exch = str(o.get("exchange", "NSE")).strip().upper()
        side = str(o.get("transaction_type", o.get("side", ""))).strip().upper()
        product = str(o.get("product", "CNC")).strip().upper()
        typ = str(o.get("order_type", "MARKET")).strip().upper()
        validity = str(o.get("validity", "DAY")).strip().upper()
        variety = str(o.get("variety", "regular")).strip().lower()
        qty = o.get("quantity")
        price, trig = o.get("price"), o.get("trigger_price")
        d = details.get(sym) or {}
        value = None

        # format
        if not sym:
            reasons.append("format: no tradingsymbol")
        if exch not in ("NSE", "BSE"):
            reasons.append("format: exchange must be NSE or BSE for shares, got %r" % o.get("exchange"))
        if side not in ("BUY", "SELL"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r" % o.get("transaction_type"))
        if qty is None or not _is_int(qty) or _f(qty) <= 0:
            reasons.append("format: quantity must be a positive whole number of shares, got %r" % qty)
        elif not isinstance(qty, int):
            warns.append("send quantity as a whole number (%d), not %r" % (int(round(_f(qty))), qty))
        if product not in PRODUCTS:
            reasons.append("format: unknown product %r (CNC, MIS, MTF, NRML)" % product)
        if typ not in TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT, SL, SL-M)" % typ)
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r (DAY, IOC)" % validity)
        if variety not in VARIETIES:
            reasons.append("format: variety %r is not allowed here (regular or amo)" % variety)
        if typ in ("LIMIT", "SL") and (_f(price) is None or _f(price) <= 0):
            reasons.append("format: a %s order needs a positive price" % typ)
        if typ in ("SL", "SL-M") and (_f(trig) is None or _f(trig) <= 0):
            reasons.append("format: a %s order needs a trigger_price" % typ)
        if typ == "MARKET" and _f(price):
            reasons.append("format: a MARKET order must not carry a price")
        for k, v in (("price", price), ("trigger_price", trig)):
            fv = _f(v)
            if fv:
                if abs(fv * 100 - round(fv * 100)) > 1e-6:
                    reasons.append("format: %s %s is not in whole paise" % (k, v))
                else:
                    tick = _f(d.get("tick_size"))
                    if tick and abs(fv / tick - round(fv / tick)) > 1e-6:
                        reasons.append("format: %s %s is not on %s's tick of %s" % (k, v, sym, tick))
                    elif not tick and abs(fv * 20 - round(fv * 20)) > 1e-6:
                        warns.append("%s %s is not a multiple of 5 paise; check the tick size in your broker"
                                     % (k, v))

        # product rules
        if product == "MIS" and not c.get("allow_intraday"):
            reasons.append("product: MIS is intraday trading; this pack is set to delivery only "
                           "(\"allow_intraday\": false)")
        if product == "MTF" and not c.get("allow_mtf"):
            reasons.append("product: MTF buys with borrowed money; margin is off (\"allow_mtf\": false)")
        if product == "NRML" and not c.get("allow_fno"):
            reasons.append("product: NRML is for futures and options; F&O is off (\"allow_fno\": false)")

        # asset
        if sym and DERIVATIVE.match(sym):
            if not c.get("allow_fno"):
                reasons.append("asset: %s is a futures or options contract; F&O is off (\"allow_fno\": false)" % sym)
        elif sym and not (EQUITY.match(sym) or (exch == "BSE" and BSE_CODE.match(sym))):
            reasons.append("asset: %s is not an NSE symbol or a BSE code" % sym)
        if sym in blocked:
            reasons.append("blocked: %s is on your blocked list" % sym)

        # surveillance and band
        surv = str(d.get("surveillance") or "")
        if surv and surv.upper() not in ("-", "NONE", "NULL"):
            if c.get("block_surveillance", True):
                reasons.append("surveillance: %s is under exchange surveillance (%s); set \"block_surveillance\": "
                               "false only if you understand ASM/GSM" % (sym, surv))
            else:
                warns.append("%s is under exchange surveillance (%s): extra margins and limits apply" % (sym, surv))
        if str(d.get("series") or "").upper() in ("BE", "BZ"):
            if product == "MIS":
                reasons.append("surveillance: %s trades in the %s (trade-to-trade) series: delivery only, no "
                               "intraday" % (sym, d["series"]))
            else:
                warns.append("%s is trade-to-trade (series %s): every trade is settled by delivery"
                             % (sym, d["series"]))
        up, lo = _f(d.get("upper_circuit")), _f(d.get("lower_circuit"))
        lp = _f(price) if typ in ("LIMIT", "SL") else None
        if lp and up and side == "BUY" and lp > up + PAISA:
            reasons.append("band: buy limit %s is above today's upper circuit %s; the exchange rejects it"
                           % (inr(lp), inr(up)))
        if lp and lo and side == "SELL" and lp < lo - PAISA:
            reasons.append("band: sell limit %s is below today's lower circuit %s; the exchange rejects it"
                           % (inr(lp), inr(lo)))
        ltp = prices.get(sym)
        if ltp and up and side == "BUY" and ltp >= up - PAISA:
            warns.append("%s is at its upper circuit (%s): sellers are scarce, a buy may not fill" % (sym, inr(up)))
        if ltp and lo and side == "SELL" and ltp <= lo + PAISA:
            warns.append("%s is at its lower circuit (%s): a sell may not fill" % (sym, inr(lo)))
        band = str(d.get("price_band") or "")
        if band in ("2", "5") and typ == "MARKET":
            warns.append("%s has a %s%% price band: a LIMIT order is safer than MARKET" % (sym, band))
        if typ == "MARKET" and not band:
            warns.append("MARKET order: the fill price is not known in advance; a LIMIT caps it")

        # rupee value of the order (worst case)
        ref = None
        if typ in ("LIMIT", "SL"):
            ref = _f(price)
        elif typ == "SL-M":
            ref = _f(trig)
        if ref is None and ltp:
            slip = float(c.get("market_slippage_pct") or 0) / 100.0
            ref = ltp * (1 + slip) if side == "BUY" else ltp * (1 - slip)
        if _is_int(qty) and _f(qty) and _f(qty) > 0:
            if ref:
                value = _f(qty) * ref
            else:
                reasons.append("price: no price for %s to value a %s-share order; refresh trader/prices.json "
                               "(india-alerts)" % (sym, qty))
        charges = 0.0
        if value is not None and side in ("BUY", "SELL"):
            charges = costs.trade_costs(side.lower(), value, exch if exch in ("NSE", "BSE") else "NSE",
                                        "MIS" if product == "MIS" else "CNC", rates,
                                        dp=(sym not in sold_today))["total"]
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                reasons.append("size: %s is under your %s minimum order" % (inr(value), inr(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                reasons.append("size: %s is over your %s max order" % (inr(value), inr(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "BUY":
            after = cash - value - charges
            if after < -PAISA and not (c.get("allow_mtf") and product == "MTF"):
                reasons.append("cash: needs %s (%s + %s charges) but only %s cash; that would be margin"
                               % (inr(value + charges), inr(value), inr(charges), inr(cash)))
            elif after < buffer_inr - PAISA:
                reasons.append("cash: leaves %s cash, under your %s%% buffer of %s"
                               % (inr(max(after, 0)), _pct(c["cash_buffer_pct"]), inr(buffer_inr)))
            pos_after = held_value + (_f(qty) * (ltp or ref) if _f(qty) else value)
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + 1.0:
                reasons.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                               % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total),
                                  _pct(c["max_position_pct"])))
        if side == "SELL" and sym and _is_int(qty) and _f(qty):
            if _f(qty) > held + 1e-9 and not (c.get("allow_short") and product == "MIS"):
                reasons.append("short: selling %s %s but you hold %s; delivery shares you do not own cannot "
                               "be sold" % (_num(_f(qty)), sym, _num(held)))

        key = (sym, side)
        if key in seen:
            reasons.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            reasons.append("duplicate: a %s %s was already placed today" % (side, sym))
        if (sym, "SELL" if side == "BUY" else "BUY") in seen | placed_keys and product == "CNC":
            warns.append("a BUY and a SELL of %s on the same day are netted by the broker as an intraday "
                         "trade (speculative income for tax), not a delivery sale and re-buy" % sym)
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            reasons.append("daily limit: %d placed today plus %d passed earlier in this batch, your limit is %d a day"
                           % (count_today, passed_so_far, cap_n))
        seen.add(key)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and value is not None:
            q = _f(qty)
            mark = ltp or ref
            if side == "BUY":
                acct["cash"] -= value + charges
                acct["values"][sym] = acct["values"].get(sym, 0.0) + q * mark
                acct["shares"][sym] = held + q
            else:
                acct["cash"] += value - charges
                acct["values"][sym] = max(acct["values"].get(sym, 0.0) - q * mark, 0.0)
                acct["shares"][sym] = held - q
                sold_today.add(sym)
        results.append({
            "index": i, "symbol": sym, "exchange": exch, "side": side, "quantity": qty, "order_type": typ,
            "price": price, "value_inr": round(value, 2) if value is not None else None,
            "est_charges": round(charges, 2), "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns,
        })

    return {"date": today, "paper": bool(c.get("paper", True)), "results": results,
            "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
            "warnings": top_warn, "cash_after": round(acct["cash"], 2)}


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        how = r["order_type"] + ((" at " + inr(r["price"])) if _f(r.get("price")) else "")
        lines.append("%-5s #%d %s %s x %s:%s, %s, about %s + %s charges"
                     % (r["verdict"], r["index"], r["side"], r["quantity"], r["exchange"], r["symbol"], how,
                        inr(r["value_inr"] or 0), inr(r["est_charges"])))
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked. Cash after the passed orders, worst case: %s (%s account)."
                 % (res["pass"], res["block"], inr(res["cash_after"]), "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. india-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def _age_hours(as_of):
    if not as_of:
        return None
    try:
        t = datetime.datetime.strptime(str(as_of)[:19].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - t).total_seconds() / 3600.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (india-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what india-broker placed today")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--out", default="trader/state/risk.json")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        doc = read_json(a.orders, "orders file (run india-budget, india-rebalance or india-tax first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices = pdoc.get("prices", pdoc) if isinstance(pdoc, dict) else {}
        state = read_json(a.state, "broker state", required=False)
        age = None if a.today else _age_hours(pdoc.get("as_of"))
        res = check(orders, cfg, portfolio, prices, pdoc.get("detail") or {}, state, a.today, age)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now().replace(microsecond=0).isoformat()
            rec["orders_file"] = a.orders
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
