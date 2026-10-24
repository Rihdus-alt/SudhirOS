#!/usr/bin/env python3
"""The Time Machine (india-fno-timemachine): every way a trade can end, and how often. Standard library only.

    python3 timemachine.py --candles trader/fno/NIFTY-1d.json --chain trader/fno/NIFTY-chain.json
    python3 timemachine.py --candles NIFTY-1d.csv --direction short --instrument both --paths 20000
    python3 timemachine.py --candles RELIANCE-1d.csv --lot 500 --direction long --sl 1180 --tp1 1240 --tp2 1280
    python3 timemachine.py --candles HDFCBANK-1d.csv --instrument equity --qty 35 --direction long
    python3 timemachine.py ... --grid          # how the result moves with the stop and the target
    python3 timemachine.py ... --json

It reads the chart (trend, momentum, volatility regime, structure, and the option chain when given), fixes a
trade on the last closed bar (the setup that fired there, or the direction and levels you give), and replays
that trade with the pack's exact exit rules (stop first when a bar touches both, TP1 books part and moves the
stop to entry, a chandelier trail that only tightens, the time stop, gaps filled at the open) three
independent ways:

  replay     every bar of this chart's own history where the same setup fired the same way (and, as the
             baseline, every bar at all), entered at the next open, each occurrence its own scenario
  bootstrap  N futures strung together from this chart's real days in blocks (the open gap, high, low and
             close of each day in units of the ATR before it), rescaled by the path's own ATR: fat tails,
             gaps and volatility clustering come from the data, not from a formula. The history's drift is
             removed (a neutral view) unless --keep-drift
  implied    N paths at the volatility the option chain prices (ATM IV, or --iv), risk-neutral, with the
             overnight share of variance measured from this chart and each day's high and low drawn from
             the Brownian bridge between its open and close

Every path ends in exactly one outcome: no entry (the next open is already past the stop or TP1), stop-loss
before TP1 (and how often the open gapped through it), time stop, TP1 then break-even, TP1 then trailed out,
TP2 then trailed out, or still open when the horizon (or the contract's expiry) ends the replay. For each the
tool gives the probability with its 95% interval, the average result in R and in rupees per lot, and the time
it takes; overall the expectancy after every charge, the chance of a profit, percentiles, the worst
adverse excursion, and the same for the option leg repriced with Black-76 along every path (time decay and the
basis included, expiry settled at intrinsic value).

No method sees the future: the replay uses bars up to each signal only, and the simulations start from the
last closed bar. The models are stated so you can judge them; where the three disagree, that is information.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import random
import sys


def _core():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-fno-plan", "fnocore.py")
    if not os.path.exists(path):
        sys.exit("timemachine.py: india-fno-plan/fnocore.py not found: install every india-* folder")
    spec = importlib.util.spec_from_file_location("fnocore", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f = _core()

OUTCOMES = (
    ("gap", "no entry: opens past SL or TP1"),
    ("stop", "stop-loss before TP1"),
    ("time", "time stop: no TP1 in time"),
    ("tp1_be", "TP1, then stopped at entry"),
    ("tp1_trail", "TP1, then trailed out"),
    ("tp2_trail", "TP2, then trailed out"),
    ("horizon", "still open at the horizon"),
    ("expiry", "held to expiry: settled"),
)
LABEL = dict(OUTCOMES)
TRADING_DAYS = 252.0


class TMError(Exception):
    pass


# ---------------------------------------------------------------- the exit engine

def run_trade(bars, t, k, horizon=None):
    """One path through the exit rules. bars: [(open, high, low, close, atr_before), ...] starting with the
    session after the signal. t: direction, stop, tp1, tp2, risk (planned), frac (booked at TP1).

    Mirrors fnocore.trail exactly (the tests hold it to that): a bar is checked for the stop before anything
    else, a gap through the stop fills at the open, TP1 moves the stop to entry, the chandelier trail is set on
    the bar's close with the ATR known before it and only tightens, and the time stop applies before TP1."""
    long_ = t["direction"] == "long"
    sgn = 1 if long_ else -1
    stop, tp1, tp2 = t["stop"], t["tp1"], t["tp2"]
    o0 = bars[0][0]
    if (o0 <= stop or o0 >= tp1) if long_ else (o0 >= stop or o0 <= tp1):
        return {"cls": "gap", "entered": False}
    entry = o0
    best = entry
    stage = "open"
    held = 0
    lo, hi = entry, entry
    tp1_bar = None
    tp2_bar = None
    exit_px, exit_bar, cls, gapped = None, None, None, False
    end = len(bars) if horizon is None else min(horizon, len(bars))
    m1, m2, tstop = k["trail_atr_mult"], k["trail_after_tp2_atr_mult"], k["time_stop_bars"]
    for n in range(end):
        o, h, l, c, a = bars[n]
        held += 1
        lo, hi = min(lo, l), max(hi, h)
        if (l <= stop) if long_ else (h >= stop):
            gapped = (o <= stop) if long_ else (o >= stop)
            exit_px, exit_bar = (o if gapped else stop), n
            if stage == "open":
                cls = "stop"
            elif stage == "tp1":
                cls = "tp1_be" if abs(stop - entry) < 0.005 else "tp1_trail"
            else:
                cls = "tp2_trail"
            break
        if stage == "open" and ((h >= tp1) if long_ else (l <= tp1)):
            stage, tp1_bar = "tp1", n
            if (long_ and entry > stop) or ((not long_) and entry < stop):
                stop = entry
        if stage == "tp1" and ((h >= tp2) if long_ else (l <= tp2)):
            stage, tp2_bar = "tp2", n
        best = max(best, h) if long_ else min(best, l)
        if stage in ("tp1", "tp2"):
            new = round(best - sgn * (m1 if stage == "tp1" else m2) * a, 2)
            if (long_ and new > stop + 0.004) or ((not long_) and new < stop - 0.004):
                stop = new
        elif held >= tstop:
            exit_px, exit_bar, cls = c, n, "time"
            break
    if cls is None:
        exit_px, exit_bar, cls = bars[end - 1][3], end - 1, "horizon"
    risk = t["risk"]
    frac = t["frac"] if tp1_bar is not None else 0.0
    pts = frac * sgn * (tp1 - entry) + (1.0 - frac) * sgn * (exit_px - entry)
    return {"cls": cls, "entered": True, "entry": entry, "exit": exit_px, "exit_bar": exit_bar, "bars": held,
            "tp1_bar": tp1_bar, "tp2_bar": tp2_bar, "gapped_stop": gapped and cls == "stop", "pts": pts,
            "r": pts / risk if risk else 0.0, "mae_r": (sgn * (entry - (lo if long_ else hi))) / risk if risk else 0.0,
            "mfe_r": (sgn * ((hi if long_ else lo) - entry)) / risk if risk else 0.0}


