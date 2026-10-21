#!/usr/bin/env python3
"""Insider trades, promoter pledges, and bulk and block deals for india-insider.
Standard library only.

India's version of watching what insiders do, from three public feeds:

  pit     SEBI (Prohibition of Insider Trading) Regulations 2015, Reg 7(2):
          promoters, directors, key managerial people and designated
          employees must tell the company within 2 trading days of trading
          over Rs 10 lakh in a quarter, and the company tells the exchange
          within 2 trading days. NSE publishes every disclosure:
          www.nseindia.com/api/corporates-pit
  deals   bulk deals (one client trading more than 0.5% of a company's shares
          in a day) and block deals (large trades in the block window), the
          same evening: nsearchives.nseindia.com/content/equities/bulk.csv
          and block.csv, or the date-range API for history
  pledge  promoter pledge creation, revocation and invocation, from the same
          insider disclosures (a rising pledge is a risk flag)

    python3 insider.py pit --from 2026-10-01 --to 2026-10-19            # every disclosure
    python3 insider.py pit --symbol INFY --days 90
    python3 insider.py pit --promoters --buys --min-value 10000000      # promoter market buys over Rs 1 crore
    python3 insider.py pit --clusters                                   # 2+ insiders buying the same stock
    python3 insider.py deals --kind bulk --client "kacholia"            # what a known investor traded
    python3 insider.py deals --kind block --days 7
    python3 insider.py new --watch-holdings --investors trader/investors.json   # only what is new; exit 1 if nothing
    --offline DIR reads DIR/pit.json, DIR/bulk.csv, DIR/block.csv instead of the network.

Everything is public and published after the trade. It says what an insider
did, never what they know.
"""

import argparse
import csv
import datetime
import http.cookiejar
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 trader-skill-india/1.0")
PIT_API = "https://www.nseindia.com/api/corporates-pit?index=equities&from_date=%s&to_date=%s"
DEAL_API = "https://www.nseindia.com/api/historical/%s-deals?from=%s&to=%s"
DEAL_CSV = "https://nsearchives.nseindia.com/content/equities/%s.csv"
INSIDER_CATS = ("promoter", "promoter group", "director", "key managerial personnel", "kmp", "designated employee",
                "immediate relative")


