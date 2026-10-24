#!/usr/bin/env python3
"""Quarterly results of Indian listed companies (india-analyst). Standard library only.

Every listed company files its quarterly results with NSE and BSE within 45
days of the quarter end (60 days for the March quarter, with the audited
year), as a PDF and as XBRL under SEBI LODR Regulation 33. The XBRL carries
the filed numbers with their periods. This reads those files, lines the
quarters up, and says plainly what changed.

    python3 results.py fetch INFY --quarters 5                 # NSE's list of result filings + their XBRL
    python3 results.py read INFY-Q2.xml INFY-Q1.xml ...        # one or more results XBRL files
    python3 results.py read *.xml --basis standalone
    python3 results.py read quarters.csv --symbol INFY         # or your own CSV of quarterly numbers
    python3 results.py read *.xml --metrics trader/metrics.json   # a row for india-screener

What it works out per quarter, from the filed numbers:
  revenue        revenue from operations (banks: interest earned)
  EBITDA         profit before tax + finance costs + depreciation - other
                 income - exceptional items: the operating profit, before the
                 one-offs and the treasury income that flatter a quarter
  margins        EBITDA and net margin on revenue
  net profit     profit for the period, and the part that belongs to the
                 company's own shareholders (after minority interest)
  EPS            diluted, as filed (not annualised)
  growth         against the SAME quarter a year earlier (YoY) and the quarter
                 before (QoQ); Indian businesses are seasonal
  TTM            the last four quarters added up
  flags          other income over 25% of profit before tax; any exceptional
                 item; a tax rate under 15% or over 35%; a loss; consolidated
                 and standalone mixed

Consolidated numbers are used when the filing has them (--basis to choose).
Figures are in rupees; the report shows crores.
"""

import argparse
import csv
import datetime
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
NSE_RESULTS = "https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly&symbol=%s"
CRORE = 1e7

ALIASES = {
    "revenue": ("RevenueFromOperations", "TotalRevenueFromOperations", "InterestEarned", "IncomeFromOperations"),
    "other_income": ("OtherIncome",),
    "total_income": ("Income", "TotalIncome"),
    "expenses": ("Expenses", "TotalExpenses"),
    "finance_costs": ("FinanceCosts", "InterestExpended"),
    "depreciation": ("DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense"),
    "exceptional": ("ExceptionalItemsBeforeTax", "ExceptionalItems"),
    "pbt": ("ProfitBeforeTax", "ProfitLossBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"),
    "tax": ("TaxExpense", "TotalTaxExpenses"),
    "net_profit": ("ProfitLossForPeriod", "ProfitLossForThePeriod", "NetProfitLossForThePeriod"),
    "owners_profit": ("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAttributableToOwnersOfParent"),
    "eps_basic": ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShare"),
    "eps_diluted": ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShare"),
    "paid_up_capital": ("PaidUpValueOfEquityShareCapital",),
    "face_value": ("FaceValueOfEquityShareCapital",),
}
META = ("Symbol", "NameOfTheCompany", "ScripCode", "NatureOfReportStandaloneConsolidated", "WhetherResultsAreAuditedOrUnaudited",
        "DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod", "DateOfBoardMeetingWhenFinancialResultsWereApproved",
        "ISIN", "LevelOfRoundingUsedInFinancialStatements", "TypeOfCompany")


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- XBRL

def _local(tag):
    return tag.split("}")[-1]


