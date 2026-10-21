#!/usr/bin/env python3
"""Price alerts, quotes and the NSE clock for india-alerts. Standard library only.

    python3 alerts.py clock                            # is NSE open? IST, pre-open, next session
    python3 alerts.py quote RELIANCE TCS --write       # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write         # every ticker in your portfolio and targets
    python3 alerts.py quote INFY --verify              # two sources, flags any disagreement
    python3 alerts.py import indmoney details.json --write   # INDmoney connector output -> prices
    python3 alerts.py add --symbol RELIANCE --above 1300
    python3 alerts.py add --symbol HDFCBANK --stop 690
    python3 alerts.py add --symbol INFY --pct 3
    python3 alerts.py add --symbol AXISBANK --near-circuit 2
    python3 alerts.py add --opening-bell
    python3 alerts.py check                            # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py list
    python3 alerts.py reset reliance-above-1300

Symbols are NSE trading symbols (RELIANCE, M&M, BAJAJ-AUTO). BSE: prefix
BSE: (BSE:500325). Prices are in rupees.

Rule types:
  above          price at or above "price" (a target)
  below / stop   price at or below "price" (a stop or a dip to buy)
  pct_move       moved "pct" percent or more from the previous close (or "ref");
                 "direction": "up" or "down"
  near_circuit   within "pct" percent of the day's upper or lower circuit
                 (price band); needs a source that reports the band
  opening_bell   NSE's normal session opened today (once per trading day)

An above / below alert rings ONCE, then stays quiet until reset, unless the
rule has "rearm": true (it re-arms when the price crosses back). pct_move and
near_circuit ring at most once per day.

Quote sources (free, delayed or snapshot; for alerts and sizing checks, never
for timing an order; your broker's price is the one that fills):
  yahoo     query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (default)
  nse       www.nseindia.com/api/quote-equity?symbol=RELIANCE (needs the
            site's cookies, which this tool fetches from the home page first;
            adds the price band, series and surveillance flags)
  indmoney  the JSON the INDmoney connector's get_indian_stocks_details
            returns, saved to a file (alerts.py import indmoney FILE); adds
            the circuit limits
  --offline DIR reads DIR/<source>-<SYMBOL>.json instead of the network.

NSE calendar: the 2026 trading holidays from NSE's circulars of December
2025 (15 weekday closures), and Muhurat trading on Sunday 2026-11-08 (times
to be notified by NSE; set them in trader/calendar.json). Any other year, or
a change NSE announces, goes in trader/calendar.json:
  {"holidays": {"2027-01-26": "Republic Day"},
   "special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
India has no daylight saving time: IST is always UTC+05:30.
"""

import argparse
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 trader-skill-india/1.0")
PAUSE = 0.5
IST = datetime.timedelta(hours=5, minutes=30)