class InsiderError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.warm = False

    def _get(self, url, referer=None):
        h = {"User-Agent": UA, "Accept": "application/json,text/csv,*/*", "Accept-Language": "en-IN,en;q=0.9"}
        if referer:
            h["Referer"] = referer
        try:
            with self.opener.open(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read().decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            raise InsiderError("%s answered HTTP %d" % (url.split("?")[0], e.code))
        except urllib.error.URLError as e:
            raise InsiderError("could not reach %s: %s" % (url.split("/")[2], e.reason))

    def nse(self, url):
        if not self.warm:
            self._get("https://www.nseindia.com/")
            self.warm = True
        return self._get(url, referer="https://www.nseindia.com/")

    def read(self, name, url, nse=True):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.exists(path):
                raise InsiderError("no offline file %s" % path)
            with open(path, encoding="utf-8-sig") as fh:
                return fh.read()
        return self.nse(url) if nse else self._get(url)


def dmy(d):
    return d.strftime("%d-%m-%Y")


def parse_day(s):
    s = str(s or "").strip()
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%B-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "NIL", "Nil"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- insider disclosures (PIT)

def g(r, *keys):
    for k in keys:
        if r.get(k) not in (None, "", "-"):
            return r[k]
    return None


def kind_of(txn, mode):
    t, m = str(txn or "").lower(), str(mode or "").lower()
    if "pledge" in t or "pledge" in m or "invocation" in t or "revok" in t:
        if "revok" in t or "release" in t or "revok" in m:
            return "pledge revoked"
        if "invoc" in t or "invoc" in m:
            return "pledge invoked"
        return "pledge created"
    if "esop" in m or "esos" in m:
        return "esop"
    if any(w in m for w in ("gift", "inter-se", "inter se", "off market", "off-market", "transmission", "inheritance")):
        return "off-market"
    if "buy" in t or "acqui" in t or "purchase" in m:
        return "market buy" if ("market" in m or not m) else "buy"
    if "sell" in t or "sale" in t or "dispos" in t or "sale" in m:
        return "market sell" if ("market" in m or not m) else "sell"
    return t or "other"


def parse_pit(text):
    try:
        d = json.loads(text)
    except ValueError:
        raise InsiderError("the insider-trading feed did not return JSON (NSE may have blocked the request)")
    rows = d.get("data", d) if isinstance(d, dict) else d
    out = []
    for r in rows or []:
        sym = str(g(r, "symbol") or "").upper()
        if not sym:
            continue
        frm, to = parse_day(g(r, "acqfromDt", "acqFromDt")), parse_day(g(r, "acqtoDt", "acqToDt"))
        posted = parse_day(g(r, "date", "broadcastDate", "intimDt"))
        qty, val = num(g(r, "secAcq")), num(g(r, "secVal"))
        txn, mode = g(r, "tdpTransactionType", "transactionType"), g(r, "acqMode")
        out.append({
            "symbol": sym, "company": g(r, "company") or "", "person": g(r, "acqName") or "",
            "category": g(r, "personCategory") or "", "security": g(r, "secType") or "",
            "transaction": txn or "", "mode": mode or "", "kind": kind_of(txn, mode),
            "shares": qty, "value_inr": val, "avg_price": round(val / qty, 2) if qty and val else None,
            "before_pct": num(g(r, "befAcqSharesPer")), "after_pct": num(g(r, "afterAcqSharesPer")),
            "traded_from": frm.isoformat() if frm else None, "traded_to": to.isoformat() if to else None,
            "told_company": (parse_day(g(r, "intimDt")) or posted).isoformat() if (parse_day(g(r, "intimDt")) or posted) else None,
            "posted": posted.isoformat() if posted else None,
            "delay_days": (posted - to).days if posted and to else None,
            "exchange": g(r, "exchange") or "NSE", "xbrl": g(r, "xbrl"), "remarks": g(r, "remarks") or "",
        })
    return out


def is_promoter(row):
    c = row["category"].lower()
    return "promoter" in c


def is_insider(row):
    c = row["category"].lower()
    return any(k in c for k in INSIDER_CATS)


def filter_pit(rows, symbol=None, promoters=False, buys=False, sells=False, min_value=0.0, person=None):
    out = []
    for r in rows:
        if symbol and r["symbol"] != symbol.upper():
            continue
        if promoters and not is_promoter(r):
            continue
        if buys and r["kind"] != "market buy":
            continue
        if sells and r["kind"] != "market sell":
            continue
        if min_value and (r["value_inr"] or 0) < min_value:
            continue
        if person and person.lower() not in r["person"].lower():
            continue
        out.append(r)
    return out


def clusters(rows, window_days=30, min_people=2):
    """Stocks where 2+ different insiders bought in the market inside the window."""
    by = {}
    for r in rows:
        if r["kind"] == "market buy" and is_insider(r) and r["traded_to"]:
            by.setdefault(r["symbol"], []).append(r)
    out = []
    for sym, rs in by.items():
        rs.sort(key=lambda r: r["traded_to"])
        for i, first in enumerate(rs):
            start = datetime.date.fromisoformat(first["traded_to"])
            inside = [r for r in rs[i:] if (datetime.date.fromisoformat(r["traded_to"]) - start).days <= window_days]
            people = sorted({r["person"] for r in inside})
            if len(people) >= min_people:
                out.append({"symbol": sym, "company": first["company"], "people": people,
                            "from": first["traded_to"], "to": inside[-1]["traded_to"],
                            "value_inr": sum(r["value_inr"] or 0 for r in inside), "rows": len(inside)})
                break
    out.sort(key=lambda c: -c["value_inr"])
    return out


# ---------------------------------------------------------------- bulk and block deals

def parse_deals_csv(text, kind):
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n or h.startswith(n):
                    return i
        return None

    ci = {"date": col("date"), "symbol": col("symbol"), "name": col("security name", "name"),
          "client": col("client name", "client"), "side": col("buy/sell", "buy / sell"),
          "qty": col("quantity traded", "quantity"), "price": col("trade price", "price"),
          "remarks": col("remarks")}
    if ci["symbol"] is None or ci["client"] is None:
        raise InsiderError("the %s deals file has no Symbol / Client Name columns" % kind)
    out = []
    for r in rows[1:]:
        if len(r) <= max(v for v in ci.values() if v is not None):
            continue
        get = lambda k: r[ci[k]].strip() if ci[k] is not None else ""
        if not get("symbol") or get("symbol").lower().startswith("no records"):
            continue
        d = parse_day(get("date"))
        q, p = num(get("qty")), num(get("price"))
        out.append({"kind": kind, "date": d.isoformat() if d else get("date"), "symbol": get("symbol").upper(),
                    "company": get("name"), "client": get("client"), "side": get("side").upper(),
                    "shares": q, "price": p, "value_inr": round(q * p, 2) if q and p else None,
                    "remarks": get("remarks")})
    return out


def parse_deals_api(text, kind):
    try:
        d = json.loads(text)
    except ValueError:
        raise InsiderError("the %s deals API did not return JSON" % kind)
    out = []
    for r in d.get("data", []):
        dd = parse_day(g(r, "BD_DT_DATE", "date"))
        q, p = num(g(r, "BD_QTY_TRD", "qty")), num(g(r, "BD_TP_WATP", "watp", "price"))
        out.append({"kind": kind, "date": dd.isoformat() if dd else None, "symbol": str(g(r, "BD_SYMBOL", "symbol") or "").upper(),
                    "company": g(r, "BD_SCRIP_NAME", "name") or "", "client": g(r, "BD_CLIENT_NAME", "clientName") or "",
                    "side": str(g(r, "BD_BUY_SELL", "buySell") or "").upper(), "shares": q, "price": p,
                    "value_inr": round(q * p, 2) if q and p else None, "remarks": g(r, "BD_REMARKS", "remarks") or ""})
    return out


def match_client(name, wanted):
    """Every word of a wanted name must be a whole word of the client name."""
    words = set(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())
    return any(all(w in words for w in re.sub(r"[^a-z0-9 ]", " ", x.lower()).split()) for x in wanted if x.strip())


# ---------------------------------------------------------------- state and output

def inr(x):
    if x is None:
        return "?"
    x = float(x)
    if abs(x) >= 1e7:
        return "₹%.2f cr" % (x / 1e7)
    if abs(x) >= 1e5:
        return "₹%.2f lakh" % (x / 1e5)
    return "₹{:,.0f}".format(x)


def indian(n):
    whole = "%d" % round(abs(n))
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups)
    return ("-" if n < 0 else "") + whole