# ---------------------------------------------------------------- reading the chart

def percentile_rank(series, value):
    vals = [v for v in series if v is not None]
    if not vals or value is None:
        return None
    return 100.0 * sum(1 for v in vals if v <= value) / len(vals)


def chart_read(c, ind, an=None):
    i = len(c) - 1
    cl = [b["close"] for b in c]
    rets = [math.log(cl[j] / cl[j - 1]) for j in range(max(1, i - 19), i + 1) if cl[j - 1] > 0]
    rv = (math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(TRADING_DAYS)) if rets else None
    e20, e50 = ind["ema20"][i], ind["ema50"][i]
    e50_prev = ind["ema50"][i - 5] if i >= 5 else None
    trend = "up" if e20 > e50 else "down"
    slope = "rising" if e50_prev is not None and e50 > e50_prev else "falling"
    year = c[-250:]
    hi52, lo52 = max(b["high"] for b in year), min(b["low"] for b in year)
    sl, sh = f.swing_low(c, i), f.swing_high(c, i)

    def chg(n):
        return round(100.0 * (cl[i] / cl[i - n] - 1), 2) if i >= n and cl[i - n] else None
    out = {"date": c[i]["date"], "close": cl[i], "change_pct": {"1": chg(1), "5": chg(5), "20": chg(20)},
           "trend": trend, "ema50_slope": slope, "ema20": round(e20, 2), "ema50": round(e50, 2),
           "adx": round(ind["adx"][i], 1) if ind["adx"][i] is not None else None,
           "rsi": round(ind["rsi"][i], 1) if ind["rsi"][i] is not None else None,
           "supertrend": "up" if ind["st_dir"][i] == 1 else "down",
           "atr": round(ind["atr"][i], 2), "atr_pct": round(100 * ind["atr"][i] / cl[i], 2),
           "atr_percentile": round(percentile_rank(ind["atr"][-250:], ind["atr"][i]), 0),
           "bb_width_percentile": round(percentile_rank(ind["bbw"][-250:], ind["bbw"][i]), 0)
           if ind["bbw"][i] is not None else None,
           "realized_vol_20d_pct": round(100 * rv, 2) if rv else None,
           "high_52w": hi52, "low_52w": lo52, "from_high_pct": round(100 * (cl[i] / hi52 - 1), 2),
           "swing_low": sl, "swing_high": sh, "bars": len(c)}
    if an:
        out["chain"] = {kk: an.get(kk) for kk in ("expiry", "days_to_expiry", "atm_iv_pct", "expected_move_1sd",
                                                   "pcr_oi", "max_pain", "resistance", "support")}
    return out


# ---------------------------------------------------------------- the trade

def trade_from(c, ind, k, direction=None, setup=None, entry=None, sl=None, tp1=None, tp2=None, an=None):
    """The trade on the last closed bar: the best setup that fired there, or the direction (and any levels)
    given. Returns the levels and the trade's shape (stop in ATRs, targets in R) for the historical replay."""
    i = len(c) - 1
    sigs = f.signals_at(c, ind, i)
    fired = None
    if direction is None:
        if not sigs:
            raise TMError("no setup fired on the last bar (%s): pass --direction long|short to replay a trade of "
                          "your own" % c[i]["date"])
        fired = max(sigs, key=lambda s: s["score"])
        direction = fired["direction"]
    else:
        same = [s for s in sigs if s["direction"] == direction and (setup is None or s["setup"] == setup)]
        fired = max(same, key=lambda s: s["score"]) if same else None
    lv = f.levels(c, ind, i, direction, k, entry=entry, resistance=(an or {}).get("resistance"),
                  support=(an or {}).get("support"))
    long_ = direction == "long"
    sgn = 1 if long_ else -1
    e = lv["entry"]
    stop = float(sl) if sl is not None else lv["stop"]
    if (long_ and stop >= e) or ((not long_) and stop <= e):
        raise TMError("the stop %s is on the wrong side of the entry %s for a %s" % (stop, e, direction))
    risk = abs(e - stop)
    t1 = float(tp1) if tp1 is not None else (e + sgn * k["tp1_r"] * risk if sl is not None else lv["tp1"])
    t2 = float(tp2) if tp2 is not None else (e + sgn * k["tp2_r"] * risk if sl is not None else lv["tp2"])
    if sgn * (t1 - e) <= 0 or sgn * (t2 - t1) < 0:
        raise TMError("targets must lie beyond the entry in the trade's direction, TP2 at or beyond TP1")
    custom = any(x is not None for x in (sl, tp1, tp2))
    a = ind["atr"][i]
    return {"direction": direction, "setup": (fired or {}).get("setup") if not setup else setup,
            "why": (fired or {}).get("why"), "date": c[i]["date"], "entry_plan": e, "stop": round(stop, 2),
            "tp1": round(t1, 2), "tp2": round(t2, 2), "risk": round(risk, 2), "atr": round(a, 2),
            "stop_atr": risk / a if a else None, "tp1_r": abs(t1 - e) / risk, "tp2_r": abs(t2 - e) / risk,
            "custom_levels": custom, "notes": lv["notes"] if not custom else [], "ok": lv["ok"] if not custom else True,
            "no_edge_p_tp1": f.no_edge_probability(e, stop, t1), "no_edge_p_tp2": f.no_edge_probability(e, stop, t2)}


# ---------------------------------------------------------------- method 1: the chart's own history

def bars_from(c, ind, start):
    return [(c[j]["open"], c[j]["high"], c[j]["low"], c[j]["close"], ind["atr"][j - 1] or ind["atr"][j])
            for j in range(start, len(c))]


