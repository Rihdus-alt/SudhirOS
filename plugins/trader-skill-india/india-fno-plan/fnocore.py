#!/usr/bin/env python3
"""Shared maths for the India F&O skills (india-fno-scan, -plan, -chain, -backtest).
Standard library only, no network.

  candles      load OHLCV from a CSV or the INDmoney connector's get_indian_stocks_ohlc output
  indicators   EMA, SMA, ATR, RSI, ADX (Wilder), Supertrend, Donchian, Bollinger width, swing points
  options      Black-76 (European, on the futures/forward price: NSE index and stock options are
               European), greeks, implied volatility, the forward implied by put-call parity
  odds         the no-edge probability of reaching a target before a stop: for a fair game it is
               risk / (risk + reward), whatever the volatility (optional stopping)
  setups       trend pullback, breakout, Supertrend flip, volatility squeeze; evaluated on closed
               bars only (no look-ahead)
  plan         entry, stop-loss, targets and a trailing stop, sized to a rupee risk in whole lots,
               with every charge counted
  trail        the trailing stop on an open position, bar by bar, that only ever tightens
  backtest     the same setups and exits replayed over history, entering at the NEXT bar's open,
               with the stop assumed hit first when a bar touches both, gaps filled at the open,
               costs included, and win rate shown with its 95% confidence interval
"""

import csv
import datetime
import importlib.util
import json
import math
import os

EPS = 1e-12
INDEX_LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "NIFTYNXT50": 25}
INDEX_LOTS_AS_OF = "NSE circular of 2025-10-03 (effective from the January 2026 expiries)"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
WEEKLY_MONTH = "123456789OND"
IST = datetime.timedelta(hours=5, minutes=30)
EXPIRY_TIME = datetime.time(15, 30)

DEFAULTS = {
    "capital": 500000,
    "risk_per_trade_pct": 1.0,
    "max_lots_per_order": 5,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "sl_min_atr": 0.75,
    "sl_max_atr": 3.0,
    "sl_buffer_atr": 0.2,
    "tp1_r": 1.0,
    "tp1_exit_fraction": 0.5,
    "tp2_r": 2.0,
    "min_reward_r": 1.5,
    "trail_atr_mult": 2.5,
    "trail_after_tp2_atr_mult": 1.5,
    "time_stop_bars": 8,
    "futures_margin_pct": {"index": 12.0, "stock": 20.0},
    "option_target_delta": 0.55,
    "option_min_dte": 3,
    "option_max_spread_pct": 2.0,
    "option_min_oi": 50000,
    "risk_free_rate": 0.065,
    "limit_buffer_pct": 0.3,
    "allow_option_selling": False,
}


class FnoError(Exception):
    pass


def cfg_with(cfg):
    c = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def _sibling(folder, name):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), folder, name + ".py")
    if not os.path.exists(path):
        raise FnoError("%s/%s.py not found next to india-fno-plan: install every india-* folder" % (folder, name))
    spec = importlib.util.spec_from_file_location("india_" + name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


costs = _sibling("india-costs", "costs")
inr = costs.inr


# ---------------------------------------------------------------- candles

def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_candles(src):
    """CSV path, INDmoney OHLC JSON path, or an already-loaded object -> [{date, open, high, low, close, volume}]."""
    obj = src
    if isinstance(src, str):
        if not os.path.exists(src):
            raise FnoError("no candle file %s" % src)
        if src.lower().endswith(".csv"):
            with open(src, encoding="utf-8-sig", newline="") as fh:
                rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in csv.DictReader(fh)]
            return _clean([{"date": r.get("date") or r.get("datetime") or r.get("datetime_ist") or r.get("time"),
                            "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
                            "close": r.get("close"), "volume": r.get("volume")} for r in rows])
        with open(src, encoding="utf-8") as fh:
            obj = json.load(fh)
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        obj = json.loads(obj["result"])
    rows = obj.get("candles", obj) if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        raise FnoError("no candles found")
    late = _unfinished(rows, obj) if isinstance(obj, dict) else set()
    load_candles.dropped = sorted(late)
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("datetime_ist") or r.get("date") or "") not in late]
    return _clean([{"date": r.get("datetime_ist") or r.get("date") or r.get("datetime"), "open": r.get("open"),
                    "high": r.get("high"), "low": r.get("low"), "close": r.get("close"),
                    "volume": r.get("volume")} for r in rows])


load_candles.dropped = []


def _unfinished(rows, obj):
    """INDmoney returns the session still in progress as a candle (before the open it is a copy of the last day).
    A setup must see finished bars only, so a candle whose period had not ended at query_end_ist is left out."""
    q = str(obj.get("query_end_ist") or obj.get("retrieved_at_ist") or "").replace("T", " ")[:16]
    iv = str(obj.get("interval") or "").lower()
    try:
        qt = datetime.datetime.strptime(q, "%Y-%m-%d %H:%M")
    except ValueError:
        return set()
    out = set()
    for r in rows:
        stamp = str((r or {}).get("datetime_ist") or (r or {}).get("date") or "")
        try:
            day = datetime.datetime.strptime(stamp[:10], "%Y-%m-%d")
        except ValueError:
            continue
        close = day.replace(hour=15, minute=30)
        if iv == "1day":
            end = close
        elif iv.endswith("minute"):
            try:
                start = datetime.datetime.strptime(stamp[:16], "%Y-%m-%d %H:%M")
                end = min(start + datetime.timedelta(minutes=int(iv[:-6] or 1)), close)
            except ValueError:
                continue
        elif iv == "1week":
            end = close + datetime.timedelta(days=4 - day.weekday())
        elif iv == "1month":
            nxt = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            end = (nxt - datetime.timedelta(days=1)).replace(hour=15, minute=30)
        else:
            continue
        if end > qt:
            out.add(stamp)
    return out


def _clean(rows):
    out, seen = [], set()
    for r in rows:
        d = str(r["date"] or "").strip()
        if not d:
            continue
        # daily candles from INDmoney are stamped 05:30 IST (midnight UTC): keep the date
        if len(d) >= 16 and d[11:16] in ("05:30", "00:00"):
            d = d[:10]
        elif len(d) > 16:
            d = d[:16]
        o, h, l, c = _num(r["open"]), _num(r["high"]), _num(r["low"]), _num(r["close"])
        if None in (o, h, l, c) or d in seen:
            continue
        if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            raise FnoError("bad candle on %s: high/low do not contain open/close" % d)
        seen.add(d)
        out.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": _num(r.get("volume")) or 0.0})
    out.sort(key=lambda r: r["date"])
    if len(out) < 30:
        raise FnoError("only %d candles: need at least 30 for the indicators" % len(out))
    return out