NSE_HOLIDAYS = {  # equity segment, NSE circulars of December 2025
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
SPECIAL_SESSIONS = {  # date -> [open, close] IST, or None while NSE has not notified the times
    "2026-11-08": None,  # Muhurat trading, Diwali Laxmi Pujan (Sunday)
}
CALENDAR_YEARS = (2026,)
PRE_OPEN = datetime.time(9, 0)
PRE_OPEN_END = datetime.time(9, 8)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
POST_CLOSE_END = datetime.time(16, 0)


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- calendar

def load_calendar(path):
    """Merge trader/calendar.json (holidays, special_sessions) over the built-in lists."""
    hol, special = dict(NSE_HOLIDAYS), dict(SPECIAL_SESSIONS)
    years = set(CALENDAR_YEARS)
    doc = _read(path, None) if path else None
    if doc:
        for d, name in (doc.get("holidays") or {}).items():
            hol[str(d)[:10]] = name
            years.add(int(str(d)[:4]))
        for d in doc.get("not_holidays") or []:
            hol.pop(str(d)[:10], None)
        for d, times in (doc.get("special_sessions") or {}).items():
            special[str(d)[:10]] = times
        for y in doc.get("years") or []:
            years.add(int(y))
    return {"holidays": hol, "special": special, "years": tuple(sorted(years))}


def default_calendar():
    return {"holidays": dict(NSE_HOLIDAYS), "special": dict(SPECIAL_SESSIONS), "years": CALENDAR_YEARS}


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants an ISO time like 2026-10-19T09:20 (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt - IST
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _t(s):
    h, m = str(s).split(":")[:2]
    return datetime.time(int(h), int(m))


def trading_day(d, cal=None):
    """(is_trading_day, reason)."""
    cal = cal or default_calendar()
    if d.isoformat() in cal["special"]:
        return True, "special session"
    if d.weekday() >= 5:
        return False, "weekend"
    name = cal["holidays"].get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d, cal=None):
    """(open, close) IST for the normal market, None if closed all day, or
    "tba" for a special session whose times are not set yet."""
    cal = cal or default_calendar()
    if d.isoformat() in cal["special"]:
        times = cal["special"][d.isoformat()]
        if not times:
            return "tba"
        return _t(times[0]), _t(times[1])
    ok, _ = trading_day(d, cal)
    return (OPEN, CLOSE) if ok else None


def market_status(now_utc, cal=None):
    cal = cal or default_calendar()
    local = to_ist(now_utc)
    d, tm = local.date(), local.time()
    ok, reason = trading_day(d, cal)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(), "weekday": local.strftime("%A"),
          "calendar_known": d.year in cal["years"], "open": False, "phase": "closed", "reason": reason,
          "special_session": d.isoformat() in cal["special"]}
    s = session(d, cal)
    if s == "tba":
        st["reason"] = "special session (Muhurat trading); NSE has not set the times in this calendar yet"
        st["phase"] = "special session, times unknown"
    elif s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if st["special_session"]:
            if o <= tm < c:
                st.update(open=True, phase="special session", reason="Muhurat trading session")
            else:
                st["reason"] = "special session day, outside its hours"
        elif PRE_OPEN <= tm < PRE_OPEN_END:
            st.update(phase="pre-open", reason="pre-open call auction (orders collected 09:00 to 09:08)")
        elif PRE_OPEN_END <= tm < o:
            st.update(phase="pre-open matching", reason="pre-open order matching; normal market opens 09:15")
        elif o <= tm < c:
            st.update(open=True, phase="normal market", reason="normal market")
        elif c <= tm < POST_CLOSE_END:
            st.update(phase="post-close", reason="closing session (trades at the closing price only)")
        elif tm < PRE_OPEN:
            st["reason"] = "before the pre-open"
        else:
            st["reason"] = "after the close"
    nd = d if (s and s != "tba" and not st["special_session"] and tm < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(20):
        ns = session(nd, cal)
        if ns and ns != "tba" and nd.isoformat() not in cal["special"]:
            st["next_open"] = "%s 09:15" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    if st["phase"] == "closed":
        st["orders_now"] = ("an order placed now is an after-market order (AMO) and goes to the exchange "
                            "at the next session")
    elif st["phase"] == "pre-open matching":
        st["orders_now"] = "order entry is paused until 09:15; most brokers queue an order placed now"
    return st


def last_close_utc(now_utc, cal=None):
    """The UTC time of the most recent completed 15:30 IST close before now."""
    cal = cal or default_calendar()
    local = to_ist(now_utc)
    d = local.date()
    for _ in range(20):
        s = session(d, cal)
        if s and s != "tba" and d.isoformat() not in cal["special"]:
            close_dt = datetime.datetime.combine(d, s[1])
            if close_dt <= local:
                return close_dt - IST
        d -= datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- symbols

SYMBOL = re.compile(r"^(BSE:\d{6}|(?:NSE:|BSE:)?[A-Z0-9][A-Z0-9&\-]{0,19})$")


def norm_symbol(s):
    s = str(s or "").strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith(".NS"):
        s = s[:-3]
    if not SYMBOL.match(s):
        raise AlertError("%r is not an NSE symbol (RELIANCE, M&M) or BSE:<6-digit code>" % s)
    return s


def yahoo_symbol(sym):
    if sym.startswith("BSE:"):
        return sym[4:] + ".BO"
    return sym + ".NS"


# ---------------------------------------------------------------- quotes

def yahoo_url(sym):
    return ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1d"
            % urllib.parse.quote(yahoo_symbol(sym)))