def replay(c, ind, k, tr, frac, horizon, conditioned=True):
    """Every bar where the same setup fired the same way (or, conditioned=False, every bar), with that bar's
    own levels by the same rules (or the trade's shape when the levels were given), entered at the next open."""
    out, still_open = [], 0
    long_ = tr["direction"] == "long"
    sgn = 1 if long_ else -1
    for i in range(60, len(c) - 1):
        if conditioned:
            sigs = f.signals_at(c, ind, i)
            if not any(s["direction"] == tr["direction"] and (tr["setup"] is None or s["setup"] == tr["setup"])
                       for s in sigs):
                continue
        a = ind["atr"][i]
        e = c[i]["close"]
        if tr["custom_levels"]:
            risk = tr["stop_atr"] * a
            t = {"direction": tr["direction"], "stop": e - sgn * risk, "tp1": e + sgn * tr["tp1_r"] * risk,
                 "tp2": e + sgn * tr["tp2_r"] * risk, "risk": risk, "frac": frac}
        else:
            lv = f.levels(c, ind, i, tr["direction"], k)
            t = {"direction": tr["direction"], "stop": lv["stop"], "tp1": lv["tp1"], "tp2": lv["tp2"],
                 "risk": lv["risk_points"], "frac": frac}
        res = run_trade(bars_from(c, ind, i + 1), t, k, horizon)
        if res["cls"] == "horizon" and res["exit_bar"] < horizon - 1:
            still_open += 1      # the data ran out before the trade could finish: not a result
            continue
        res["date"] = c[i]["date"]
        res["start"] = i + 1
        out.append(res)
    return out, still_open


def episodes(results):
    """Occurrences that do not overlap in time: a setup often fires on several bars in a row, and those
    trades share most of their path, so they are not independent evidence."""
    n, free_from = 0, -1
    for r in sorted(results, key=lambda x: x["start"]):
        if r["start"] > free_from:
            n += 1
            free_from = r["start"] + (r["exit_bar"] if r.get("entered") else 0)
    return n


# ---------------------------------------------------------------- method 2: block bootstrap of real days

def bar_library(c, ind, keep_drift=False):
    """Each day as (open gap, high, low, close) from the previous close, in units of the ATR before it."""
    lib = []
    for j in range(15, len(c)):
        a = ind["atr"][j - 1]
        if not a:
            continue
        pc = c[j - 1]["close"]
        lib.append([(c[j]["open"] - pc) / a, (c[j]["high"] - pc) / a, (c[j]["low"] - pc) / a, (c[j]["close"] - pc) / a])
    if not lib:
        raise TMError("not enough history to build the bar library")
    if not keep_drift:
        mu = sum(x[3] for x in lib) / len(lib)
        lib = [[x[0] - mu, x[1] - mu, x[2] - mu, x[3] - mu] for x in lib]
    return lib


def bootstrap_path(rng, lib, close0, atr0, n, k, block=5.0):
    """A stationary bootstrap (Politis-Romano): runs of consecutive real days, a new run with chance 1/block."""
    out = []
    pc, a = close0, atr0
    j = rng.randrange(len(lib))
    p = 1.0 / block
    period = k["atr_period"]
    for _ in range(n):
        g, h, l, cl = lib[j]
        o, hh, ll, cc = pc + g * a, pc + h * a, pc + l * a, pc + cl * a
        out.append((o, hh, ll, cc, a))
        tr = max(hh - ll, abs(hh - pc), abs(ll - pc))
        a = (a * (period - 1) + tr) / period
        pc = cc
        j = rng.randrange(len(lib)) if rng.random() < p else (j + 1) % len(lib)
    return out


# ---------------------------------------------------------------- method 3: the volatility the options price

def overnight_share(c):
    """The share of daily variance that arrives between the close and the next open, from this chart."""
    gap = intra = 0.0
    for j in range(1, len(c)):
        pc, o, cl = c[j - 1]["close"], c[j]["open"], c[j]["close"]
        if pc > 0 and o > 0 and cl > 0:
            gap += math.log(o / pc) ** 2
            intra += math.log(cl / o) ** 2
    return gap / (gap + intra) if gap + intra > 0 else 0.2


def implied_path(rng, close0, atr0, sigma, phi, n, k):
    """Risk-neutral daily bars at volatility sigma: an overnight gap, then the session as a Brownian bridge
    whose maximum and minimum give the high and the low."""
    dt = 1.0 / TRADING_DAYS
    vg, vi = sigma * sigma * phi * dt, sigma * sigma * (1 - phi) * dt
    out, pc, a = [], close0, atr0
    period = k["atr_period"]
    for _ in range(n):
        g = rng.gauss(0.0, 1.0) * math.sqrt(vg) - 0.5 * vg
        x = rng.gauss(0.0, 1.0) * math.sqrt(vi) - 0.5 * vi
        u1, u2 = 1.0 - rng.random(), 1.0 - rng.random()
        mx = 0.5 * (x + math.sqrt(x * x - 2.0 * vi * math.log(u1)))
        mn = 0.5 * (x - math.sqrt(x * x - 2.0 * vi * math.log(u2)))
        o = pc * math.exp(g)
        hh, ll, cc = o * math.exp(mx), o * math.exp(mn), o * math.exp(x)
        out.append((o, hh, ll, cc, a))
        tr = max(hh - ll, abs(hh - pc), abs(ll - pc))
        a = (a * (period - 1) + tr) / period
        pc = cc
    return out


# ---------------------------------------------------------------- the instruments

def trading_days_after(day, n, holidays=()):
    """The next n NSE sessions after day (weekdays, less the exchange holidays when india-alerts is installed)."""
    d = datetime.date.fromisoformat(day[:10])
    out = []
    while len(out) < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d)
    return out


def nse_holidays():
    try:
        al = f._sibling("india-alerts", "alerts")
        return set(getattr(al, "NSE_HOLIDAYS", {}) or {})
    except Exception:   # the clock is optional; weekdays alone are close
        return set()