# ---------------------------------------------------------------- indicators

def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out, k, e = [None] * len(vals), 2.0 / (n + 1), None
    for i, v in enumerate(vals):
        if i < n - 1:
            continue
        e = (sum(vals[:n]) / n) if i == n - 1 else (v - e) * k + e
        out[i] = e
    return out


def true_ranges(c):
    out = []
    for i, b in enumerate(c):
        if i == 0:
            out.append(b["high"] - b["low"])
        else:
            pc = c[i - 1]["close"]
            out.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return out


def wilder(vals, n):
    out, a = [None] * len(vals), None
    for i, v in enumerate(vals):
        if i < n - 1:
            continue
        a = (sum(vals[:n]) / n) if i == n - 1 else (a * (n - 1) + v) / n
        out[i] = a
    return out


def atr(c, n=14):
    return wilder(true_ranges(c), n)


def rsi(closes, n=14):
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = wilder(gains[1:], n), wilder(losses[1:], n)
    out = [None]
    for g, l in zip(ag, al):
        if g is None:
            out.append(None)
        else:
            out.append(100.0 if l < EPS else 100.0 - 100.0 / (1 + g / l))
    return out


def adx(c, n=14):
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(c)):
        up, dn = c[i]["high"] - c[i - 1]["high"], c[i - 1]["low"] - c[i]["low"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = wilder(true_ranges(c), n)
    sp, sm = wilder(pdm, n), wilder(mdm, n)
    dx = []
    for t, p, m in zip(tr, sp, sm):
        if t is None or t < EPS:
            dx.append(None)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > EPS else 0.0)
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    out = [None] * len(c)
    if first is None:
        return out
    vals = dx[first:]
    sm_dx = wilder(vals, n)
    for j, v in enumerate(sm_dx):
        out[first + j] = v
    return out


def supertrend(c, n=10, mult=3.0):
    """-> (line, direction) per bar; direction +1 up, -1 down, None during warm-up."""
    a = atr(c, n)
    line, dirn = [None] * len(c), [None] * len(c)
    fu = fl = None
    for i, b in enumerate(c):
        if a[i] is None:
            continue
        mid = (b["high"] + b["low"]) / 2.0
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        pc = c[i - 1]["close"] if i else b["close"]
        fu = bu if (fu is None or bu < fu or pc > fu) else fu
        fl = bl if (fl is None or bl > fl or pc < fl) else fl
        prev = dirn[i - 1] if i else None
        if prev is None:
            d = 1 if b["close"] > mid else -1
        elif prev == 1:
            d = -1 if b["close"] < fl else 1
        else:
            d = 1 if b["close"] > fu else -1
        dirn[i] = d
        line[i] = fl if d == 1 else fu
    return line, dirn


def bb_width(closes, n=20, k=2.0):
    out = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        out[i] = (2 * k * sd / m) if m else None
    return out


def swing_low(c, i, look=10, wing=2):
    """Lowest confirmed swing low (a low with `wing` higher lows each side) in the last `look` bars up to i."""
    best = None
    for j in range(max(wing, i - look), i - wing + 1):
        l = c[j]["low"]
        if all(c[j - k]["low"] >= l for k in range(1, wing + 1)) and all(c[j + k]["low"] >= l for k in range(1, wing + 1)
                                                                           if j + k <= i):
            best = l if best is None else min(best, l)
    return best


def swing_high(c, i, look=10, wing=2):
    best = None
    for j in range(max(wing, i - look), i - wing + 1):
        h = c[j]["high"]
        if all(c[j - k]["high"] <= h for k in range(1, wing + 1)) and all(c[j + k]["high"] <= h for k in range(1, wing + 1)
                                                                            if j + k <= i):
            best = h if best is None else max(best, h)
    return best


def indicators(c, cfg=None):
    k = cfg_with(cfg)
    closes = [b["close"] for b in c]
    vols = [b["volume"] for b in c]
    st_line, st_dir = supertrend(c)
    return {"close": closes, "ema20": ema(closes, 20), "ema50": ema(closes, 50), "atr": atr(c, k["atr_period"]),
            "rsi": rsi(closes, 14), "adx": adx(c, 14), "st_line": st_line, "st_dir": st_dir,
            "bbw": bb_width(closes), "vol_avg": sma(vols, 20) if any(vols) else [None] * len(c)}


# ---------------------------------------------------------------- options (Black-76)