def parse_xbrl(text):
    """-> {"meta": {...}, "periods": {(start, end): {metric: value}}}. Contexts with
    dimensions (segments) are skipped: those are segment rows, not the totals."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise ResultsError("not XML: %s" % e)
    ctx = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        has_dim = any(_local(x.tag) in ("explicitMember", "typedMember") for x in c.iter())
        start = end = instant = None
        for x in c.iter():
            n = _local(x.tag)
            if n == "startDate":
                start = (x.text or "").strip()
            elif n == "endDate":
                end = (x.text or "").strip()
            elif n == "instant":
                instant = (x.text or "").strip()
        ctx[c.get("id")] = {"dim": has_dim, "start": start, "end": end or instant}
    want = {}
    for metric, names in ALIASES.items():
        for i, n in enumerate(names):
            want[n] = (metric, i)
    meta, periods, rank = {}, {}, {}
    for el in root.iter():
        name = _local(el.tag)
        ref = el.get("contextRef")
        val = (el.text or "").strip()
        if not ref or val == "":
            continue
        if name in META:
            meta.setdefault(name, val)
            continue
        if name not in want:
            continue
        c = ctx.get(ref)
        if not c or c["dim"] or not c["end"]:
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        metric, prio = want[name]
        key = (c["start"], c["end"])
        cur = rank.get((key, metric))
        if cur is None or prio < cur:
            periods.setdefault(key, {})[metric] = v
            rank[(key, metric)] = prio
    return {"meta": meta, "periods": periods}


def days(start, end):
    try:
        return (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1
    except (TypeError, ValueError):
        return None


def quarters_from(parsed, basis=None):
    """The 3-month periods in one filing (current quarter, previous quarter, a year ago)."""
    meta = parsed["meta"]
    nature = (meta.get("NatureOfReportStandaloneConsolidated") or "").lower()
    out = []
    for (start, end), vals in parsed["periods"].items():
        d = days(start, end)
        if d is None or not 80 <= d <= 100:
            continue
        q = dict(vals)
        q.update({"start": start, "end": end, "basis": "consolidated" if "consol" in nature else
                  ("standalone" if nature else "unknown"), "audited": meta.get("WhetherResultsAreAuditedOrUnaudited"),
                  "filing_period_end": meta.get("DateOfEndOfReportingPeriod")})
        out.append(q)
    if basis:
        out = [q for q in out if q["basis"] == basis]
    return out


def merge(quarters):
    """One row per quarter end. The latest filing's figure wins (restated numbers replace old ones)."""
    by = {}
    for q in sorted(quarters, key=lambda q: q.get("filing_period_end") or ""):
        cur = by.get(q["end"])
        if cur is None or (q.get("filing_period_end") or "") >= (cur.get("filing_period_end") or ""):
            merged = dict(cur or {})
            merged.update({k: v for k, v in q.items() if v is not None})
            if cur and any(abs((cur.get(m) or 0) - (q.get(m) or 0)) > 0.5 for m in ("revenue", "net_profit")
                           if cur.get(m) is not None and q.get(m) is not None):
                merged["restated"] = True
            by[q["end"]] = merged
    return [by[k] for k in sorted(by)]