class OptionLeg(object):
    """A bought option repriced with Black-76 along a path: the chain's own IV (sticky strike), time decay on
    the calendar, the futures basis shrinking to zero at expiry, intrinsic value at expiry."""

    def __init__(self, op, chain, sessions, now_utc, k, iv_shift=0.0):
        self.kind, self.K, self.lot = op["kind"], float(op["strike"]), int(op["lot_size"])
        self.iv = max(op["iv_pct"] / 100.0 + iv_shift, 0.01)
        self.quote = float(op["entry_premium"])
        self.r = k["risk_free_rate"]
        exp = str(op["expiry"])
        self.exp_day = datetime.date(int(exp[:4]), int(exp[4:6]), int(exp[6:8]))
        self.T0 = f.years_to_expiry(exp, now_utc)
        self.spot = chain["spot"]
        self.basis = (chain.get("forward") or chain["spot"]) - chain["spot"]
        # time to expiry at each session's close; a session's open is about 6.25 hours before its close
        self.Ts = [max((self.exp_day - d).days / 365.0, 0.0) for d in sessions]
        self.T_open0 = (self.Ts[0] + 6.25 / 24 / 365.0) if self.Ts else 0.0
        self.bars_to_expiry = next((n + 1 for n, d in enumerate(sessions) if d >= self.exp_day), None)
        self.rates = f.costs.rates_from(k)
        # the chain's ask over the model price at the chain's own spot and clock: the spread and the smile,
        # carried into every repricing so a path that opens where the chain was priced pays the quote
        self.offset = self.quote - self._model(self.spot, self.T0)

    def _model(self, level, T):
        F = level + (self.basis * T / self.T0 if self.T0 > 0 else 0.0)
        return f.black76(F, self.K, T, self.iv, self.r, self.kind)

    def premium(self, level, bar):
        return self._model(level, self.Ts[min(bar, len(self.Ts) - 1)])

    def entry_premium(self, level):
        """What the option costs when the order fills at the next open (the ask, as the chain prices it)."""
        return max(self._model(level, self.T_open0) + self.offset, 0.05)

    def pnl(self, res, tr):
        """Rupees per lot for one path, after charges (the buy at the next open, the sells at TP1 and the exit).
        Exits are priced at the session's close clock (slightly more decay than an intraday exit)."""
        if not res["entered"]:
            return None
        entry = self.entry_premium(res["entry"])
        frac = tr["frac"] if res["tp1_bar"] is not None else 0.0
        p_exit = self.premium(res["exit"], res["exit_bar"])
        p_tp1 = self.premium(tr["tp1"], res["tp1_bar"]) if res["tp1_bar"] is not None else 0.0
        gross = (frac * (p_tp1 - entry) + (1 - frac) * (p_exit - entry)) * self.lot
        sold = (frac * p_tp1 + (1 - frac) * p_exit) * self.lot
        ch = f.costs.fno_costs("buy", entry * self.lot, "OPT", "NFO", self.rates)["total"]
        if sold > 0:
            ch += f.costs.fno_costs("sell", sold, "OPT", "NFO", self.rates)["total"]
        return gross - ch


# ---------------------------------------------------------------- statistics