def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black76(F, K, T, sigma, r=0.0, kind="CE"):
    kind = kind.upper()
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        return df * max(F - K, 0.0) if kind == "CE" else df * max(K - F, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    if kind == "CE":
        return df * (F * ncdf(d1) - K * ncdf(d2))
    return df * (K * ncdf(-d2) - F * ncdf(-d1))


def greeks(F, K, T, sigma, r=0.0, kind="CE"):
    """delta (per 1 point of the forward), gamma, vega per 1 vol point, theta per calendar day."""
    kind = kind.upper()
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        itm = (F > K) if kind == "CE" else (F < K)
        return {"delta": (1.0 if kind == "CE" else -1.0) * (df if itm else 0.0), "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    delta = df * ncdf(d1) if kind == "CE" else -df * ncdf(-d1)
    gamma = df * npdf(d1) / (F * sq)
    vega = df * F * npdf(d1) * math.sqrt(T) / 100.0
    price = black76(F, K, T, sigma, r, kind)
    theta = (-df * F * npdf(d1) * sigma / (2 * math.sqrt(T)) + r * price) / 365.0
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol(price, F, K, T, r=0.0, kind="CE"):
    """Bisection on Black-76. None when the price is below intrinsic or the time is up."""
    if price is None or price <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    intrinsic = black76(F, K, T, 1e-9, r, kind)
    if price < intrinsic - 1e-6 or price > black76(F, K, T, hi, r, kind):
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if black76(F, K, T, mid, r, kind) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2


def parse_now(text=None, chain=None):
    """The option clock, as naive UTC. An ISO time without an offset is IST. With no time given, a chain's own
    price time is used when it is older than the wall clock (its premiums were struck then), else the wall clock."""
    if text:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
        return dt - IST if dt.tzinfo is None else (dt - dt.utcoffset()).replace(tzinfo=None)
    wall = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    t = (chain or {}).get("spot_time")
    if t:
        try:
            ct = parse_now(str(t).replace(" ", "T"))
        except ValueError:
            ct = None
        if ct and ct < wall:
            return ct
    return wall


def years_to_expiry(expiry, now_utc):
    """Calendar years from now to 15:30 IST on the expiry date."""
    if isinstance(expiry, str):
        e = expiry.replace("-", "")[:8]
        expiry = datetime.date(int(e[:4]), int(e[4:6]), int(e[6:8]))
    end = datetime.datetime.combine(expiry, EXPIRY_TIME) - IST
    return max((end - now_utc).total_seconds(), 0.0) / (365.0 * 86400)


def parity_forward(call_mid, put_mid, K, T, r):
    """The forward implied by put-call parity: F = K + (C - P) e^{rT}."""
    return K + (call_mid - put_mid) * math.exp(r * T)


# ---------------------------------------------------------------- odds

def no_edge_probability(entry, stop, target):
    """Probability that a fair (martingale) price reaches `target` before `stop`.

    Optional stopping: E[exit] = entry, so p * target + (1 - p) * stop = entry, which gives
    p = |entry - stop| / |target - stop| = risk / (risk + reward). It is also the break-even win rate
    before costs. A setup has an edge only if its measured win rate beats this."""
    risk, reward = abs(entry - stop), abs(target - entry)
    return risk / (risk + reward) if risk + reward > 0 else None


def wilson(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    p = wins / float(n)
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------- contracts

def is_monthly(expiry, expiry_list):
    e = str(expiry).replace("-", "")[:8]
    same = [x.replace("-", "")[:8] for x in expiry_list if x.replace("-", "")[:6] == e[:6]]
    return bool(same) and e == max(same)


def kite_symbol(underlying, expiry, kind, strike=None, monthly=True):
    """Kite trading symbols: NIFTY26OCTFUT, NIFTY26OCT23000CE (monthly), NIFTY26O2723000CE (weekly)."""
    e = str(expiry).replace("-", "")[:8]
    y, m, d = e[2:4], int(e[4:6]), e[6:8]
    kind = kind.upper()
    if kind == "FUT":
        return "%s%s%sFUT" % (underlying, y, MONTHS[m - 1])
    k = ("%d" % strike) if float(strike) == int(strike) else ("%g" % strike)
    if monthly:
        return "%s%s%s%s%s" % (underlying, y, MONTHS[m - 1], k, kind)
    return "%s%s%s%s%s%s" % (underlying, y, WEEKLY_MONTH[m - 1], d, k, kind)


def lot_size_for(underlying, given=None):
    if given:
        return int(given)
    if underlying.upper() in INDEX_LOTS:
        return INDEX_LOTS[underlying.upper()]
    raise FnoError("no lot size for %s: pass --lot (it is on the option chain, or the exchange's contract list)"
                   % underlying)


def round_tick(x, tick=0.05, up=True):
    steps = x / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * tick, 2)


# ---------------------------------------------------------------- setups

SETUPS = ("trend_pullback", "breakout", "supertrend_flip", "squeeze")


def signals_at(c, ind, i, enabled=SETUPS):
    """Setups that fire on the CLOSE of bar i, using bars <= i only."""
    out = []
    if i < 60 or ind["atr"][i] is None or ind["ema50"][i] is None:
        return out
    b, a, cl = c[i], ind["atr"][i], ind["close"]
    e20, e50, r, x = ind["ema20"][i], ind["ema50"][i], ind["rsi"][i], ind["adx"][i] or 0
    e50_slope = e50 - ind["ema50"][i - 5]
    vol_ok = bool(ind["vol_avg"][i]) and b["volume"] > 1.5 * ind["vol_avg"][i]

    def add(name, d, why, extra=0.0):
        score = 1.0 + extra + (1.0 if x >= 25 else 0.0) + (0.5 if vol_ok else 0.0)
        out.append({"setup": name, "direction": d, "score": round(score, 2), "why": why, "bar": i,
                    "date": b["date"], "close": b["close"], "atr": a, "adx": round(x, 1),
                    "rsi": round(r, 1) if r is not None else None})

    if "trend_pullback" in enabled and r is not None:
        if e20 > e50 and e50_slope > 0 and b["low"] <= e20 + 0.25 * a and b["close"] > e20 and b["close"] > b["open"] \
                and 40 <= r <= 60:
            add("trend_pullback", "long", "uptrend (EMA20 %.1f > EMA50 %.1f, EMA50 rising), pulled back to EMA20 and "
                                          "closed back above it, RSI %.0f" % (e20, e50, r))
        if e20 < e50 and e50_slope < 0 and b["high"] >= e20 - 0.25 * a and b["close"] < e20 and b["close"] < b["open"] \
                and 40 <= r <= 60:
            add("trend_pullback", "short", "downtrend (EMA20 %.1f < EMA50 %.1f, EMA50 falling), rallied to EMA20 and "
                                           "closed back below it, RSI %.0f" % (e20, e50, r))
    if "breakout" in enabled:
        hi20 = max(x_["high"] for x_ in c[i - 20:i])
        lo20 = min(x_["low"] for x_ in c[i - 20:i])
        if b["close"] > hi20 and x >= 20:
            add("breakout", "long", "closed above the 20-day high %.2f with ADX %.0f" % (hi20, x),
                0.5 if vol_ok else 0.0)
        if b["close"] < lo20 and x >= 20:
            add("breakout", "short", "closed below the 20-day low %.2f with ADX %.0f" % (lo20, x),
                0.5 if vol_ok else 0.0)
    if "supertrend_flip" in enabled and ind["st_dir"][i] is not None and ind["st_dir"][i - 1] is not None:
        if ind["st_dir"][i] == 1 and ind["st_dir"][i - 1] == -1 and b["close"] > e50:
            add("supertrend_flip", "long", "Supertrend(10,3) turned up and the close is above EMA50")
        if ind["st_dir"][i] == -1 and ind["st_dir"][i - 1] == 1 and b["close"] < e50:
            add("supertrend_flip", "short", "Supertrend(10,3) turned down and the close is below EMA50")
    if "squeeze" in enabled and ind["bbw"][i - 1] is not None:
        window = [w for w in ind["bbw"][i - 60:i] if w is not None]
        if window and ind["bbw"][i - 1] <= min(window) * 1.05:
            m = sum(cl[i - 19:i + 1]) / 20.0
            sd = math.sqrt(sum((v - m) ** 2 for v in cl[i - 19:i + 1]) / 20.0)
            if b["close"] > m + 2 * sd:
                add("squeeze", "long", "Bollinger width at a 60-bar low, then a close above the upper band")
            elif b["close"] < m - 2 * sd:
                add("squeeze", "short", "Bollinger width at a 60-bar low, then a close below the lower band")
    return out


# ---------------------------------------------------------------- levels and the plan

def levels(c, ind, i, direction, cfg=None, entry=None, resistance=None, support=None):
    """Entry, stop, targets and trailing rules for a trade decided on the close of bar i.

    stop     the structure stop (beyond the last swing low/high, plus a buffer) when it is between
             sl_min_atr and sl_max_atr away; otherwise sl_atr_mult x ATR. Never inside the noise,
             never so far that the size collapses.
    targets  TP1 at tp1_r x risk (book tp1_exit_fraction, stop to break-even), TP2 at tp2_r x risk,
             pulled in to just before a resistance/support level that sits closer; if that leaves
             less than min_reward_r, the trade is flagged as not worth it.
    trail    after TP1, a chandelier stop trail_atr_mult x ATR from the best price since entry;
             after TP2, trail_after_tp2_atr_mult x ATR. A stop only ever tightens."""
    k = cfg_with(cfg)
    a = ind["atr"][i]
    long_ = direction == "long"
    e = float(entry if entry is not None else c[i]["close"])
    swing = swing_low(c, i) if long_ else swing_high(c, i)
    struct = None
    if swing is not None:
        struct = swing - k["sl_buffer_atr"] * a if long_ else swing + k["sl_buffer_atr"] * a
    atr_stop = e - k["sl_atr_mult"] * a if long_ else e + k["sl_atr_mult"] * a
    stop, basis = atr_stop, "%.1f x ATR" % k["sl_atr_mult"]
    if struct is not None:
        dist = (e - struct) if long_ else (struct - e)
        if k["sl_min_atr"] * a <= dist <= k["sl_max_atr"] * a:
            stop, basis = struct, "beyond the swing %s %.2f" % ("low" if long_ else "high", swing)
    risk = abs(e - stop)
    sgn = 1 if long_ else -1
    tp1 = e + sgn * k["tp1_r"] * risk
    tp2 = e + sgn * k["tp2_r"] * risk
    notes = []
    wall = resistance if long_ else support
    if wall is not None and ((long_ and tp1 < wall < tp2) or ((not long_) and tp2 < wall < tp1)):
        tp2 = wall - sgn * 0.1 * a
        notes.append("TP2 pulled in ahead of %s %.2f" % ("resistance" if long_ else "support", wall))
    reward_r = abs(tp2 - e) / risk if risk else 0.0
    ok = reward_r >= k["min_reward_r"] - 1e-9
    if not ok:
        notes.append("reward to TP2 is only %.2fR (minimum %.1fR): skip, or wait for a better entry"
                     % (reward_r, k["min_reward_r"]))
    if wall is not None and ((long_ and e < wall <= tp1) or ((not long_) and tp1 <= wall < e)):
        ok = False
        notes.append("%s %.2f sits before TP1: the trade has no room" % ("resistance" if long_ else "support", wall))
    return {"direction": direction, "entry": round(e, 2), "stop": round(stop, 2), "stop_basis": basis,
            "risk_points": round(risk, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2), "reward_r": round(reward_r, 2),
            "atr": round(a, 2), "trail": {"after_tp1_atr": k["trail_atr_mult"], "after_tp2_atr": k["trail_after_tp2_atr_mult"],
                                          "tp1_exit_fraction": k["tp1_exit_fraction"], "time_stop_bars": k["time_stop_bars"]},
            "no_edge_p_tp1": round(no_edge_probability(e, stop, tp1), 3),
            "no_edge_p_tp2": round(no_edge_probability(e, stop, tp2), 3), "ok": ok, "notes": notes}


def futures_sizing(lv, lot, price, cfg=None, index=True, exchange="NFO"):
    k = cfg_with(cfg)
    rates = costs.rates_from(k)
    notional = price * lot
    rt = costs.fno_costs("buy", notional, "FUT", exchange, rates)["total"] + \
        costs.fno_costs("sell", notional, "FUT", exchange, rates)["total"]
    per_lot_risk = lv["risk_points"] * lot + rt
    budget = k["capital"] * k["risk_per_trade_pct"] / 100.0
    lots = int(budget // per_lot_risk) if per_lot_risk > 0 else 0
    lots = min(lots, int(k["max_lots_per_order"]))
    m_pct = k["futures_margin_pct"]["index" if index else "stock"]
    return {"lot_size": lot, "lots": lots, "quantity": lots * lot, "risk_budget": round(budget, 2),
            "per_lot_risk": round(per_lot_risk, 2), "max_loss": round(lots * per_lot_risk, 2),
            "roundtrip_charges_per_lot": round(rt, 2), "notional": round(notional * lots, 2),
            "est_margin": round(notional * lots * m_pct / 100.0, 2), "margin_pct_used": m_pct,
            "reward_tp1": round(lots * (lv["risk_points"] * k["tp1_r"] * lot - rt), 2),
            "why_zero": None if lots else ("one lot risks %s (stop %s points x %d + charges), more than your %s budget"
                                           % (inr(per_lot_risk), lv["risk_points"], lot, inr(budget)))}


def option_plan(lv, chain, cfg=None, now_utc=None, lot=None, underlying="NIFTY"):
    """Pick a liquid strike near the target delta and translate the underlying's stop and targets into
    premium levels by repricing with Black-76 at the expected exit time (theta included)."""
    k = cfg_with(cfg)
    long_ = lv["direction"] == "long"
    kind = "CE" if long_ else "PE"
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    exp = chain["expiry"]
    T = years_to_expiry(exp, now_utc)
    dte = T * 365.0
    note = []
    if dte < k["option_min_dte"]:
        note.append("only %.1f days to expiry: theta and gamma are extreme; use the next expiry" % dte)
    r = k["risk_free_rate"]
    F = chain.get("forward") or chain["spot"]
    cands = []
    for row in chain["strikes"]:
        leg = row.get(kind.lower())
        if not leg or not leg.get("ltp"):
            continue
        bid, ask = leg.get("bid"), leg.get("ask")
        mid = (bid + ask) / 2.0 if bid and ask else leg["ltp"]
        spread_pct = 100.0 * (ask - bid) / mid if bid and ask and mid else None
        iv = leg.get("iv") or implied_vol(mid, F, row["strike"], T, r, kind)
        if not iv:
            continue
        g = greeks(F, row["strike"], T, iv, r, kind)
        liquid = (spread_pct is not None and spread_pct <= k["option_max_spread_pct"]) and (leg.get("oi") or 0) >= k["option_min_oi"]
        cands.append({"strike": row["strike"], "kind": kind, "ltp": leg["ltp"], "bid": bid, "ask": ask, "mid": mid,
                      "iv": iv, "delta": g["delta"], "theta": g["theta"], "spread_pct": spread_pct,
                      "oi": leg.get("oi"), "liquid": liquid, "id": leg.get("id")})
    liquid = [x for x in cands if x["liquid"]]
    if not liquid:
        raise FnoError("no liquid %s strike in the chain (spread under %.1f%% and OI over %d)"
                       % (kind, k["option_max_spread_pct"], k["option_min_oi"]))
    pick = min(liquid, key=lambda x: (abs(abs(x["delta"]) - k["option_target_delta"]), x["spread_pct"]))
    lot = lot_size_for(underlying, lot or chain.get("lot_size"))
    a = lv["atr"]
    # expected time to reach each level, in days, from its distance in ATRs (at least half a day)
    t_sl = max(0.5, abs(lv["entry"] - lv["stop"]) / a)
    t_tp1 = max(0.5, abs(lv["tp1"] - lv["entry"]) / a)
    t_tp2 = max(1.0, abs(lv["tp2"] - lv["entry"]) / a)
    basis = F - chain["spot"]

    def premium_at(level, days):
        t = max(T - days / 365.0, 1.0 / (365 * 24))
        return black76(level + basis, pick["strike"], t, pick["iv"], r, kind)

    entry_p = pick["ask"] or pick["ltp"]
    sl_p = premium_at(lv["stop"], t_sl)
    tp1_p = premium_at(lv["tp1"], t_tp1)
    tp2_p = premium_at(lv["tp2"], t_tp2)
    # a stop is a trigger on the premium; trail it with the same rules on the underlying
    risk_per_unit = entry_p - sl_p
    rates = costs.rates_from(k)
    rt = costs.fno_costs("buy", entry_p * lot, "OPT", "NFO", rates)["total"] + \
        costs.fno_costs("sell", sl_p * lot, "OPT", "NFO", rates)["total"]
    per_lot_risk = risk_per_unit * lot + rt
    budget = k["capital"] * k["risk_per_trade_pct"] / 100.0
    lots = int(budget // per_lot_risk) if per_lot_risk > 0 else 0
    lots = min(lots, int(k["max_lots_per_order"]))
    # the premium at the stop and TP1 is priced at the expected time to reach them; when that is at or past
    # expiry the premium is just what is left at settlement (often the 0.05 floor): no stop order can protect it
    too_late = None
    if max(t_sl, t_tp1) >= dte:
        too_late = ("the stop is about %.1f days away and TP1 %.1f, but the contract has %.1f days left: its premium "
                    "levels cannot be priced before expiry; use the next expiry" % (t_sl, t_tp1, dte))
        note.append(too_late)
        lots = 0
    hold_theta = pick["theta"] * t_tp2 * lot
    if pick["theta"] * t_tp2 < -0.25 * (tp2_p - entry_p):
        note.append("time decay over the expected holding (%.0f days) eats over a quarter of the target gain" % t_tp2)
    return {"expiry": exp, "dte": round(dte, 2), "kind": kind, "strike": pick["strike"], "delta": round(pick["delta"], 3),
            "iv_pct": round(100 * pick["iv"], 2), "spread_pct": round(pick["spread_pct"], 2), "oi": pick["oi"],
            "forward": round(F, 2), "entry_premium": round(entry_p, 2), "sl_premium": round(max(sl_p, 0.05), 2),
            "tp1_premium": round(tp1_p, 2), "tp2_premium": round(tp2_p, 2),
            "sl_premium_delta_approx": round(max(entry_p + pick["delta"] * (lv["stop"] - lv["entry"]), 0.05), 2),
            "expected_days": {"sl": round(t_sl, 1), "tp1": round(t_tp1, 1), "tp2": round(t_tp2, 1)},
            "theta_per_day": round(pick["theta"], 2), "theta_cost_to_tp2_inr": round(hold_theta * max(lots, 1), 2),
            "lot_size": lot, "lots": lots, "quantity": lots * lot, "per_lot_risk": round(per_lot_risk, 2),
            "max_loss_at_stop": round(lots * per_lot_risk, 2), "premium_outlay": round(lots * lot * entry_p, 2),
            "worst_case_loss": round(lots * lot * entry_p + rt * lots, 2), "risk_budget": round(budget, 2),
            "why_zero": None if lots else too_late or ("one lot risks %s at the stop (premium %s -> %s x %d + charges), more than "
                                           "your %s budget" % (inr(per_lot_risk), entry_p, round(max(sl_p, 0.05), 2), lot,
                                                               inr(budget))),
            "notes": note, "derivative_id": pick.get("id")}


# ---------------------------------------------------------------- trailing an open position

def trail(position, bars, atr_now, cfg=None):
    """Advance an open position through new bars. Returns the updated position and events.

    position: {direction, entry, stop, tp1, tp2, best, stage ("open"|"tp1"|"tp2"), bars_held}
    Exits are checked against each bar's range before the trail moves (the stop first when a bar
    touches both, the conservative assumption). The trail is computed on the bar's close and applies
    from the next bar."""
    k = cfg_with(cfg)
    p = dict(position)
    long_ = p["direction"] == "long"
    sgn = 1 if long_ else -1
    events = []
    for b in bars:
        if p.get("closed"):
            break
        p["bars_held"] = p.get("bars_held", 0) + 1
        hit_stop = (b["low"] <= p["stop"]) if long_ else (b["high"] >= p["stop"])
        gap = (b["open"] <= p["stop"]) if long_ else (b["open"] >= p["stop"])
        if hit_stop:
            px = b["open"] if gap else p["stop"]
            events.append({"date": b["date"], "event": "stop" if p["stage"] == "open" else "trailing stop",
                           "price": round(px, 2)})
            p.update(closed=True, exit=round(px, 2), exit_date=b["date"])
            break
        if p["stage"] == "open" and ((b["high"] >= p["tp1"]) if long_ else (b["low"] <= p["tp1"])):
            # a session that opens past TP1 fills the resting TP1 limit at that better open
            fill = b["open"] if ((b["open"] >= p["tp1"]) if long_ else (b["open"] <= p["tp1"])) else p["tp1"]
            events.append({"date": b["date"], "event": "TP1: book %d%%, stop to break-even"
                           % round(100 * k["tp1_exit_fraction"]), "price": round(fill, 2)})
            p["stage"] = "tp1"
            p["tp1_fill"] = round(fill, 2)
            p["stop"] = p["entry"] if (long_ and p["entry"] > p["stop"]) or (not long_ and p["entry"] < p["stop"]) else p["stop"]
        if p["stage"] == "tp1" and ((b["high"] >= p["tp2"]) if long_ else (b["low"] <= p["tp2"])):
            events.append({"date": b["date"], "event": "TP2: trail the rest tighter", "price": p["tp2"]})
            p["stage"] = "tp2"
        p["best"] = max(p.get("best", p["entry"]), b["high"]) if long_ else min(p.get("best", p["entry"]), b["low"])
        if p["stage"] in ("tp1", "tp2"):
            m = k["trail_atr_mult"] if p["stage"] == "tp1" else k["trail_after_tp2_atr_mult"]
            new = round(p["best"] - sgn * m * atr_now, 2)
            if (long_ and new > p["stop"] + 0.004) or ((not long_) and new < p["stop"] - 0.004):
                events.append({"date": b["date"], "event": "trail stop to %.2f" % new, "price": round(new, 2)})
                p["stop"] = round(new, 2)
        elif p["bars_held"] >= k["time_stop_bars"]:
            events.append({"date": b["date"], "event": "time stop: no TP1 in %d bars, exit at the close" % p["bars_held"],
                           "price": b["close"]})
            p.update(closed=True, exit=b["close"], exit_date=b["date"])
            break
    return p, events


# ---------------------------------------------------------------- backtest

MIN_CONTRACT_INR = 1500000.0   # SEBI's minimum value of an equity derivative contract (from November 2024)
PACK_FILES = ("plans.json", "journal.json", "scan.json")


def roundtrip_points(px, lot=None, cfg=None):
    """Round-trip futures charges per unit, in price points, and the lot they were worked out on. Without a lot
    size a contract of SEBI's minimum value stands in: charges are nearly all a percent of value, so the
    estimate is close; a backtest without any charges would flatter every setup."""
    use = int(lot) if lot else max(1, int(round(MIN_CONTRACT_INR / px)))
    rates = costs.rates_from(cfg_with(cfg))
    rt = costs.fno_costs("buy", px * use, "FUT", "NFO", rates)["total"] + \
        costs.fno_costs("sell", px * use, "FUT", "NFO", rates)["total"]
    return rt / use, use, not lot


def not_candles(path):
    """The pack's own JSON files that sit next to the candles in trader/fno/ (globs pick them up)."""
    base = os.path.basename(path).lower()
    return base in PACK_FILES or base.startswith("scan") or base.endswith("-chain.json")


def backtest(c, cfg=None, enabled=SETUPS, cost_points=0.0, start=60, end=None):
    """Replay the setups bar by bar. Each signal on bar i enters at bar i+1's open; one position at a time.

    R is measured on the planned risk. cost_points: round-trip charges per unit, in price points.
    A partial exit at TP1 (tp1_exit_fraction) and the trailing rest are combined into one R result."""
    k = cfg_with(cfg)
    ind = indicators(c, k)
    trades, skipped, i = [], [], max(start, 60)
    end = len(c) - 1 if end is None else min(end, len(c) - 1)
    while i < end:
        sigs = signals_at(c, ind, i, enabled)
        if not sigs:
            i += 1
            continue
        s = max(sigs, key=lambda x: x["score"])
        # the plan is made on bar i's close (levels are fixed prices); the order fills at bar i+1's open
        lv = levels(c, ind, i, s["direction"], k)
        long_ = s["direction"] == "long"
        entry = c[i + 1]["open"]
        gapped = (entry <= lv["stop"] or entry >= lv["tp1"]) if long_ else (entry >= lv["stop"] or entry <= lv["tp1"])
        if not lv["ok"] or gapped:
            skipped.append({"date": c[i]["date"], "setup": s["setup"], "direction": s["direction"],
                            "why": "opened beyond the stop or TP1" if gapped else "; ".join(lv["notes"])})
            i += 1
            continue
        pos = {"direction": s["direction"], "entry": entry, "stop": lv["stop"], "tp1": lv["tp1"], "tp2": lv["tp2"],
               "best": entry, "stage": "open", "bars_held": 0}
        # R is the planned risk (bar i's close to the stop): the size was set on it, so a fill that lands
        # nearer the stop or nearer TP1 shows up as a smaller or larger result, not as a different R
        risk = lv["risk_points"]
        sgn = 1 if long_ else -1
        booked, frac_left, j = 0.0, 1.0, i + 1
        events = []
        while j <= end and not pos.get("closed"):
            before = pos["stage"]
            pos, ev = trail(pos, [c[j]], ind["atr"][j - 1] or lv["atr"], k)
            events += ev
            if before == "open" and pos["stage"] in ("tp1", "tp2") and not pos.get("closed"):
                booked += k["tp1_exit_fraction"] * sgn * (pos.get("tp1_fill", pos["tp1"]) - entry) / risk
                frac_left -= k["tp1_exit_fraction"]
            j += 1
        if not pos.get("closed"):
            pos.update(closed=True, exit=c[end]["close"], exit_date=c[end]["date"], open_at_end=True)
        r_rest = sgn * (pos["exit"] - entry) / risk
        r_total = booked + frac_left * r_rest - cost_points / risk
        trades.append({"setup": s["setup"], "direction": s["direction"], "signal_date": c[i]["date"],
                       "entry_date": c[i + 1]["date"], "entry": round(entry, 2), "stop": lv["stop"], "tp1": lv["tp1"],
                       "tp2": lv["tp2"], "tp1_fill": pos.get("tp1_fill"), "exit_date": pos["exit_date"],
                       "exit": pos["exit"], "r": round(r_total, 3),
                       "bars": pos.get("bars_held", 0), "open_at_end": bool(pos.get("open_at_end")),
                       "events": [e["event"] for e in events]})
        exit_idx = next((n for n in range(i + 1, len(c)) if c[n]["date"] == pos["exit_date"]), end)
        i = max(exit_idx, i + 1)
    backtest.skipped = skipped
    return trades


def stats(trades):
    """Closed trades only: a trade still open at the end of the data is not a result. The drawdown is taken
    in exit-date order, so pooling several underlyings does not depend on the order of the files."""
    trades = sorted((t for t in trades if not t.get("open_at_end")),
                    key=lambda t: (t.get("exit_date") or "", t.get("entry_date") or ""))
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    lo, hi = wilson(len(wins), n)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = -sum(losses) / len(losses) if losses else 0.0
    return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n, 3),
            "win_rate_95ci": [round(lo, 3), round(hi, 3)], "avg_win_r": round(aw, 3), "avg_loss_r": round(al, 3),
            "breakeven_win_rate": round(al / (aw + al), 3) if aw + al > 0 else None,
            "expectancy_r": round(mean, 3), "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "total_r": round(sum(rs), 2), "max_drawdown_r": round(dd, 2),
            "avg_bars": round(sum(t["bars"] for t in trades) / n, 1)}


# ---------------------------------------------------------------- option chains

def _leg_indmoney(d):
    if not d:
        return None
    bid = _num(d.get("buy_price", d.get("bp")))
    ask = _num(d.get("sell_price", d.get("sp")))
    return {"id": d.get("derivative_id"), "ltp": _num(d.get("current_price")), "bid": bid or None, "ask": ask or None,
            "oi": _num(d.get("oi")) or 0.0, "oi_chg_pct": _num(d.get("oi_change")),
            "volume": _num(d.get("volume", d.get("v"))) or 0.0, "iv": (_num(d.get("iv")) or 0) / 100.0 or None,
            "lot_size": d.get("lot_size")}


def _leg_nse(d):
    if not d:
        return None
    oi = _num(d.get("openInterest")) or 0.0
    chg = _num(d.get("changeinOpenInterest")) or 0.0
    prev = oi - chg
    return {"id": d.get("identifier"), "ltp": _num(d.get("lastPrice")), "bid": _num(d.get("bidprice")) or None,
            "ask": _num(d.get("askPrice")) or None, "oi": oi, "oi_chg_pct": (100.0 * chg / prev) if prev > 0 else None,
            "volume": _num(d.get("totalTradedVolume")) or 0.0, "iv": (_num(d.get("impliedVolatility")) or 0) / 100.0 or None}


def parse_chain(src, underlying=None, expiry=None):
    """INDmoney get_indian_stocks_option_chain output, or NSE's option-chain JSON -> a normalized chain.

    Note: INDmoney's oi_change is a percent; NSE's changeinOpenInterest is contracts (converted to a percent)."""
    obj = src
    if isinstance(src, str):
        with open(src, encoding="utf-8") as fh:
            obj = json.load(fh)
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        obj = json.loads(obj["result"])
    if "option_chain_data" in obj:
        ent = obj.get("entity_details") or {}
        rows = []
        lot = None
        for r in obj["option_chain_data"]:
            ce, pe = _leg_indmoney(r.get("call_data")), _leg_indmoney(r.get("put_data"))
            lot = lot or (ce or {}).get("lot_size") or (pe or {}).get("lot_size")
            rows.append({"strike": float(r["strike_price"]), "ce": ce, "pe": pe})
        name = (underlying or ent.get("name") or "").upper().replace(" 50", "").replace("NIFTY BANK", "BANKNIFTY")
        name = {"BANK NIFTY": "BANKNIFTY"}.get(name, name)
        return {"source": "indmoney", "underlying": name.replace(" ", ""), "spot": _num(ent.get("live_price")),
                "spot_time": ent.get("price_updated_at_ist"), "expiry": obj.get("selected_expiry"),
                "expiry_list": obj.get("expiry_list") or [], "lot_size": int(lot) if lot else None,
                "provider_pcr": _num(ent.get("overall_pcr_string")),
                "strikes": sorted(rows, key=lambda r: r["strike"]), "retrieved": obj.get("retrieved_at_ist")}
    rec = obj.get("records") or obj.get("filtered") or {}
    data = rec.get("data") or obj.get("data")
    if data is None:
        raise FnoError("not an INDmoney or NSE option chain")
    exps = rec.get("expiryDates") or []
    want = expiry or (exps[0] if exps else None)
    rows = []
    for r in data:
        if want and r.get("expiryDate") and r["expiryDate"] != want:
            continue
        rows.append({"strike": float(r["strikePrice"]), "ce": _leg_nse(r.get("CE")), "pe": _leg_nse(r.get("PE"))})
    exp = None
    if want:
        exp = datetime.datetime.strptime(want, "%d-%b-%Y").strftime("%Y%m%d")
    return {"source": "nse", "underlying": (underlying or "").upper(), "spot": _num(rec.get("underlyingValue")),
            "spot_time": rec.get("timestamp"), "expiry": exp,
            "expiry_list": [datetime.datetime.strptime(e, "%d-%b-%Y").strftime("%Y%m%d") for e in exps],
            "lot_size": None, "strikes": sorted(rows, key=lambda r: r["strike"])}


def _mid(leg):
    if not leg:
        return None
    if leg.get("bid") and leg.get("ask"):
        return (leg["bid"] + leg["ask"]) / 2.0
    return leg.get("ltp")


def chain_analytics(chain, now_utc=None, r=0.065):
    """Forward, IV per strike, ATM IV, expected move, PCR, max pain, OI walls and OI build-up."""
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    T = years_to_expiry(chain["expiry"], now_utc)
    rows = chain["strikes"]
    S = chain["spot"]
    if not rows or not S:
        raise FnoError("the chain has no strikes or no underlying price")
    both = [x for x in rows if _mid(x["ce"]) and _mid(x["pe"])]
    atm = min(both or rows, key=lambda x: abs(x["strike"] - S))
    F = parity_forward(_mid(atm["ce"]), _mid(atm["pe"]), atm["strike"], T, r) if both and T > 0 else S
    chain["forward"] = F
    out_rows = []
    for x in rows:
        row = {"strike": x["strike"]}
        for kind, leg in (("CE", x["ce"]), ("PE", x["pe"])):
            if not leg:
                continue
            m = _mid(leg)
            iv = leg.get("iv") or (implied_vol(m, F, x["strike"], T, r, kind) if m else None)
            leg["iv"] = iv
            g = greeks(F, x["strike"], T, iv, r, kind) if iv else {}
            row[kind] = {"ltp": leg.get("ltp"), "mid": round(m, 2) if m else None, "oi": leg.get("oi"),
                         "oi_chg_pct": leg.get("oi_chg_pct"), "iv_pct": round(100 * iv, 2) if iv else None,
                         "delta": round(g["delta"], 3) if g else None,
                         "spread_pct": round(100 * (leg["ask"] - leg["bid"]) / m, 2) if leg.get("bid") and leg.get("ask") and m else None}
        out_rows.append(row)
    call_oi = sum((x["ce"] or {}).get("oi", 0) for x in rows)
    put_oi = sum((x["pe"] or {}).get("oi", 0) for x in rows)
    call_vol = sum((x["ce"] or {}).get("volume", 0) for x in rows)
    put_vol = sum((x["pe"] or {}).get("volume", 0) for x in rows)
    strikes = [x["strike"] for x in rows]

    def pain(settle):
        tot = 0.0
        for x in rows:
            tot += (x["ce"] or {}).get("oi", 0) * max(settle - x["strike"], 0)
            tot += (x["pe"] or {}).get("oi", 0) * max(x["strike"] - settle, 0)
        return tot
    max_pain = min(strikes, key=pain)
    call_wall = max(rows, key=lambda x: (x["ce"] or {}).get("oi", 0))
    put_wall = max(rows, key=lambda x: (x["pe"] or {}).get("oi", 0))
    above = [x for x in rows if x["strike"] > S]
    below = [x for x in rows if x["strike"] < S]
    res = max(above, key=lambda x: (x["ce"] or {}).get("oi", 0))["strike"] if above else None
    sup = max(below, key=lambda x: (x["pe"] or {}).get("oi", 0))["strike"] if below else None
    atm_iv = None
    if atm["ce"] and atm["pe"] and atm["ce"].get("iv") and atm["pe"].get("iv"):
        atm_iv = (atm["ce"]["iv"] + atm["pe"]["iv"]) / 2.0
    straddle = (_mid(atm["ce"]) or 0) + (_mid(atm["pe"]) or 0)

    def added(leg):
        if not leg or leg.get("oi_chg_pct") in (None, -100):
            return 0.0
        return leg["oi"] - leg["oi"] / (1 + leg["oi_chg_pct"] / 100.0)
    adds_c = sorted(rows, key=lambda x: -added(x["ce"]))[:3]
    adds_p = sorted(rows, key=lambda x: -added(x["pe"]))[:3]
    return {"underlying": chain.get("underlying"), "expiry": chain["expiry"], "days_to_expiry": round(T * 365, 2),
            "spot": S, "forward": round(F, 2), "atm_strike": atm["strike"],
            "atm_iv_pct": round(100 * atm_iv, 2) if atm_iv else None,
            "expected_move_1sd": round(F * atm_iv * math.sqrt(T), 1) if atm_iv else None,
            "atm_straddle": round(straddle, 2),
            "pcr_oi": round(put_oi / call_oi, 3) if call_oi else None,
            "pcr_volume": round(put_vol / call_vol, 3) if call_vol else None, "provider_pcr": chain.get("provider_pcr"),
            "max_pain": max_pain, "call_wall": call_wall["strike"], "put_wall": put_wall["strike"],
            "resistance": res, "support": sup,
            "top_call_oi_added": [(x["strike"], round(added(x["ce"]))) for x in adds_c],
            "top_put_oi_added": [(x["strike"], round(added(x["pe"]))) for x in adds_p],
            "strikes_shown": len(rows), "rows": out_rows}


def futures_buildup(price_chg_pct, oi_chg_pct):
    """Price and open-interest change -> the standard reading."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    if price_chg_pct >= 0 and oi_chg_pct >= 0:
        return "long build-up"
    if price_chg_pct < 0 and oi_chg_pct >= 0:
        return "short build-up"
    if price_chg_pct >= 0 and oi_chg_pct < 0:
        return "short covering"
    return "long unwinding"


# ---------------------------------------------------------------- F&O journal (paper or real fills)

def journal_apply(journal, ts, side, qty, price, date, lot_size=None, segment=None, rates=None, exchange="NFO",
                  charges=None):
    """Add a fill to the F&O journal; realize P&L on the part that reduces a position (average-price method).

    Cash effect: futures move cash only by realized P&L and charges (the margin is blocked, not spent);
    a bought option costs its premium, a sold option brings it in."""
    side = side.upper()
    segment = (segment or ("FUT" if ts.endswith("FUT") else "OPT")).upper()
    qty, price = int(qty), float(price)
    sgn = 1 if side == "BUY" else -1
    pos = journal.setdefault("positions", {}).get(ts) or {"qty": 0, "avg": 0.0, "segment": segment, "lot_size": lot_size}
    if charges is None:
        charges = costs.fno_costs(side.lower(), qty * price, segment, exchange, rates)["total"]
    realized = 0.0
    q_old = pos["qty"]
    if q_old == 0 or (q_old > 0) == (sgn > 0):
        new_q = q_old + sgn * qty
        pos["avg"] = (abs(q_old) * pos["avg"] + qty * price) / abs(new_q)
        pos["qty"] = new_q
    else:
        close_q = min(qty, abs(q_old))
        realized = close_q * (price - pos["avg"]) * (1 if q_old > 0 else -1)
        rest = qty - close_q
        pos["qty"] = q_old + sgn * close_q
        if rest:
            pos["qty"] = sgn * rest
            pos["avg"] = price
        elif pos["qty"] == 0:
            pos["avg"] = 0.0
    cash = -charges
    if segment == "OPT":
        cash += -sgn * qty * price
    else:
        cash += realized
    journal["cash_change"] = round(journal.get("cash_change", 0.0) + cash, 2)
    journal.setdefault("fills", []).append({"date": date, "tradingsymbol": ts, "side": side, "quantity": qty,
                                            "price": price, "charges": round(charges, 2), "realized": round(realized, 2)})
    if realized or (q_old and pos["qty"] == 0):
        journal.setdefault("realized", []).append({"date": date, "tradingsymbol": ts, "pnl": round(realized, 2),
                                                   "charges": round(charges, 2)})
    if pos["qty"] == 0:
        journal["positions"].pop(ts, None)
    else:
        journal["positions"][ts] = pos
    return journal, realized, cash


def journal_summary(journal, fy_start=None):
    """Realized P&L, charges and the turnover used for tax-audit thresholds (sum of absolute P&L per
    square-off, per the ICAI Guidance Note on Tax Audit), for one financial year."""
    rows = journal.get("realized", [])
    if fy_start:
        end = "%d-04-01" % (int(fy_start[:4]) + 1)
        rows = [r for r in rows if fy_start <= r["date"] < end]
    fills = journal.get("fills", [])
    if fy_start:
        fills = [x for x in fills if fy_start <= x["date"] < end]
    gross = sum(r["pnl"] for r in rows)
    chg = sum(x["charges"] for x in fills)
    return {"square_offs": len(rows), "gross_pnl": round(gross, 2), "charges": round(chg, 2),
            "net_pnl": round(gross - chg, 2), "turnover": round(sum(abs(r["pnl"]) for r in rows), 2),
            "open_positions": journal.get("positions", {})}