def round_trips(rows):
    """Mark a client who bought AND sold the same stock the same day (trading, not a position)."""
    sides = {}
    for r in rows:
        sides.setdefault((r["date"], r["symbol"], r["client"].upper()), set()).add(r["side"][:1])
    for r in rows:
        r["round_trip"] = sides[(r["date"], r["symbol"], r["client"].upper())] >= {"B", "S"}
    return rows


def key_pit(r):
    return "pit|%s|%s|%s|%s|%s" % (r["symbol"], r["person"], r["traded_to"], r["shares"], r["transaction"])


def key_deal(r):
    return "%s|%s|%s|%s|%s|%s" % (r["kind"], r["date"], r["symbol"], r["client"], r["side"], r["shares"])


def holdings(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return {str(p.get("ticker") or p.get("symbol") or "").upper() for p in d.get("positions", [])} | \
        {str(w.get("ticker") or "").upper() for w in d.get("weights", [])}


def line_pit(r, mine=()):
    flag = "  [YOU HOLD]" if r["symbol"] in mine else ""
    late = ""
    if r["delay_days"] is not None and r["delay_days"] > 6:
        late = ", posted %d days after the trade (late?)" % r["delay_days"]
    pct = ""
    if r["before_pct"] is not None and r["after_pct"] is not None:
        pct = ", holding %.2f%% -> %.2f%%" % (r["before_pct"], r["after_pct"])
    priced = r["kind"] in ("market buy", "market sell", "buy", "sell")
    return ("%-12s %s (%s): %s %s shares, %s%s, traded %s%s, posted %s%s%s"
            % (r["symbol"], r["person"], r["category"] or "?", r["kind"],
               indian(r["shares"]) if r["shares"] else "?",
               ("value " if priced else "valued at ") + inr(r["value_inr"]),
               (" at about ₹%.2f a share" % r["avg_price"]) if (r["avg_price"] and priced) else "",
               r["traded_to"] or "?", pct, r["posted"] or "?", late, flag))


def line_deal(r, mine=()):
    flag = "  [YOU HOLD]" if r["symbol"] in mine else ""
    rt = "  (bought and sold the same day: trading, not a position)" if r.get("round_trip") else ""
    return ("%-5s %s %-12s %s %s %s shares at ₹%.2f (%s)%s%s"
            % (r["kind"].upper(), r["date"], r["symbol"], r["client"], r["side"],
               indian(r["shares"]) if r["shares"] else "?", r["price"] or 0, inr(r["value_inr"]), rt, flag))


def _range(a):
    to = parse_day(a.to) if a.to else datetime.date.today()
    frm = parse_day(a.frm) if a.frm else to - datetime.timedelta(days=a.days)
    if not frm or not to:
        raise InsiderError("dates are YYYY-MM-DD")
    return frm, to


def get_pit(f, frm, to, symbol=None):
    url = PIT_API % (dmy(frm), dmy(to))
    if symbol:
        url += "&symbol=" + urllib.parse.quote(symbol.upper())
    return parse_pit(f.read("pit.json", url))


def get_deals(f, kind, frm, to):
    today = datetime.date.today()
    if f.offline or (frm >= today - datetime.timedelta(days=1)):
        try:
            return parse_deals_csv(f.read("%s.csv" % kind, DEAL_CSV % kind, nse=False), kind)
        except InsiderError:
            if f.offline:
                raise
    return parse_deals_api(f.read("%s.json" % kind, DEAL_API % (kind, dmy(frm), dmy(to))), kind)


def cmd_pit(a):
    f = Fetcher(a.offline)
    frm, to = _range(a)
    rows = get_pit(f, frm, to, a.symbol)
    if a.offline:  # the live API filters by date itself
        rows = [r for r in rows if not r["posted"] or frm.isoformat() <= r["posted"] <= to.isoformat()]
    sel = filter_pit(rows, a.symbol, a.promoters, a.buys, a.sells, a.min_value, a.person)
    if a.pledges:
        sel = [r for r in sel if r["kind"].startswith("pledge")]
    mine = holdings(a.portfolio)
    if a.clusters:
        cl = clusters(rows, a.window)
        if a.json:
            print(json.dumps(cl, indent=2, ensure_ascii=False))
        else:
            for c in cl:
                print("CLUSTER %-12s %d insiders bought in the market %s to %s, %s: %s"
                      % (c["symbol"], len(c["people"]), c["from"], c["to"], inr(c["value_inr"]), "; ".join(c["people"])))
            if not cl:
                print("No stock had 2 or more insiders buying in the market within %d days." % a.window)
        return 0 if cl else 1
    if a.json:
        print(json.dumps(sel, indent=2, ensure_ascii=False))
        return 0 if sel else 1
    sel.sort(key=lambda r: (r["posted"] or "", r["value_inr"] or 0), reverse=True)
    for r in sel:
        print(line_pit(r, mine))
    if not sel:
        print("No insider disclosures match, %s to %s." % (frm, to))
    print("Source: NSE insider-trading disclosures (SEBI PIT Reg 7(2)), published after the trade. "
          "An insider's trade is public information, not a forecast.")
    return 0 if sel else 1


def cmd_deals(a):
    f = Fetcher(a.offline)
    frm, to = _range(a)
    rows = []
    for kind in (["bulk", "block"] if a.kind == "both" else [a.kind]):
        rows += get_deals(f, kind, frm, to)
    round_trips(rows)
    if a.client:
        rows = [r for r in rows if match_client(r["client"], a.client)]
    if a.symbol:
        rows = [r for r in rows if r["symbol"] == a.symbol.upper()]
    mine = holdings(a.portfolio)
    rows.sort(key=lambda r: (r["date"] or "", r["value_inr"] or 0), reverse=True)
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0 if rows else 1
    for r in rows:
        print(line_deal(r, mine))
    if not rows:
        print("No %s deals match." % a.kind)
    print("Bulk deal: one client traded over 0.5% of the company's shares that day. A client name can be a fund, "
          "a broker's client account or a person; same-day buy and sell by one client is often trading, not investing.")
    return 0 if rows else 1


def cmd_new(a):
    f = Fetcher(a.offline)
    to = parse_day(a.to) if a.to else datetime.date.today()
    frm = to - datetime.timedelta(days=a.days)
    state = {}
    if os.path.exists(a.state):
        with open(a.state, encoding="utf-8") as fh:
            state = json.load(fh)
    seen = set(state.get("seen", []))
    mine = holdings(a.portfolio) if a.watch_holdings else set()
    investors = []
    if a.investors and os.path.exists(a.investors):
        with open(a.investors, encoding="utf-8") as fh:
            inv = json.load(fh)
        investors = inv.get("names", inv) if isinstance(inv, dict) else inv
    alerts = []
    errors = []
    try:
        for r in get_pit(f, frm, to):
            k = key_pit(r)
            if k in seen:
                continue
            interesting = (r["symbol"] in mine) or (is_promoter(r) and r["kind"] in ("market buy", "market sell")
                                                    and (r["value_inr"] or 0) >= a.min_value) or \
                r["kind"] in ("pledge created", "pledge invoked") and r["symbol"] in mine
            if interesting:
                alerts.append(("insider", r))
            seen.add(k)
    except InsiderError as e:
        errors.append(str(e))
    for kind in ("bulk", "block"):
        try:
            for r in round_trips(get_deals(f, kind, frm, to)):
                k = key_deal(r)
                if k in seen:
                    continue
                if r["symbol"] in mine or (investors and match_client(r["client"], investors)):
                    alerts.append(("deal", r))
                seen.add(k)
        except InsiderError as e:
            errors.append(str(e))
    if not a.dry_run:
        d = os.path.dirname(a.state)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump({"seen": sorted(seen)[-20000:], "last_check": datetime.datetime.now().replace(microsecond=0).isoformat()},
                      fh, indent=1)
    if a.json:
        print(json.dumps({"alerts": [dict(r, type=t) for t, r in alerts], "errors": errors}, indent=2, ensure_ascii=False))
    else:
        for t, r in alerts:
            print("NEW " + (line_pit(r, mine) if t == "insider" else line_deal(r, mine)))
        if not alerts:
            print("Nothing new since the last check.")
        for e in errors:
            print("error: " + e)
    return 0 if alerts else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider trades, pledges, bulk and block deals (india-insider).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--from", dest="frm", help="YYYY-MM-DD")
        p.add_argument("--to", help="YYYY-MM-DD (default today)")
        p.add_argument("--days", type=int, default=30, help="look back this many days (default 30)")
        p.add_argument("--symbol")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--offline", metavar="DIR")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("pit", help="insider trading disclosures")
    common(p)
    p.add_argument("--promoters", action="store_true")
    p.add_argument("--buys", action="store_true", help="market purchases only")
    p.add_argument("--sells", action="store_true", help="market sales only")
    p.add_argument("--pledges", action="store_true", help="pledge creation, revocation, invocation only")
    p.add_argument("--person")
    p.add_argument("--min-value", type=float, default=0.0, help="rupees")
    p.add_argument("--clusters", action="store_true", help="stocks with 2+ insiders buying")
    p.add_argument("--window", type=int, default=30)
    p = sub.add_parser("deals", help="bulk and block deals")
    common(p)
    p.add_argument("--kind", default="both", choices=["bulk", "block", "both"])
    p.add_argument("--client", nargs="*", help="client names to match (all words must appear)")
    p = sub.add_parser("new", help="only what is new since the last check")
    p.add_argument("--to")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--state", default="trader/state/insider.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watch-holdings", action="store_true")
    p.add_argument("--investors", default="trader/investors.json", help='{"names": ["kacholia", "damani"]}')
    p.add_argument("--min-value", type=float, default=10000000.0, help="promoter trades at least this (rupees)")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"pit": cmd_pit, "deals": cmd_deals, "new": cmd_new}[a.cmd](a)
    except InsiderError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