def quantiles(xs, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
    if not xs:
        return {}
    s = sorted(xs)
    out = {}
    for q in qs:
        pos = q * (len(s) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(s) - 1)
        out["p%d" % round(100 * q)] = s[lo] + (s[hi] - s[lo]) * (pos - lo)
    return out


def summarise(results, money=None, exact_ci=False):
    """Outcome probabilities (with 95% intervals), and the trade's distribution in R, rupees and time."""
    n = len(results)
    if not n:
        return {"n": 0}
    counts = {}
    for r in results:
        counts[r["cls"]] = counts.get(r["cls"], 0) + 1
    outcomes = {}
    for key, _ in OUTCOMES:
        m = counts.get(key, 0)
        if not m and key in ("expiry", "horizon"):
            continue
        p = m / float(n)
        if exact_ci:
            lo, hi = f.wilson(m, n)
        else:
            se = math.sqrt(p * (1 - p) / n)
            lo, hi = max(p - 1.96 * se, 0.0), min(p + 1.96 * se, 1.0)
        rows = [r for r in results if r["cls"] == key]
        outcomes[key] = {"count": m, "p": round(p, 4), "ci95": [round(lo, 4), round(hi, 4)],
                         "avg_r": round(sum(r["r"] for r in rows) / m, 3) if m and rows[0]["entered"] else None,
                         "avg_bars": round(sum(r["bars"] for r in rows) / m, 1) if m and rows[0]["entered"] else None}
    gapped = sum(1 for r in results if r.get("gapped_stop"))
    if "stop" in outcomes:
        outcomes["stop"]["gapped_through"] = round(gapped / float(n), 4)
    live = [r for r in results if r["entered"]]
    rs = [r["r"] for r in live]
    out = {"n": n, "entered": len(live), "outcomes": outcomes}
    if rs:
        mean = sum(rs) / len(rs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0.0
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        pw = len(wins) / float(len(rs))
        wl, wh = f.wilson(len(wins), len(rs)) if exact_ci else (pw - 1.96 * math.sqrt(pw * (1 - pw) / len(rs)),
                                                                pw + 1.96 * math.sqrt(pw * (1 - pw) / len(rs)))
        out.update({
            "expectancy_r": round(mean, 4), "expectancy_se": round(sd / math.sqrt(len(rs)), 4) if len(rs) > 1 else None,
            "p_profit": round(pw, 4), "p_profit_ci95": [round(max(wl, 0), 4), round(min(wh, 1), 4)],
            "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
            "avg_loss_r": round(-sum(losses) / len(losses), 3) if losses else 0.0,
            "r_quantiles": {kk: round(v, 3) for kk, v in quantiles(rs).items()},
            "worst_r": round(min(rs), 3), "best_r": round(max(rs), 3),
            "mae_r_median": round(quantiles([r["mae_r"] for r in live])["p50"], 3),
            "mae_r_p90": round(quantiles([r["mae_r"] for r in live], (0.9,))["p90"], 3),
            "mfe_r_median": round(quantiles([r["mfe_r"] for r in live])["p50"], 3),
            "bars_median": quantiles([r["bars"] for r in live])["p50"],
            "bars_p90": quantiles([r["bars"] for r in live], (0.9,))["p90"]})
    if money:
        for name, vals in money.items():
            v = [x for x in vals if x is not None]
            if not v:
                continue
            mu = sum(v) / len(v)
            q = quantiles(v)
            out[name] = {"expectancy": round(mu, 2), "p_loss": round(sum(1 for x in v if x < 0) / float(len(v)), 4),
                         "p5": round(q["p5"], 2), "p50": round(q["p50"], 2), "p95": round(q["p95"], 2),
                         "worst": round(min(v), 2), "best": round(max(v), 2)}
    return out


# ---------------------------------------------------------------- the run

def analyse(c, cfg=None, direction=None, setup=None, entry=None, sl=None, tp1=None, tp2=None, chain=None,
            now_utc=None, lot=None, lots=1, qty=None, instrument="both", symbol="", paths=20000, horizon=40,
            seed=7, iv=None, keep_drift=False, block=5.0, grid=False, methods=("replay", "bootstrap", "implied")):
    k = f.cfg_with(cfg)
    ind = f.indicators(c, k)
    if len(c) < 80:
        raise TMError("only %d candles: the Time Machine needs at least 80 (a year of daily bars is best)" % len(c))
    an = f.chain_analytics(chain, now_utc, k["risk_free_rate"]) if chain else None
    tr = trade_from(c, ind, k, direction, setup, entry, sl, tp1, tp2, an)
    if instrument == "equity" and tr["direction"] == "short":
        raise TMError("delivery shares cannot be held short overnight: a short view needs futures or a put")
    sym = symbol.upper()
    lot = lot or (chain or {}).get("lot_size") or f.INDEX_LOTS.get(sym)
    if instrument == "equity":
        q = int(qty or 1)
        frac = math.floor(q * k["tp1_exit_fraction"]) / q if q >= 2 else 0.0
    else:
        frac = k["tp1_exit_fraction"] if int(lots) >= 2 else 0.0
    tr["frac"] = frac
    sessions = trading_days_after(c[-1]["date"], horizon, nse_holidays())
    leg = None
    notes = list(tr["notes"])
    tr["rules"] = {"trail_atr": k["trail_atr_mult"], "trail_after_tp2_atr": k["trail_after_tp2_atr_mult"],
                   "time_stop_bars": k["time_stop_bars"]}
    if instrument in ("option", "both"):
        if not chain:
            notes.append("no option chain given (--chain): the option leg and the implied method are left out")
        else:
            try:
                op = f.option_plan({"direction": tr["direction"], "entry": tr["entry_plan"], "stop": tr["stop"],
                                    "tp1": tr["tp1"], "tp2": tr["tp2"], "atr": tr["atr"]}, chain, k, now_utc, lot, sym)
                leg = OptionLeg(op, chain, sessions, now_utc or f.parse_now(None, chain), k)
                tr["option"] = {kk: op[kk] for kk in ("expiry", "kind", "strike", "delta", "iv_pct", "entry_premium",
                                                      "lot_size", "spread_pct", "oi")}
                tr["option"]["bars_to_expiry"] = leg.bars_to_expiry
            except f.FnoError as e:
                notes.append("option leg left out: %s" % e)
    if instrument in ("future", "both") and lot:
        cost_points = f.roundtrip_points(tr["entry_plan"], lot, k)[0]
    elif instrument == "equity":
        q = int(qty or 1)
        rates = f.costs.rates_from(k)
        cost_points = (f.costs.trade_costs("buy", tr["entry_plan"] * q, "NSE", "CNC", rates)["total"] +
                       f.costs.trade_costs("sell", tr["entry_plan"] * q, "NSE", "CNC", rates)["total"]) / q
    else:
        cost_points = f.roundtrip_points(tr["entry_plan"], None, k)[0]
    tr["cost_points"] = round(cost_points, 2)
    if instrument in ("future", "both") and lot:
        notes.append("futures: a trade that outlives the month is assumed rolled to the next one (the roll's "
                     "spread and charges are not counted)")
    hz = horizon
    t = {"direction": tr["direction"], "stop": tr["stop"], "tp1": tr["tp1"], "tp2": tr["tp2"], "risk": tr["risk"],
         "frac": frac}

    def money_of(res):
        """Rupees per lot (or for the shares) for the futures/equity position: points less every charge."""
        if not res["entered"]:
            return None
        pts = res["pts"] - cost_points
        if instrument == "equity":
            return pts * int(qty or 1)
        return pts * lot if lot else None

    def finish(res):
        """R after the round trip's charges, on the planned risk."""
        if res["entered"]:
            res["r"] = (res["pts"] - cost_points) / tr["risk"] if tr["risk"] else 0.0
        return res

    report = {"symbol": sym, "chart": chart_read(c, ind, an), "trade": tr, "notes": notes, "horizon_bars": hz,
              "lot": lot, "lots": int(lots), "qty": qty, "instrument": instrument, "methods": {}}
    money_key = "equity_inr" if instrument == "equity" else "futures_inr_per_lot"

    if "replay" in methods:
        occ, open_end = replay(c, ind, k, tr, frac, hz, True)
        base, _ = replay(c, ind, k, tr, frac, hz, False)
        occ = [finish(r) for r in occ]
        base = [finish(r) for r in base]
        m = {money_key: [money_of(r) for r in occ]} if (lot or instrument == "equity") else None
        report["methods"]["replay"] = dict(summarise(occ, m, exact_ci=True), still_open_at_end=open_end,
                                           independent=episodes(occ),
                                           what=("every bar where %s %s fired" % (tr["setup"], tr["direction"])
                                                 if tr["setup"] else "every bar where any setup fired %s"
                                                 % tr["direction"]))
        report["methods"]["baseline"] = dict(summarise(base, None, exact_ci=True), independent=episodes(base),
                                             what="every bar, %s, same rules: the edge has to beat this"
                                                  % tr["direction"])
    rng_seed = seed
    sims = []
    if "bootstrap" in methods:
        lib = bar_library(c, ind, keep_drift)
        sims.append(("bootstrap", lambda rng: bootstrap_path(rng, lib, c[-1]["close"], ind["atr"][-1], hz, k, block),
                     "%d paths from this chart's %d real days, blocks of about %g days, %s"
                     % (paths, len(lib), block, "the history's drift kept" if keep_drift else "drift removed")))
    sigma = (iv / 100.0) if iv else ((an or {}).get("atm_iv_pct") or 0) / 100.0
    if "implied" in methods and sigma > 0:
        phi = overnight_share(c)
        sims.append(("implied", lambda rng: implied_path(rng, c[-1]["close"], ind["atr"][-1], sigma, phi, hz, k),
                     "%d risk-neutral paths at %.2f%% volatility (%s), %.0f%% of it overnight (measured here)"
                     % (paths, 100 * sigma, "given" if iv else "the chain's ATM IV", 100 * phi)))
    elif "implied" in methods:
        notes.append("no volatility for the implied method: give --chain or --iv")
    for name, gen, what in sims:
        rng = random.Random("%s-%s" % (rng_seed, name))
        res_all, fut_money, opt_money, opt_cls = [], [], [], []
        for _ in range(int(paths)):
            bars = gen(rng)
            res = finish(run_trade(bars, t, k, hz))
            res_all.append(res)
            fut_money.append(money_of(res))
            if leg:
                bte = leg.bars_to_expiry
                ro = run_trade(bars, t, k, min(hz, bte) if bte else hz)
                if ro["entered"] and ro["cls"] == "horizon" and bte and bte <= hz:
                    ro["cls"] = "expiry"
                opt_money.append(leg.pnl(ro, t))
                opt_cls.append(ro["cls"])
        money = {}
        if lot or instrument == "equity":
            money[money_key] = fut_money
        if leg:
            money["option_inr_per_lot"] = opt_money
        s = summarise(res_all, money)
        if leg and opt_money:
            v = [x for x in opt_money if x is not None]
            prem = leg.quote * leg.lot
            s["option_inr_per_lot"]["premium_paid"] = round(prem, 2)
            s["option_inr_per_lot"]["p_lose_half_premium"] = round(sum(1 for x in v if x <= -0.5 * prem) / float(len(v)), 4) if v else None
            s["option_inr_per_lot"]["p_held_to_expiry"] = round(opt_cls.count("expiry") / float(len(opt_cls)), 4)
        s["what"] = what
        report["methods"][name] = s
    if grid and "bootstrap" in methods:
        report["grid"] = sensitivity(c, ind, k, tr, frac, hz, min(int(paths), 6000), seed, keep_drift, block, cost_points)
    return report


def sensitivity(c, ind, k, tr, frac, hz, paths, seed, keep_drift, block, cost_points):
    """Expectancy for other stops (in ATRs) and TP2 targets (in R) on the SAME bootstrap paths."""
    lib = bar_library(c, ind, keep_drift)
    a = ind["atr"][-1]
    e = c[-1]["close"]
    sgn = 1 if tr["direction"] == "long" else -1
    stops = [1.0, 1.5, 2.0, 2.5, 3.0]
    targets = [1.5, 2.0, 3.0]
    cells = []
    for sm in stops:
        for tm in targets:
            risk = sm * a
            cells.append({"stop_atr": sm, "tp2_r": tm, "t": {"direction": tr["direction"], "stop": e - sgn * risk,
                                                              "tp1": e + sgn * k["tp1_r"] * risk,
                                                              "tp2": e + sgn * tm * risk, "risk": risk, "frac": frac},
                          "rs": []})
    rng = random.Random("%s-grid" % seed)
    for _ in range(paths):
        bars = bootstrap_path(rng, lib, e, a, hz, k, block)
        for cell in cells:
            res = run_trade(bars, cell["t"], k, hz)
            if res["entered"]:
                cell["rs"].append((res["pts"] - cost_points) / cell["t"]["risk"])
    out = []
    for cell in cells:
        rs = cell["rs"]
        mu = sum(rs) / len(rs) if rs else 0.0
        out.append({"stop_atr": cell["stop_atr"], "tp2_r": cell["tp2_r"], "expectancy_r": round(mu, 3),
                    "p_profit": round(sum(1 for x in rs if x > 0) / float(len(rs)), 3) if rs else None,
                    "se": round(math.sqrt(sum((x - mu) ** 2 for x in rs) / max(len(rs) - 1, 1)) / math.sqrt(len(rs)), 3)
                    if len(rs) > 1 else None})
    return {"paths": paths, "cells": out, "current": {"stop_atr": round(tr["stop_atr"], 2), "tp2_r": round(tr["tp2_r"], 2)}}


# ---------------------------------------------------------------- output

def pct(x):
    return "%5.1f%%" % (100 * x) if x is not None else "    -"


def render(rep):
    inr = f.inr
    ch, tr = rep["chart"], rep["trade"]
    L = []
    L.append("TIME MACHINE  %s %s%s on the %s close, entry %s"
             % (rep["symbol"] or "?", tr["direction"].upper(), (" [%s]" % tr["setup"]) if tr["setup"] else "",
                ch["date"], inr(tr["entry_plan"])))
    L.append("")
    L.append("The chart")
    L.append("  close %s (%+.2f%% 1 day, %+.2f%% 5 days, %+.2f%% 20 days); %s of the 52-week high %s, low %s"
             % (inr(ch["close"]), ch["change_pct"]["1"] or 0, ch["change_pct"]["5"] or 0, ch["change_pct"]["20"] or 0,
                "%+.1f%%" % ch["from_high_pct"], inr(ch["high_52w"]), inr(ch["low_52w"])))
    L.append("  trend %s (EMA20 %s vs EMA50 %s, EMA50 %s), ADX %s, RSI %s, Supertrend %s"
             % (ch["trend"], ch["ema20"], ch["ema50"], ch["ema50_slope"], ch["adx"], ch["rsi"], ch["supertrend"]))
    L.append("  volatility: ATR %s (%.2f%% of price, %s percentile of the year), Bollinger width %s percentile, "
             "20-day realized %s%%" % (ch["atr"], ch["atr_pct"], _ord(ch["atr_percentile"]),
                                        _ord(ch["bb_width_percentile"]), ch["realized_vol_20d_pct"]))
    L.append("  structure: swing low %s, swing high %s" % (ch["swing_low"], ch["swing_high"]))
    if ch.get("chain"):
        x = ch["chain"]
        L.append("  options %s (%.1f days): ATM IV %s%% vs realized %s%%, 1-SD move +/-%s, PCR %s, max pain %s, "
                 "OI resistance %s, support %s" % (x["expiry"], x["days_to_expiry"] or 0, x["atm_iv_pct"],
                                                   ch["realized_vol_20d_pct"], x["expected_move_1sd"], x["pcr_oi"],
                                                   x["max_pain"], x["resistance"], x["support"]))
    L.append("")
    L.append("The trade%s" % ("" if not tr.get("why") else ": " + tr["why"]))
    L.append("  entry %s next open | SL %s (%.2f points, %.2f ATR) | TP1 %s (%.2fR) | TP2 %s (%.2fR)"
             % (inr(tr["entry_plan"]), inr(tr["stop"]), tr["risk"], tr["stop_atr"] or 0, inr(tr["tp1"]), tr["tp1_r"],
                inr(tr["tp2"]), tr["tp2_r"]))
    L.append("  exits: at TP1 %s and the stop moves to entry; trail %s ATR, %s ATR after TP2; time stop %d bars; "
             "horizon %d bars; charges %s points a round trip"
             % ("%.0f%% is booked" % (100 * tr["frac"]) if tr["frac"] else "nothing is booked (one lot cannot be split)",
                tr["rules"]["trail_atr"], tr["rules"]["trail_after_tp2_atr"], tr["rules"]["time_stop_bars"],
                rep["horizon_bars"], tr["cost_points"]))
    L.append("  with no edge, P(TP1 before SL) = %.1f%% and P(TP2 before SL) = %.1f%% (risk / (risk + reward))"
             % (100 * tr["no_edge_p_tp1"], 100 * tr["no_edge_p_tp2"]))
    if tr.get("option"):
        o = tr["option"]
        L.append("  option leg: %s %s %s at %s (delta %s, IV %s%%, %d sessions to expiry), lot %d"
                 % (o["expiry"], int(o["strike"]), o["kind"], o["entry_premium"], o["delta"], o["iv_pct"],
                    o["bars_to_expiry"], o["lot_size"]))
    for n in rep["notes"]:
        L.append("  note: " + n)
    L.append("")
    cols = [m for m in ("replay", "baseline", "bootstrap", "implied") if m in rep["methods"]]
    if not cols:
        return "\n".join(L)
    L.append("%-34s" % "How it ends" + "".join("%-21s" % _head(m, rep["methods"][m]) for m in cols))
    for key, label in OUTCOMES:
        if not any(key in rep["methods"][m].get("outcomes", {}) for m in cols):
            continue
        row = "  %-32s" % label
        for m in cols:
            o = rep["methods"][m].get("outcomes", {}).get(key)
            if not o:
                row += "%-21s" % "  0.0%"
                continue
            cell = "%s %s" % (pct(o["p"]), ("%+.2fR" % o["avg_r"]) if o.get("avg_r") is not None else "")
            row += "%-21s" % cell
        L.append(row)
        if key == "stop":
            row = "  %-32s" % "  gapped through the stop"
            for m in cols:
                o = rep["methods"][m].get("outcomes", {}).get("stop")
                row += "%-21s" % (pct(o.get("gapped_through")) if o else "")
            L.append(row)
    L.append("")
    for label, key, fmt in (("expectancy per trade", "expectancy_r", "%+.3fR"), ("chance of a profit", "p_profit", None),
                            ("average win / loss", None, None), ("median result", None, None),
                            ("worst 5% / best 5%", None, None), ("adverse move, median / 90%", None, None),
                            ("bars held, median / 90%", None, None)):
        row = "  %-32s" % label
        for m in cols:
            s = rep["methods"][m]
            if not s.get("entered"):
                row += "%-21s" % "-"
                continue
            if key == "expectancy_r":
                se = s.get("expectancy_se")
                cell = (fmt % s["expectancy_r"]) + ((" +/-%.3f" % (1.96 * se)) if se else "")
            elif key == "p_profit":
                lo, hi = s["p_profit_ci95"]
                cell = "%.1f%% (%.0f-%.0f)" % (100 * s["p_profit"], 100 * lo, 100 * hi)
            elif label.startswith("average"):
                cell = "+%.2fR / -%.2fR" % (s["avg_win_r"], s["avg_loss_r"])
            elif label.startswith("median"):
                cell = "%+.2fR" % s["r_quantiles"]["p50"]
            elif label.startswith("worst"):
                cell = "%+.2fR / %+.2fR" % (s["r_quantiles"]["p5"], s["r_quantiles"]["p95"])
            elif label.startswith("adverse"):
                cell = "%.2fR / %.2fR" % (s["mae_r_median"], s["mae_r_p90"])
            else:
                cell = "%g / %g" % (round(s["bars_median"], 1), round(s["bars_p90"], 1))
            row += "%-21s" % cell
        L.append(row)
    for key, title in (("futures_inr_per_lot", "futures, rupees per lot after charges"),
                       ("equity_inr", "shares, rupees for the position after charges"),
                       ("option_inr_per_lot", "option, rupees per lot after charges")):
        if not any(key in rep["methods"][m] for m in cols):
            continue
        L.append("")
        L.append("  " + title)
        for sub, lab in (("expectancy", "expectancy"), ("p_loss", "chance of a loss"), ("p5", "worst 5%"),
                         ("p50", "median"), ("p95", "best 5%"), ("worst", "worst path"),
                         ("premium_paid", "premium paid (the quote)"),
                         ("p_lose_half_premium", "loses half the premium or more"),
                         ("p_held_to_expiry", "still held at expiry")):
            if not any(sub in rep["methods"][m].get(key, {}) for m in cols):
                continue
            row = "    %-30s" % lab
            for m in cols:
                v = rep["methods"][m].get(key, {}).get(sub)
                row += "%-21s" % ("-" if v is None else (pct(v) if sub.startswith("p_") else inr(v)))
            L.append(row)
    if rep.get("grid"):
        g = rep["grid"]
        L.append("")
        L.append("Stop and target sensitivity (bootstrap, the same %d paths; expectancy R / chance of profit)" % g["paths"])
        tps = sorted({x["tp2_r"] for x in g["cells"]})
        L.append("  stop \\ TP2   " + "".join("%-18s" % ("%gR" % t) for t in tps))
        for sm in sorted({x["stop_atr"] for x in g["cells"]}):
            row = "  %-4g ATR     " % sm
            for t in tps:
                x = next(y for y in g["cells"] if y["stop_atr"] == sm and y["tp2_r"] == t)
                row += "%-18s" % ("%+.3f / %.0f%%" % (x["expectancy_r"], 100 * (x["p_profit"] or 0)))
            L.append(row)
        L.append("  (now: stop %.2f ATR, TP2 %.2fR). Picking the best cell is fitting this chart's past: treat it as "
                 "a range, not a setting." % (g["current"]["stop_atr"], g["current"]["tp2_r"]))
    L.append("")
    L.extend(verdict(rep))
    L.append("Models, not forecasts: the replay is this chart's past, the bootstrap reshuffles it, the implied "
             "paths are what option prices assume. Education and tooling, not financial advice.")
    return "\n".join(L)


def _head(m, s):
    n = s.get("n", 0)
    ind = s.get("independent")
    return {"replay": "replay %d (%s ind.)" % (n, ind) if ind is not None else "replay (n=%d)" % n,
            "baseline": "every bar %d" % n, "bootstrap": "bootstrap (%d)" % n,
            "implied": "implied IV (%d)" % n}.get(m, m)


def _ord(x):
    if x is None:
        return "-"
    x = int(x)
    suf = "th" if 10 <= x % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(x % 10, "th")
    return "%d%s" % (x, suf)


def verdict(rep):
    ms = rep["methods"]
    lines = []
    sims = [(m, ms[m]) for m in ("bootstrap", "implied") if m in ms and ms[m].get("entered")]
    for m, s in sims:
        se = s.get("expectancy_se") or 0
        lo, hi = s["expectancy_r"] - 1.96 * se, s["expectancy_r"] + 1.96 * se
        word = "positive" if lo > 0 else ("negative" if hi < 0 else "indistinguishable from zero")
        lines.append("Verdict (%s): expectancy %+.3fR (95%% %+.3f to %+.3f), %s." % (m, s["expectancy_r"], lo, hi, word))
    rp, base = ms.get("replay"), ms.get("baseline")
    if rp and rp.get("entered"):
        if rp.get("independent", rp["entered"]) < 20:
            lines.append("Verdict (replay): %d past occurrences, %d of them independent: too few to judge (under 20); "
                         "read the interval, not the average." % (rp["entered"], rp.get("independent", rp["entered"])))
        else:
            lines.append("Verdict (replay): %d past occurrences, expectancy %+.3fR against %+.3fR for entering on "
                         "every bar the same way: %s." % (rp["entered"], rp["expectancy_r"], base.get("expectancy_r", 0),
                                                          "the setup added something here" if
                                                          rp["expectancy_r"] > (base.get("expectancy_r") or 0) else
                                                          "the setup added nothing here"))
    if len(sims) == 2:
        a, b = sims[0][1], sims[1][1]
        gap = abs(a["expectancy_r"] - b["expectancy_r"])
        tol = 1.96 * math.hypot(a.get("expectancy_se") or 0, b.get("expectancy_se") or 0)
        if gap > tol:
            lines.append("The bootstrap and the implied paths disagree by %.3fR: the chart's own behaviour (gaps, "
                         "trends, mean reversion) differs from what the option prices assume." % gap)
    return lines


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description="Every way a trade can end, and how often (india-fno-timemachine).")
    ap.add_argument("--candles", required=True, help="SYMBOL-*.csv or the INDmoney OHLC JSON (closed bars)")
    ap.add_argument("--symbol", help="default: the file name before the first '-'")
    ap.add_argument("--chain", help="option chain JSON (INDmoney or NSE): the option leg and the implied method")
    ap.add_argument("--direction", choices=["long", "short"], help="default: the setup that fired on the last bar")
    ap.add_argument("--setup", choices=list(f.SETUPS), help="replay only this setup's past occurrences")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--sl", type=float)
    ap.add_argument("--tp1", type=float)
    ap.add_argument("--tp2", type=float)
    ap.add_argument("--instrument", choices=["future", "option", "both", "equity"], default="both")
    ap.add_argument("--lot", type=int, help="lot size (indices and the chain have it)")
    ap.add_argument("--lots", type=int, default=1, help="lots held: with 1, nothing can be booked at TP1")
    ap.add_argument("--qty", type=int, help="shares, for --instrument equity")
    ap.add_argument("--paths", type=int, default=20000)
    ap.add_argument("--horizon", type=int, default=40, help="bars to follow a trade at most")
    ap.add_argument("--iv", type=float, help="volatility in %% for the implied method (default: the chain's ATM IV)")
    ap.add_argument("--keep-drift", action="store_true", help="keep the history's trend in the bootstrap")
    ap.add_argument("--block", type=float, default=5.0, help="average bootstrap block, in days")
    ap.add_argument("--methods", nargs="*", default=["replay", "bootstrap", "implied"],
                    choices=["replay", "bootstrap", "implied"])
    ap.add_argument("--grid", action="store_true", help="stop and target sensitivity")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--now", help="ISO time for the option clock (no offset = IST); default: the chain's price time")
    ap.add_argument("--config", default="trader/config.json", help="reads the \"fno\" section and \"charges\"")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = {}
        if a.config and os.path.exists(a.config):
            with open(a.config, encoding="utf-8") as fh:
                doc = json.load(fh)
            cfg = dict(doc.get("fno") or {})
            if doc.get("charges"):
                cfg["charges"] = doc["charges"]
        sym = (a.symbol or os.path.basename(a.candles).split("-")[0].split(".")[0]).upper()
        c = f.load_candles(a.candles)
        if f.load_candles.dropped:
            print("note: left out the unfinished bar of %s" % ", ".join(f.load_candles.dropped), file=sys.stderr)
        chain = f.parse_chain(a.chain, sym) if a.chain else None
        now = f.parse_now(a.now, chain)
        if a.paths < 100:
            raise TMError("--paths: at least 100 (20,000 gives about +/-0.7 points on a 50% probability)")
        rep = analyse(c, cfg, a.direction, a.setup, a.entry, a.sl, a.tp1, a.tp2, chain, now, a.lot, a.lots, a.qty,
                      a.instrument, sym, a.paths, a.horizon, a.seed, a.iv, a.keep_drift, a.block, a.grid,
                      tuple(a.methods))
    except (TMError, f.FnoError, ValueError, OSError) as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