def nse_url(sym):
    return "https://www.nseindia.com/api/quote-equity?symbol=%s" % urllib.parse.quote(sym)


def _iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    if m.get("currency") and m["currency"] != "INR":
        raise AlertError("Yahoo quoted %s in %s, not INR: wrong listing?" % (sym, m["currency"]))
    t = m.get("regularMarketTime")
    return {"symbol": sym, "price": float(price), "prev_close": m.get("previousClose") or m.get("chartPreviousClose"),
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "time": _iso_utc(t) if t else "", "currency": "INR", "source": "yahoo"}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _nse_time(s):
    """'17-Oct-2026 16:00:00' (IST) -> ISO UTC."""
    try:
        dt = datetime.datetime.strptime(str(s).strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return ""
    return (dt - IST).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_nse(text, sym):
    try:
        d = json.loads(text)
        pi = d["priceInfo"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("NSE returned something unexpected for %s (wrong symbol, or the site blocked "
                         "the request)" % sym)
    price = _num(pi.get("lastPrice"))
    if not price:
        raise AlertError("NSE has no price for %s" % sym)
    meta, sec, info = d.get("metadata") or {}, d.get("securityInfo") or {}, d.get("info") or {}
    surv = (sec.get("surveillance") or {})
    hl = pi.get("intraDayHighLow") or {}
    return {"symbol": sym, "price": price, "prev_close": _num(pi.get("previousClose")),
            "high": _num(hl.get("max")), "low": _num(hl.get("min")),
            "upper_circuit": _num(pi.get("upperCP")), "lower_circuit": _num(pi.get("lowerCP")),
            "price_band": pi.get("pPriceBand"), "series": meta.get("series"), "isin": info.get("isin"),
            "name": info.get("companyName"), "surveillance": surv.get("surv") or None,
            "trading_status": sec.get("tradingStatus"),
            "time": _nse_time(meta.get("lastUpdateTime") or d.get("lastUpdateTime") or ""),
            "currency": "INR", "source": "nse"}


def parse_indmoney(doc):
    """INDmoney get_indian_stocks_details output -> {SYMBOL: quote}.

    Accepts the tool's raw result ({"result": "<json string>"}), the decoded
    object keyed by ind_key, or a list of those."""
    if isinstance(doc, list):
        out = {}
        for x in doc:
            out.update(parse_indmoney(x))
        return out
    if isinstance(doc, dict) and isinstance(doc.get("result"), str):
        try:
            doc = json.loads(doc["result"])
        except ValueError:
            raise AlertError("the INDmoney result string is not JSON")
    if not isinstance(doc, dict):
        raise AlertError("expected the INDmoney details object")
    out = {}
    for key, ent in doc.items():
        if not isinstance(ent, dict) or "entity_stats" not in ent:
            continue
        b, s = ent.get("entity_basic") or {}, ent.get("entity_stats") or {}
        sym = str(b.get("symbol") or "").upper()
        price = _num(s.get("live_price"))
        if not sym or not price:
            continue
        if str(b.get("exchange", "NSE")).upper() == "BSE" and re.match(r"^\d{6}$", sym):
            sym = "BSE:" + sym
        when = ""
        if s.get("price_updated_at_ist"):
            try:
                dt = datetime.datetime.fromisoformat(s["price_updated_at_ist"])
                when = (dt - dt.utcoffset()).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                when = ""
        out[sym] = {"symbol": sym, "price": price, "prev_close": _num(s.get("prev_close")),
                    "high": _num(s.get("day_high")), "low": _num(s.get("day_low")),
                    "upper_circuit": _num(s.get("upper_circuit")), "lower_circuit": _num(s.get("lower_circuit")),
                    "week52_high": _num(s.get("52week_high")), "week52_low": _num(s.get("52week_low")),
                    "name": b.get("name"), "ind_key": b.get("ind_key") or key,
                    "market_cap": b.get("market_cap"), "time": when, "currency": "INR", "source": "indmoney"}
    if not out:
        raise AlertError("no stock quotes found in the INDmoney output")
    return out


class Fetcher(object):
    """One cookie jar per run: NSE's API answers only after the home page sets cookies."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.nse_ready = False

    def get(self, url, referer=None, timeout=20):
        h = {"User-Agent": UA, "Accept": "application/json,text/html,*/*", "Accept-Language": "en-IN,en;q=0.9"}
        if referer:
            h["Referer"] = referer
        try:
            with self.opener.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise AlertError("%s answered HTTP %d" % (url.split("?")[0], e.code))
        except urllib.error.URLError as e:
            raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))

    def nse(self, url):
        if not self.nse_ready:
            self.get("https://www.nseindia.com/", timeout=20)
            self.nse_ready = True
        return self.get(url, referer="https://www.nseindia.com/")


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE, fetcher=None):
    """{SYM: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    fetcher = fetcher or Fetcher()
    for i, sym in enumerate(symbols):
        try:
            sym = norm_symbol(sym)
            if offline:
                path = os.path.join(offline, "%s-%s.json" % (source, sym.replace(":", "_")))
                if not os.path.exists(path):
                    raise AlertError("no offline quote file %s" % path)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            else:
                if i and pause:
                    time.sleep(pause)
                if source == "nse":
                    if sym.startswith("BSE:"):
                        raise AlertError("%s is a BSE code; NSE's API quotes NSE symbols only" % sym)
                    text = fetcher.nse(nse_url(sym))
                else:
                    text = fetcher.get(yahoo_url(sym))
            quotes[sym] = parse_nse(text, sym) if source == "nse" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def stale(q, now_utc, cal=None):
    """True if the quote is older than the last completed close (a day or more out of date)."""
    if not q.get("time"):
        return False
    try:
        t = datetime.datetime.strptime(q["time"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    lc = last_close_utc(now_utc, cal)
    return bool(lc and t < lc - datetime.timedelta(minutes=30))


def verify(a_quotes, b_quotes, tolerance_pct=0.5):
    """Compare two sources symbol by symbol. Returns rows with the gap in percent."""
    rows = []
    for s in sorted(set(a_quotes) & set(b_quotes)):
        pa, pb = a_quotes[s]["price"], b_quotes[s]["price"]
        gap = 100.0 * abs(pa - pb) / min(pa, pb)
        rows.append({"symbol": s, a_quotes[s]["source"]: pa, b_quotes[s]["source"]: pb,
                     "gap_pct": round(gap, 3), "agree": gap <= tolerance_pct})
    return rows


def write_prices(path, quotes, now_utc, source):
    old = _read(path, {}) or {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {k: v for k, v in q.items() if k not in ("symbol", "price") and v is not None}
    doc = {"as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": "INR", "source": source,
           "note": "Delayed or snapshot quotes for alerts and sizing checks. Your broker's price is the one that fills.",
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

TYPES = {"above", "below", "stop", "target", "pct_move", "near_circuit", "opening_bell"}


def validate(rule):
    t = rule.get("type")
    if t not in TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t != "opening_bell":
        if not rule.get("symbol"):
            raise AlertError("rule %s needs a symbol" % rule.get("id"))
        norm_symbol(rule["symbol"])
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t in ("pct_move", "near_circuit") and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))


def rs(x):
    """Rupees with Indian digit grouping: 121905 -> ₹1,21,905; 1257.3 -> ₹1,257.30"""
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


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and rung.get(rid, "")[:10] != today:
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST, %s)."
                              % (status["now_ist"], status["phase"])})
                rung[rid] = stamp
            continue
        sym = norm_symbol(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        verb = "is" if status["open"] else "last"
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], typ)
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s, %s your %s %s%s."
                              % (sym, verb, rs(p), word, label, rs(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and rung.get(rid, "")[:10] != today:
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, rs(p), rs(ref))})
                rung[rid] = stamp
        elif typ == "near_circuit":
            up, lo = q.get("upper_circuit"), q.get("lower_circuit")
            if not up or not lo:
                continue
            msg = None
            if p >= up * (1 - r["pct"] / 100.0):
                msg = "%s %s %s, within %s%% of its upper circuit %s" % (sym, verb, rs(p), _n(r["pct"]), rs(up))
            elif p <= lo * (1 + r["pct"] / 100.0):
                msg = "%s %s %s, within %s%% of its lower circuit %s" % (sym, verb, rs(p), _n(r["pct"]), rs(lo))
            if msg and rung.get(rid, "")[:10] != today:
                rings.append({"id": rid, "symbol": sym, "message": msg + ": at the band, orders may not fill."})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {}) or {}
    for p in pf.get("positions", []):
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    tg = _read(a.targets, {}) or {}
    for w in tg.get("weights", []) if isinstance(tg, dict) else []:
        syms.append(str(w.get("ticker") or w.get("symbol") or "").upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    cal = load_calendar(a.calendar)
    st = market_status(parse_now(a.now), cal)
    if a.json:
        print(json.dumps(st, indent=2))
        return 0 if st["open"] else 1
    print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else "closed",
                                        st["reason"]))
    if st.get("opens"):
        print("Today's session: %s to %s IST%s." % (st["opens"], st["closes"],
                                                    " (special session)" if st["special_session"] else
                                                    "; pre-open 09:00 to 09:08, closing session 15:40 to 16:00"))
    if not st["open"] and st.get("next_open"):
        print("Next normal session: %s IST." % st["next_open"])
    if st.get("orders_now"):
        print("Note: " + st["orders_now"] + ".")
    if not st["calendar_known"]:
        print("The holiday list in this pack covers %s only. Add this year's NSE holidays to "
              "trader/calendar.json from NSE's circular." % ", ".join(map(str, cal["years"])))
    return 0 if st["open"] else 1


