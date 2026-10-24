/* The Time Machine, core engine (swing mode). A port of plugins/trader-skill-india/india-fno-timemachine
 * (timemachine.py) and india-fno-plan/fnocore.py, held to them by test/core.test.js.
 * No dependencies, no network. Runs in a browser page and in Node.
 *
 * It replays one trade, with the pack's exact exit rules, three ways: the chart's own history, a block bootstrap
 * of its real days, and the volatility the option chain prices. It never predicts: it counts. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.TMX = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const EPS = 1e-12;
  const TRADING_DAYS = 252.0;
  const INDEX_LOTS = { NIFTY: 65, BANKNIFTY: 30, FINNIFTY: 60, MIDCPNIFTY: 120, NIFTYNXT50: 25, SENSEX: 20, BANKEX: 30 };
  const BSE_UNDERLYINGS = { SENSEX: 1, BANKEX: 1 };
  const SETUPS = ["trend_pullback", "breakout", "supertrend_flip", "squeeze"];
  const DEFAULTS = {
    capital: 500000, risk_per_trade_pct: 1.0, max_lots_per_order: 5, atr_period: 14, sl_atr_mult: 1.5,
    sl_min_atr: 0.75, sl_max_atr: 3.0, sl_buffer_atr: 0.2, tp1_r: 1.0, tp1_exit_fraction: 0.5, tp2_r: 2.0,
    min_reward_r: 1.5, trail_atr_mult: 2.5, trail_after_tp2_atr_mult: 1.5, time_stop_bars: 8,
    option_target_delta: 0.55, option_min_dte: 3, option_max_spread_pct: 2.0, option_min_oi: 50000,
    risk_free_rate: 0.065, limit_buffer_pct: 0.3,
  };
  const OUTCOMES = [
    ["gap", "no entry: opens past SL or TP1"], ["nofill", "no fill: the limit not reached"],
    ["stop", "stop-loss before TP1"], ["time", "time stop: no TP1 in time"],
    ["tp1_all", "TP1: the whole position out"], ["tp1_be", "TP1, then stopped at entry"],
    ["tp1_trail", "TP1, then trailed out"], ["tp2_trail", "TP2, then trailed out"],
    ["horizon", "still open at the horizon"], ["expiry", "held to expiry: settled"],
  ];

  class TMError extends Error {}

  function cfgWith(cfg) {
    const c = Object.assign({}, DEFAULTS);
    for (const k of Object.keys(cfg || {})) c[k] = cfg[k];
    return c;
  }
  const r2 = (x) => Math.round(x * 100) / 100;
  const rd = (x, d) => { const m = Math.pow(10, d); return Math.round(x * m) / m; };
  const sum = (a) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return s; };

  // ------------------------------------------------------------------ random numbers (seeded, so a run repeats)
  function xmur3(str) {
    let h = 1779033703 ^ str.length;
    for (let i = 0; i < str.length; i++) { h = Math.imul(h ^ str.charCodeAt(i), 3432918353); h = (h << 13) | (h >>> 19); }
    return function () {
      h = Math.imul(h ^ (h >>> 16), 2246822507); h = Math.imul(h ^ (h >>> 13), 3266489909);
      return (h ^= h >>> 16) >>> 0;
    };
  }
  function makeRng(seed) {
    const s = xmur3(String(seed));
    let a = s(), b = s(), c = s(), d = s();
    let spare = null;
    const rng = {
      random() {                       // sfc32
        a >>>= 0; b >>>= 0; c >>>= 0; d >>>= 0;
        let t = (a + b) | 0; a = b ^ (b >>> 9); b = (c + (c << 3)) | 0; c = (c << 21) | (c >>> 11);
        d = (d + 1) | 0; t = (t + d) | 0; c = (c + t) | 0;
        return (t >>> 0) / 4294967296;
      },
      randrange(n) { return Math.floor(rng.random() * n); },
      gauss() {
        if (spare !== null) { const v = spare; spare = null; return v; }
        let u, v, q;
        do { u = 2 * rng.random() - 1; v = 2 * rng.random() - 1; q = u * u + v * v; } while (q >= 1 || q === 0);
        const m = Math.sqrt(-2 * Math.log(q) / q);
        spare = v * m;
        return u * m;
      },
    };
    for (let i = 0; i < 12; i++) rng.random();
    return rng;
  }

  // ------------------------------------------------------------------ the normal distribution and Black-76
  function ncdf(x) {            // Hart's double-precision approximation (West 2005), error below 1e-15
    const xa = Math.abs(x);
    let c;
    if (xa > 37) c = 0;
    else {
      const e = Math.exp(-xa * xa / 2);
      if (xa < 7.07106781186547) {
        let b = 3.52624965998911e-02 * xa + 0.700383064443688;
        b = b * xa + 6.37396220353165; b = b * xa + 33.912866078383; b = b * xa + 112.079291497871;
        b = b * xa + 221.213596169931; b = b * xa + 220.206867912376;
        c = e * b;
        b = 8.83883476483184e-02 * xa + 1.75566716318264;
        b = b * xa + 16.064177579207; b = b * xa + 86.7807322029461; b = b * xa + 296.564248779674;
        b = b * xa + 637.333633378831; b = b * xa + 793.826512519948; b = b * xa + 440.413735824752;
        c = c / b;
      } else {
        let b = xa + 0.65; b = xa + 4 / b; b = xa + 3 / b; b = xa + 2 / b; b = xa + 1 / b;
        c = e / b / 2.506628274631;
      }
    }
    return x > 0 ? 1 - c : c;
  }
  const npdf = (x) => Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);

  function black76(F, K, T, sigma, r, kind) {
    const call = kind === "CE";
    const df = Math.exp(-r * T);
    if (T <= 0 || sigma <= 0) return df * Math.max(call ? F - K : K - F, 0);
    const sq = sigma * Math.sqrt(T);
    const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / sq;
    const d2 = d1 - sq;
    return call ? df * (F * ncdf(d1) - K * ncdf(d2)) : df * (K * ncdf(-d2) - F * ncdf(-d1));
  }
  function delta76(F, K, T, sigma, r, kind) {
    const call = kind === "CE";
    const df = Math.exp(-r * T);
    if (T <= 0 || sigma <= 0) {
      const itm = call ? F > K : F < K;
      return (call ? 1 : -1) * (itm ? df : 0);
    }
    const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * Math.sqrt(T));
    return call ? df * ncdf(d1) : -df * ncdf(-d1);
  }
  function impliedVol(price, F, K, T, r, kind) {
    if (price == null || !(price > 0) || T <= 0) return null;
    let lo = 1e-4, hi = 5.0;
    const intrinsic = black76(F, K, T, 1e-9, r, kind);
    if (price < intrinsic - 1e-6 || price > black76(F, K, T, hi, r, kind)) return null;
    for (let i = 0; i < 200; i++) {
      const mid = (lo + hi) / 2;
      if (black76(F, K, T, mid, r, kind) > price) hi = mid; else lo = mid;
      if (hi - lo < 1e-7) break;
    }
    return (lo + hi) / 2;
  }
  function wilson(wins, n, z) {
    z = z || 1.96;
    if (n === 0) return [null, null];
    const p = wins / n, den = 1 + z * z / n;
    const centre = (p + z * z / (2 * n)) / den;
    const half = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den;
    return [Math.max(0, centre - half), Math.min(1, centre + half)];
  }
  function noEdgeProbability(entry, stop, target) {
    const risk = Math.abs(entry - stop), reward = Math.abs(target - entry);
    return risk + reward > 0 ? risk / (risk + reward) : null;
  }

  // ------------------------------------------------------------------ charges (a port of india-costs)
  const DEFAULT_RATES = {
    stt_delivery_pct: 0.1, stamp_delivery_buy_pct: 0.015, exchange_txn_pct: { NSE: 0.00297, BSE: 0.00375 },
    sebi_fee_per_crore: 10.0, gst_pct: 18.0, dp_charge_per_scrip: 15.93, round_stt: true,
    fno: {
      stt_futures_sell_pct: 0.05, stt_options_sell_pct: 0.15, stt_options_exercise_pct: 0.15,
      exchange_txn_pct: { NFO: { FUT: 0.00183, OPT: 0.03553 }, BFO: { FUT: 0.0, OPT: 0.0325 } },
      stamp_futures_buy_pct: 0.002, stamp_options_buy_pct: 0.003, brokerage_per_order: 20.0,
    },
  };
  const rstt = (x) => Math.floor(x + 0.5);
  function fnoCosts(side, value, kind, exchange, rates, exerciseIntrinsic) {
    const R = rates || DEFAULT_RATES;
    const f = Object.assign({}, DEFAULT_RATES.fno, R.fno || {});
    side = String(side).toLowerCase(); kind = String(kind).toUpperCase(); exchange = String(exchange || "NFO").toUpperCase();
    value = Number(value);
    const brokerage = value > 0 ? Number(f.brokerage_per_order) : 0;
    let stt, stamp;
    if (kind === "FUT") {
      stt = side === "sell" ? value * f.stt_futures_sell_pct / 100 : 0;
      stamp = side === "buy" ? value * f.stamp_futures_buy_pct / 100 : 0;
    } else {
      stt = side === "sell" ? value * f.stt_options_sell_pct / 100 : 0;
      stamp = side === "buy" ? value * f.stamp_options_buy_pct / 100 : 0;
    }
    stt += (exerciseIntrinsic || 0) * f.stt_options_exercise_pct / 100;
    if (R.round_stt !== false) stt = rstt(stt);
    const ex = f.exchange_txn_pct[exchange] || f.exchange_txn_pct.NFO;
    const exch = value * Number(ex[kind] || 0) / 100;
    const sebi = value * Number(R.sebi_fee_per_crore == null ? 10 : R.sebi_fee_per_crore) / 1e7;
    const gst = (brokerage + exch + sebi) * Number(R.gst_pct == null ? 18 : R.gst_pct) / 100;
    const out = { brokerage: r2(brokerage), stt: r2(stt), exchange: r2(exch), sebi: r2(sebi), stamp: r2(stamp), gst: r2(gst), dp: 0 };
    out.total = r2(out.brokerage + out.stt + out.exchange + out.sebi + out.stamp + out.gst);
    return out;
  }
  function tradeCosts(side, value, exchange, rates) {   // delivery (CNC) shares
    const R = rates || DEFAULT_RATES;
    side = String(side).toLowerCase(); exchange = String(exchange || "NSE").toUpperCase();
    value = Number(value);
    let stt = value * R.stt_delivery_pct / 100;
    if (R.round_stt !== false) stt = rstt(stt);
    const txn = R.exchange_txn_pct[exchange] != null ? R.exchange_txn_pct[exchange] : R.exchange_txn_pct.NSE;
    const exch = value * txn / 100, sebi = value * R.sebi_fee_per_crore / 1e7;
    const stamp = side === "buy" ? value * R.stamp_delivery_buy_pct / 100 : 0;
    const gst = (exch + sebi) * R.gst_pct / 100;
    const dp = side === "sell" && value > 0 ? R.dp_charge_per_scrip : 0;
    const out = { brokerage: 0, stt: r2(stt), exchange: r2(exch), sebi: r2(sebi), stamp: r2(stamp), gst: r2(gst), dp: r2(dp) };
    out.total = r2(out.stt + out.exchange + out.sebi + out.stamp + out.gst + out.dp);
    return out;
  }
  function roundtripPoints(px, lot, rates, exchange) {   // futures, per unit
    const use = lot ? Math.round(lot) : Math.max(1, Math.round(1500000 / px));
    const rt = fnoCosts("buy", px * use, "FUT", exchange || "NFO", rates).total + fnoCosts("sell", px * use, "FUT", exchange || "NFO", rates).total;
    return [rt / use, use, !lot];
  }

  // ------------------------------------------------------------------ candles
  const num = (v) => { const n = parseFloat(String(v == null ? "" : v).replace(/,/g, "")); return isFinite(n) ? n : null; };

  /** INDmoney get_indian_stocks_ohlc rows -> clean daily bars [{date, open, high, low, close, volume}].
   *  opts.queryEnd: "YYYY-MM-DD HH:MM" (IST). A daily bar whose session had not closed by then is left out. */
  function loadCandles(rows, opts) {
    opts = opts || {};
    const dropped = [];
    const q = String(opts.queryEnd || "").replace("T", " ").slice(0, 16);
    const out = [], seen = new Set();
    for (const r of rows || []) {
      if (!r || typeof r !== "object") continue;
      let d = String(r.datetime_ist || r.date || r.datetime || "").trim();
      if (!d) continue;
      if (d.length >= 16 && (d.slice(11, 16) === "05:30" || d.slice(11, 16) === "00:00")) d = d.slice(0, 10);
      else if (d.length > 16) d = d.slice(0, 16);
      if (d.length === 10 && q && (d + " 15:30") > q) { dropped.push(d); continue; }
      const o = num(r.open), h = num(r.high), l = num(r.low), c = num(r.close);
      if (o == null || h == null || l == null || c == null || seen.has(d)) continue;
      if (h < Math.max(o, c) - 1e-9 || l > Math.min(o, c) + 1e-9) throw new TMError("bad candle on " + d + ": high/low do not contain open/close");
      seen.add(d);
      out.push({ date: d, open: o, high: h, low: l, close: c, volume: num(r.volume) || 0 });
    }
    out.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    if (out.length < 30) throw new TMError("only " + out.length + " candles: need at least 30 for the indicators");
    out.dropped = dropped;
    return out;
  }

  // ------------------------------------------------------------------ indicators
  function smaA(vals, n) {
    const out = new Array(vals.length).fill(null); let s = 0;
    for (let i = 0; i < vals.length; i++) { s += vals[i]; if (i >= n) s -= vals[i - n]; if (i >= n - 1) out[i] = s / n; }
    return out;
  }
  function emaA(vals, n) {
    const out = new Array(vals.length).fill(null), k = 2 / (n + 1); let e = null;
    for (let i = 0; i < vals.length; i++) {
      if (i < n - 1) continue;
      e = i === n - 1 ? sum(vals.slice(0, n)) / n : (vals[i] - e) * k + e;
      out[i] = e;
    }
    return out;
  }
  function trueRanges(c) {
    return c.map((b, i) => i === 0 ? b.high - b.low :
      Math.max(b.high - b.low, Math.abs(b.high - c[i - 1].close), Math.abs(b.low - c[i - 1].close)));
  }
  function wilderA(vals, n) {
    const out = new Array(vals.length).fill(null); let a = null;
    for (let i = 0; i < vals.length; i++) {
      if (i < n - 1) continue;
      a = i === n - 1 ? sum(vals.slice(0, n)) / n : (a * (n - 1) + vals[i]) / n;
      out[i] = a;
    }
    return out;
  }
  const atrA = (c, n) => wilderA(trueRanges(c), n || 14);
  function rsiA(closes, n) {
    n = n || 14;
    const gains = [], losses = [];
    for (let i = 1; i < closes.length; i++) { gains.push(Math.max(closes[i] - closes[i - 1], 0)); losses.push(Math.max(closes[i - 1] - closes[i], 0)); }
    const ag = wilderA(gains, n), al = wilderA(losses, n), out = [null];
    for (let i = 0; i < ag.length; i++) out.push(ag[i] === null ? null : (al[i] < EPS ? 100 : 100 - 100 / (1 + ag[i] / al[i])));
    return out;
  }
  function adxA(c, n) {
    n = n || 14;
    const pdm = [0], mdm = [0];
    for (let i = 1; i < c.length; i++) {
      const up = c[i].high - c[i - 1].high, dn = c[i - 1].low - c[i].low;
      pdm.push(up > dn && up > 0 ? up : 0); mdm.push(dn > up && dn > 0 ? dn : 0);
    }
    const tr = wilderA(trueRanges(c), n), sp = wilderA(pdm, n), sm = wilderA(mdm, n), dx = [];
    for (let i = 0; i < tr.length; i++) {
      if (tr[i] === null || tr[i] < EPS) { dx.push(null); continue; }
      const pdi = 100 * sp[i] / tr[i], mdi = 100 * sm[i] / tr[i];
      dx.push(pdi + mdi > EPS ? 100 * Math.abs(pdi - mdi) / (pdi + mdi) : 0);
    }
    const first = dx.findIndex((v) => v !== null), out = new Array(c.length).fill(null);
    if (first < 0) return out;
    const sd = wilderA(dx.slice(first), n);
    for (let j = 0; j < sd.length; j++) out[first + j] = sd[j];
    return out;
  }
  function supertrendA(c, n, mult) {
    n = n || 10; mult = mult || 3.0;
    const a = atrA(c, n), line = new Array(c.length).fill(null), dirn = new Array(c.length).fill(null);
    let fu = null, fl = null;
    for (let i = 0; i < c.length; i++) {
      if (a[i] === null) continue;
      const b = c[i], mid = (b.high + b.low) / 2, bu = mid + mult * a[i], bl = mid - mult * a[i];
      const pc = i ? c[i - 1].close : b.close;
      fu = (fu === null || bu < fu || pc > fu) ? bu : fu;
      fl = (fl === null || bl > fl || pc < fl) ? bl : fl;
      const prev = i ? dirn[i - 1] : null;
      let d;
      if (prev === null) d = b.close > mid ? 1 : -1;
      else if (prev === 1) d = b.close < fl ? -1 : 1;
      else d = b.close > fu ? 1 : -1;
      dirn[i] = d; line[i] = d === 1 ? fl : fu;
    }
    return [line, dirn];
  }
  function bbWidthA(closes, n, k) {
    n = n || 20; k = k || 2.0;
    const out = new Array(closes.length).fill(null);
    for (let i = n - 1; i < closes.length; i++) {
      const w = closes.slice(i - n + 1, i + 1), m = sum(w) / n;
      let v = 0; for (const x of w) v += (x - m) * (x - m);
      out[i] = m ? (2 * k * Math.sqrt(v / n)) / m : null;
    }
    return out;
  }
  function swingLow(c, i, look, wing) {
    look = look || 10; wing = wing || 2; let best = null;
    for (let j = Math.max(wing, i - look); j <= i - wing; j++) {
      const l = c[j].low; let ok = true;
      for (let k = 1; k <= wing && ok; k++) if (!(c[j - k].low >= l)) ok = false;
      for (let k = 1; k <= wing && ok; k++) if (j + k <= i && !(c[j + k].low >= l)) ok = false;
      if (ok) best = best === null ? l : Math.min(best, l);
    }
    return best;
  }
  function swingHigh(c, i, look, wing) {
    look = look || 10; wing = wing || 2; let best = null;
    for (let j = Math.max(wing, i - look); j <= i - wing; j++) {
      const h = c[j].high; let ok = true;
      for (let k = 1; k <= wing && ok; k++) if (!(c[j - k].high <= h)) ok = false;
      for (let k = 1; k <= wing && ok; k++) if (j + k <= i && !(c[j + k].high <= h)) ok = false;
      if (ok) best = best === null ? h : Math.max(best, h);
    }
    return best;
  }
  function indicators(c, cfg) {
    const k = cfgWith(cfg), closes = c.map((b) => b.close), vols = c.map((b) => b.volume);
    const [stLine, stDir] = supertrendA(c);
    return {
      close: closes, ema20: emaA(closes, 20), ema50: emaA(closes, 50), atr: atrA(c, k.atr_period),
      rsi: rsiA(closes, 14), adx: adxA(c, 14), st_line: stLine, st_dir: stDir, bbw: bbWidthA(closes),
      vol_avg: vols.some((v) => v) ? smaA(vols, 20) : new Array(c.length).fill(null),
    };
  }

  // ------------------------------------------------------------------ setups and levels
  function signalsAt(c, ind, i, enabled) {
    enabled = enabled || SETUPS;
    const out = [];
    if (i < 60 || ind.atr[i] === null || ind.ema50[i] === null) return out;
    const b = c[i], a = ind.atr[i], cl = ind.close;
    const e20 = ind.ema20[i], e50 = ind.ema50[i], r = ind.rsi[i], x = ind.adx[i] || 0;
    const e50Slope = e50 - ind.ema50[i - 5];
    const volOk = !!ind.vol_avg[i] && b.volume > 1.5 * ind.vol_avg[i];
    const add = (name, d, why, extra) => {
      const score = 1.0 + (extra || 0) + (x >= 25 ? 1.0 : 0) + (volOk ? 0.5 : 0);
      out.push({ setup: name, direction: d, score: rd(score, 2), why: why, bar: i, date: b.date, close: b.close, atr: a,
        adx: rd(x, 1), rsi: r !== null ? rd(r, 1) : null });
    };
    const f1 = (v) => v.toFixed(1), f0 = (v) => v.toFixed(0);
    if (enabled.includes("trend_pullback") && r !== null) {
      if (e20 > e50 && e50Slope > 0 && b.low <= e20 + 0.25 * a && b.close > e20 && b.close > b.open && r >= 40 && r <= 60)
        add("trend_pullback", "long", "uptrend (EMA20 " + f1(e20) + " > EMA50 " + f1(e50) + ", EMA50 rising), pulled back to EMA20 and closed back above it, RSI " + f0(r));
      if (e20 < e50 && e50Slope < 0 && b.high >= e20 - 0.25 * a && b.close < e20 && b.close < b.open && r >= 40 && r <= 60)
        add("trend_pullback", "short", "downtrend (EMA20 " + f1(e20) + " < EMA50 " + f1(e50) + ", EMA50 falling), rallied to EMA20 and closed back below it, RSI " + f0(r));
    }
    if (enabled.includes("breakout")) {
      let hi20 = -Infinity, lo20 = Infinity;
      for (let j = i - 20; j < i; j++) { hi20 = Math.max(hi20, c[j].high); lo20 = Math.min(lo20, c[j].low); }
      if (b.close > hi20 && x >= 20) add("breakout", "long", "closed above the 20-day high " + hi20.toFixed(2) + " with ADX " + f0(x), volOk ? 0.5 : 0);
      if (b.close < lo20 && x >= 20) add("breakout", "short", "closed below the 20-day low " + lo20.toFixed(2) + " with ADX " + f0(x), volOk ? 0.5 : 0);
    }
    if (enabled.includes("supertrend_flip") && ind.st_dir[i] !== null && ind.st_dir[i - 1] !== null) {
      if (ind.st_dir[i] === 1 && ind.st_dir[i - 1] === -1 && b.close > e50) add("supertrend_flip", "long", "Supertrend(10,3) turned up and the close is above EMA50");
      if (ind.st_dir[i] === -1 && ind.st_dir[i - 1] === 1 && b.close < e50) add("supertrend_flip", "short", "Supertrend(10,3) turned down and the close is below EMA50");
    }
    if (enabled.includes("squeeze") && ind.bbw[i - 1] !== null) {
      const window = ind.bbw.slice(i - 60, i).filter((w) => w !== null);
      if (window.length && ind.bbw[i - 1] <= Math.min.apply(null, window) * 1.05) {
        const seg = cl.slice(i - 19, i + 1), m = sum(seg) / 20;
        let v = 0; for (const z of seg) v += (z - m) * (z - m);
        const sd = Math.sqrt(v / 20);
        if (b.close > m + 2 * sd) add("squeeze", "long", "Bollinger width at a 60-bar low, then a close above the upper band");
        else if (b.close < m - 2 * sd) add("squeeze", "short", "Bollinger width at a 60-bar low, then a close below the lower band");
      }
    }
    return out;
  }

  function levels(c, ind, i, direction, cfg, entry, resistance, support) {
    const k = cfgWith(cfg), a = ind.atr[i], long_ = direction === "long";
    const e = Number(entry != null ? entry : c[i].close);
    const swing = long_ ? swingLow(c, i) : swingHigh(c, i);
    let struct = null;
    if (swing !== null) struct = long_ ? swing - k.sl_buffer_atr * a : swing + k.sl_buffer_atr * a;
    const atrStop = long_ ? e - k.sl_atr_mult * a : e + k.sl_atr_mult * a;
    let stop = atrStop, basis = k.sl_atr_mult.toFixed(1) + " x ATR";
    if (struct !== null) {
      const dist = long_ ? e - struct : struct - e;
      if (k.sl_min_atr * a <= dist && dist <= k.sl_max_atr * a) { stop = struct; basis = "beyond the swing " + (long_ ? "low" : "high") + " " + swing.toFixed(2); }
    }
    const risk = Math.abs(e - stop), sgn = long_ ? 1 : -1;
    const tp1 = e + sgn * k.tp1_r * risk;
    let tp2 = e + sgn * k.tp2_r * risk;
    const notes = [];
    const wall = long_ ? resistance : support;
    if (wall != null && ((long_ && tp1 < wall && wall < tp2) || (!long_ && tp2 < wall && wall < tp1))) {
      tp2 = wall - sgn * 0.1 * a;
      notes.push("TP2 pulled in ahead of " + (long_ ? "resistance" : "support") + " " + wall.toFixed(2));
    }
    const rewardR = risk ? Math.abs(tp2 - e) / risk : 0;
    let ok = rewardR >= k.min_reward_r - 1e-9;
    if (!ok) notes.push("reward to TP2 is only " + rewardR.toFixed(2) + "R (minimum " + k.min_reward_r.toFixed(1) + "R): skip, or wait for a better entry");
    if (wall != null && ((long_ && e < wall && wall <= tp1) || (!long_ && tp1 <= wall && wall < e))) {
      ok = false; notes.push((long_ ? "resistance" : "support") + " " + wall.toFixed(2) + " sits before TP1: the trade has no room");
    }
    return {
      direction: direction, entry: r2(e), stop: r2(stop), stop_basis: basis, risk_points: r2(risk), tp1: r2(tp1), tp2: r2(tp2),
      reward_r: r2(rewardR), atr: r2(a),
      trail: { after_tp1_atr: k.trail_atr_mult, after_tp2_atr: k.trail_after_tp2_atr_mult, tp1_exit_fraction: k.tp1_exit_fraction, time_stop_bars: k.time_stop_bars },
      no_edge_p_tp1: rd(noEdgeProbability(e, stop, tp1), 3), no_edge_p_tp2: rd(noEdgeProbability(e, stop, tp2), 3), ok: ok, notes: notes,
    };
  }

  // ------------------------------------------------------------------ the exit engine
  /** One path through the exit rules. bars: [[open, high, low, close, atrBefore], ...] from the session after the
   *  signal. t: {direction, stop, tp1, tp2, risk, frac, limit?}. Mirrors fnocore.trail (held to it by the tests). */
  function runTrade(bars, t, k, horizon) {
    const long_ = t.direction === "long", sgn = long_ ? 1 : -1;
    let stop = t.stop;
    const tp1 = t.tp1, tp2 = t.tp2, o0 = bars[0][0];
    if (long_ ? (o0 <= stop || o0 >= tp1) : (o0 >= stop || o0 <= tp1)) return { cls: "gap", entered: false };
    let entry = o0, targetsOnBar0 = true;
    const lim = t.limit;
    if (lim != null) {
      const h0 = bars[0][1], l0 = bars[0][2];
      if (long_ ? o0 <= lim : o0 >= lim) entry = o0;
      else if (long_ ? l0 <= lim : h0 >= lim) { entry = lim; targetsOnBar0 = false; }
      else return { cls: "nofill", entered: false };
    }
    let best = entry, stage = "open", held = 0, lo = entry, hi = entry;
    let tp1Bar = null, tp1Px = tp1, tp2Bar = null, exitPx = null, exitBar = null, cls = null, gapped = false;
    const end = horizon == null ? bars.length : Math.min(horizon, bars.length);
    const m1 = k.trail_atr_mult, m2 = k.trail_after_tp2_atr_mult, tstop = k.time_stop_bars;
    for (let n = 0; n < end; n++) {
      const b = bars[n], o = b[0], h = b[1], l = b[2], c = b[3], a = b[4];
      held++;
      if (l < lo) lo = l;
      if (h > hi) hi = h;
      if (long_ ? l <= stop : h >= stop) {
        gapped = long_ ? o <= stop : o >= stop;
        exitPx = gapped ? o : stop; exitBar = n;
        cls = stage === "open" ? "stop" : stage === "tp1" ? (Math.abs(stop - entry) < 0.005 ? "tp1_be" : "tp1_trail") : "tp2_trail";
        break;
      }
      if (n === 0 && !targetsOnBar0) continue;
      if (stage === "open" && (long_ ? h >= tp1 : l <= tp1)) {
        stage = "tp1"; tp1Bar = n;
        tp1Px = (long_ ? o >= tp1 : o <= tp1) ? o : tp1;
        if (t.frac >= 1.0) { exitPx = tp1Px; exitBar = n; cls = "tp1_all"; break; }
        if ((long_ && entry > stop) || (!long_ && entry < stop)) stop = entry;
      }
      if (stage === "tp1" && (long_ ? h >= tp2 : l <= tp2)) { stage = "tp2"; tp2Bar = n; }
      best = long_ ? Math.max(best, h) : Math.min(best, l);
      if (stage === "tp1" || stage === "tp2") {
        const nw = r2(best - sgn * (stage === "tp1" ? m1 : m2) * a);
        if ((long_ && nw > stop + 0.004) || (!long_ && nw < stop - 0.004)) stop = nw;
      } else if (held >= tstop) { exitPx = c; exitBar = n; cls = "time"; break; }
    }
    if (cls === null) { exitPx = bars[end - 1][3]; exitBar = end - 1; cls = "horizon"; }
    const risk = t.risk, frac = tp1Bar !== null ? t.frac : 0;
    const pts = frac * sgn * (tp1Px - entry) + (1 - frac) * sgn * (exitPx - entry);
    return {
      cls: cls, entered: true, entry: entry, exit: exitPx, exit_bar: exitBar, bars: held, tp1_bar: tp1Bar, tp1_px: tp1Px,
      tp2_bar: tp2Bar, gapped_stop: gapped && cls === "stop", pts: pts, r: risk ? pts / risk : 0,
      mae_r: risk ? (sgn * (entry - (long_ ? lo : hi))) / risk : 0, mfe_r: risk ? (sgn * ((long_ ? hi : lo) - entry)) / risk : 0, risk: risk,
    };
  }

  // ------------------------------------------------------------------ reading the chart
  function percentileRank(series, value) {
    const vals = series.filter((v) => v !== null);
    if (!vals.length || value === null) return null;
    return 100 * vals.filter((v) => v <= value).length / vals.length;
  }
  function chartRead(c, ind, an) {
    const i = c.length - 1, cl = c.map((b) => b.close);
    const rets = [];
    for (let j = Math.max(1, i - 19); j <= i; j++) if (cl[j - 1] > 0) rets.push(Math.log(cl[j] / cl[j - 1]));
    const rv = rets.length ? Math.sqrt(sum(rets.map((x) => x * x)) / rets.length) * Math.sqrt(TRADING_DAYS) : null;
    const e20 = ind.ema20[i], e50 = ind.ema50[i], e50p = i >= 5 ? ind.ema50[i - 5] : null;
    const year = c.slice(-250);
    const hi52 = Math.max.apply(null, year.map((b) => b.high)), lo52 = Math.min.apply(null, year.map((b) => b.low));
    const chg = (n) => (i >= n && cl[i - n] ? rd(100 * (cl[i] / cl[i - n] - 1), 2) : null);
    const out = {
      date: c[i].date, close: cl[i], change_pct: { "1": chg(1), "5": chg(5), "20": chg(20) },
      trend: e20 > e50 ? "up" : "down", ema50_slope: e50p !== null && e50 > e50p ? "rising" : "falling",
      ema20: r2(e20), ema50: r2(e50), adx: ind.adx[i] !== null ? rd(ind.adx[i], 1) : null, rsi: ind.rsi[i] !== null ? rd(ind.rsi[i], 1) : null,
      supertrend: ind.st_dir[i] === 1 ? "up" : "down", atr: r2(ind.atr[i]), atr_pct: r2(100 * ind.atr[i] / cl[i]),
      atr_percentile: Math.round(percentileRank(ind.atr.slice(-250), ind.atr[i])),
      bb_width_percentile: ind.bbw[i] !== null ? Math.round(percentileRank(ind.bbw.slice(-250), ind.bbw[i])) : null,
      realized_vol_20d_pct: rv ? r2(100 * rv) : null, high_52w: hi52, low_52w: lo52, from_high_pct: r2(100 * (cl[i] / hi52 - 1)),
      swing_low: swingLow(c, i), swing_high: swingHigh(c, i), bars: c.length,
    };
    if (an) {
      out.chain = {};
      for (const kk of ["expiry", "days_to_expiry", "atm_iv_pct", "expected_move_1sd", "pcr_oi", "max_pain", "resistance", "support"]) out.chain[kk] = an[kk];
    }
    return out;
  }

  // ------------------------------------------------------------------ the trade
  function tradeFrom(c, ind, k, direction, setup, entry, sl, tp1, tp2, an) {
    const i = c.length - 1, sigs = signalsAt(c, ind, i);
    let fired = null;
    if (direction == null) {
      const cand = sigs.filter((s) => !setup || s.setup === setup);
      if (!cand.length) throw new TMError((setup ? setup + " did not fire" : "no setup fired") + " on the last bar (" + c[i].date + "): choose long or short to replay a trade of your own");
      fired = cand.reduce((a, b) => (b.score > a.score ? b : a));
      direction = fired.direction;
    } else {
      const same = sigs.filter((s) => s.direction === direction && (!setup || s.setup === setup));
      fired = same.length ? same.reduce((a, b) => (b.score > a.score ? b : a)) : null;
    }
    const lv = levels(c, ind, i, direction, k, entry, (an || {}).resistance, (an || {}).support);
    const long_ = direction === "long", sgn = long_ ? 1 : -1, e = lv.entry;
    const stop = sl != null ? Number(sl) : lv.stop;
    if ((long_ && stop >= e) || (!long_ && stop <= e)) throw new TMError("the stop " + stop + " is on the wrong side of the entry " + e + " for a " + direction);
    const risk = Math.abs(e - stop);
    const t1 = tp1 != null ? Number(tp1) : (sl != null ? e + sgn * k.tp1_r * risk : lv.tp1);
    const t2 = tp2 != null ? Number(tp2) : (sl != null ? e + sgn * k.tp2_r * risk : lv.tp2);
    if (sgn * (t1 - e) <= 0 || sgn * (t2 - t1) < 0) throw new TMError("targets must lie beyond the entry in the trade's direction, TP2 at or beyond TP1");
    const custom = sl != null || tp1 != null || tp2 != null, a = ind.atr[i];
    const notes = custom ? [] : lv.notes.slice();
    if (setup && !fired) notes.push(setup + " did not fire on the last bar; its past " + direction + " occurrences are replayed anyway");
    const limit = entry != null ? Number(entry) : null;
    return {
      direction: direction, setup: setup || (fired ? fired.setup : null), why: fired ? fired.why : null, date: c[i].date, entry_plan: e,
      stop: r2(stop), limit: limit, limit_atr: limit !== null && a ? (limit - c[i].close) / a : null, tp1: r2(t1), tp2: r2(t2),
      risk: r2(risk), atr: r2(a), stop_atr: a ? risk / a : null, tp1_r: Math.abs(t1 - e) / risk, tp2_r: Math.abs(t2 - e) / risk,
      custom_levels: custom, notes: notes, ok: custom ? true : lv.ok,
      no_edge_p_tp1: noEdgeProbability(e, stop, t1), no_edge_p_tp2: noEdgeProbability(e, stop, t2),
    };
  }

  // ------------------------------------------------------------------ method 1: the chart's own history
  function barsFrom(c, ind, start) {
    const out = [];
    for (let j = start; j < c.length; j++) out.push([c[j].open, c[j].high, c[j].low, c[j].close, ind.atr[j - 1] || ind.atr[j]]);
    return out;
  }
  function replay(c, ind, k, tr, frac, horizon, conditioned) {
    const out = []; let stillOpen = 0;
    const sgn = tr.direction === "long" ? 1 : -1;
    for (let i = 60; i < c.length - 1; i++) {
      if (conditioned) {
        const sigs = signalsAt(c, ind, i);
        if (!sigs.some((s) => s.direction === tr.direction && (tr.setup == null || s.setup === tr.setup))) continue;
      }
      const a = ind.atr[i];
      let e = c[i].close;
      const lim = tr.limit_atr != null ? e + tr.limit_atr * a : null;
      if (lim !== null) e = lim;
      let t;
      if (tr.custom_levels) {
        const risk = tr.stop_atr * a;
        t = { direction: tr.direction, stop: e - sgn * risk, tp1: e + sgn * tr.tp1_r * risk, tp2: e + sgn * tr.tp2_r * risk, risk: risk, frac: frac, limit: lim };
      } else {
        const lv = levels(c, ind, i, tr.direction, k, lim);
        t = { direction: tr.direction, stop: lv.stop, tp1: lv.tp1, tp2: lv.tp2, risk: lv.risk_points, frac: frac, limit: lim };
      }
      const res = runTrade(barsFrom(c, ind, i + 1), t, k, horizon);
      if (res.cls === "horizon" && res.exit_bar < horizon - 1) { stillOpen++; continue; }
      res.date = c[i].date; res.start = i + 1;
      out.push(res);
    }
    return [out, stillOpen];
  }
  function episodes(results) {
    let n = 0, freeFrom = -1;
    for (const r of results.slice().sort((a, b) => a.start - b.start)) {
      if (r.start > freeFrom) { n++; freeFrom = r.start + (r.entered ? r.exit_bar : 0); }
    }
    return n;
  }

  // ------------------------------------------------------------------ method 2: block bootstrap of real days
  function barLibrary(c, ind, keepDrift) {
    const lib = [];
    for (let j = 15; j < c.length; j++) {
      const a = ind.atr[j - 1];
      if (!a) continue;
      const pc = c[j - 1].close;
      lib.push([(c[j].open - pc) / a, (c[j].high - pc) / a, (c[j].low - pc) / a, (c[j].close - pc) / a]);
    }
    if (!lib.length) throw new TMError("not enough history to build the bar library");
    if (!keepDrift) {
      const mu = sum(lib.map((x) => x[3])) / lib.length;
      return lib.map((x) => [x[0] - mu, x[1] - mu, x[2] - mu, x[3] - mu]);
    }
    return lib;
  }
  function resample(rng, lib, block) {
    const out = []; let j = rng.randrange(lib.length);
    while (out.length < lib.length) { out.push(lib[j]); j = rng.random() < 1 / block ? rng.randrange(lib.length) : (j + 1) % lib.length; }
    return out;
  }
  function bootstrapPath(rng, lib, close0, atr0, n, k, block) {
    const out = []; let pc = close0, a = atr0, j = rng.randrange(lib.length);
    const p = 1 / block, period = k.atr_period;
    for (let i = 0; i < n; i++) {
      const g = lib[j];
      const o = pc + g[0] * a, hh = pc + g[1] * a, ll = pc + g[2] * a, cc = pc + g[3] * a;
      out.push([o, hh, ll, cc, a]);
      const tr = Math.max(hh - ll, Math.abs(hh - pc), Math.abs(ll - pc));
      a = (a * (period - 1) + tr) / period; pc = cc;
      j = rng.random() < p ? rng.randrange(lib.length) : (j + 1) % lib.length;
    }
    return out;
  }
  function neutralise(lib, close0, atr0, hz, k, block, seed, n, rounds) {
    n = n || 3000; rounds = rounds == null ? 2 : rounds;
    const rng = makeRng(seed + "-neutral");
    for (let r = 0; r < rounds; r++) {
      let tot = 0;
      for (let i = 0; i < n; i++) { const p = bootstrapPath(rng, lib, close0, atr0, hz, k, block); tot += p[p.length - 1][3] - close0; }
      const d = tot / n / (hz * atr0);
      lib = lib.map((x) => [x[0] - d, x[1] - d, x[2] - d, x[3] - d]);
    }
    return lib;
  }
  function historySe(lib, close0, atr0, hz, k, block, seed, t, costOf, keepDrift, R, M) {
    R = R || 20; M = M || 800;
    const rng = makeRng(seed + "-history"), exps = [], noise = [];
    for (let r = 0; r < R; r++) {
      let lb = resample(rng, lib, block);
      if (!keepDrift) {
        const mu = sum(lb.map((x) => x[3])) / lb.length;
        lb = neutralise(lb.map((x) => [x[0] - mu, x[1] - mu, x[2] - mu, x[3] - mu]), close0, atr0, hz, k, block, seed + "-" + r, Math.max(200, M), 1);
      }
      const rs = [];
      for (let i = 0; i < M; i++) {
        const res = runTrade(bootstrapPath(rng, lb, close0, atr0, hz, k, block), t, k, hz);
        if (res.entered) rs.push((res.pts - costOf(res)) / (res.risk || t.risk));
      }
      if (rs.length > 1) {
        const mu = sum(rs) / rs.length; exps.push(mu);
        noise.push(sum(rs.map((x) => (x - mu) * (x - mu))) / (rs.length - 1) / rs.length);
      }
    }
    if (exps.length < 3) return null;
    const m = sum(exps) / exps.length;
    const between = sum(exps.map((x) => (x - m) * (x - m))) / (exps.length - 1);
    return Math.sqrt(Math.max(between - sum(noise) / noise.length, 0));
  }

  // ------------------------------------------------------------------ method 3: the volatility the options price
  function overnightShare(c) {
    let gap = 0, intra = 0;
    for (let j = 1; j < c.length; j++) {
      const pc = c[j - 1].close, o = c[j].open, cl = c[j].close;
      if (pc > 0 && o > 0 && cl > 0) { gap += Math.pow(Math.log(o / pc), 2); intra += Math.pow(Math.log(cl / o), 2); }
    }
    return gap + intra > 0 ? gap / (gap + intra) : 0.2;
  }
  function impliedPath(rng, close0, atr0, sigma, phi, n, k, dts) {
    const out = []; let pc = close0, a = atr0;
    const period = k.atr_period, one = dts ? Math.min.apply(null, dts) : 1 / TRADING_DAYS;
    for (let i = 0; i < n; i++) {
      const dt = dts ? dts[i] : 1 / TRADING_DAYS;
      const vi = sigma * sigma * (1 - phi) * one, vg = Math.max(sigma * sigma * dt - vi, 0);
      const g = rng.gauss() * Math.sqrt(vg) - 0.5 * vg, x = rng.gauss() * Math.sqrt(vi) - 0.5 * vi;
      const u1 = 1 - rng.random(), u2 = 1 - rng.random();
      const mx = 0.5 * (x + Math.sqrt(x * x - 2 * vi * Math.log(u1))), mn = 0.5 * (x - Math.sqrt(x * x - 2 * vi * Math.log(u2)));
      const o = pc * Math.exp(g), hh = o * Math.exp(mx), ll = o * Math.exp(mn), cc = o * Math.exp(x);
      out.push([o, hh, ll, cc, a]);
      const tr = Math.max(hh - ll, Math.abs(hh - pc), Math.abs(ll - pc));
      a = (a * (period - 1) + tr) / period; pc = cc;
    }
    return out;
  }

  // ------------------------------------------------------------------ dates and the calendar
  const DAY = 86400000, IST_MS = 5.5 * 3600000;
  const isoMs = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
  const msIso = (ms) => new Date(ms).toISOString().slice(0, 10);
  function tradingDaysAfter(day, n, holidays) {
    const out = []; let d = isoMs(day);
    while (out.length < n) {
      d += DAY;
      const w = new Date(d).getUTCDay(), iso = msIso(d);
      if (w !== 0 && w !== 6 && !(holidays && holidays.has && holidays.has(iso))) out.push(iso);
    }
    return out;
  }
  /** Calendar years from `nowMs` (epoch ms) to 15:30 IST on the expiry (YYYYMMDD or YYYY-MM-DD). */
  function yearsToExpiry(expiry, nowMs) {
    const e = String(expiry).replace(/-/g, "").slice(0, 8);
    const end = Date.UTC(+e.slice(0, 4), +e.slice(4, 6) - 1, +e.slice(6, 8), 10, 0, 0);   // 15:30 IST = 10:00 UTC
    return Math.max((end - nowMs) / 1000, 0) / (365 * 86400);
  }
  const expiryIso = (e) => { e = String(e).replace(/-/g, ""); return e.slice(0, 4) + "-" + e.slice(4, 6) + "-" + e.slice(6, 8); };

  // ------------------------------------------------------------------ option chains (INDmoney)
  function legIndmoney(d) {
    if (!d) return null;
    const bid = num(d.buy_price != null ? d.buy_price : d.bp), ask = num(d.sell_price != null ? d.sell_price : d.sp);
    const iv = (num(d.iv) || 0) / 100;
    return { id: d.derivative_id, ltp: num(d.current_price), bid: bid || null, ask: ask || null, oi: num(d.oi) || 0,
      oi_chg_pct: num(d.oi_change), volume: num(d.volume != null ? d.volume : d.v) || 0, iv: iv || null, lot_size: d.lot_size,
      delta: num(d.delta) };
  }
  /** INDmoney get_indian_stocks_option_chain output (object or its JSON text) -> a normalised chain. */
  function parseChain(obj, underlying) {
    if (typeof obj === "string") obj = JSON.parse(obj);
    if (obj && typeof obj.result === "string") obj = JSON.parse(obj.result);
    if (!obj || !obj.option_chain_data) throw new TMError("not an INDmoney option chain");
    const ent = obj.entity_details || {};
    let lot = null;
    const rows = obj.option_chain_data.map((r) => {
      const ce = legIndmoney(r.call_data), pe = legIndmoney(r.put_data);
      lot = lot || (ce && ce.lot_size) || (pe && pe.lot_size);
      return { strike: Number(r.strike_price), ce: ce, pe: pe };
    });
    let name = String(underlying || ent.name || "").toUpperCase().replace(" 50", "").replace("NIFTY BANK", "BANKNIFTY");
    if (name === "BANK NIFTY") name = "BANKNIFTY";
    return {
      source: "indmoney", underlying: name.replace(/ /g, ""), spot: num(ent.live_price), spot_time: ent.price_updated_at_ist || null,
      expiry: obj.selected_expiry, expiry_list: obj.expiry_list || [], lot_size: lot ? Math.round(lot) : null,
      provider_pcr: num(ent.overall_pcr_string), strikes: rows.sort((a, b) => a.strike - b.strike), retrieved: obj.retrieved_at_ist || null,
    };
  }
  /** The chain's price time as epoch ms, else null. */
  function chainTimeMs(chain) {
    const t = chain && chain.spot_time;
    if (!t) return null;
    const m = String(t).replace(" ", "T");
    const ms = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(m) ? m : m + "+05:30");
    return isFinite(ms) ? ms : null;
  }
  const legMid = (leg) => !leg ? null : (leg.bid && leg.ask ? (leg.bid + leg.ask) / 2 : leg.ltp);
  function chainAnalytics(chain, nowMs, r) {
    r = r == null ? 0.065 : r;
    const T = yearsToExpiry(chain.expiry, nowMs), rows = chain.strikes, S = chain.spot;
    if (!rows.length || !S) throw new TMError("the chain has no strikes or no underlying price");
    const both = rows.filter((x) => legMid(x.ce) && legMid(x.pe));
    const atm = (both.length ? both : rows).reduce((a, b) => (Math.abs(b.strike - S) < Math.abs(a.strike - S) ? b : a));
    const F = both.length && T > 0 ? atm.strike + (legMid(atm.ce) - legMid(atm.pe)) * Math.exp(r * T) : S;
    chain.forward = F;
    for (const x of rows) {
      for (const kind of ["CE", "PE"]) {
        const leg = kind === "CE" ? x.ce : x.pe;
        if (!leg) continue;
        const m = legMid(leg);
        leg.iv = leg.iv || (m ? impliedVol(m, F, x.strike, T, r, kind) : null);
      }
    }
    const oiOf = (leg) => (leg && leg.oi) || 0;
    const strikes = rows.map((x) => x.strike);
    const pain = (settle) => { let tot = 0; for (const x of rows) tot += oiOf(x.ce) * Math.max(settle - x.strike, 0) + oiOf(x.pe) * Math.max(x.strike - settle, 0); return tot; };
    let maxPain = strikes[0], bestPain = Infinity;
    for (const s of strikes) { const p = pain(s); if (p < bestPain) { bestPain = p; maxPain = s; } }
    const above = rows.filter((x) => x.strike > S), below = rows.filter((x) => x.strike < S);
    const top = (arr, f) => arr.reduce((a, b) => (f(b) > f(a) ? b : a));
    const res = above.length ? top(above, (x) => oiOf(x.ce)).strike : null, sup = below.length ? top(below, (x) => oiOf(x.pe)).strike : null;
    let atmIv = null;
    if (atm.ce && atm.pe && atm.ce.iv && atm.pe.iv) atmIv = (atm.ce.iv + atm.pe.iv) / 2;
    const callOi = sum(rows.map((x) => oiOf(x.ce))), putOi = sum(rows.map((x) => oiOf(x.pe)));
    return {
      underlying: chain.underlying, expiry: chain.expiry, days_to_expiry: r2(T * 365), spot: S, forward: r2(F), atm_strike: atm.strike,
      atm_iv_pct: atmIv ? r2(100 * atmIv) : null, expected_move_1sd: atmIv ? rd(F * atmIv * Math.sqrt(T), 1) : null,
      atm_straddle: r2((legMid(atm.ce) || 0) + (legMid(atm.pe) || 0)), pcr_oi: callOi ? rd(putOi / callOi, 3) : null,
      max_pain: maxPain, call_wall: top(rows, (x) => oiOf(x.ce)).strike, put_wall: top(rows, (x) => oiOf(x.pe)).strike,
      resistance: res, support: sup, strikes_shown: rows.length,
    };
  }
  /** The strike india-fno-plan would buy: liquid, near the target delta. chain must have been through chainAnalytics. */
  function pickOption(direction, chain, cfg, nowMs, lot, underlying) {
    const k = cfgWith(cfg), kind = direction === "long" ? "CE" : "PE", T = yearsToExpiry(chain.expiry, nowMs), r = k.risk_free_rate;
    const F = chain.forward || chain.spot, cands = [];
    for (const row of chain.strikes) {
      const leg = kind === "CE" ? row.ce : row.pe;
      if (!leg || !leg.ltp) continue;
      const bid = leg.bid, ask = leg.ask, mid = bid && ask ? (bid + ask) / 2 : leg.ltp;
      const spreadPct = bid && ask && mid ? 100 * (ask - bid) / mid : null;
      const iv = leg.iv || impliedVol(mid, F, row.strike, T, r, kind);
      if (!iv) continue;
      const dl = delta76(F, row.strike, T, iv, r, kind);
      const liquid = spreadPct !== null && spreadPct <= k.option_max_spread_pct && (leg.oi || 0) >= k.option_min_oi;
      cands.push({ strike: row.strike, kind: kind, ltp: leg.ltp, bid: bid, ask: ask, mid: mid, iv: iv, delta: dl, spread_pct: spreadPct, oi: leg.oi, liquid: liquid, id: leg.id });
    }
    const liquid = cands.filter((x) => x.liquid);
    if (!liquid.length) throw new TMError("no liquid " + kind + " strike in the chain (spread under " + k.option_max_spread_pct.toFixed(1) + "% and OI over " + k.option_min_oi + ")");
    const pick = liquid.reduce((a, b) => {
      const da = Math.abs(Math.abs(a.delta) - k.option_target_delta), db = Math.abs(Math.abs(b.delta) - k.option_target_delta);
      return db < da || (db === da && b.spread_pct < a.spread_pct) ? b : a;
    });
    const lt = lot || chain.lot_size || INDEX_LOTS[String(underlying || "").toUpperCase()];
    if (!lt) throw new TMError("no lot size for " + (underlying || "it") + ": it is on the option chain");
    return {
      expiry: chain.expiry, dte: r2(T * 365), kind: kind, strike: pick.strike, delta: rd(pick.delta, 3), iv_pct: r2(100 * pick.iv),
      spread_pct: r2(pick.spread_pct), oi: pick.oi, forward: r2(F), entry_premium: r2(pick.ask || pick.ltp), bid: pick.bid, mid: pick.mid,
      lot_size: Math.round(lt), derivative_id: pick.id || null,
    };
  }

  // ------------------------------------------------------------------ the option leg
  /** A bought option repriced with Black-76 along a path: the chain's own IV (sticky strike), the NSE/BSE calendar,
   *  today's basis held constant, the ask to buy, the bid to sell, intrinsic value at expiry. */
  class OptionLeg {
    constructor(op, chain, sessions, nowMs, k, exitIv, rates, exchange) {
      this.kind = op.kind; this.K = Number(op.strike); this.lot = Math.round(op.lot_size);
      this.iv = Math.max(op.iv_pct / 100, 0.01);
      this.exitIv = exitIv ? Math.max(exitIv, 0.01) : this.iv;
      this.quote = Number(op.entry_premium); this.r = k.risk_free_rate;
      const exp = String(op.expiry).replace(/-/g, "");
      this.expDay = exp.slice(0, 4) + "-" + exp.slice(4, 6) + "-" + exp.slice(6, 8);
      this.T0 = yearsToExpiry(exp, nowMs); this.spot = chain.spot;
      this.basis = (chain.forward || chain.spot) - chain.spot;
      const sp = Number(op.spread_pct || 0);
      this.spread = this.quote * sp / (100 + sp / 2);
      const e = isoMs(this.expDay);
      this.Ts = sessions.map((d) => Math.max((e - isoMs(d)) / DAY / 365, 0));
      this.Topen0 = this.Ts.length ? this.Ts[0] + 6.25 / 24 / 365 : 0;
      let bte = null;
      for (let n = 0; n < sessions.length; n++) if (sessions[n] >= this.expDay) { bte = n + 1; break; }
      this.barsToExpiry = bte;
      this.rates = rates; this.exchange = exchange || "NFO";
      this.offset = this.quote - this.model(this.spot, this.T0);
    }
    model(level, T, iv) { return black76(level + this.basis, this.K, T, iv || this.iv, this.r, this.kind); }
    premium(level, bar) {
      const T = this.Ts[Math.min(bar, this.Ts.length - 1)], p = this.model(level, T, this.exitIv);
      return T <= 0 ? p : Math.max(p + this.offset - this.spread, 0.05);
    }
    entryPremium(level) { return Math.max(this.model(level, this.Topen0) + this.offset, 0.05); }
    /** Rupees per lot for one path, after charges: the buy, and each sell order (TP1's part, the exit) on its own. */
    pnl(res, tr) {
      if (!res.entered) return null;
      const entry = this.entryPremium(res.entry), frac = res.tp1_bar !== null ? tr.frac : 0;
      const pExit = this.premium(res.exit, res.exit_bar), pTp1 = res.tp1_bar !== null ? this.premium(res.tp1_px, res.tp1_bar) : 0;
      const gross = (frac * (pTp1 - entry) + (1 - frac) * (pExit - entry)) * this.lot;
      let ch = fnoCosts("buy", entry * this.lot, "OPT", this.exchange, this.rates).total;
      if (frac > 0) ch += fnoCosts("sell", frac * pTp1 * this.lot, "OPT", this.exchange, this.rates).total;
      if (1 - frac > 0) ch += fnoCosts("sell", (1 - frac) * pExit * this.lot, "OPT", this.exchange, this.rates).total;
      return gross - ch;
    }
  }

  // ------------------------------------------------------------------ statistics
  function quantiles(xs, qs) {
    qs = qs || [0.05, 0.25, 0.5, 0.75, 0.95];
    if (!xs.length) return {};
    const s = xs.slice().sort((a, b) => a - b), out = {};
    for (const q of qs) {
      const pos = q * (s.length - 1), lo = Math.floor(pos), hi = Math.min(lo + 1, s.length - 1);
      out["p" + Math.round(100 * q)] = s[lo] + (s[hi] - s[lo]) * (pos - lo);
    }
    return out;
  }
  function summarise(results, money, exactCi) {
    const n = results.length;
    if (!n) return { n: 0 };
    const counts = {};
    for (const r of results) counts[r.cls] = (counts[r.cls] || 0) + 1;
    const outcomes = {};
    for (const [key] of OUTCOMES) {
      const m = counts[key] || 0;
      if (!m && (key === "expiry" || key === "horizon" || key === "nofill" || key === "tp1_all")) continue;
      const p = m / n;
      let lo, hi;
      if (exactCi) [lo, hi] = wilson(m, n);
      else { const se = Math.sqrt(p * (1 - p) / n); lo = Math.max(p - 1.96 * se, 0); hi = Math.min(p + 1.96 * se, 1); }
      const rows = results.filter((r) => r.cls === key);
      outcomes[key] = { count: m, p: rd(p, 4), ci95: [rd(lo, 4), rd(hi, 4)],
        avg_r: m && rows[0].entered ? rd(sum(rows.map((r) => r.r)) / m, 3) : null,
        avg_bars: m && rows[0].entered ? rd(sum(rows.map((r) => r.bars)) / m, 1) : null };
    }
    if (outcomes.stop) outcomes.stop.gapped_through = rd(results.filter((r) => r.gapped_stop).length / n, 4);
    const live = results.filter((r) => r.entered), rs = live.map((r) => r.r);
    const out = { n: n, entered: live.length, outcomes: outcomes };
    if (rs.length) {
      const mean = sum(rs) / rs.length;
      const sd = rs.length > 1 ? Math.sqrt(sum(rs.map((x) => (x - mean) * (x - mean))) / (rs.length - 1)) : 0;
      const wins = rs.filter((x) => x > 0), losses = rs.filter((x) => x <= 0), pw = wins.length / rs.length;
      let wl, wh;
      if (exactCi) [wl, wh] = wilson(wins.length, rs.length);
      else { const h = 1.96 * Math.sqrt(pw * (1 - pw) / rs.length); wl = pw - h; wh = pw + h; }
      const q = quantiles(rs);
      Object.assign(out, {
        expectancy_r: rd(mean, 4), expectancy_se: rs.length > 1 ? rd(sd / Math.sqrt(rs.length), 4) : null,
        p_profit: rd(pw, 4), p_profit_ci95: [rd(Math.max(wl, 0), 4), rd(Math.min(wh, 1), 4)],
        avg_win_r: wins.length ? rd(sum(wins) / wins.length, 3) : 0, avg_loss_r: losses.length ? rd(-sum(losses) / losses.length, 3) : 0,
        r_quantiles: Object.fromEntries(Object.entries(q).map(([a, b]) => [a, rd(b, 3)])),
        worst_r: rd(Math.min.apply(null, rs), 3), best_r: rd(Math.max.apply(null, rs), 3),
        mae_r_median: rd(quantiles(live.map((r) => r.mae_r)).p50, 3), mae_r_p90: rd(quantiles(live.map((r) => r.mae_r), [0.9]).p90, 3),
        mfe_r_median: rd(quantiles(live.map((r) => r.mfe_r)).p50, 3),
        bars_median: quantiles(live.map((r) => r.bars)).p50, bars_p90: quantiles(live.map((r) => r.bars), [0.9]).p90,
      });
    }
    if (money) {
      for (const name of Object.keys(money)) {
        const v = money[name].filter((x) => x !== null && x !== undefined);
        if (!v.length) continue;
        const mu = sum(v) / v.length, q = quantiles(v);
        out[name] = { expectancy: r2(mu), p_loss: rd(v.filter((x) => x < 0).length / v.length, 4), p5: r2(q.p5), p50: r2(q.p50), p95: r2(q.p95),
          worst: r2(Math.min.apply(null, v)), best: r2(Math.max.apply(null, v)) };
      }
    }
    return out;
  }

  const tick = () => new Promise((res) => setTimeout(res, 0));

  // ------------------------------------------------------------------ the run
  /** The whole analysis. c: daily bars from loadCandles. opts mirror timemachine.py's analyse(); chain is parseChain()'s.
   *  opts.nowMs: the option clock (epoch ms), default the chain's price time if older than the wall clock.
   *  opts.onProgress(fraction, label), opts.isCancelled(). Returns the same report shape as the Python tool's --json. */
  async function analyseSwing(c, opts) {
    opts = opts || {};
    const k = cfgWith(opts.cfg), direction = opts.direction || null, setup = opts.setup || null;
    const instrument = opts.instrument || "both", symbol = String(opts.symbol || "").toUpperCase();
    const paths = Math.round(opts.paths || 10000), horizon = Math.round(opts.horizon || 40), seed = opts.seed == null ? 7 : opts.seed;
    const methods = opts.methods || ["replay", "bootstrap", "implied"], keepDrift = !!opts.keepDrift, block = opts.block || 5.0;
    const lots = Math.round(opts.lots || 1), qty = opts.qty ? Math.round(opts.qty) : null, chain = opts.chain || null, iv = opts.iv || null;
    const rates = opts.rates || DEFAULT_RATES, holidays = opts.holidays || null;
    const progress = opts.onProgress || (() => {}), cancelled = opts.isCancelled || (() => false);
    if (c.length && String(c[c.length - 1].date).length > 10) throw new TMError("intraday candles (" + c[c.length - 1].date + "): the swing replay needs daily bars");
    if (horizon < 1) throw new TMError("horizon: at least 1 bar");
    if (chain && chain.spot && Math.abs(chain.spot / c[c.length - 1].close - 1) > 0.1)
      throw new TMError("the chain's underlying is at " + chain.spot + " but the chart closed at " + c[c.length - 1].close + ": is it the same instrument?");
    if (c.length < 80) throw new TMError("only " + c.length + " candles: the Time Machine needs at least 80 (a year of daily bars is best)");
    const ind = indicators(c, k);
    const nowMs = opts.nowMs != null ? opts.nowMs : (() => { const wall = Date.now(), ct = chain ? chainTimeMs(chain) : null; return ct && ct < wall ? ct : wall; })();
    const an = chain ? chainAnalytics(chain, nowMs, k.risk_free_rate) : null;
    const tr = tradeFrom(c, ind, k, direction, setup, opts.entry, opts.sl, opts.tp1, opts.tp2, an);
    if (instrument === "equity" && tr.direction === "short") throw new TMError("delivery shares cannot be held short overnight: a short view needs futures or a put");
    const lot = opts.lot || (chain && chain.lot_size) || INDEX_LOTS[symbol] || null;
    const exchange = BSE_UNDERLYINGS[symbol] ? "BFO" : "NFO";
    const units = instrument === "equity" ? (qty || 1) : lots;
    const frac = units >= 2 ? Math.floor(units * k.tp1_exit_fraction) / units : (opts.trailWhole ? 0 : 1);
    tr.frac = frac;
    const sessions = tradingDaysAfter(c[c.length - 1].date, horizon, holidays);
    const notes = tr.notes.slice();
    tr.rules = { trail_atr: k.trail_atr_mult, trail_after_tp2_atr: k.trail_after_tp2_atr_mult, time_stop_bars: k.time_stop_bars };
    if (instrument === "option" && !chain) throw new TMError("the option instrument needs the option chain whose strike would be bought");
    let leg = null;
    if (instrument === "option" || instrument === "both") {
      const exp = String((chain || {}).expiry || "");
      if (!chain) notes.push("no option chain: the option leg is left out" + (iv ? "" : ", and the implied method has no volatility"));
      else if (exp.length >= 8 && expiryIso(exp) < sessions[0]) notes.push("option leg left out: the chain's expiry " + exp + " is before the entry session " + sessions[0] + "; load a later expiry");
      else {
        try {
          const op = pickOption(tr.direction, chain, k, nowMs, lot, symbol);
          leg = new OptionLeg(op, chain, sessions, nowMs, k, iv ? iv / 100 : null, rates, exchange);
          tr.option = { expiry: op.expiry, kind: op.kind, strike: op.strike, delta: op.delta, iv_pct: op.iv_pct, entry_premium: op.entry_premium,
            lot_size: op.lot_size, spread_pct: op.spread_pct, oi: op.oi, bars_to_expiry: leg.barsToExpiry, derivative_id: op.derivative_id };
        } catch (e) { if (e instanceof TMError) notes.push("option leg left out: " + e.message); else throw e; }
      }
    }
    let costPoints;
    if ((instrument === "future" || instrument === "both") && lot) costPoints = roundtripPoints(tr.entry_plan, lot, rates, exchange)[0];
    else if (instrument === "equity") {
      const q = qty || 1;
      costPoints = (tradeCosts("buy", tr.entry_plan * q, "NSE", rates).total + tradeCosts("sell", tr.entry_plan * q, "NSE", rates).total) / q;
    } else costPoints = roundtripPoints(tr.entry_plan, null, rates, exchange)[0];
    tr.cost_points = r2(costPoints);
    if ((instrument === "future" || instrument === "both") && lot) notes.push("futures: a trade that outlives the month is assumed rolled to the next one (the roll's spread and charges are not counted)");
    if ((instrument === "future" || instrument === "both") && !lot) notes.push("no lot size for " + (symbol || "it") + ": rupees per lot are left out, R is still measured");
    const hz = horizon;
    const t = { direction: tr.direction, stop: tr.stop, tp1: tr.tp1, tp2: tr.tp2, risk: tr.risk, frac: frac, limit: tr.limit };
    const dpExtra = instrument === "equity" && frac > 0 && frac < 1 ? (rates.dp_charge_per_scrip || 0) / (qty || 1) : 0;
    const costOf = (res) => {
      const scale = res.entry && tr.entry_plan ? res.entry / tr.entry_plan : 1;
      return (costPoints + (res.tp1_bar !== null && res.tp1_bar !== undefined ? dpExtra : 0)) * scale;
    };
    const moneyOf = (res) => {
      if (!res.entered) return null;
      const pts = res.pts - costOf(res);
      return instrument === "equity" ? pts * (qty || 1) : (lot ? pts * lot : null);
    };
    const finish = (res) => { if (res.entered) { const risk = res.risk || tr.risk; res.r = risk ? (res.pts - costOf(res)) / risk : 0; } return res; };
    const report = { symbol: symbol, chart: chartRead(c, ind, an), trade: tr, notes: notes, horizon_bars: hz, lot: lot, lots: lots, qty: qty,
      instrument: instrument, methods: {}, samples: {} };
    const moneyKey = instrument === "equity" ? "equity_inr" : "futures_inr_per_lot";
    const futOn = (lot && (instrument === "future" || instrument === "both")) || instrument === "equity";

    progress(0.02, "replaying this chart's history");
    if (methods.includes("replay")) {
      const [occ0, openEnd] = replay(c, ind, k, tr, frac, hz, true);
      const [base0] = replay(c, ind, k, tr, frac, hz, false);
      const occ = occ0.map(finish), base = base0.map(finish);
      const m = futOn ? { [moneyKey]: occ.map(moneyOf) } : null;
      report.methods.replay = Object.assign(summarise(occ, m, true), { still_open_at_end: openEnd, independent: episodes(occ),
        what: tr.setup ? "every bar where " + tr.setup + " " + tr.direction + " fired" : "every bar where any setup fired " + tr.direction });
      report.methods.baseline = Object.assign(summarise(base, null, true), { independent: episodes(base),
        what: "every bar, " + tr.direction + ", same rules: the edge has to beat this" });
      report.replay_trades = occ.map((r) => ({ date: r.date, cls: r.cls, r: rd(r.r, 3), entered: r.entered })).slice(-60);
    }
    const sims = [];
    const closeLast = c[c.length - 1].close, atrLast = ind.atr[ind.atr.length - 1];
    if (methods.includes("bootstrap")) {
      let lib = barLibrary(c, ind, keepDrift);
      if (!keepDrift) lib = neutralise(lib, closeLast, atrLast, hz, k, block, seed);
      sims.push(["bootstrap", (rng) => bootstrapPath(rng, lib, closeLast, atrLast, hz, k, block),
        paths + " paths from this chart's " + lib.length + " real days, blocks of about " + block + " days, " + (keepDrift ? "the history's drift kept" : "drift removed")]);
    }
    const sigma = iv ? iv / 100 : ((an || {}).atm_iv_pct || 0) / 100;
    if (methods.includes("implied") && sigma > 0) {
      const phi = overnightShare(c);
      const days = [c[c.length - 1].date.slice(0, 10)].concat(sessions);
      const dts = []; for (let i = 1; i < days.length; i++) dts.push((isoMs(days[i]) - isoMs(days[i - 1])) / DAY / 365);
      sims.push(["implied", (rng) => impliedPath(rng, closeLast, atrLast, sigma, phi, hz, k, dts),
        paths + " risk-neutral paths at " + (100 * sigma).toFixed(2) + "% volatility (" + (iv ? "given" : "the chain's ATM IV") + "), " + Math.round(100 * phi) + "% of it overnight (measured here)"]);
    } else if (methods.includes("implied")) notes.push("no volatility for the implied method: load an option chain or give an IV");
    let done = 0;
    const total = sims.length * paths;
    for (const [name, gen, what] of sims) {
      const rng = makeRng(seed + "-" + name);
      const resAll = [], futMoney = [], optMoney = [], optCls = [], keep = [];
      for (let p = 0; p < paths; p++) {
        const bars = gen(rng);
        const res = finish(runTrade(bars, t, k, hz));
        resAll.push(res); futMoney.push(moneyOf(res));
        if (p < 400) keep.push(bars.map((b) => [b[0], b[1], b[2], b[3]]));
        if (leg) {
          const bte = leg.barsToExpiry, ro = runTrade(bars, t, k, bte ? Math.min(hz, bte) : hz);
          if (ro.entered && ro.cls === "horizon" && bte && bte <= hz) ro.cls = "expiry";
          optMoney.push(leg.pnl(ro, t)); optCls.push(ro.cls);
        }
        if ((p & 1023) === 1023) {
          done += 1024; progress(0.05 + 0.8 * Math.min(done / total, 1), name + " paths");
          await tick(); if (cancelled()) throw new TMError("cancelled");
        }
      }
      const money = {};
      if (futOn) money[moneyKey] = futMoney;
      if (leg) money.option_inr_per_lot = optMoney;
      const s = summarise(resAll, money);
      if (leg && optMoney.length) {
        const v = optMoney.filter((x) => x !== null), prem = leg.quote * leg.lot;
        s.option_inr_per_lot.premium_paid = r2(prem);
        s.option_inr_per_lot.p_lose_half_premium = v.length ? rd(v.filter((x) => x <= -0.5 * prem).length / v.length, 4) : null;
        s.option_inr_per_lot.p_held_to_expiry = rd(optCls.filter((x) => x === "expiry").length / optCls.length, 4);
      }
      s.what = what;
      s.result_r = resAll.filter((r) => r.entered).map((r) => rd(r.r, 3));
      if (futOn) s.result_inr = futMoney.filter((x) => x !== null).map((x) => Math.round(x));
      if (leg) s.option_result_inr = optMoney.filter((x) => x !== null).map((x) => Math.round(x));
      if (name === "bootstrap" && s.entered && paths >= 1000) {
        const RM = paths >= 4000 ? [20, 800] : [12, 600];
        s.history_se = historySe(barLibrary(c, ind, keepDrift), closeLast, atrLast, hz, k, block, seed, t, costOf, keepDrift, RM[0], RM[1]);
      }
      report.methods[name] = s;
      report.samples[name] = keep;
    }
    if (opts.grid && methods.includes("bootstrap")) {
      progress(0.9, "stop and target grid");
      await tick();
      report.grid = sensitivity(c, ind, k, tr, frac, hz, Math.min(paths, 6000), seed, keepDrift, block, costPoints);
    } else if (opts.grid) notes.push("the grid needs the bootstrap method; it is left out");
    report.verdict = verdict(report);
    report.sessions = sessions.slice(0, 5);
    progress(1, "done");
    return report;
  }
  function sensitivity(c, ind, k, tr, frac, hz, paths, seed, keepDrift, block, costPoints) {
    let lib = barLibrary(c, ind, keepDrift);
    const e = c[c.length - 1].close, a = ind.atr[ind.atr.length - 1];
    if (!keepDrift) lib = neutralise(lib, e, a, hz, k, block, seed);
    const sgn = tr.direction === "long" ? 1 : -1, cells = [];
    for (const sm of [1.0, 1.5, 2.0, 2.5, 3.0]) for (const tm of [1.5, 2.0, 3.0]) {
      const risk = sm * a;
      cells.push({ stop_atr: sm, tp2_r: tm, t: { direction: tr.direction, stop: e - sgn * risk, tp1: e + sgn * k.tp1_r * risk, tp2: e + sgn * tm * risk, risk: risk, frac: frac }, rs: [] });
    }
    const rng = makeRng(seed + "-grid");
    for (let p = 0; p < paths; p++) {
      const bars = bootstrapPath(rng, lib, e, a, hz, k, block);
      for (const cell of cells) { const res = runTrade(bars, cell.t, k, hz); if (res.entered) cell.rs.push((res.pts - costPoints) / cell.t.risk); }
    }
    return { paths: paths, current: { stop_atr: r2(tr.stop_atr), tp2_r: r2(tr.tp2_r) }, cells: cells.map((cell) => {
      const rs = cell.rs, mu = rs.length ? sum(rs) / rs.length : 0;
      return { stop_atr: cell.stop_atr, tp2_r: cell.tp2_r, expectancy_r: rd(mu, 3), p_profit: rs.length ? rd(rs.filter((x) => x > 0).length / rs.length, 3) : null,
        se: rs.length > 1 ? rd(Math.sqrt(sum(rs.map((x) => (x - mu) * (x - mu))) / Math.max(rs.length - 1, 1)) / Math.sqrt(rs.length), 3) : null };
    }) };
  }
  /** Plain-words verdicts, as structured lines {kind, text}. */
  function verdict(rep) {
    const ms = rep.methods, lines = [];
    const sims = ["bootstrap", "implied"].filter((m) => ms[m] && ms[m].entered).map((m) => [m, ms[m]]);
    for (const [m, s] of sims) {
      const mc = s.expectancy_se || 0, hist = s.history_se || 0, se = Math.hypot(mc, hist);
      const lo = s.expectancy_r - 1.96 * se, hi = s.expectancy_r + 1.96 * se;
      const word = lo > 0 ? "positive" : hi < 0 ? "negative" : "indistinguishable from zero";
      lines.push({ kind: m, word: word, expectancy_r: s.expectancy_r, lo: lo, hi: hi, mc: 1.96 * mc, hist: hist,
        text: "Verdict (" + m + "): expectancy " + fmtR(s.expectancy_r) + " (95% " + fmtR(lo) + " to " + fmtR(hi) +
          (hist ? ", counting that one year of history is only one sample; the simulation alone is +/-" + (1.96 * mc).toFixed(3) : " from the simulation; the model's own assumptions are not in it") + "), " + word + "." });
    }
    const rp = ms.replay, base = ms.baseline;
    if (rp && rp.entered) {
      const ind = rp.independent != null ? rp.independent : rp.entered;
      if (ind < 20) lines.push({ kind: "replay", word: "too few", text: "Verdict (replay): " + rp.entered + " past occurrences, " + ind + " of them independent: too few to judge (under 20); read the interval, not the average." });
      else {
        const eff = (s) => (s.expectancy_se || 0) * Math.sqrt(s.entered / Math.max(s.independent || 1, 1));
        const diff = rp.expectancy_r - (base.expectancy_r || 0), tol = 1.96 * Math.hypot(eff(rp), eff(base));
        const word = diff > tol ? "the setup added something here" : diff < -tol ? "the setup did worse here" : "no difference this history can show (" + fmtR(diff) + ", noise +/-" + tol.toFixed(3) + "R)";
        lines.push({ kind: "replay", word: word, text: "Verdict (replay): " + rp.entered + " past occurrences (" + ind + " independent), expectancy " + fmtR(rp.expectancy_r) + " against " + fmtR(base.expectancy_r || 0) + " for entering on every bar the same way: " + word + "." });
      }
    }
    if (sims.length === 2) {
      const a = sims[0][1], b = sims[1][1], gap = Math.abs(a.expectancy_r - b.expectancy_r);
      const tol = 1.96 * Math.hypot(Math.hypot(a.expectancy_se || 0, a.history_se || 0), Math.hypot(b.expectancy_se || 0, b.history_se || 0));
      if (gap > tol) lines.push({ kind: "disagree", text: "The bootstrap and the implied paths disagree by " + gap.toFixed(3) + "R: the chart's own behaviour (gaps, trends, mean reversion) differs from what the option prices assume." });
    }
    return lines;
  }
  function fmtR(x) { return (x >= 0 ? "+" : "-") + Math.abs(x).toFixed(3) + "R"; }

  return {
    TMError, DEFAULTS, DEFAULT_RATES, OUTCOMES, SETUPS, INDEX_LOTS, BSE_UNDERLYINGS, TRADING_DAYS,
    cfgWith, makeRng, ncdf, npdf, black76, delta76, impliedVol, wilson, noEdgeProbability,
    fnoCosts, tradeCosts, roundtripPoints, loadCandles, indicators, emaA, smaA, atrA, rsiA, adxA, supertrendA, bbWidthA, swingLow, swingHigh,
    signalsAt, levels, runTrade, chartRead, tradeFrom, replay, episodes, barLibrary, resample, bootstrapPath, neutralise, historySe,
    overnightShare, impliedPath, tradingDaysAfter, yearsToExpiry, expiryIso, isoMs, msIso, parseChain, chainTimeMs, chainAnalytics, pickOption,
    OptionLeg, quantiles, summarise, analyseSwing, sensitivity, verdict, fmtR, tick, r2, rd, sum, legMid,
  };
});