def _csv_num(s, where):
    """A results-table number: commas and the rupee sign dropped, (20) is -20, "-" or "NA" is no figure."""
    t = s.replace(",", "").replace("\u20b9", "").strip()
    if t.lower() in ("-", "--", "na", "n/a", "nil"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    try:
        x = float(t[1:-1] if neg else t)
    except ValueError:
        raise ResultsError("%s: %r is not a number" % (where, s))
    return -x if neg else x


def _csv_date(s, where):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s.strip()[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise ResultsError("%s: period_end %r is not a date (use YYYY-MM-DD)" % (where, s))


def load_csv(path):
    """Columns: period_end (YYYY-MM-DD), revenue, other_income, finance_costs, depreciation,
    exceptional, pbt, tax, net_profit, owners_profit, eps_diluted. Rupees, or add a "unit"
    column (crore / lakh) or name the file's numbers with --unit."""
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            end = r.get("period_end") or r.get("end") or r.get("quarter_end")
            if not end:
                raise ResultsError("%s needs a period_end column" % path)
            end = _csv_date(end, path)
            unit = {"crore": CRORE, "cr": CRORE, "lakh": 1e5, "lakhs": 1e5}.get(r.get("unit", "").lower(), 1.0)
            q = {"end": end, "start": None, "basis": r.get("basis", "unknown")}
            for m in ALIASES:
                if r.get(m) not in (None, ""):
                    v = _csv_num(r[m], "%s %s %s" % (path, end, m))
                    if v is None:
                        continue
                    q[m] = v if m.startswith("eps") or m == "face_value" else v * unit
            out.append(q)
    return out


# ---------------------------------------------------------------- analysis

def derive(q):
    rev = q.get("revenue")
    pbt = q.get("pbt")
    if pbt is not None and q.get("finance_costs") is not None and q.get("depreciation") is not None:
        q["ebitda"] = pbt + q["finance_costs"] + q["depreciation"] - (q.get("other_income") or 0) - \
            (q.get("exceptional") or 0)
    if rev:
        if q.get("ebitda") is not None:
            q["ebitda_margin"] = 100.0 * q["ebitda"] / rev
        if q.get("net_profit") is not None:
            q["net_margin"] = 100.0 * q["net_profit"] / rev
    if pbt and pbt > 0 and q.get("tax") is not None:
        q["tax_rate"] = 100.0 * q["tax"] / pbt
    if pbt and pbt > 0 and q.get("other_income") is not None:
        q["other_income_share"] = 100.0 * q["other_income"] / pbt
    return q


def pct_change(new, old):
    if new is None or old is None or old == 0:
        return None
    if old < 0:
        return None  # growth from a loss is not a percentage
    return 100.0 * (new / old - 1)


def year_ago(end):
    d = datetime.date.fromisoformat(end)
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def months_before(end, n):
    d = datetime.date.fromisoformat(end)
    m = d.month - 1 - n
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def near(by_end, target, slack=7):
    return next((by_end[e] for e in by_end if abs((datetime.date.fromisoformat(e) - target).days) <= slack), None)


def analyse(quarters):
    qs = [derive(dict(q)) for q in merge(quarters)]
    by_end = {q["end"]: q for q in qs}
    for q in qs:
        prev_year = near(by_end, year_ago(q["end"]))
        prev_q = near(by_end, months_before(q["end"], 3))
        for m in ("revenue", "ebitda", "net_profit", "owners_profit", "eps_diluted"):
            q[m + "_yoy"] = pct_change(q.get(m), prev_year.get(m)) if prev_year else None
            q[m + "_qoq"] = pct_change(q.get(m), prev_q.get(m)) if prev_q else None
        flags = []
        if (q.get("other_income_share") or 0) > 25:
            flags.append("other income is %.0f%% of profit before tax" % q["other_income_share"])
        if q.get("exceptional"):
            flags.append("exceptional item of ₹%.2f cr in profit" % (q["exceptional"] / CRORE))
        if q.get("tax_rate") is not None and not 15 <= q["tax_rate"] <= 35:
            flags.append("tax rate %.1f%% (the usual is about 25%%)" % q["tax_rate"])
        if (q.get("net_profit") or 0) < 0:
            flags.append("a loss")
        if q.get("restated"):
            flags.append("restated in a later filing")
        q["flags"] = flags
    ttm = None
    if len(qs) >= 4:
        last4 = qs[-4:]
        spans = (datetime.date.fromisoformat(last4[-1]["end"]) - datetime.date.fromisoformat(last4[0]["end"])).days
        if 260 <= spans <= 290:
            ttm = {}
            for m in ("revenue", "ebitda", "net_profit", "owners_profit", "eps_diluted"):
                vals = [q.get(m) for q in last4]
                ttm[m] = sum(vals) if all(v is not None for v in vals) else None
    bases = sorted({q.get("basis") for q in qs if q.get("basis")})
    return {"quarters": qs, "ttm": ttm, "mixed_basis": len(bases) > 1, "bases": bases}


def metrics_row(symbol, name, res):
    qs = res["quarters"]
    if not qs:
        return None
    q = qs[-1]
    t = res["ttm"] or {}
    r = lambda x, n=2: round(x, n) if x is not None else None
    return {"ticker": symbol, "name": name, "period_end": q["end"], "basis": q.get("basis"),
            "revenue_ttm": t.get("revenue"), "net_profit_ttm": t.get("owners_profit") or t.get("net_profit"),
            "eps_ttm": r(t.get("eps_diluted")), "revenue_growth_yoy": r(q.get("revenue_yoy"), 1),
            "profit_growth_yoy": r(q.get("owners_profit_yoy") if q.get("owners_profit_yoy") is not None
                                   else q.get("net_profit_yoy"), 1),
            "eps_growth_yoy": r(q.get("eps_diluted_yoy"), 1), "ebitda_margin": r(q.get("ebitda_margin"), 1),
            "net_margin": r(q.get("net_margin"), 1), "other_income_share": r(q.get("other_income_share"), 1),
            "flags": q["flags"], "source": "exchange results XBRL"}


# ---------------------------------------------------------------- fetching from NSE

def fetch_list(symbol, offline=None):
    if offline:
        path = os.path.join(offline, "results-%s.json" % symbol)
        if not os.path.exists(path):
            raise ResultsError("no offline file %s" % path)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        opener = None
    else:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        h = {"User-Agent": UA, "Accept": "application/json,*/*", "Referer": "https://www.nseindia.com/"}
        try:
            opener.open(urllib.request.Request("https://www.nseindia.com/", headers=h), timeout=30).read()
            with opener.open(urllib.request.Request(NSE_RESULTS % urllib.parse.quote(symbol), headers=h), timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise ResultsError("could not read NSE's results list for %s: %s" % (symbol, e))
    try:
        rows = json.loads(text)
    except ValueError:
        raise ResultsError("NSE's results list for %s was not JSON (the site may have blocked the request)" % symbol)
    rows = rows.get("data", rows) if isinstance(rows, dict) else rows
    out = []
    for r in rows or []:
        out.append({"to": r.get("toDate") or r.get("to_date"), "from": r.get("fromDate"),
                    "consolidated": str(r.get("consolidated") or "").lower(), "audited": r.get("audited"),
                    "filed": r.get("filingDate") or r.get("broadCastDate"), "xbrl": r.get("xbrl") or "",
                    "period": r.get("period") or r.get("relatingTo")})
    return out, opener


def cmd_fetch(a):
    rows, opener = fetch_list(a.symbol.upper(), a.offline)
    want = "consolidated" if a.basis != "standalone" else "non-consolidated"
    pick = [r for r in rows if r["xbrl"].lower().endswith(".xml") and
            (r["consolidated"].startswith(want) or (want == "consolidated" and r["consolidated"] == "consolidated"))]
    if not pick:
        pick = [r for r in rows if r["xbrl"].lower().endswith(".xml")]
    pick = pick[:a.quarters]
    os.makedirs(a.dir, exist_ok=True)
    saved = []
    for r in pick:
        name = os.path.join(a.dir, "%s-%s.xml" % (a.symbol.upper(), re.sub(r"[^0-9A-Za-z]+", "", r["to"] or "x")))
        if a.offline:
            src = os.path.join(a.offline, os.path.basename(r["xbrl"]))
            if not os.path.exists(src):
                raise ResultsError("no offline file %s" % src)
            with open(src, "rb") as fh, open(name, "wb") as out:
                out.write(fh.read())
        else:
            h = {"User-Agent": UA, "Referer": "https://www.nseindia.com/"}
            try:
                with opener.open(urllib.request.Request(r["xbrl"], headers=h), timeout=60) as resp, \
                        open(name, "wb") as out:
                    out.write(resp.read())
            except (urllib.error.URLError, urllib.error.HTTPError) as e:
                raise ResultsError("could not download %s: %s" % (r["xbrl"], e))
        saved.append(name)
        print("%s  %-18s %s  filed %s -> %s" % (r["to"], r["consolidated"] or "?", r["audited"] or "", r["filed"], name))
    if not saved:
        print("No XBRL results found for %s." % a.symbol.upper())
        return 1
    print("Next: results.py read %s" % " ".join(saved))
    return 0


# ---------------------------------------------------------------- report

def cr(x):
    return "" if x is None else "%.2f" % (x / CRORE)


def pc(x):
    return "" if x is None else "%+.1f%%" % x


def cmd_read(a):
    quarters, name, symbol = [], a.name or "", a.symbol or ""
    for path in a.files:
        if path.lower().endswith(".csv"):
            quarters += load_csv(path)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            parsed = parse_xbrl(fh.read())
        name = name or parsed["meta"].get("NameOfTheCompany", "")
        symbol = symbol or parsed["meta"].get("Symbol", "") or parsed["meta"].get("ScripCode", "")
        quarters += quarters_from(parsed)
    if a.basis:
        quarters = [q for q in quarters if q.get("basis") in (a.basis, "unknown")]
    elif any(q.get("basis") == "consolidated" for q in quarters):
        quarters = [q for q in quarters if q.get("basis") != "standalone"]
    if not quarters:
        raise ResultsError("no 3-month periods found in these files")
    res = analyse(quarters)
    qs = res["quarters"][-a.quarters:]
    row = metrics_row(symbol.upper(), name, res)
    if a.metrics and row and not row.get("ticker"):
        raise ResultsError("--metrics needs --symbol: these files do not name the company, and a row without a "
                           "ticker breaks the screener's metrics file")
    if a.metrics and row:
        doc = {"as_of": datetime.date.today().isoformat(), "companies": []}
        if os.path.exists(a.metrics):
            with open(a.metrics, encoding="utf-8") as fh:
                doc = json.load(fh)
        doc["companies"] = [c for c in doc.get("companies", []) if c.get("ticker") != row["ticker"]] + [row]
        d = os.path.dirname(a.metrics)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.metrics, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps({"symbol": symbol, "name": name, "quarters": qs, "ttm": res["ttm"], "metrics": row},
                         indent=2, ensure_ascii=False))
        return 0
    print("%s %s, %s figures, rupees in crores" % (symbol, name, "/".join(res["bases"]) or "?"))
    print("%-11s %10s %8s %8s %10s %7s %10s %8s %8s %7s" % ("quarter", "revenue", "YoY", "QoQ", "EBITDA", "margin",
                                                           "profit", "YoY", "EPS", "YoY"))
    for q in qs:
        prof = q.get("owners_profit") if q.get("owners_profit") is not None else q.get("net_profit")
        prof_yoy = q.get("owners_profit_yoy") if q.get("owners_profit") is not None else q.get("net_profit_yoy")
        print("%-11s %10s %8s %8s %10s %7s %10s %8s %8s %7s"
              % (q["end"], cr(q.get("revenue")), pc(q.get("revenue_yoy")), pc(q.get("revenue_qoq")), cr(q.get("ebitda")),
                 ("%.1f%%" % q["ebitda_margin"]) if q.get("ebitda_margin") is not None else "", cr(prof), pc(prof_yoy),
                 ("%.2f" % q["eps_diluted"]) if q.get("eps_diluted") is not None else "", pc(q.get("eps_diluted_yoy"))))
    for q in qs:
        for f in q["flags"]:
            print("FLAG %s: %s" % (q["end"], f))
    if res["ttm"]:
        t = res["ttm"]
        print("Last four quarters: revenue ₹%s cr, profit ₹%s cr, diluted EPS ₹%s."
              % (cr(t["revenue"]), cr(t.get("owners_profit") or t.get("net_profit")),
                 ("%.2f" % t["eps_diluted"]) if t.get("eps_diluted") is not None else "?"))
    else:
        print("Fewer than four consecutive quarters: no trailing-twelve-month figures.")
    if res["mixed_basis"]:
        print("WARNING: consolidated and standalone quarters are mixed; compare like with like (--basis).")
    print("Numbers as filed with the exchange. EPS is for the quarter, not annualised. Read the results PDF and the "
          "notes for one-offs before drawing conclusions.")
    if a.metrics and row:
        print("Updated %s for india-screener." % a.metrics)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quarterly results from exchange XBRL (india-analyst).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("fetch", help="download the result XBRL files listed on NSE")
    p.add_argument("symbol")
    p.add_argument("--quarters", type=int, default=5)
    p.add_argument("--basis", choices=["consolidated", "standalone"], default="consolidated")
    p.add_argument("--dir", default="trader/state/results")
    p.add_argument("--offline", metavar="DIR")
    p = sub.add_parser("read", help="analyse result XBRL or CSV files")
    p.add_argument("files", nargs="+")
    p.add_argument("--symbol")
    p.add_argument("--name")
    p.add_argument("--basis", choices=["consolidated", "standalone"])
    p.add_argument("--quarters", type=int, default=8)
    p.add_argument("--metrics", help="add a row to this metrics file (trader/metrics.json)")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"fetch": cmd_fetch, "read": cmd_read}[a.cmd](a)
    except (ResultsError, OSError) as e:
        print("results.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
