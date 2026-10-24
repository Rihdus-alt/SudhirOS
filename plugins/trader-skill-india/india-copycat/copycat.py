#!/usr/bin/env python3
"""Copy a fund or an investor you trust, from public Indian disclosures (india-copycat).
Standard library only.

Two kinds of source, both public:

  mutual fund   SEBI makes every mutual fund scheme publish its FULL portfolio
                every month (and fortnightly for some debt schemes): each
                holding with its ISIN, quantity, market value and % of net
                assets, within 10 days of the month end. AMCs post it as an
                Excel file. This reads that file (.xlsx or .csv), or the
                INDmoney connector's get_mf_funds_details output.
  investor      Listed companies file a shareholding pattern every quarter
                (SEBI LODR Reg 31) naming every PUBLIC shareholder with more
                than 1% of the company. That is how "superstar" portfolios
                (Rekha Jhunjhunwala, Radhakishan Damani, Ashish Kacholia ...)
                are rebuilt. This reads rows you collect from those filings
                (CSV or JSON), or the shareholding-pattern XBRL itself.

    python3 copycat.py symbols --download                    # NSE's equity list -> ISIN to symbol
    python3 copycat.py mf portfolio.xlsx --sheet "Flexi Cap" # read an AMC monthly portfolio
    python3 copycat.py mf fund.json --indmoney               # INDmoney connector output
    python3 copycat.py diff last-month.xlsx this-month.xlsx  # what the fund bought and sold
    python3 copycat.py copy portfolio.xlsx --top 10 --out trader/targets.json
    python3 copycat.py investor holdings.csv --name "Kacholia" --prices trader/prices.json
    python3 copycat.py shp SHP.xml --min-pct 1               # named holders in one SHP XBRL

ISIN to NSE symbol: trader/symbols.json first ({"INE002A01018": "RELIANCE"}),
then NSE's equity list (EQUITY_L.csv, saved by `symbols --download` to
trader/state/nse-equity-list.csv), then an exact company-name match. A row it
cannot resolve is listed, never guessed, unless --guess (marked *).

Every ISIN is checked against its check digit. Rows that are not Indian
listed equity (debt, T-bills, cash, REIT/InvIT units, foreign stocks such as
a US share a flexi-cap fund holds) are listed as excluded with the reason.
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

UA = "Mozilla/5.0 (X11; Linux x86_64) trader-skill-india/1.0"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
EPS = 1e-9


class CopyError(Exception):
    pass


# ---------------------------------------------------------------- ISIN

def isin_valid(s):
    s = str(s or "").strip().upper()
    if not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(s[-1])


def isin_kind(s):
    """(kind, reason). kind is "equity" for an Indian listed company's fully paid equity share."""
    s = str(s or "").strip().upper()
    if not s:
        return "unknown", "no ISIN"
    if not isin_valid(s):
        return "invalid", "ISIN %s fails its check digit" % s
    if not s.startswith("IN"):
        return "foreign", "%s is a foreign security; it cannot be bought on NSE or BSE" % s
    if s.startswith("IN9"):
        return "partly-paid", "%s is a partly paid share" % s
    if s[2] != "E":
        return "government", "%s is a government security or T-bill" % s
    if s[7:9] == "01":
        return "equity", ""
    return "other", "%s is not an equity share (security code %s: debt, units or other)" % (s, s[7:9])


# ---------------------------------------------------------------- symbols

