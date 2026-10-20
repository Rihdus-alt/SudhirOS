#!/usr/bin/env python3
"""Order tickets and the decision log for india-broker. Standard library only.

This tool never talks to a broker and never places anything. It does the
work around the one moment that matters, your yes:

  show        renders each proposed order as a plain-English ticket plus the
              exact arguments for the broker connector (Zerodha Kite MCP's
              place_order field names), or, for "manual", what to type into
              your broker's app. An order is READY only if india-risk passed
              THIS exact order today (matched by fingerprint) and nobody has
              decided on it yet.
  approve     records your explicit yes for ONE order, word for word. It
              refuses "yes to all", "ok", "sure", a yes that names a different
              stock or quantity, and any order that is not READY.
  decline     records a no.
  result      records what happened after the connector's Allow once / Deny
              card or your manual entry: placed (with the broker's order id),
              denied, or failed; with --filled-qty and --filled-price it adds
              the fill to trader/portfolio.json.
  paper-fill  "paper": true (the default): fills an approved order on paper at
              its limit (or the last price) and records it, charges included.
              No broker is involved.
  fill        adds a real fill (from the contract note) to trader/portfolio.json
  log         prints today's decisions.

    python3 ticket.py show
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py paper-fill --index 1
    python3 ticket.py result --index 1 --status placed --broker-id 260421000123456
    python3 ticket.py fill --symbol HDFCBANK --side BUY --qty 35 --price 712.40 --charges 29.67 --stt 25
    python3 ticket.py log

Fills keep the tax lots right: a buy lot's cost is price x qty plus the buy
charges except STT (the Income-tax Act does not allow STT as a cost on these
gains); a sell reduces lots first-in, first-out and is logged for india-tax.
Everything is written to trader/state/broker.json (append only).
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys

ORDER_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                "order_type", "price", "trigger_price", "validity", "disclosed_quantity", "tag")
PRODUCT_WORDS = {"CNC": "delivery (CNC)", "MIS": "intraday (MIS)", "MTF": "margin trading (MTF)",
                 "NRML": "carry forward (NRML)"}
FNO_EXCHANGES = ("NFO", "BFO")
VALIDITY_WORDS = {"DAY": "valid for today", "IOC": "immediate or cancel"}
YES_FIRST = ("yes", "y", "haan", "han", "ha")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each",
               "sab", "sabhi", "saare", "sare"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later", "hold",
               "unless", "if", "nahi", "nahin", "mat", "ruko", "lekin", "shayad"}
BROKERS = {"manual": "your broker's app (you place it yourself)", "kite": "Zerodha Kite (Kite MCP connector)",
           "other": "your broker's MCP connector"}


class TicketError(Exception):
    pass


def _sibling(folder, name):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), folder, name + ".py")
    if not os.path.exists(path):
        raise TicketError("%s/%s.py not found next to india-broker: install every india-* folder" % (folder, name))
    spec = importlib.util.spec_from_file_location("india_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = _sibling("india-costs", "costs")
inr = costs.inr


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as india-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def tool_input(order):
    """The exact arguments for the connector's place_order: Kite's field names."""
    return {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}


def plain(order):
    """BUY 35 HDFCBANK on NSE, delivery (CNC), limit Rs 715.55, valid for today"""
    side = str(order.get("transaction_type", "")).upper()
    q = order.get("quantity")
    if str(order.get("exchange", "")).upper() in FNO_EXCHANGES:
        lot = order.get("lot_size")
        lots = (" (%d lot%s of %s)" % (int(q) // int(lot), "" if int(q) // int(lot) == 1 else "s", lot)) if lot and q else ""
        what = "%s %s%s on %s" % (q, order.get("tradingsymbol", "?"), lots, order.get("exchange"))
    else:
        what = "%s share%s of %s on %s" % (q, "" if str(q) == "1" else "s", order.get("tradingsymbol", "?"),
                                           order.get("exchange", "NSE"))
    typ = str(order.get("order_type", "MARKET")).upper()
    if typ == "LIMIT":
        how = "limit %s" % inr(order.get("price"))
    elif typ == "SL":
        how = "stop-loss: trigger %s, limit %s" % (inr(order.get("trigger_price")), inr(order.get("price")))
    elif typ == "SL-M":
        how = "stop-loss market: trigger %s" % inr(order.get("trigger_price"))
    else:
        how = "market price"
    extra = " (after-market order)" if str(order.get("variety", "regular")).lower() == "amo" else ""
    if order.get("sl") is not None and not order.get("closing"):
        extra += "; stop-loss %s, TP1 %s, TP2 %s (on the %s)" % (order["sl"], order.get("tp1"), order.get("tp2"),
                                                                order.get("levels_on", "price"))
    return "%s %s, %s, %s, %s%s" % (side, what, PRODUCT_WORDS.get(str(order.get("product", "CNC")).upper(),
                                                                   order.get("product")), how,
                                    VALIDITY_WORDS.get(str(order.get("validity", "DAY")).upper(), order.get("validity")),
                                    extra)


def amount_numbers(order):
    nums = []
    for k in ("quantity", "price", "trigger_price"):
        if order.get(k) is not None:
            try:
                nums.append(float(order[k]))
            except (TypeError, ValueError):
                pass
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    first = words[1] if len(words) > 1 and words[0] == "ji" else (words[0] if words else "")
    if first not in YES_FIRST:
        return False, "only a reply that starts with \"yes\" (or \"haan\") counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("tradingsymbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{1,19}", raw.upper()))
    others = {str(s).upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(("%g" % n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "india-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by india-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting to be placed" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: india-risk PASS, waiting for your yes", rv


# ---------------------------------------------------------------- rendering

def manual_steps(order):
    o = order
    steps = ["Open your broker's app, search %s, choose %s." % (o.get("tradingsymbol"), o.get("exchange", "NSE")),
             "%s, quantity %s, product %s, order type %s%s, validity %s."
             % (str(o.get("transaction_type", "")).title(), o.get("quantity"),
                PRODUCT_WORDS.get(str(o.get("product", "CNC")).upper(), o.get("product")), o.get("order_type"),
                (", price %s" % inr(o["price"])) if o.get("price") else "",
                o.get("validity", "DAY"))]
    if o.get("trigger_price"):
        steps.append("Trigger price %s." % inr(o["trigger_price"]))
    steps.append("Check every field against this ticket, then submit it yourself.")
    return steps


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    broker = str(cfg.get("broker", "manual")).lower()
    paper = cfg.get("paper", True)
    acct = "PAPER (simulated, no broker)" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, BROKERS.get(broker, broker)), "  " + plain(order)]
    if rv and rv.get("value_inr") is not None:
        lines.append("  about %s + %s charges (worst case)" % (inr(rv["value_inr"]), inr(rv.get("est_charges", 0))))
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if paper:
        lines.append("  On paper: after your yes, ticket.py paper-fill records it at its limit or last price.")
    elif broker == "manual":
        for s in manual_steps(order):
            lines.append("  " + s)
    else:
        lines.append("  Claude will call place_order with exactly:")
        for l in json.dumps(tool_input(order), indent=2, ensure_ascii=False).splitlines():
            lines.append("    " + l)
    if ready:
        lines.append("  Reply \"yes\" to %s this one order, or \"no\" to skip it."
                     % ("paper-trade" if paper else "place"))
        if not paper and broker not in ("manual",):
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order), "fingerprint": fingerprint(order),
            "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "manual"), "tool": "place_order",
            "tool_input": tool_input(order), "why": order.get("why", "")}


# ---------------------------------------------------------------- fills -> lots

def apply_fno_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=None, exchange="NFO", lot_size=None):
    """F&O fills go to trader/fno/journal.json (business income, not capital gains); cash moves in the portfolio."""
    core = _sibling("india-fno-plan", "fnocore")
    jpath = os.path.join(os.path.dirname(portfolio_path) or ".", "fno", "journal.json")
    journal = read_json(jpath, "F&O journal", required=False) or {"positions": {}, "fills": [], "realized": []}
    journal, realized, cash = core.journal_apply(journal, symbol.upper(), side, qty, price, date, lot_size,
                                                 rates=costs.rates_from(cfg), exchange=exchange, charges=charges)
    write_json(jpath, journal)
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "currency": "INR", "positions": []}
    pf["cash"] = round(float(pf.get("cash", 0)) + cash, 2)
    return pf, {"gain": realized, "fno": True}


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=None, stt=None, exchange="NSE",
               lot_size=None):
    """Record a filled order in trader/portfolio.json and, for a sell, the realized gain for india-tax."""
    if str(exchange).upper() in FNO_EXCHANGES:
        return apply_fno_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges, str(exchange).upper(),
                              lot_size)
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "currency": "INR", "positions": []}
    symbol = str(symbol).upper()
    side = str(side).upper()
    qty, price = float(qty), float(price)
    if abs(qty - round(qty)) > 1e-9 or qty <= 0:
        raise TicketError("a fill is a positive whole number of shares, got %s" % qty)
    value = qty * price
    if charges is None:
        c = costs.trade_costs(side.lower(), value, exchange, "CNC", costs.rates_from(cfg))
        charges, stt = c["total"], c["stt"]
    stt = float(stt or 0)
    charges = float(charges)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "BUY":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": int(qty),
                     "cost": round(value + charges - stt, 2), "price": price,
                     "charges": round(charges, 2), "stt": round(stt, 2)})
        pos["shares"] = int(round(float(pos.get("shares", 0)) + qty))
        pf["cash"] = round(float(pf.get("cash", 0)) - value - charges, 2)
        return pf, None
    tax = _sibling("india-tax", "lots")
    held = float(pos.get("shares", 0)) if pos else 0
    if pos is None or held + 1e-9 < qty:
        raise TicketError("cannot record selling %s %s: the portfolio holds %s" % (int(qty), symbol, int(held)))
    lots = tax.lots_of({"positions": [pos]})[symbol]
    sale = tax.simulate_sale(lots, qty, price, tax.day(date), charges - stt)
    state_path = os.path.join(os.path.dirname(portfolio_path) or ".", "state", "tax.json")
    tstate = read_json(state_path, "tax state", required=False) or {"realized": []}
    pf, tstate = tax.record_sale(pf, symbol, sale, price, tstate)
    pf["cash"] = round(float(pf["cash"]) - stt, 2)  # record_sale added proceeds net of charges except STT
    write_json(state_path, tstate)
    return pf, sale


# ---------------------------------------------------------------- commands

def _load(a):
    doc = read_json(a.orders, "orders file (run india-budget, india-rebalance or india-tax first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    today = a.today or datetime.date.today().isoformat()
    return orders, cfg, risk, state, today


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("tradingsymbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    broker = str(cfg.get("broker", "manual")).lower()
    paper = bool(cfg.get("paper", True))
    rec = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("tradingsymbol", "")).upper(), "side": str(order.get("transaction_type")).upper(),
           "plain": plain(order), "status": "approved", "reply": a.reply, "paper": paper, "broker": broker}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    if paper:
        print("Paper account: run ticket.py paper-fill --index %d to fill it on paper. Nothing goes to a broker."
              % a.index)
    elif broker == "manual":
        print("Place it yourself in your broker's app:")
        for s in manual_steps(order):
            print("  " + s)
        print("Then: ticket.py result --index %d --status placed --broker-id <order id> "
              "[--filled-qty Q --filled-price P]" % a.index)
    else:
        print("Now call place_order with exactly:")
        print(json.dumps(tool_input(order), indent=2, ensure_ascii=False))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("tradingsymbol", "")).upper(), "side": str(order.get("transaction_type")).upper(),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def _approved(state, order, today, index):
    rec = latest(state, fingerprint(order), today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % index)
    return rec


def cmd_result(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    rec = _approved(state, order, today, a.index)
    new = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": rec["fingerprint"], "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        pf, _ = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today,
                           exchange=order.get("exchange", "NSE"))
        write_json(a.portfolio, pf)
        print("Added the fill to %s (charges estimated; correct them from the contract note with ticket.py fill)."
              % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it from the contract note: ticket.py fill --symbol %s --side %s --qty Q "
              "--price P --charges C --stt S" % (rec["symbol"], rec["side"]))
    return 0


def cmd_paper_fill(a):
    orders, cfg, risk, state, today = _load(a)
    if not cfg.get("paper", True):
        raise TicketError("paper-fill is for a paper account; this config is LIVE (\"paper\": false)")
    order = _pick(orders, a.index)
    rec = _approved(state, order, today, a.index)
    pdoc = read_json(a.prices, "prices", required=False) or {}
    ltp = (pdoc.get("prices") or {}).get(rec["symbol"])
    typ = str(order.get("order_type", "MARKET")).upper()
    if typ == "LIMIT":
        lim = float(order["price"])
        if ltp and ((rec["side"] == "BUY" and ltp > lim) or (rec["side"] == "SELL" and ltp < lim)):
            raise TicketError("on paper the limit %s would not fill at the last price %s" % (inr(lim), inr(ltp)))
        price = min(lim, ltp) if (ltp and rec["side"] == "BUY") else (max(lim, ltp) if ltp else lim)
    else:
        if not ltp:
            raise TicketError("no last price for %s in %s to fill a market order on paper" % (rec["symbol"], a.prices))
        price = float(ltp)
    pf, sale = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], order["quantity"], price, today,
                          exchange=order.get("exchange", "NSE"), lot_size=order.get("lot_size"))
    write_json(a.portfolio, pf)
    state["orders"].append({"at": now_iso(), "date": today, "index": a.index, "fingerprint": rec["fingerprint"],
                            "symbol": rec["symbol"], "side": rec["side"], "plain": rec["plain"], "status": "placed",
                            "paper": True, "fill_price": price, "broker_order_id": "PAPER-%s-%d" % (today, a.index)})
    write_json(a.state, state)
    print("Paper fill: %s %s %s at %s. Cash now %s." % (rec["side"], order["quantity"], rec["symbol"], inr(price),
                                                        inr(pf["cash"])))
    if sale and (sale["gain"] or not sale.get("fno")):
        print("Realized %s (logged for %s)." % (inr(sale["gain"]), "the F&O journal" if sale.get("fno") else "india-tax"))
    if str(order.get("exchange", "")).upper() in FNO_EXCHANGES and order.get("then") and rec["side"] in ("BUY", "SELL") \
            and not order.get("closing"):
        sl = order["then"][0]
        print("Now protect it: %s %s %s, SL trigger %s, limit %s (india-fno-plan trail moves it later)."
              % (sl["transaction_type"], sl["quantity"], rec["symbol"], inr(sl["trigger_price"]), inr(sl["price"])))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    date = a.date or datetime.date.today().isoformat()
    exch = a.exchange.upper()
    if exch in ("NSE", "BSE") and re.match(r"^[A-Z&\-]+\d{2}[A-Z0-9]{3}[\d.]*(CE|PE|FUT)$", a.symbol.upper()):
        exch = "NFO" if exch == "NSE" else "BFO"
    pf, sale = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, a.charges, a.stt, exch)
    write_json(a.portfolio, pf)
    print("Recorded %s %s %s at %s on %s. Cash now %s." % (a.side.upper(), a.qty, a.symbol.upper(), inr(a.price),
                                                          date, inr(pf["cash"])))
    if sale and (sale["gain"] or not sale.get("fno")):
        print("Realized %s (logged for %s)." % (inr(sale["gain"]), "the F&O journal" if sale.get("fno") else "india-tax"))
    return 0


def cmd_log(a):
    state = load_state(a.state)
    today = a.today or datetime.date.today().isoformat()
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        print("%s  #%s %-9s %s%s%s" % (r.get("at"), r.get("index"), r.get("status"), r.get("plain"), reply, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (india-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--today", help="YYYY-MM-DD (default: today)")

    p = sub.add_parser("show")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("paper-fill")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, type=str.upper, choices=["BUY", "SELL"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--charges", type=float, help="total charges on the contract note (default: estimated)")
    p.add_argument("--stt", type=float, help="the STT part of those charges")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--date")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("log")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--today")
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "paper-fill": cmd_paper_fill, "fill": cmd_fill, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
