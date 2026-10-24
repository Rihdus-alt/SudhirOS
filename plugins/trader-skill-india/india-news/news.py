#!/usr/bin/env python3
"""Exchange filings and headlines for the stocks you hold (india-news). Standard library only.

Two kinds of feed, filings first:

  filings    every listed company must tell the exchanges about material
             events (SEBI LODR Regulation 30): results, dividends, bonuses,
             auditor or CEO resignations, credit rating changes, defaults,
             orders, acquisitions. NSE and BSE publish each one:
               www.nseindia.com/api/corporate-announcements?index=equities&symbol=X
               api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w (BSE code)
  headlines  Google News India search per company (en-IN), and any RSS feed
             you add (Economic Times, Moneycontrol, Mint, Business Standard)

    python3 news.py                                       # trader/portfolio.json or targets.json
    python3 news.py --symbols RELIANCE TCS --hours 48
    python3 news.py --symbols RELIANCE --bse 500325       # BSE too (scrip code)
    python3 news.py --only-filings --important            # just the red-flag filings
    python3 news.py --feeds https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms
    python3 news.py --all --json
    --offline DIR reads DIR/nse-<SYMBOL>.json, DIR/bse-<CODE>.json, DIR/gnews-<SYMBOL>.xml, DIR/feed-<n>.xml.

Each item is shown once (trader/state/news.json remembers it for 30 days).
Exit code 1 when nothing is new.
"""

import argparse
import datetime
import email.utils
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 trader-skill-india/1.0")
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
BSE_ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=%s"
           "&strScrip=%s&strSearch=P&strToDate=%s&strType=C&subcategory=-1")
BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/%s"
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
IST = datetime.timedelta(hours=5, minutes=30)

# (tag, important, words). Matched on the filing's category and subject.
TAGS = [
    ("default", True, ("default", "delay in payment", "non-payment", "insolvency", "nclt", "cirp", "ibc",
                       "winding up", "liquidation")),
    ("auditor", True, ("resignation of statutory auditor", "resignation of auditor", "auditor resign",
                       "qualified opinion", "adverse opinion", "disclaimer of opinion", "forensic audit")),
    ("fraud", True, ("fraud", "misappropriation", "search and seizure", "raid", "enforcement directorate")),
    ("regulator", True, ("sebi order", "show cause", "penalty", "adjudication", "suspension of trading",
                         "surveillance", "delisting", "trading restriction")),
    ("rating", False, ("credit rating", "rating action", "rating revised", "reaffirm")),
    ("downgrade", True, ("downgrade", "downgraded", "rating watch with negative", "negative outlook")),
    ("results", False, ("financial result", "outcome of board meeting", "integrated filing", "quarterly result",
                        "audited result", "unaudited result")),
    ("people", False, ("resignation", "appointment", "cessation", "change in director", "change in key managerial",
                       "kmp", "managing director", "chief executive", "cfo", "company secretary")),
    ("payout", False, ("dividend", "bonus", "sub-division", "split", "buyback", "buy back", "record date",
                       "rights issue")),
    ("capital", False, ("allotment", "qip", "preferential issue", "esop", "esos", "fund raising", "warrants",
                        "ncd", "debenture")),
    ("deal", False, ("acquisition", "acquire", "amalgamation", "merger", "demerger", "scheme of arrangement",
                     "joint venture", "stake sale", "divest", "order", "contract", "bagging", "letter of award")),
    ("legal", False, ("litigation", "court", "tribunal", "arbitration", "tax demand", "gst demand",
                      "income tax", "dispute", "claim")),
    ("pledge", True, ("pledge", "encumbrance", "invocation")),
    ("cyber", True, ("cyber", "data breach", "ransomware")),
    ("meeting", False, ("analyst", "investor meet", "earnings call", "con. call", "conference call", "agm", "egm",
                        "postal ballot")),
]


class NewsError(Exception):
    pass