def _print_quotes(syms, quotes, errors, now, cal):
    for s in syms:
        q = quotes.get(s)
        if not q:
            continue
        chg = ""
        if q.get("prev_close"):
            chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1), rs(q["prev_close"]))
        band = ""
        if q.get("upper_circuit") and q.get("lower_circuit"):
            band = "  band %s to %s" % (rs(q["lower_circuit"]), rs(q["upper_circuit"]))
        flags = []
        if q.get("surveillance"):
            flags.append("surveillance: %s" % q["surveillance"])
        if q.get("series") and q["series"] not in ("EQ", "BE"):
            flags.append("series %s" % q["series"])
        if q.get("series") == "BE":
            flags.append("trade-to-trade (BE): delivery only, no intraday")
        if stale(q, now, cal):
            flags.append("STALE: older than the last close")
        print("%-12s %s%s%s  %s  [%s]%s" % (s, rs(q["price"]), chg, band, q.get("time", ""), q["source"],
                                          ("  " + "; ".join(flags)) if flags else ""))
    for e in errors:
        print("error: " + e)


def cmd_quote(a):
    syms = _dedupe([norm_symbol(s) for s in a.symbols] + ([norm_symbol(s) for s in holdings(a)] if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    cal = load_calendar(a.calendar)
    fetcher = Fetcher()
    quotes, errors = get_quotes(syms, a.source, a.offline, fetcher=fetcher)
    checks = []
    if a.verify:
        other = "nse" if a.source == "yahoo" else "yahoo"
        q2, e2 = get_quotes(syms, other, a.offline, fetcher=fetcher)
        errors += e2
        checks = verify(quotes, q2, a.tolerance)
        for s, q in q2.items():
            if s in quotes:
                for k in ("upper_circuit", "lower_circuit", "series", "surveillance", "isin", "price_band"):
                    if q.get(k) is not None and quotes[s].get(k) is None:
                        quotes[s][k] = q[k]
            else:
                quotes[s] = q
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors, "verify": checks}, indent=2))
    else:
        _print_quotes(syms, quotes, errors, now, cal)
        for c in checks:
            print("%s %-12s %s" % ("agree   " if c["agree"] else "DISAGREE", c["symbol"],
                                   ", ".join("%s %s" % (k, rs(v)) for k, v in c.items()
                                             if k not in ("symbol", "gap_pct", "agree"))
                                   + "  gap %.3f%%" % c["gap_pct"]))
        if any(not c["agree"] for c in checks):
            print("Sources disagree by more than %s%%: check the price in your broker before sizing." % _n(a.tolerance))
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are delayed or snapshots. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_import(a):
    doc = _read(a.file, None)
    if doc is None:
        raise AlertError("no file %s" % a.file)
    quotes = parse_indmoney(doc)
    now = parse_now(a.now)
    cal = load_calendar(a.calendar)
    if a.write:
        write_prices(a.write, quotes, now, "indmoney")
    if a.json:
        print(json.dumps(quotes, indent=2))
    else:
        _print_quotes(sorted(quotes), quotes, [], now, cal)
        if a.write:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
    return 0


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add one with alerts.py add --symbol RELIANCE --above 1300" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    cal = load_calendar(a.calendar)
    status = market_status(now, cal)
    syms = _dedupe([norm_symbol(r["symbol"]) for r in rules if r.get("type") != "opening_bell"])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    # a symbol the live sources missed falls back to the last saved quote (an INDmoney import or an earlier
    # check), with its own time so evaluate() can call it stale; a rule with no quote at all is NOT CHECKED
    pin = a.prices_in or (a.prices_out if a.prices_out and os.path.exists(a.prices_out) else None)
    if pin:
        pdoc = _read(pin, {}) or {}
        for s, p in (pdoc.get("prices") or {}).items():
            if s in syms and s not in quotes:
                q = dict((pdoc.get("detail") or {}).get(s) or {})
                q.update({"symbol": s, "price": float(p), "source": q.get("source", "prices.json")})
                quotes[s] = q
    unchecked = [s for s in syms if s not in quotes]
    state = _read(a.state, {}) or {}
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        fresh = {s: q for s, q in quotes.items() if q.get("source") in ("yahoo", "nse")}
        if fresh:
            write_prices(a.prices_out, fresh, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors, "not_checked": unchecked,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"], status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        for s in unchecked:
            print("NOT CHECKED: %s (no quote from any source); its rules could not be tested" % s)
        if not rings:
            print("Nothing hit. %d rule(s) watched%s." % (len(rules), (", %d symbol(s) NOT CHECKED" % len(unchecked))
                                                          if unchecked else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. To act on one, size it (india-budget or "
                  "india-rebalance), check it (india-risk) and say yes to it (india-broker).")
    return 0 if rings else (2 if unchecked else 1)


def cmd_add(a):
    doc = _read(a.rules, {"rules": []}) or {"rules": []}
    doc.setdefault("rules", [])
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --opening-bell)")
        sym = norm_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.near_circuit is not None:
            rule = {"type": "near_circuit", "pct": a.near_circuit}
        else:
            raise AlertError("give one of --above, --below, --stop, --pct or --near-circuit")
        val = rule.get("price", rule.get("pct"))
        slug = re.sub(r"[^a-z0-9]+", "", sym.lower())
        rule = dict({"id": a.id or ("%s-%s-%s" % (slug, rule["type"].replace("_", "-"), _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule, ensure_ascii=False)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []}) or {"rules": []}
    state = _read(a.state, {}) or {}
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-28s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}, ensure_ascii=False),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {}) or {}
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts, quotes and the NSE clock (india-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "nse"])
        p.add_argument("--offline", metavar="DIR", help="read <source>-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (ISO; no offset = IST)")
        p.add_argument("--calendar", default="trader/calendar.json")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is NSE open?")
    p.add_argument("--now")
    p.add_argument("--calendar", default="trader/calendar.json")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and targets")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--targets", default="trader/targets.json")
    p.add_argument("--verify", action="store_true", help="also fetch the other source and compare")
    p.add_argument("--tolerance", type=float, default=0.5, help="percent gap that counts as a disagreement")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH")
    p = sub.add_parser("import", help="turn connector output into prices")
    p.add_argument("kind", choices=["indmoney"])
    p.add_argument("file")
    p.add_argument("--now")
    p.add_argument("--calendar", default="trader/calendar.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-in", default=None, help="fallback prices for symbols the live sources miss "
                   "(default: --prices-out, trader/prices.json, where an INDmoney import is saved)")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--near-circuit", type=float, metavar="PCT")
    g.add_argument("--opening-bell", action="store_true")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "import": cmd_import, "check": cmd_check,
                "add": cmd_add, "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