def norm_name(name):
    n = str(name or "").lower().replace("&", " and ")
    n = re.sub(r"\b(limited|ltd|the|co|company|corporation|corp|inc|india|ord|ordinary|shares|eq)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def load_symbols(user_map=None, equity_list=None):
    """-> (isin->symbol, normalized name->symbol, symbol->name)"""
    by_isin, by_name, names = {}, {}, {}
    if equity_list and os.path.exists(equity_list):
        with open(equity_list, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
                sym, isin, name = r.get("SYMBOL"), r.get("ISIN NUMBER"), r.get("NAME OF COMPANY")
                series = r.get("SERIES", "EQ")
                if not sym or series not in ("EQ", "BE", "BZ", "SM", "ST", ""):
                    continue
                if isin:
                    by_isin[isin.upper()] = sym
                if name:
                    by_name.setdefault(norm_name(name), sym)
                    names[sym] = name
    if user_map and os.path.exists(user_map):
        with open(user_map, encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                if isin_valid(k):
                    by_isin[k.upper()] = v.upper()
                else:
                    by_name[norm_name(k)] = v.upper()
    return by_isin, by_name, names


def resolve(row, maps, guess=False):
    by_isin, by_name, _ = maps
    isin = (row.get("isin") or "").upper()
    if row.get("symbol"):
        return row["symbol"].upper(), "given"
    if isin and isin in by_isin:
        return by_isin[isin], "isin"
    n = norm_name(row.get("name"))
    if n and n in by_name:
        return by_name[n], "name"
    if guess and n:
        hits = sorted(s for k, s in by_name.items() if k.startswith(n[:8]) or n.startswith(k[:8]))
        if len(hits) == 1:
            return hits[0] + "*", "guess"
    return None, "unresolved"


# ---------------------------------------------------------------- xlsx (stdlib)

def _col(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path):
    """{sheet name: [[cell, ...], ...]} using only zipfile and ElementTree."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise CopyError("%s is not an .xlsx file (an old .xls? save it as .xlsx or .csv)" % path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for sh in wb.find("m:sheets", ns):
        rid = sh.get("{%s}id" % ns["r"])
        t = target.get(rid, "")
        part = "xl/" + t.lstrip("/").replace("xl/", "", 1) if not t.startswith("xl/") else t
        if part not in z.namelist():
            continue
        root = ET.fromstring(z.read(part))
        rows = []
        for row in root.iter("{%s}row" % ns["m"]):
            cells = {}
            for c in row.findall("m:c", ns):
                ref, typ = c.get("r", "A1"), c.get("t")
                v = c.find("m:v", ns)
                if typ == "s" and v is not None:
                    val = shared[int(v.text)]
                elif typ == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                else:
                    val = v.text if v is not None else ""
                cells[_col(ref)] = val
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        out[sh.get("name")] = rows
    return out


def read_table(path, sheet=None):
    """-> (sheet name, rows). CSV, or the matching sheet of an .xlsx."""
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return os.path.basename(path), [r for r in csv.reader(fh)]
    sheets = read_xlsx(path)
    if sheet:
        hits = [k for k in sheets if sheet.lower() in k.lower()]
        if not hits:
            hits = [k for k, rows in sheets.items()
                    if any(sheet.lower() in " ".join(map(str, r[:6])).lower() for r in rows[:8])]
        if not hits:
            raise CopyError("no sheet matches %r; sheets: %s" % (sheet, ", ".join(sheets)))
        if len(hits) > 1:
            raise CopyError("%r matches several sheets: %s" % (sheet, ", ".join(hits)))
        return hits[0], sheets[hits[0]]
    with_isin = [k for k, rows in sheets.items() if any("isin" in " ".join(map(str, r)).lower() for r in rows[:40])]
    if len(with_isin) != 1:
        raise CopyError("%s has %d sheets with a portfolio (%s): pick one with --sheet"
                        % (path, len(with_isin), ", ".join(with_isin) or "none"))
    return with_isin[0], sheets[with_isin[0]]


# ---------------------------------------------------------------- AMC monthly portfolio

def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("₹", "")
    if s in ("", "-", "--", "NIL", "Nil", "nil"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


HEAD = {
    "isin": ("isin",),
    "name": ("name of the instrument", "name of instrument", "company/issuer/instrument name", "name of the security",
             "instrument", "security name", "company name", "name"),
    "quantity": ("quantity", "no. of shares", "number of shares", "qty"),
    "value": ("market value", "market/fair value", "market / fair value", "fair value", "value"),
    "pct": ("% to net assets", "% to nav", "% of nav", "% to net asset", "%age to nav", "% of net assets",
            "% to aum", "percentage to nav", "% of net asset"),
    "industry": ("industry", "rating", "industry / rating", "industry/rating", "sector"),
}


def find_header(rows):
    for i, r in enumerate(rows[:60]):
        low = [str(c).strip().lower() for c in r]
        if not any("isin" == c or c.startswith("isin") for c in low):
            continue
        cols = {}
        for key, words in HEAD.items():
            for j, c in enumerate(low):
                if j in cols.values():
                    continue
                if any(c == w or c.startswith(w) for w in words):
                    cols[key] = j
                    break
        if "pct" in cols and ("name" in cols or "isin" in cols):
            return i, cols
    raise CopyError("no header row with ISIN and % to net assets found in the first 60 rows")


def parse_amc(rows, sheet=""):
    """-> {"title", "as_of", "rows": [...], "unit"}. Value unit is read from the header (lakhs or crores)."""
    hi, cols = find_header(rows)
    head_text = " ".join(str(c) for c in rows[hi]).lower()
    unit = 1e5 if ("lakh" in head_text or "lacs" in head_text) else (1e7 if "crore" in head_text else 1.0)
    title, as_of = None, None
    for r in rows[:hi]:
        line = " ".join(str(c) for c in r if str(c).strip())
        if line and not title and re.search(r"\bfund\b|\bscheme\b|\betf\b", line, re.I) \
                and not re.match(r"^[A-Z0-9 &]+MUTUAL FUND$", line.strip()):
            title = re.sub(r"\s*\(.*\)\s*$", "", line).strip()
        m = re.search(r"(?:as on|as at|as of)\s+([0-9]{1,2}[\s\-/][A-Za-z0-9]{2,9}[\s\-/,]+[0-9]{2,4})", line, re.I)
        if m and not as_of:
            as_of = _date(m.group(1))
    out, section = [], ""
    for r in rows[hi + 1:]:
        cells = [str(c).strip() for c in r]
        get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""
        isin = get("isin").upper()
        name = get("name")
        if not isin:
            label = " ".join(c for c in cells if c)
            if label and not any(num(c) is not None for c in cells if c and c != label):
                section = label
            if re.match(r"^(grand\s+)?total|^net assets", label, re.I):
                section = label
            continue
        kind, why = isin_kind(isin)
        out.append({"isin": isin, "name": name, "quantity": num(get("quantity")),
                    "value_inr": (num(get("value")) * unit) if num(get("value")) is not None else None,
                    "pct": num(get("pct")), "industry": get("industry"), "section": section, "kind": kind,
                    "excluded": why})
    if not out:
        raise CopyError("no holdings rows under the header")
    pcts = [r["pct"] for r in out if r["pct"] is not None]
    if pcts and max(abs(x) for x in pcts) <= 1.0 and sum(pcts) <= 1.05:
        # an Excel percentage column stores 7.79% as 0.0779
        for r in out:
            if r["pct"] is not None:
                r["pct"] *= 100.0
    return {"title": title or sheet, "as_of": as_of, "rows": out, "unit": unit}


def _date(text):
    t = re.sub(r"[\s,]+", " ", text.replace("-", " ").replace("/", " ")).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d %m %Y", "%d %b %y", "%d %B %y"):
        try:
            return datetime.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_indmoney_mf(doc):
    """INDmoney get_mf_funds_details (with "holdings") -> rows. Weights are rounded to 0.1%."""
    if isinstance(doc, dict) and isinstance(doc.get("result"), str):
        doc = json.loads(doc["result"])
    data = (doc.get("data") or [{}])[0].get("data", {}) if isinstance(doc, dict) else {}
    fd, hold = data.get("fund_detail", {}), data.get("holdings", {})
    if not hold:
        raise CopyError("no holdings in the INDmoney output: call get_mf_funds_details with includes [\"holdings\"]")
    counts = {c.get("name"): c for c in hold.get("holdings_count", [])}
    rows = []
    for group in hold.get("holdings", []):
        equity = str(group.get("name", "")).lower().startswith("equity")
        for h in group.get("holds", []):
            pct = num(h.get("perc"))
            rows.append({"isin": "", "name": h.get("name"), "quantity": num(h.get("current_number_of_shares")),
                         "value_inr": None, "pct": pct, "industry": h.get("sector", ""), "section": group.get("name"),
                         "kind": "equity" if equity else "debt", "excluded": "" if equity else "debt or cash",
                         "change_pct": h.get("share_change_percentage")})
    eq = counts.get("Equity Holdings", {})
    return {"title": fd.get("name", "fund"), "as_of": None, "rows": rows, "unit": 1.0, "source": "indmoney",
            "equity_count": eq.get("count"), "equity_pct": eq.get("perc"), "aum_cr": fd.get("aum")}


# ---------------------------------------------------------------- weights

def equity_rows(port, maps, guess=False):
    """Indian listed equity rows with symbols; everything else listed with a reason."""
    keep, skipped = [], []
    for r in port["rows"]:
        if r["kind"] != "equity":
            if r["kind"] == "unknown" and port.get("source") != "indmoney":
                skipped.append(dict(r, reason="no ISIN"))
            elif r["kind"] != "unknown":
                skipped.append(dict(r, reason=r["excluded"] or r["kind"]))
            continue
        if port.get("source") == "indmoney" and r["name"] and re.search(r"\b(inc|corp|plc|ag|nv|sa)\b\.?$",
                                                                           r["name"].lower()):
            skipped.append(dict(r, reason="a foreign company; it cannot be bought on NSE or BSE"))
            continue
        if (r.get("pct") or 0) <= 0:
            skipped.append(dict(r, reason="0% of net assets (unlisted, written off or a crumb)"))
            continue
        sym, how = resolve(r, maps, guess)
        if not sym:
            skipped.append(dict(r, reason="no NSE symbol for ISIN %s / %r: add it to trader/symbols.json"
                                % (r["isin"] or "?", r["name"])))
            continue
        keep.append(dict(r, symbol=sym, matched_by=how))
    merged = {}
    for r in keep:
        m = merged.get(r["symbol"])
        if m:
            m["pct"] += r["pct"]
            m["quantity"] = (m["quantity"] or 0) + (r["quantity"] or 0)
            m["value_inr"] = (m["value_inr"] or 0) + (r["value_inr"] or 0) if r["value_inr"] is not None else m["value_inr"]
        else:
            merged[r["symbol"]] = dict(r)
    rows = sorted(merged.values(), key=lambda r: (-r["pct"], r["symbol"]))
    return rows, skipped


def targets(rows, top=None, exclude=(), min_pct=0.0):
    ex = {e.upper() for e in exclude or []}
    pick, dropped = [], []
    for r in rows:
        if r["symbol"].rstrip("*") in ex:
            dropped.append({"ticker": r["symbol"], "reason": "excluded by you"})
        elif r["pct"] < min_pct:
            dropped.append({"ticker": r["symbol"], "reason": "under %s%% of net assets" % min_pct})
        else:
            pick.append(r)
    if top:
        for r in pick[top:]:
            dropped.append({"ticker": r["symbol"], "reason": "outside the top %d" % top})
        pick = pick[:top]
    total = sum(r["pct"] for r in pick)
    if not total:
        raise CopyError("nothing left to copy")
    weights = [{"ticker": r["symbol"], "name": r["name"], "isin": r.get("isin") or None,
                "weight": round(100.0 * r["pct"] / total, 4), "pct_of_source": round(r["pct"], 4),
                "guessed": r["symbol"].endswith("*")} for r in pick]
    return weights, dropped


def diff(old_rows, new_rows):
    """By ISIN (or symbol) and share count: new, exited, added, trimmed."""
    # the resolved symbol first: a split or consolidation gives the same company a new ISIN
    key = lambda r: (r.get("symbol") or "").rstrip("*") or r.get("isin") or norm_name(r.get("name"))
    a = {key(r): r for r in old_rows}
    b = {key(r): r for r in new_rows}
    out = {"new": [], "exited": [], "added": [], "trimmed": [], "same": []}
    for k, r in b.items():
        if k not in a:
            out["new"].append(r)
            continue
        q0, q1 = a[k].get("quantity") or 0, r.get("quantity") or 0
        if q1 > q0 + EPS:
            out["added"].append(dict(r, change=q1 - q0, change_pct=100.0 * (q1 / q0 - 1) if q0 else None))
        elif q1 < q0 - EPS:
            out["trimmed"].append(dict(r, change=q1 - q0, change_pct=100.0 * (q1 / q0 - 1) if q0 else None))
        else:
            out["same"].append(r)
    for k, r in a.items():
        if k not in b:
            out["exited"].append(r)
    return out


# ---------------------------------------------------------------- investor (shareholding patterns)

def load_investor_rows(path):
    """CSV or JSON rows: investor, symbol, period (quarter end), shares[, pct_of_company, source]."""
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    else:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        rows = d.get("rows", d) if isinstance(d, dict) else d
    out = []
    for i, r in enumerate(rows):
        sym = str(r.get("symbol") or r.get("ticker") or "").upper()
        sh = num(r.get("shares"))
        if not sym or sh is None or not r.get("period"):
            raise CopyError("row %d needs symbol, period and shares: %r" % (i + 1, r))
        per = str(r["period"]).strip()
        # NSE writes quarter ends as 30-Sep-2026: a raw [:10] cut loses the year's last digit, so parse it
        per = per[:10] if re.match(r"\d{4}-\d{2}-\d{2}", per) else _date(per)
        if not per:
            raise CopyError("row %d: period %r is not a date (use YYYY-MM-DD, DD-MM-YYYY or 30-Sep-2026)"
                            % (i + 1, r["period"]))
        out.append({"investor": r.get("investor") or r.get("holder") or "", "symbol": sym, "period": per,
                    "shares": sh, "pct_of_company": num(r.get("pct_of_company") or r.get("pct")),
                    "source": r.get("source", "")})
    return out


def investor_portfolio(rows, name, prices):
    """Latest quarter's holdings by value, and the change from the quarter before."""
    mine = [r for r in rows if name.lower() in r["investor"].lower()]
    if not mine:
        raise CopyError("no rows for an investor matching %r" % name)
    periods = sorted({r["period"] for r in mine})
    latest = periods[-1]
    prev = periods[-2] if len(periods) > 1 else None
    by = {}
    for r in mine:
        by.setdefault(r["period"], {}).setdefault(r["symbol"], 0.0)
        by[r["period"]][r["symbol"]] += r["shares"]
    now = by[latest]
    missing = [s for s in now if not prices.get(s)]
    held = []
    for s, sh in now.items():
        if prices.get(s):
            held.append({"symbol": s, "shares": sh, "price": prices[s], "value_inr": sh * prices[s],
                         "pct_of_company": next((r["pct_of_company"] for r in mine
                                                 if r["symbol"] == s and r["period"] == latest), None)})
    total = sum(h["value_inr"] for h in held)
    for h in held:
        h["pct"] = 100.0 * h["value_inr"] / total if total else 0.0
    held.sort(key=lambda h: -h["value_inr"])
    changes = None
    if prev:
        before = by[prev]
        changes = {"new": sorted(s for s in now if s not in before),
                   "exited_or_below_1pct": sorted(s for s in before if s not in now),
                   "added": sorted(s for s in now if s in before and now[s] > before[s] + EPS),
                   "trimmed": sorted(s for s in now if s in before and now[s] < before[s] - EPS)}
    return {"investor": sorted({r["investor"] for r in mine}), "period": latest, "previous": prev,
            "holdings": held, "total_inr": total, "missing_prices": missing, "changes": changes}


# ---------------------------------------------------------------- shareholding pattern XBRL (best effort)

NAME_TAGS = ("nameoftheshareholder", "nameofshareholder", "nameoftheshareholders", "nameoftheholder")
SHARE_TAGS = ("numberoffullypaidupequityshares", "numberofequityshares", "totalnumberofshares",
              "numberofsharesheld", "totalnumberofsharesheld")
PCT_TAGS = ("shareholdingasapercentageoftotalnumberofshares", "shareholdingaspercentageoftotalnumberofshares",
            "percentageofshareholding", "shareholdingpercentage")


def parse_shp_xbrl(text):
    """Named holders in a shareholding-pattern XBRL: groups facts by context and keeps
    contexts that carry a name and a share count. Best effort: check it against the PDF."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise CopyError("not XML: %s" % e)
    meta, ctx = {}, {}
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        low = tag.lower()
        ref = el.get("contextRef")
        val = (el.text or "").strip()
        if not ref or not val:
            continue
        if low in ("symbol", "scripcode", "nameofthecompany", "isin", "dateofreport", "shareholdingpatternfortheperiodended",
                   "dateofthereport", "quarterending"):
            meta.setdefault(low, val)
            continue
        c = ctx.setdefault(ref, {})
        if low in NAME_TAGS:
            c["name"] = val
        elif low in SHARE_TAGS and "shares" not in c:
            c["shares"] = num(val)
        elif low in PCT_TAGS:
            p = num(val)
            c["pct"] = p * 100.0 if p is not None and p <= 1.0 and "." in val else p
        elif "category" in low and "name" not in low:
            c.setdefault("category", val)
    rows = [dict(v, context=k) for k, v in ctx.items() if v.get("name") and v.get("shares") is not None]
    rows.sort(key=lambda r: -(r["shares"] or 0))
    return {"company": meta.get("nameofthecompany"), "symbol": meta.get("symbol"), "isin": meta.get("isin"),
            "period": meta.get("shareholdingpatternfortheperiodended") or meta.get("quarterending")
            or meta.get("dateofreport"), "holders": rows}


# ---------------------------------------------------------------- output

def pct(x):
    return "%.2f%%" % x


def indian(n):
    """15872922 -> 1,58,72,922"""
    neg = n < 0
    whole = "%d" % round(abs(n))
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups)
    return ("-" if neg else "") + whole


def load_prices(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return {str(k).upper().replace("NSE:", ""): float(v) for k, v in (d.get("prices", d) or {}).items()
            if isinstance(v, (int, float))}


def _load_port(path, a):
    if a.indmoney or path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            return parse_indmoney_mf(json.load(fh))
    sheet, rows = read_table(path, a.sheet)
    port = parse_amc(rows, sheet)
    port["source"] = "amc"
    return port


def _maps(a):
    return load_symbols(a.symbols_map, a.equity_list)


def _caveats(port):
    c = []
    if port.get("source") == "indmoney":
        listed = sum(r["pct"] or 0 for r in port["rows"] if r["kind"] == "equity")
        c.append("INDmoney lists %d equity rows summing %.1f%% of net assets, rounded to 0.1%%; the fund's equity "
                 "is %.1f%%. For an exact copy use the AMC's monthly portfolio file."
                 % (sum(1 for r in port["rows"] if r["kind"] == "equity"), listed, port.get("equity_pct") or 0))
    c.append("A monthly portfolio is a snapshot of the last day of the month, published up to 10 days later. "
             "The fund may have traded since.")
    return c


def cmd_mf(a):
    port = _load_port(a.file, a)
    rows, skipped = equity_rows(port, _maps(a), a.guess)
    if a.json:
        print(json.dumps({"fund": port["title"], "as_of": port.get("as_of"), "equity": rows, "excluded": skipped,
                          "caveats": _caveats(port)}, indent=2, ensure_ascii=False))
        return 0
    print("%s%s" % (port["title"], (", portfolio as on %s" % port["as_of"]) if port.get("as_of") else ""))
    eq = sum(r["pct"] for r in rows)
    print("%d Indian listed equity holdings, %.2f%% of net assets." % (len(rows), eq))
    print("")
    print("%4s %-14s %-40s %9s %15s" % ("#", "symbol", "name", "% NAV", "quantity"))
    for i, r in enumerate(rows[:a.top or len(rows)], 1):
        print("%4d %-14s %-40s %9s %15s" % (i, r["symbol"], (r["name"] or "")[:40], pct(r["pct"]),
                                          indian(r["quantity"]) if r.get("quantity") else ""))
    by_reason = {}
    for s in skipped:
        by_reason.setdefault(s["reason"].split(":")[0] if "symbol" in s["reason"] else s["reason"], []).append(s)
    if skipped:
        print("")
        print("Not copied (%d rows, %.2f%% of net assets):" % (len(skipped), sum(s.get("pct") or 0 for s in skipped)))
        for s in skipped[:25]:
            print("  %-40s %8s  %s" % ((s.get("name") or s.get("isin") or "")[:40], pct(s.get("pct") or 0), s["reason"]))
    for c in _caveats(port):
        print("Note: " + c)
    return 0


def cmd_copy(a):
    port = _load_port(a.file, a)
    rows, skipped = equity_rows(port, _maps(a), a.guess)
    weights, dropped = targets(rows, a.top, a.exclude, a.min_pct)
    floor = min(w["pct_of_source"] for w in weights)
    unmapped = [s for s in skipped if s["reason"].startswith("no NSE symbol")]
    if a.top:
        ranked = sorted([r["pct"] for r in rows] + [s.get("pct") or 0 for s in unmapped], reverse=True)
        if len(ranked) >= a.top:
            floor = min(floor, ranked[a.top - 1])
        else:
            floor = 0.0
    unresolved = [s for s in unmapped if (s.get("pct") or 0) >= floor and (s.get("pct") or 0) > 0]
    if unresolved and not a.skip_unresolved:
        raise CopyError("%d holding(s) as big as the ones being copied have no NSE symbol: %s. Map them in "
                        "trader/symbols.json (or run symbols --download), or pass --skip-unresolved"
                        % (len(unresolved), "; ".join("%s %s" % (u.get("name"), pct(u.get("pct") or 0))
                                                       for u in unresolved[:6])))
    guessed = [w["ticker"] for w in weights if w["guessed"]]
    if guessed and not a.allow_guesses:
        raise CopyError("these symbols are guesses: %s. Confirm them in trader/symbols.json, or pass --allow-guesses"
                        % ", ".join(guessed))
    src = "%s %s%s%s" % (port["title"],
                         "holdings as listed by INDmoney" if port.get("source") == "indmoney" else "monthly portfolio",
                         (" as on %s" % port["as_of"]) if port.get("as_of") else "", (", top %d" % a.top) if a.top else "")
    doc = {"source": src, "as_of": port.get("as_of"), "kind": "mutual fund", "weights": weights,
           "dropped": dropped, "not_copied": [{"name": s.get("name"), "isin": s.get("isin"), "pct": s.get("pct"),
                                               "reason": s["reason"]} for s in skipped],
           "caveats": _caveats(port)}
    for w in weights:
        w["weight"] = round(w["weight"], 2)
    if a.out:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    print("Copying: " + src)
    print("%4s %-14s %-36s %10s %10s" % ("#", "symbol", "name", "% of fund", "copy wt"))
    for i, w in enumerate(weights, 1):
        print("%4d %-14s %-36s %10s %10s" % (i, w["ticker"], (w["name"] or "")[:36], pct(w["pct_of_source"]),
                                             pct(w["weight"])))
    for d in dropped[:10]:
        print("Dropped %s: %s" % (d["ticker"], d["reason"]))
    for c in doc["caveats"]:
        print("Note: " + c)
    if a.out:
        print("Wrote %s. Next: /india-budget sizes it to your money in whole shares." % a.out)
    return 0


def cmd_diff(a):
    maps = _maps(a)
    old = equity_rows(_load_port(a.old, a), maps, a.guess)[0]
    new = equity_rows(_load_port(a.new, a), maps, a.guess)[0]
    d = diff(old, new)
    if a.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    for k, label in (("new", "NEW"), ("exited", "EXITED"), ("added", "ADDED"), ("trimmed", "TRIMMED")):
        for r in d[k]:
            extra = ""
            if "change" in r:
                extra = " %s%s shares (%+.1f%%)" % ("+" if r["change"] > 0 else "", indian(r["change"]),
                                                   r["change_pct"] or 0)
            print("%-8s %-14s %-36s %s%s" % (label, r["symbol"], (r["name"] or "")[:36], pct(r["pct"]), extra))
    print("%d unchanged. Share counts at two month ends, not trades: say \"held more shares at the end of the month\", "
          "never \"bought on\" a day. A split or bonus changes counts too: check corporate actions." % len(d["same"]))
    return 0


def cmd_investor(a):
    rows = load_investor_rows(a.file)
    prices = load_prices(a.prices)
    p = investor_portfolio(rows, a.name, prices)
    if a.out and p["missing_prices"]:
        raise CopyError("no price for %s in %s: price every holding before writing %s (the weights would leave them "
                        "out)" % (", ".join(p["missing_prices"]), a.prices, a.out))
    if a.out:
        total = sum(h["value_inr"] for h in p["holdings"][:a.top or None])
        weights = [{"ticker": h["symbol"], "name": "", "weight": round(100.0 * h["value_inr"] / total, 2),
                    "pct_of_source": round(h["pct"], 2)} for h in p["holdings"][:a.top or None]]
        doc = {"source": "%s, shareholding patterns for the quarter ended %s (holdings over 1%% only)%s"
               % (" / ".join(p["investor"]), p["period"], (", top %d by value" % a.top) if a.top else ""),
               "as_of": p["period"], "kind": "investor", "weights": weights,
               "caveats": ["Only holdings above 1% of a company are disclosed; smaller stakes are invisible.",
                           "Quarter-end snapshot filed within 21 days of the quarter end."]}
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps(p, indent=2, ensure_ascii=False))
        return 0
    print("%s: disclosed holdings (over 1%% of each company) for the quarter ended %s"
          % (" / ".join(p["investor"]), p["period"]))
    from_cost = indian
    for h in p["holdings"]:
        print("  %-14s %14s shares  %s  %6.2f%% of the portfolio%s"
              % (h["symbol"], from_cost(h["shares"]), "₹" + from_cost(h["value_inr"]), h["pct"],
                 ("  (%.2f%% of the company)" % h["pct_of_company"]) if h.get("pct_of_company") else ""))
    if p["missing_prices"]:
        print("No price for %s: valued holdings leave them out (india-alerts quote them)." % ", ".join(p["missing_prices"]))
    if p["changes"]:
        c = p["changes"]
        print("Since %s: new %s; exited or fell below 1%%: %s; added %s; trimmed %s."
              % (p["previous"], ", ".join(c["new"]) or "none", ", ".join(c["exited_or_below_1pct"]) or "none",
                 ", ".join(c["added"]) or "none", ", ".join(c["trimmed"]) or "none"))
    print("Weights are by today's market value, not what they paid. Holdings under 1% of a company never appear.")
    if a.out:
        print("Wrote %s." % a.out)
    return 0


def cmd_shp(a):
    with open(a.file, encoding="utf-8", errors="replace") as fh:
        res = parse_shp_xbrl(fh.read())
    res["holders"] = [h for h in res["holders"] if (h.get("pct") or 0) >= a.min_pct]
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    print("%s (%s), shareholding pattern for %s:" % (res["company"] or "?", res["symbol"] or res["isin"] or "?",
                                                      res["period"] or "?"))
    for h in res["holders"]:
        print("  %-48s %16s shares  %6s%%  %s" % (h["name"][:48], indian(h["shares"]),
                                                 ("%.2f" % h["pct"]) if h.get("pct") is not None else "?",
                                                 h.get("category", "")))
    print("Parsed from XBRL by context; check the totals against the filing's PDF before you rely on them.")
    return 0


def cmd_symbols(a):
    if a.download:
        req = urllib.request.Request(NSE_EQUITY_LIST, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise CopyError("could not download %s: %s. Download it in a browser and save it as %s"
                            % (NSE_EQUITY_LIST, e, a.equity_list))
        d = os.path.dirname(a.equity_list)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.equity_list, "wb") as fh:
            fh.write(data)
    by_isin, by_name, names = load_symbols(a.symbols_map, a.equity_list)
    if not by_isin:
        raise CopyError("no equity list at %s: run symbols --download" % a.equity_list)
    for q in a.lookup or []:
        hit = by_isin.get(q.upper()) or by_name.get(norm_name(q))
        print("%s -> %s" % (q, hit or "not found"))
    print("%d NSE symbols with ISINs from %s." % (len(by_isin), a.equity_list))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Copy a mutual fund or an investor from Indian disclosures (india-copycat).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--sheet", help="the scheme's sheet name (or words in its title) in an AMC workbook")
        p.add_argument("--indmoney", action="store_true", help="the file is INDmoney get_mf_funds_details output")
        p.add_argument("--symbols-map", default="trader/symbols.json")
        p.add_argument("--equity-list", default="trader/state/nse-equity-list.csv")
        p.add_argument("--guess", action="store_true", help="guess symbols by name (marked *)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("mf", help="read a fund's monthly portfolio")
    common(p)
    p.add_argument("file")
    p.add_argument("--top", type=int)
    p = sub.add_parser("copy", help="write trader/targets.json from a fund portfolio")
    common(p)
    p.add_argument("file")
    p.add_argument("--top", type=int)
    p.add_argument("--exclude", nargs="*", default=[])
    p.add_argument("--min-pct", type=float, default=0.0)
    p.add_argument("--allow-guesses", action="store_true")
    p.add_argument("--skip-unresolved", action="store_true", help="copy even if a big holding has no symbol")
    p.add_argument("--out", default="trader/targets.json")
    p = sub.add_parser("diff", help="what changed between two monthly portfolios")
    common(p)
    p.add_argument("old")
    p.add_argument("new")
    p = sub.add_parser("investor", help="an investor's disclosed holdings from shareholding-pattern rows")
    p.add_argument("file")
    p.add_argument("--name", required=True)
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--top", type=int)
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("shp", help="named holders in a shareholding-pattern XBRL")
    p.add_argument("file")
    p.add_argument("--min-pct", type=float, default=0.0)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("symbols", help="NSE's equity list for ISIN -> symbol")
    p.add_argument("--download", action="store_true")
    p.add_argument("--lookup", nargs="*")
    p.add_argument("--symbols-map", default="trader/symbols.json")
    p.add_argument("--equity-list", default="trader/state/nse-equity-list.csv")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"mf": cmd_mf, "copy": cmd_copy, "diff": cmd_diff, "investor": cmd_investor, "shp": cmd_shp,
                "symbols": cmd_symbols}[a.cmd](a)
    except (CopyError, OSError, ValueError) as e:
        print("copycat.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