class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.nse_warm = False

    def get(self, url, referer=None):
        h = {"User-Agent": UA, "Accept": "application/json,application/rss+xml,text/xml,*/*",
             "Accept-Language": "en-IN,en;q=0.9"}
        if referer:
            h["Referer"] = referer
            h["Origin"] = referer.rstrip("/")
        try:
            with self.opener.open(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read().decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            raise NewsError("%s answered HTTP %d" % (url.split("?")[0], e.code))
        except urllib.error.URLError as e:
            raise NewsError("could not reach %s: %s" % (url.split("/")[2], e.reason))

    def read(self, name, url, referer=None, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.exists(path):
                raise NewsError("no offline file %s" % path)
            with open(path, encoding="utf-8-sig") as fh:
                return fh.read()
        if nse and not self.nse_warm:
            self.get("https://www.nseindia.com/")
            self.nse_warm = True
        return self.get(url, referer)


def tags_for(text):
    low = " %s " % str(text or "").lower()
    tags, important = [], False
    for tag, imp, words in TAGS:
        if any(w in low for w in words):
            tags.append(tag)
            important = important or imp
    if "resignation" in low and ("auditor" in low or "chief financial" in low or " cfo" in low):
        important = True
    return tags, important


def _utc(dt_ist):
    return (dt_ist - IST).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_nse(text, sym):
    try:
        d = json.loads(text)
    except ValueError:
        raise NewsError("NSE announcements for %s were not JSON (NSE may have blocked the request)" % sym)
    rows = d.get("data", d) if isinstance(d, dict) else d
    out = []
    for r in rows or []:
        when = None
        for k, fmt in (("sort_date", "%Y-%m-%d %H:%M:%S"), ("an_dt", "%d-%b-%Y %H:%M:%S"), ("dt", "%d%m%Y%H%M%S")):
            if r.get(k):
                try:
                    when = datetime.datetime.strptime(str(r[k]).strip(), fmt)
                    break
                except ValueError:
                    continue
        subject = (r.get("desc") or "").strip()
        body = (r.get("attchmntText") or "").strip()
        tags, imp = tags_for(subject + " " + body)
        out.append({"kind": "filing", "source": "NSE", "symbol": str(r.get("symbol") or sym).upper(),
                    "company": r.get("sm_name") or "", "title": subject, "detail": body[:400],
                    "time": _utc(when) if when else "", "link": r.get("attchmntFile") or "",
                    "id": "nse|%s|%s" % (r.get("seq_id") or r.get("an_dt") or "", sym), "tags": tags,
                    "important": imp})
    return out


def parse_bse(text, code, sym):
    try:
        d = json.loads(text)
    except ValueError:
        raise NewsError("BSE announcements for %s were not JSON" % code)
    out = []
    for r in d.get("Table", []):
        when = None
        for k in ("NEWS_DT", "DT_TM", "DissemDT"):
            v = str(r.get(k) or "").split(".")[0]
            if v:
                try:
                    when = datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%S")
                    break
                except ValueError:
                    continue
        subject = re.sub(r"^.*? - \d{6} - ", "", (r.get("NEWSSUB") or r.get("HEADLINE") or "").strip())
        tags, imp = tags_for("%s %s %s" % (r.get("CATEGORYNAME") or "", subject, r.get("HEADLINE") or ""))
        att = r.get("ATTACHMENTNAME") or ""
        out.append({"kind": "filing", "source": "BSE", "symbol": sym or str(r.get("SCRIP_CD")),
                    "company": r.get("SLONGNAME") or "", "title": subject,
                    "detail": (r.get("HEADLINE") or "")[:400], "time": _utc(when) if when else "",
                    "link": BSE_PDF % att if att else "", "id": "bse|%s" % (r.get("NEWSID") or subject),
                    "tags": tags, "important": imp, "category": r.get("CATEGORYNAME") or ""})
    return out


def parse_rss(text, label, sym=None):
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        raise NewsError("%s is not valid RSS: %s" % (label, e))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        when = ""
        pd = it.findtext("pubDate")
        if pd:
            try:
                dt = email.utils.parsedate_to_datetime(pd)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                when = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                when = ""
        src = it.find("source")
        tags, imp = tags_for(title)
        out.append({"kind": "headline", "source": (src.text.strip() if src is not None and src.text else label),
                    "symbol": sym or "", "title": title, "detail": "", "time": when, "link": link,
                    "id": "rss|%s" % (it.findtext("guid") or link or title), "tags": tags, "important": False,
                    "tag_hint": imp})
    return out


def touches(item, names):
    """Which of your symbols an item mentions (by symbol or company name)."""
    text = (" %s %s " % (item["title"], item.get("detail", ""))).lower()
    hit = {item["symbol"]} if (item.get("symbol") and item["kind"] == "filing") else set()
    for sym, name in names.items():
        # strip only the legal suffix: "Bank of India" minus "india" is "bank of", which every bank headline has
        short = re.sub(r"\s+", " ", re.sub(r"\b(limited|ltd\.?)", "", (name or "").lower())).strip(" .")
        if re.search(r"\b%s\b" % re.escape(sym.lower()), text) or \
                (len(short) > 3 and re.search(r"(?<!\w)%s(?!\w)" % re.escape(short), text)):
            hit.add(sym)
    return sorted(h for h in hit if h)


def load_symbols(a):
    if a.symbols:
        return [s.upper().replace("NSE:", "") for s in a.symbols]
    for path, key in ((a.portfolio, "positions"), (a.targets, "weights")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            syms = [str(p.get("ticker") or p.get("symbol") or "").upper() for p in d.get(key, [])]
            syms = [s for s in syms if s]
            if syms:
                return syms
    raise NewsError("which stocks? pass --symbols, or hold something in trader/portfolio.json")


def load_names(syms, prices_path):
    names = {s: "" for s in syms}
    if prices_path and os.path.exists(prices_path):
        with open(prices_path, encoding="utf-8") as fh:
            det = json.load(fh).get("detail", {})
        for s in syms:
            if det.get(s, {}).get("name"):
                names[s] = det[s]["name"]
    return names


def gnews_query(sym, name):
    q = '"%s"' % re.sub(r"\s+(Ltd\.?|Limited)$", "", name).strip() if name else '"%s" NSE' % sym
    return GNEWS % urllib.parse.quote(q + " when:7d")


def collect(a, syms, names, f):
    items, errors = [], []
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=max(1, int(a.hours / 24) + 1))
    bse = dict(zip(syms, a.bse)) if a.bse else {}
    for s in syms:
        if not a.only_headlines:
            try:
                items += parse_nse(f.read("nse-%s.json" % s, NSE_ANN % urllib.parse.quote(s),
                                          "https://www.nseindia.com/", nse=True), s)
            except NewsError as e:
                errors.append(str(e))
            if s in bse:
                code = bse[s]
                try:
                    items += parse_bse(f.read("bse-%s.json" % code, BSE_ANN % (frm.strftime("%Y%m%d"), code,
                                                                                to.strftime("%Y%m%d")),
                                              "https://www.bseindia.com/"), code, s)
                except NewsError as e:
                    errors.append(str(e))
        if not a.only_filings:
            try:
                items += parse_rss(f.read("gnews-%s.xml" % s, gnews_query(s, names.get(s))), "Google News", s)
            except NewsError as e:
                errors.append(str(e))
    if not a.only_filings:
        for i, url in enumerate(a.feeds or [], 1):
            try:
                items += parse_rss(f.read("feed-%d.xml" % i, url), url.split("/")[2])
            except NewsError as e:
                errors.append(str(e))
    return items, errors


def run(a):
    syms = load_symbols(a)
    names = load_names(syms, a.prices)
    f = Fetcher(a.offline)
    items, errors = collect(a, syms, names, f)
    for it in items:  # the exchange's own company name sharpens the headline match
        if it["kind"] == "filing" and it.get("company") and not names.get(it["symbol"]):
            names[it["symbol"]] = it["company"]
    now = datetime.datetime.strptime(a.now, "%Y-%m-%dT%H:%M:%SZ") if a.now else \
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = (now - datetime.timedelta(hours=a.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {}
    if os.path.exists(a.state):
        with open(a.state, encoding="utf-8") as fh:
            state = json.load(fh)
    seen = state.get("seen", {})
    out, ids = [], set()
    for it in items:
        if it["time"] and it["time"] < cutoff:
            continue
        if it["id"] in ids:
            continue
        ids.add(it["id"])
        it["touches"] = touches(it, names)
        if it["kind"] == "headline" and not it["touches"]:
            continue
        if a.important and not it["important"]:
            continue
        if not a.all and it["id"] in seen:
            continue
        out.append(it)
    # the same filing reaches both exchanges: keep the first, note the other
    dedup, keyed, twins = [], {}, []
    for it in sorted(out, key=lambda x: (x["time"], x["source"]), reverse=True):
        k = (it["kind"], it["symbol"], re.sub(r"\W+", "", it["title"].lower())[:60], it["time"][:13])
        # the other exchange's copy can arrive a run later, after the first one was shown: remember the key
        if it["kind"] == "filing" and (k in keyed or (not a.all and "twin|%s|%s|%s|%s" % k in seen)):
            if k in keyed:
                keyed[k]["also_on"] = it["source"]
            twins.append(it["id"])
            continue
        keyed[k] = it
        dedup.append(it)
    if not a.dry_run:
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in [it["id"] for it in dedup] + twins + ["twin|%s|%s|%s|%s" % k for k in keyed if k[0] == "filing"]:
            seen[i] = stamp
        old = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen = {k: v for k, v in seen.items() if v >= old}
        d = os.path.dirname(a.state)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump({"seen": seen, "last_check": stamp}, fh, indent=1)
    return dedup, errors


def ist(t):
    if not t:
        return "time ?"
    dt = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") + IST
    return dt.strftime("%d %b %H:%M IST")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exchange filings and headlines for your stocks (india-news).")
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--bse", nargs="*", default=[], help="BSE scrip codes, in the same order as --symbols")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--feeds", nargs="*", help="extra RSS feeds")
    ap.add_argument("--only-filings", action="store_true")
    ap.add_argument("--only-headlines", action="store_true")
    ap.add_argument("--important", action="store_true", help="only red-flag filings")
    ap.add_argument("--all", action="store_true", help="include items already shown")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--targets", default="trader/targets.json")
    ap.add_argument("--prices", default="trader/prices.json", help="company names for the headline search")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="pretend it is this UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        items, errors = run(a)
    except NewsError as e:
        print("news.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"items": items, "errors": errors}, indent=2, ensure_ascii=False))
        return 0 if items else 1
    for it in items:
        mark = "!" if it["important"] else " "
        print("%s %-8s %-15s %s  %s%s" % (mark, it["kind"].upper(), "/".join(it["touches"]) or "-", ist(it["time"]),
                                          it["title"][:110], ("  [%s]" % ", ".join(it["tags"])) if it["tags"] else ""))
        print("           %s%s %s" % (it["source"], (" + " + it["also_on"]) if it.get("also_on") else "", it["link"]))
    if not items:
        print("Nothing new in the last %g hours." % a.hours)
    for e in errors:
        print("error: " + e)
    print("Filings are what the company told the exchange; headlines are leads. Read the filing before you act. "
          "! marks a red-flag filing (default, auditor exit, fraud, regulator, downgrade, pledge, cyber).")
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
