/* The Time Machine, intraday mode: the Trading Desk's own plan (SL, T1, T2, the trailing ladder, out by 15:15)
 * replayed on intraday index bars of any step (1, 5 or 15 minutes, or chosen for you), with the option repriced
 * with Black-76 on every bar.
 *
 * One variance clock, kept by the minute. An option's IV is quoted on calendar time, but variance does not arrive on
 * a calendar: it arrives in the session (shaped like a U) and, overnight and over weekends, as the opening gap.
 *     Teff(minute) = (1 - S[minute]) * (1 - phi) / 365  +  (calendar days from that day to the expiry) / 365
 * counts what is left to arrive: the unspent part of today's session, plus every later calendar day in full. The
 * volatility is solved from the quote's mid on that same clock and the index paths spend variance on it, so the
 * option price is a martingale along them (the tests check it at every step size). Nothing here predicts: it counts
 * what the plan does on paths from three models, and shows how far apart the models are.
 *
 * Why the step matters: a bar that touches both the stop and a target hides which came first, and the conservative
 * answer (the stop) is biased. Finer bars shrink that bias (the tests show the odds converging to theory as the step
 * falls); coarser bars are faster. "auto" picks 1 minute when the stop is only a few bar-widths away, else 5. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./tmx-core.js"));
  else root.TMXI = factory(root.TMX);
})(typeof self !== "undefined" ? self : this, function (T) {
  "use strict";

  const DAYMIN = 375;       // 09:15 .. 15:30
  const TRADEMIN = 360;     // 09:15 .. 15:15, the last minute the plan trades
  const OPEN_MIN = 555;     // 09:15 as minutes after midnight
  const BARS5 = 75, LAST5 = 71;
  const DAY = 86400000, IST = 5.5 * 3600000;
  const sum = T.sum;
  const r05 = (n) => Math.round(n * 20) / 20;
  const hm = (m) => { const t = OPEN_MIN + Math.round(m); return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0"); };
  const minOf = (hhmm) => { const [h, m] = String(hhmm).split(":").map(Number); return h * 60 + (m || 0) - OPEN_MIN; };
  const STEPS = [1, 5, 15];

  const DEFAULT_RULES = {
    slPct: 10, tp1Pct: 15, tp2Pct: 20, ladder: [1.10, 1.15, 1.20, 1.30, 1.40, 1.50], breathPct: 0.00085, minBreath: 5,
    noEntryAfter: "15:00", exitAt: "15:15", dayStop: 3000,
  };
  /** What happens at the targets. */
  const VARIANTS = {
    desk: { label: "Desk plan", note: "T1 books half (one lot: all of it), the stop goes to entry, the ladder trails, T2 takes the rest",
      t1: true, t2: "rest", ladder: true },
    ride: { label: "Ride past T2", note: "as the desk plan, but T2 is not taken: the rest rides the ladder to 15:15",
      t1: true, t2: false, ladder: true },
    allT1: { label: "All out at T1", note: "the whole position leaves at +15%", t1: true, t1All: true, t2: false, ladder: true },
    allT2: { label: "All out at T2", note: "no part at T1; the whole position leaves at +20%", t1: false, t2: "all", ladder: true },
    trail: { label: "Ladder only", note: "no targets: the ladder trails the stop, out at 15:15", t1: false, t2: false, ladder: true },
    hold: { label: "Stop only", note: "the stop-loss and nothing else, out at 15:15", t1: false, t2: false, ladder: false },
  };
  const CLASSES = [
    ["stop", "stop-loss, nothing booked"], ["t1_all", "T1: the whole position out"], ["t1_be", "T1, then stopped at entry"],
    ["t1_trail", "T1, then stopped by the ladder"], ["t2", "T2 taken"], ["trail_out", "stopped by the ladder"], ["time", "out at 15:15"],
  ];

  // ------------------------------------------------------------------ sessions and the variance profile
  /** INDmoney 5-minute rows -> sessions [{date, bars: Array(72) of {o,h,l,c} | null, count, full}]. Bars from 15:15 on are
   *  the feed's closing prints (the 15:10 value repeated, then one closing print), not trading: they are dropped. */
  function parseBars(rows) {
    const by = new Map();
    for (const r of rows || []) {
      const s = String(r.datetime_ist || r.date || ""), day = s.slice(0, 10), hh = +s.slice(11, 13), mm = +s.slice(14, 16);
      const j = (hh * 60 + mm - OPEN_MIN) / 5;
      if (!(j >= 0 && j <= LAST5) || j !== Math.floor(j)) continue;
      const o = +r.open, h = +r.high, l = +r.low, c = +r.close;
      if (!(o > 0 && h > 0 && l > 0 && c > 0)) continue;
      if (!by.has(day)) by.set(day, { date: day, bars: new Array(LAST5 + 1).fill(null) });
      by.get(day).bars[j] = { o, h: Math.max(h, o, c), l: Math.min(l, o, c), c };
    }
    const out = [...by.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
    for (const s of out) { s.count = s.bars.filter(Boolean).length; s.full = s.count >= LAST5 + 1; }
    return out;
  }
  /** A session as compact arrays (for storing): {date, o:[], h:[], l:[], c:[]} of 72 numbers, 2 decimals (0 = no bar). */
  function packSession(s) {
    const g = (k) => s.bars.map((b) => (b ? Math.round(b[k] * 100) / 100 : 0));
    return { date: s.date, o: g("o"), h: g("h"), l: g("l"), c: g("c") };
  }
  function unpackSession(d) {
    const bars = d.o.map((o, j) => (o > 0 && d.c[j] > 0 ? { o, h: Math.max(d.h[j], o, d.c[j]), l: Math.min(d.l[j], o, d.c[j]), c: d.c[j] } : null));
    const count = bars.filter(Boolean).length;
    return { date: d.date, bars, count, full: count >= LAST5 + 1 };
  }
  /** Several stores of the same days (the feed's window, the saved library) merged: a day's bars come from the fuller one. */
  function mergeSessions(...lists) {
    const by = new Map();
    for (const list of lists) for (const s of list || []) { const cur = by.get(s.date); if (!cur || s.count > cur.count) by.set(s.date, s); }
    return [...by.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
  }
  function paramWeights() {
    const w = [];
    for (let j = 0; j < BARS5; j++) w.push(1 + 2.2 * Math.exp(-j / 5) + 0.7 * Math.exp(-(BARS5 - 1 - j) / 10));
    return w;
  }
  const paramShares = () => { const pw = paramWeights(), pt = sum(pw); return pw.map((x) => x / pt); };
  /** Each 5-minute bar's share of a session's variance (summing to 1 over the 75 bars): a U shape, blended with what the full
   *  sessions in hand show (smoothed over five bars). The weight on the sessions grows with their number: n / (n + 6). */
  function sessionProfile(sessions) {
    const param = paramShares(), full = (sessions || []).filter((s) => s.full);
    if (full.length < 2) return { shares5: param, source: "a standard U shape (fewer than two full sessions of 5-minute bars)", empirical: false, n: full.length };
    const e = new Array(LAST5 + 1).fill(0);
    for (const s of full) for (let j = 0; j <= LAST5; j++) { const b = s.bars[j], x = Math.log(b.c / b.o); e[j] += x * x / full.length; }
    return blendProfile(param, e, full.length);
  }
  /** param: the U shape (75 shares); e: the mean squared 5-minute move of each of the 72 bars; n: how many sessions it rests on. */
  function blendProfile(param, e, n) {
    const sm = e.map((_, j) => {
      if (j < 2) return e[j];
      let a = 0, c = 0;
      for (let k = Math.max(0, j - 2); k <= Math.min(LAST5, j + 2); k++) { a += e[k]; c++; }
      return a / c;
    });
    const w = n / (n + 6), st = sum(sm), pPart = sum(param.slice(0, LAST5 + 1)), shares = param.slice();
    for (let j = 0; j <= LAST5; j++) shares[j] = (1 - w) * param[j] + w * (sm[j] / st) * pPart;
    const tot = sum(shares);
    return { shares5: shares.map((x) => x / tot), source: "a U shape blended with " + n + " full sessions of 5-minute bars (" + Math.round(100 * w) + "% weight)", empirical: true, n };
  }
  /** Minute shares (375) from 5-minute shares: each bar's variance spread evenly over its five minutes. */
  function minuteShares(shares5) {
    const out = new Array(DAYMIN);
    for (let m = 0; m < DAYMIN; m++) out[m] = shares5[Math.floor(m / 5)] / 5;
    return out;
  }

  // ------------------------------------------------------------------ the clock
  const daysBetween = (a, b) => Math.round((T.isoMs(b) - T.isoMs(a)) / DAY);
  function istParts(ms) { const d = new Date(ms + IST); return { date: d.toISOString().slice(0, 10), mins: d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60 }; }
  function isTradingDay(iso, holidays) {
    const w = new Date(T.isoMs(iso)).getUTCDay();
    return w !== 0 && w !== 6 && !(holidays && holidays.has && holidays.has(iso));
  }
  function nextTradingDay(iso, holidays) {
    let d = T.isoMs(iso);
    for (let i = 0; i < 20; i++) { d += DAY; const x = T.msIso(d); if (isTradingDay(x, holidays)) return x; }
    return T.msIso(d);
  }
  /** Where the run starts: live (the next step boundary of today's session) or the next session's open (an overnight
   *  gap first, then the chosen entry time). Entry minutes per model step are worked out by entryMinute(). */
  function startPoint(nowMs, holidays, forceOpen, entryAt) {
    const t = istParts(nowMs), x = t.mins - OPEN_MIN;
    const open = isTradingDay(t.date, holidays) && x >= 0 && x < TRADEMIN;
    if (open && !forceOpen) return { kind: "live", day: t.date, xNow: x, label: "from about " + hm(Math.ceil(x)) + " today" };
    const before = isTradingDay(t.date, holidays) && x < 0;
    const day = before ? t.date : nextTradingDay(t.date, holidays);
    const em = Math.max(0, Math.min(TRADEMIN - 1, minOf(entryAt || "09:30")));
    return { kind: "open", day, entryMin: em, label: "the " + day + " session from the open, entering at " + hm(em) };
  }
  /** The minute (after the open) a model with this step enters at: the next step boundary of now (live), or the chosen
   *  entry time rounded up to the step (from the open). */
  function entryMinute(sp, step) {
    return sp.kind === "live" ? Math.ceil(sp.xNow / step - 1e-9) * step : Math.ceil(sp.entryMin / step - 1e-9) * step;
  }
  function makeClock(shares5, phi, day, expiryIso) {
    const sm = minuteShares(shares5), S = [0];
    for (let m = 0; m < DAYMIN; m++) S.push(S[m] + sm[m]);
    const D = Math.max(daysBetween(day, expiryIso), 0);
    return { S, D, phi, shares: sm, Teff: (m) => Math.max((1 - S[Math.max(0, Math.min(DAYMIN, Math.round(m)))]) * (1 - phi) / 365 + D / 365, 0) };
  }
  /** The moment the chain was struck, as a day and a minute after the open (375 = that day's close). */
  function quoteMoment(chain, holidays) {
    const ms = T.chainTimeMs(chain);
    if (ms == null) return null;
    const p = istParts(ms), x = p.mins - OPEN_MIN;
    const inSession = isTradingDay(p.date, holidays) && x >= 0 && x < DAYMIN;
    return { day: p.date, m: inSession ? Math.round(x) : (x < 0 ? 0 : DAYMIN), ms };
  }

  // ------------------------------------------------------------------ index path models
  function bridgeBar(rng, O, v) {          // a Gaussian bar; a Brownian bridge gives its high and low
    const x = rng.gauss() * Math.sqrt(v) - 0.5 * v;
    return bridgeFrom(rng, O, x, v);
  }
  function bridgeFrom(rng, O, x, v) {      // a bar that moves x (log) over variance v: its high and low from the bridge
    const u1 = 1 - rng.random(), u2 = 1 - rng.random(), vv = Math.max(v, 1e-18);
    const mx = 0.5 * (x + Math.sqrt(x * x - 2 * vv * Math.log(u1))), mn = 0.5 * (x - Math.sqrt(x * x - 2 * vv * Math.log(u2)));
    return [O, O * Math.exp(mx), O * Math.exp(mn), O * Math.exp(x)];
  }
  /** One real 5-minute bar (log moves from its open: close x, high h, low l) split into five 1-minute bars. The bar's own
   *  open, high, low and close are kept exactly; only the order in which the path visits them inside the bar is random:
   *  a Gaussian bridge to the close whose roughness comes from the bar's own range, with the excursions beyond the open
   *  and close rescaled to reach the bar's real high and low. w: the five minutes' variance shares (any scale). */
  function splitBar(rng, pc, x, h, l, w) {
    const W = sum(w), top = Math.max(0, x), bot = Math.min(0, x);
    h = Math.max(h, top); l = Math.min(l, bot);
    const v = Math.pow(Math.max(h - l, 1e-9) / 1.2533, 2);          // the bridge's roughness, from the bar's own range
    const z = w.map((wi) => rng.gauss() * Math.sqrt(v * wi / W)), sz = sum(z);
    const y = [0], hi = [], lo = [];
    for (let i = 0; i < 5; i++) {
      const inc = z[i] + (w[i] / W) * (x - sz), b = bridgeFrom(rng, 1, inc, v * w[i] / W);
      hi.push(y[i] + Math.log(b[1])); lo.push(y[i] + Math.log(b[2])); y.push(y[i] + inc);
    }
    let M = Math.max.apply(null, hi), m = Math.min.apply(null, lo);
    if (M <= top + 1e-12 && h > top) { hi[hi.indexOf(M)] = M = h; }
    if (m >= bot - 1e-12 && l < bot) { lo[lo.indexOf(m)] = m = l; }
    const up = (u) => (u > top && M > top ? top + (u - top) * (h - top) / (M - top) : u), dn = (u) => (u < bot && m < bot ? bot - (bot - u) * (bot - l) / (bot - m) : u);
    const f = (u) => (u > top ? up(u) : u < bot ? dn(u) : u);
    const out = [];
    for (let i = 0; i < 5; i++) {
      const o = pc * Math.exp(f(y[i])), c = pc * Math.exp(f(y[i + 1]));
      out.push([o, Math.max(pc * Math.exp(f(hi[i])), o, c), Math.min(pc * Math.exp(f(lo[i])), o, c), c]);
    }
    return out;
  }
  /** Five-minute bars regrouped into bars of 5 * k minutes: [o of the first, highest high, lowest low, c of the last]. */
  function aggregate(bars5, k) {
    if (k === 1) return bars5.map((b) => ({ o: b.o, h: b.h, l: b.l, c: b.c }));
    const out = [];
    for (let i = 0; i + k <= bars5.length; i += k) {
      const g = bars5.slice(i, i + k);
      out.push({ o: g[0].o, h: Math.max.apply(null, g.map((b) => b.h)), l: Math.min.apply(null, g.map((b) => b.l)), c: g[k - 1].c });
    }
    return out;
  }
  const shapeOf = (b, share) => { const sq = Math.sqrt(share); return [Math.log(b.h / b.o) / sq, Math.log(b.l / b.o) / sq, Math.log(b.c / b.o) / sq]; };
  /** Real bars as shapes: the high, low and close from the open, each over the square root of the bar's variance share, drift
   *  taken out. step 5 uses the 5-minute bars, 15 the bars regrouped by three. rho is their rms, the recent realised level on
   *  the model's own scale. A session may carry a weight w (recent ones can count more). */
  function barLibrary(sessions, clock, step) {
    const lib = [], wts = [];
    for (const s of sessions) {
      const bars = s.bars.slice(0, LAST5 + 1), w = s.w || 1;
      if (step !== 15) {
        for (let j = 0; j <= LAST5; j++) { const b = bars[j]; if (b) { lib.push(shapeOf(b, clock.S[5 * j + 5] - clock.S[5 * j])); wts.push(w); } }
      } else if (s.full) {
        aggregate(bars, 3).forEach((b, k) => { lib.push(shapeOf(b, clock.S[15 * k + 15] - clock.S[15 * k])); wts.push(w); });
      }
    }
    if (lib.length < 30) return null;
    const tw = sum(wts), mu = lib.reduce((a, x, i) => a + x[2] * wts[i], 0) / tw, out = lib.map((x) => [x[0] - mu, x[1] - mu, x[2] - mu]);
    const cum = []; let a = 0; for (const x of wts) { a += x; cum.push(a); }
    return { lib: out, rho: Math.sqrt(out.reduce((s2, x, i) => s2 + x[2] * x[2] * wts[i], 0) / tw), step: step === 15 ? 15 : 5, cum, weighted: wts.some((x) => x !== wts[0]) };
  }
  function drawIndex(rng, lib) {          // a draw, weighted when sessions carry weights
    if (!lib.weighted) return rng.randrange(lib.lib.length);
    const u = rng.random() * lib.cum[lib.cum.length - 1];
    let lo = 0, hi = lib.cum.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (lib.cum[mid] < u) lo = mid + 1; else hi = mid; }
    return lo;
  }
  /** Overnight gaps (open over the last close, in logs) from the daily bars, per square root of the calendar days. */
  function overnightGaps(daily) {
    const g = [];
    for (let i = 1; daily && i < daily.length; i++) {
      const pc = daily[i - 1].close, o = daily[i].open;
      if (pc > 0 && o > 0) g.push({ x: Math.log(o / pc), days: Math.max(1, daysBetween(daily[i - 1].date, daily[i].date)) });
    }
    if (!g.length) return null;
    const z = g.map((a) => a.x / Math.sqrt(a.days)), mu = sum(z) / z.length, zc = z.map((v) => v - mu);
    return { z: zc, sd: Math.sqrt(sum(zc.map((v) => v * v)) / zc.length) };
  }

  // ------------------------------------------------------------------ the desk's plan on one premium path
  function levelsFor(E, rules, spot, delta) {
    const sl = r05(E * (1 - rules.slPct / 100)), t1 = r05(E * (1 + rules.tp1Pct / 100)), t2 = r05(E * (1 + rules.tp2Pct / 100));
    const breathPts = Math.round(spot * rules.breathPct), breath = Math.max(rules.minBreath, Math.round(breathPts * Math.abs(delta || 0.5)));
    const steps = rules.ladder.map((m, i) => { const at = r05(E * m); return { at, stop: i === 0 ? E : Math.max(E, r05(at - breath)) }; });
    return { sl, t1, t2, steps, breath, breathPts };
  }
  /** pb(n) -> {o, hi, lo, c}: the sellable (bid) premium on the run's bar n, L bars in all (the last closes at the exit time).
   *  state: {stop, t1Done}. Stops first when a bar touches both; a stop trigger that the bar opens through fills at the open;
   *  targets are limits (a better open fills better). */
  function runDesk(pb, L, E, lv, variant, qtyLots, state) {
    const exits = [];
    let stop = state.stop, left = qtyLots, stage = state.t1Done ? "t1" : "open", stepsDone = 0, cls = null;
    let minLow = Infinity, maxHigh = -Infinity;
    const t1Lots = variant.t1 ? (variant.t1All || qtyLots < 2 ? qtyLots : Math.floor(qtyLots / 2)) : 0;
    for (let n = 0; n < L && left > 0; n++) {
      const b = pb(n);
      if (b.lo < minLow) minLow = b.lo;
      if (b.hi > maxHigh) maxHigh = b.hi;
      if (b.lo <= stop) {
        exits.push({ q: left, px: b.o <= stop ? b.o : stop, n, why: "stop" });
        const locked = stop > E + 0.004;
        cls = stage === "open" ? (locked ? "trail_out" : "stop") : (locked ? "t1_trail" : "t1_be");
        left = 0; break;
      }
      if (stage === "open" && variant.t1 && b.hi >= lv.t1) {
        exits.push({ q: t1Lots, px: b.o >= lv.t1 ? b.o : lv.t1, n, why: "t1" });
        left -= t1Lots; stage = "t1";
        if (left <= 0) { cls = "t1_all"; break; }
        if (stop < E) stop = E;
      }
      if (variant.t2 && b.hi >= lv.t2 && (variant.t2 === "all" || stage === "t1")) {
        exits.push({ q: left, px: b.o >= lv.t2 ? b.o : lv.t2, n, why: "t2" });
        left = 0; cls = "t2"; break;
      }
      if (variant.ladder) while (stepsDone < lv.steps.length && b.hi >= lv.steps[stepsDone].at) { if (lv.steps[stepsDone].stop > stop) stop = lv.steps[stepsDone].stop; stepsDone++; }
      if (n === L - 1 && left > 0) { exits.push({ q: left, px: b.c, n, why: "time" }); left = 0; cls = "time"; }
    }
    return { cls, exits, minLow, maxHigh };
  }

  // ------------------------------------------------------------------ pricing one contract along a path
  /** spec: everything about a contract that prices it on the clock, plain numbers (so a prediction can be stored and played
   *  later on a real tape): {kind, K, basis, sigma, r, half, phi, day, expiryIso, delta, lotSize, exchange, m0}. */
  function makePricer(spec, clock) {
    const price = (level, Tt) => T.black76(level + spec.basis, spec.K, Tt, spec.sigma, spec.r, spec.kind);
    const bidAt = (level, Tt) => Math.max(price(level, Tt) - spec.half, 0.05);
    const grid = (step, m0, jStart) => {            // the clock at each bar of a path that starts at jStart and enters at m0
      const n = Math.round((TRADEMIN - jStart) / step), Ts = [], Te = [], Tm = [];
      for (let i = 0; i < n; i++) { const a = clock.Teff(jStart + i * step), b = clock.Teff(jStart + (i + 1) * step); Ts.push(a); Te.push(b); Tm.push((a + b) / 2); }
      return { step, m0, jStart, n, eIdx: Math.round((m0 - jStart) / step), Ts, Te, Tm };
    };
    return { price, bidAt, grid };
  }
  /** Play the desk plan on bars (arrays [o,h,l,c] of a path on grid g), entering at bar g.eIdx. Returns {E, pb, Ltr, lv} or null
   *  when the premium is too small to trade. */
  function playPath(ctx, bars, g, acc, keys, entryFixed) {
    const { pricer, spec, rules, qLots, mode, posState, rates, exchange, valueNow, delta } = ctx;
    const cache = new Array(g.n);
    const pb = (n) => {
      if (cache[n]) return cache[n];
      const b = bars[n], o = pricer.bidAt(b[0], g.Ts[n]), c = pricer.bidAt(b[3], g.Te[n]);
      const pH = pricer.bidAt(spec.kind === "CE" ? b[1] : b[2], g.Tm[n]), pL = pricer.bidAt(spec.kind === "CE" ? b[2] : b[1], g.Tm[n]);
      return (cache[n] = { o, hi: Math.max(pH, pL, o, c), lo: Math.min(pH, pL, o, c), c });
    };
    let E, lv, state;
    if (mode === "position") { E = posState.entry; lv = levelsFor(E, rules, bars[g.eIdx][0], delta); state = { stop: posState.stop, t1Done: posState.t1Done }; }
    else {
      E = entryFixed != null ? entryFixed : r05(pricer.price(bars[g.eIdx][0], g.Ts[g.eIdx]) + spec.half);
      if (E < 0.1) return null;
      lv = levelsFor(E, rules, bars[g.eIdx][0], delta); state = { stop: lv.sl, t1Done: false };
    }
    const pbT = (m) => pb(g.eIdx + m), Ltr = g.n - g.eIdx;
    const buyCh = mode === "position" ? 0 : T.fnoCosts("buy", E * qLots * spec.lotSize, "OPT", exchange, rates).total;
    keys.forEach((key, vi) => {
      const res = runDesk(pbT, Ltr, E, lv, VARIANTS[key], qLots, state);
      let cash = 0, ch = buyCh, qty = 0;
      for (const e of res.exits) { const val = e.px * e.q * spec.lotSize; cash += val; qty += e.q * spec.lotSize; ch += T.fnoCosts("sell", val, "OPT", exchange, rates).total; }
      const pnl = mode === "position" ? cash - ch - valueNow : cash - E * qty - ch;
      const last = res.exits[res.exits.length - 1], a = acc[vi], mins = g.step * (last.n + 1);
      a.n++; a.sum += pnl; a.sumsq += pnl * pnl; a.pnl.push(pnl); a.cls[res.cls] = (a.cls[res.cls] || 0) + 1; a.minutes.push(mins);
      a.mae.push(Math.max(0, (E - res.minLow) / E)); a.mfe.push(Math.max(0, (res.maxHigh - E) / E));
      (a.byCls[res.cls] = a.byCls[res.cls] || []).push(pnl); (a.byClsMin[res.cls] = a.byClsMin[res.cls] || []).push(mins);
      a.t1.push(res.exits.some((e) => e.why === "t1" || e.why === "t2") ? 1 : 0);
      a.stopped.push(res.cls === "stop" ? 1 : 0);
    });
    return { E, pb: pbT, Ltr, lv };
  }
  const newAcc = (keys) => keys.map(() => ({ n: 0, sum: 0, sumsq: 0, pnl: [], cls: {}, byCls: {}, byClsMin: {}, minutes: [], mae: [], mfe: [], t1: [], stopped: [] }));
  function summarise(accs, keys, rules, mode) {
    const out = {};
    keys.forEach((key, vi) => {
      const a = accs[vi], n = a.n;
      if (!n) { out[key] = { n: 0 }; return; }
      const mu = a.sum / n, sd = n > 1 ? Math.sqrt(Math.max(a.sumsq - n * mu * mu, 0) / (n - 1)) : 0;
      const qn = T.quantiles(a.pnl), wins = a.pnl.filter((x) => x > 0), loss = a.pnl.filter((x) => x <= 0), classes = {};
      for (const [ck] of CLASSES) {
        const m = a.cls[ck] || 0;
        if (!m) continue;
        const p = m / n, se = Math.sqrt(p * (1 - p) / n);
        classes[ck] = { count: m, p, ci95: [Math.max(0, p - 1.96 * se), Math.min(1, p + 1.96 * se)], avg: sum(a.byCls[ck]) / m, minutes: sum(a.byClsMin[ck]) / m };
      }
      const pw = wins.length / n, se = Math.sqrt(pw * (1 - pw) / n);
      out[key] = {
        n, label: VARIANTS[key].label, note: VARIANTS[key].note, classes, expectancy: mu, se: sd / Math.sqrt(n), sd,
        p_profit: pw, p_profit_ci95: [Math.max(0, pw - 1.96 * se), Math.min(1, pw + 1.96 * se)],
        p_day_stop: mode === "position" ? null : a.pnl.filter((x) => x <= -rules.dayStop).length / n, p_hit_t1: sum(a.t1) / n, p_stop: sum(a.stopped) / n,
        avg_win: wins.length ? sum(wins) / wins.length : 0, avg_loss: loss.length ? sum(loss) / loss.length : 0,
        p5: qn.p5, p25: qn.p25, p50: qn.p50, p75: qn.p75, p95: qn.p95, worst: Math.min.apply(null, a.pnl), best: Math.max.apply(null, a.pnl),
        minutes_median: T.quantiles(a.minutes, [0.5]).p50, minutes_p90: T.quantiles(a.minutes, [0.9]).p90,
        mae_median: T.quantiles(a.mae, [0.5]).p50, mae_p90: T.quantiles(a.mae, [0.9]).p90, mfe_median: T.quantiles(a.mfe, [0.5]).p50, pnl: a.pnl,
      };
    });
    return out;
  }

  /** Play the desk plan on one real tape (5-minute bars from the entry on), with a stored contract spec and a forced entry
   *  price: what a stored prediction would have done. Returns the plan's three binary outcomes and its P&L, or null. */
  function playTape(spec, bars5, profile, rules, rates, lots, entry) {
    const R = Object.assign({}, DEFAULT_RULES, rules || {});
    const clock = makeClock((profile && profile.shares5) || paramShares(), spec.phi, spec.day, spec.expiryIso);
    const pricer = makePricer(spec, clock), g = pricer.grid(5, spec.m0, spec.m0);
    if (bars5.length < g.n) return null;
    const bars = bars5.slice(0, g.n).map((b) => [b.o, b.h, b.l, b.c]);
    const acc = newAcc(["desk"]);
    const ctx = { pricer, spec, rules: R, qLots: Math.max(1, lots || 1), mode: "new", posState: null, rates: rates || T.DEFAULT_RATES, exchange: spec.exchange || "NFO", valueNow: 0, delta: spec.delta };
    const res = playPath(ctx, bars, g, acc, ["desk"], entry);
    if (!res) return null;
    const a = acc[0];
    return { pnl: a.pnl[0], profit: a.pnl[0] > 0 ? 1 : 0, t1: a.t1[0], stop: a.stopped[0], cls: Object.keys(a.cls)[0] };
  }

  // ------------------------------------------------------------------ the verdict and the blend
  /** The gate, the same rule as timemachine.gate in the Python tool: the two simulated models, on the desk plan, after charges. */
  function gateOf(models) {
    const words = [];
    for (const m of ["implied", "bootstrap"]) {
      const v = models[m] && models[m].variants && models[m].variants.desk;
      if (!v || !v.n) continue;
      const lo = v.expectancy - 1.96 * v.se, hi = v.expectancy + 1.96 * v.se;
      words.push({ model: m, word: lo > 0 ? "positive" : hi < 0 ? "negative" : "zero", expectancy: v.expectancy, lo, hi });
    }
    if (!words.length) return { gate: "error", reason: "no simulated model could run", words };
    const neg = words.filter((w) => w.word === "negative"), pos = words.filter((w) => w.word === "positive");
    if (neg.length) return { gate: "fail", reason: neg.map((w) => w.model).join(" and ") + (neg.length === 1 ? " is" : " are") + " negative after charges (95% band below zero)", words };
    if (pos.length) return { gate: "pass", reason: pos.map((w) => w.model).join(" and ") + " positive after charges and none negative (95% band above zero)", words };
    return { gate: "watch", reason: "no model can tell the average from zero: no edge shown, none ruled out", words };
  }
  /** The models' desk-plan results mixed by weights (the replay counts for what its independent sessions are worth), with the
   *  simulation error and the disagreement between models both in the band. */
  function blendOf(models, weights) {
    const base = Object.assign({ implied: 0.4, bootstrap: 0.4, replay: 0.2 }, weights || {});
    const rows = [];
    for (const m of ["implied", "bootstrap", "replay"]) {
      const v = models[m] && models[m].variants && models[m].variants.desk;
      if (!v || !v.n) continue;
      let w = base[m];
      if (m === "replay") w *= Math.min(1, (models.replay.independent || 0) / 10);
      if (w > 0) rows.push({ m, w, v });
    }
    if (!rows.length) return null;
    const tw = sum(rows.map((r) => r.w)), E = sum(rows.map((r) => r.w * r.v.expectancy)) / tw;
    const seMc = Math.sqrt(sum(rows.map((r) => r.w * r.w * r.v.se * r.v.se))) / tw, spread = Math.sqrt(sum(rows.map((r) => r.w * (r.v.expectancy - E) * (r.v.expectancy - E))) / tw);
    const pf = (k) => sum(rows.map((r) => r.w * r.v[k])) / tw;
    return { expectancy: E, se: Math.hypot(seMc, spread), se_sim: seMc, spread, p_profit: pf("p_profit"), p_hit_t1: pf("p_hit_t1"), p_stop: pf("p_stop"),
      p_day_stop: rows[0].v.p_day_stop == null ? null : pf("p_day_stop"), weights: Object.fromEntries(rows.map((r) => [r.m, r.w / tw])) };
  }

  // ------------------------------------------------------------------ the run
  /** opts.sessions (parseBars), opts.chain (T.parseChain), opts.side/strike or derivativeId, opts.nowMs; see the README.
   *  step: 1 | 5 | 15 | "auto"; paths: a number or "auto" (batches until the average is pinned to opts.tolInr, or maxPaths). */
  async function analyseIntraday(opts) {
    const t0 = Date.now();
    const rules = Object.assign({}, DEFAULT_RULES, opts.rules || {});
    const k = T.cfgWith(opts.cfg), progress = opts.onProgress || (() => {}), cancelled = opts.isCancelled || (() => false);
    const nowMs = opts.nowMs, holidays = opts.holidays || null, rates = opts.rates || T.DEFAULT_RATES;
    const auto = opts.paths === "auto", paths = auto ? 0 : Math.round(opts.paths || 4000), seed = opts.seed == null ? 7 : opts.seed;
    const minPaths = opts.minPaths || 1000, maxPaths = opts.maxPaths || 12000, budget = opts.timeBudgetMs || 0;
    const chain = opts.chain, notes = [];
    if (!chain) throw new T.TMError("an option chain is needed: it carries the strike's price and the volatility");
    const sessions = (opts.sessions || []).filter((s) => s.count >= 20);
    const mode = opts.mode === "position" ? "position" : "new";

    // ---- the contract
    const row = opts.derivativeId
      ? chain.strikes.find((x) => (x.ce && x.ce.id === opts.derivativeId) || (x.pe && x.pe.id === opts.derivativeId))
      : chain.strikes.find((x) => x.strike === Number(opts.strike));
    if (!row) throw new T.TMError("that strike is not in the loaded chain: load the expiry with strikes around it");
    const kind = opts.derivativeId ? (row.ce && row.ce.id === opts.derivativeId ? "CE" : "PE") : opts.side;
    const leg = kind === "CE" ? row.ce : row.pe;
    if (!leg || !leg.ltp) throw new T.TMError("no price for that strike in the chain");
    const K = row.strike;
    const lotSize = Math.round(opts.lotSize || leg.lot_size || chain.lot_size || 0);
    if (!lotSize) throw new T.TMError("no lot size for this contract");
    const exchange = opts.exchange || (T.BSE_UNDERLYINGS[String(chain.underlying || "").toUpperCase()] ? "BFO" : "NFO");
    const r = k.risk_free_rate, expIso = T.expiryIso(chain.expiry);
    const qLots = mode === "position" ? Math.max(1, Math.round((opts.position || {}).remainingLots || opts.lots || 1)) : Math.max(1, Math.round(opts.lots || 1));

    // ---- the clock, and the volatility solved from the quote's mid on that clock
    const sp = startPoint(nowMs, holidays, !!opts.startAtOpen, opts.entryAt);
    const noEntryMin = minOf(rules.noEntryAfter);
    if (minOf(rules.exitAt) !== TRADEMIN) throw new T.TMError("the exit time is fixed at 15:15 for now");
    if ((sp.kind === "live" && Math.ceil(sp.xNow) >= TRADEMIN) || (mode === "position" && sp.kind !== "live")) throw new T.TMError("an open position can be managed only while the session runs, and it is past " + rules.exitAt + " or the market is closed: the plan has no bars left today");
    if (sp.kind === "live" && Math.ceil(sp.xNow) > noEntryMin && mode !== "position") throw new T.TMError("no new entries after " + rules.noEntryAfter + " (the desk rule): run it from the next open instead");
    if (expIso < sp.day) throw new T.TMError("this expiry (" + chain.expiry + ") is before the run starts: load a later expiry");
    const phi = opts.daily ? T.overnightShare(opts.daily) : 0.25;
    const prof = opts.profile || sessionProfile(sessions), clock = makeClock(prof.shares5, phi, sp.day, expIso);
    const q = quoteMoment(chain, holidays) || { day: sp.day, m: Math.ceil(sp.xNow || 0) };
    if (expIso < q.day) throw new T.TMError("the chain was struck after this expiry");
    const TeffQ = q.m >= DAYMIN ? Math.max(daysBetween(q.day, expIso) / 365, 0) : makeClock(prof.shares5, phi, q.day, expIso).Teff(q.m);
    const bid = leg.bid || null, ask = leg.ask || null, mid = bid && ask ? (bid + ask) / 2 : leg.ltp, half = bid && ask ? (ask - bid) / 2 : 0;
    const F = chain.forward || chain.spot, basis = F - chain.spot, spot0 = Number(opts.spot0 || chain.spot);
    const sigmaQuote = T.impliedVol(mid, F, K, TeffQ, r, kind) || T.impliedVol(leg.ltp, F, K, TeffQ, r, kind);
    if (!sigmaQuote) throw new T.TMError("the quote cannot be priced (below intrinsic value, or no time left)");
    const sigma = opts.ivPct ? opts.ivPct / 100 : sigmaQuote;
    const sessVar = sigma * sigma * (1 - phi) / 365;                  // the variance one full session spends
    const volMult = opts.volMult && opts.volMult > 0 ? opts.volMult : 1;     // the market's pace against the option's (learned)
    const delta = T.delta76(F, K, TeffQ, sigmaQuote, r, kind);
    const spec = { kind, K, basis, sigma, r, half, phi, day: sp.day, expiryIso: expIso, delta, lotSize, exchange, m0: 0 };
    const pricer = makePricer(spec, clock);
    const gapVar = sp.kind === "open" ? Math.max(sigma * sigma * (TeffQ - clock.Teff(0)), 0) : 0;
    const eMin1 = entryMinute(sp, 1), eMin5 = entryMinute(sp, 5);
    const entryNow = pricer.price(spot0, clock.Teff(sp.kind === "open" ? eMin5 : eMin1)) + half;
    const entryFixed = mode === "new" && opts.entryPrice && sp.kind === "live" ? Number(opts.entryPrice) : null;
    const entryPlan = mode === "position" ? Number(opts.position.entry) : (entryFixed != null ? entryFixed : r05(sp.kind === "open" ? (ask || leg.ltp) : entryNow));
    const planLv = levelsFor(entryPlan, rules, spot0, delta);
    const posState = mode === "position"
      ? { entry: Number(opts.position.entry), stop: Number(opts.position.stop != null ? opts.position.stop : planLv.sl), t1Done: !!opts.position.t1Done, lots: qLots } : null;
    const bidNow = pricer.bidAt(spot0, clock.Teff(sp.kind === "live" ? eMin1 : eMin5));
    const valueNow = qLots * lotSize * bidNow - T.fnoCosts("sell", qLots * lotSize * bidNow, "OPT", exchange, rates).total;

    // ---- the candle size
    const barSd5 = spot0 * Math.sqrt(sessVar * volMult * volMult / BARS5);             // one typical 5-minute move, in index points
    const stopPts = entryPlan * rules.slPct / 100 / Math.max(Math.abs(delta), 0.05);
    const tight = stopPts / barSd5;
    const chosen = STEPS.includes(+opts.step);
    const step = chosen ? +opts.step : (tight < 3 ? 1 : 5);
    const stepNote = chosen ? "chosen: " + step + "-minute bars" + (step === 15 && tight < 2 ? " (coarse for a stop only " + tight.toFixed(1) + " typical 5-minute moves away: the odds of reaching a nearby target first read low)" : "")
      : step === 1 ? "auto: 1-minute bars (your stop is " + tight.toFixed(1) + " typical 5-minute moves away; finer bars are cheap here and drop the 'stop first' assumption for bars that touch both levels)"
        : "auto: 5-minute bars (your stop is " + tight.toFixed(1) + " typical 5-minute moves away)";

    // ---- the three models
    const wanted = opts.models || ["implied", "bootstrap", "replay"];
    const gaps = overnightGaps(opts.daily), volModeB = opts.volMode === "implied" ? "implied" : "realised";
    const libs = {};
    const libFor = (s) => (libs[s] !== undefined ? libs[s] : (libs[s] = barLibrary(sessions, clock, s === 15 ? 15 : 5)));
    const libMain = libFor(step === 15 ? 15 : 5);
    if (!libMain && (wanted.includes("bootstrap") || wanted.includes("replay"))) notes.push("the bootstrap and the replay need full sessions of 5-minute bars (about 3 are available at first, more as the app saves them); they are left out");
    const realisedAnn = libMain ? libMain.rho / Math.sqrt((1 - phi) / 365) : null;
    const nCalDays = sp.kind === "open" ? Math.max(1, daysBetween(q.day, sp.day)) : 1;
    const gapDraw = (rng, mult) => { const v = gapVar * (mult || 1) * (mult || 1); return rng.gauss() * Math.sqrt(v) - 0.5 * v; };
    const gridCache = {};
    const gridOf = (s, entryStep) => {          // a model's grid: its own step, and the entry minute that step allows
      const e = entryMinute(sp, entryStep), key = s + ":" + e;
      return gridCache[key] || (gridCache[key] = pricer.grid(s, e, sp.kind === "open" ? 0 : e));
    };
    const genImplied = (g) => (rng, mult) => {
      const out = []; let pc = spot0;
      if (sp.kind === "open") pc *= Math.exp(gapDraw(rng, mult));
      const mm = (mult || 1) * volMult, sv = sessVar * mm * mm;
      for (let n = 0; n < g.n; n++) {
        const m = g.jStart + n * g.step, b = bridgeBar(rng, pc, sv * (clock.S[m + g.step] - clock.S[m]));
        out.push(b); pc = b[3];
      }
      return out;
    };
    const gapBoot = (rng, pc) => {
      if (sp.kind !== "open") return pc;
      if (gaps) { const z = gaps.z[rng.randrange(gaps.z.length)]; return pc * Math.exp(volModeB === "implied" ? z / gaps.sd * Math.sqrt(gapVar) : z * Math.sqrt(nCalDays)); }
      return pc * Math.exp(gapDraw(rng));
    };
    const genBoot = (g) => {
      const lib = libFor(g.step === 15 ? 15 : 5), kappa = volModeB === "implied" ? Math.sqrt(sessVar) / lib.rho : 1;
      if (g.step === 1) {                          // real 5-minute bars, each split into five 1-minute bars
        return (rng) => {
          const out = []; let pc = gapBoot(rng, spot0), j = drawIndex(rng, lib);
          for (let n = 0; n < g.n; n += 5) {
            const m = g.jStart + n, e = lib.lib[j], W = clock.S[m + 5] - clock.S[m], sq = Math.sqrt(W) * kappa;
            const w = [0, 1, 2, 3, 4].map((i) => clock.S[m + i + 1] - clock.S[m + i]);
            const bars = splitBar(rng, pc, e[2] * sq, e[0] * sq, e[1] * sq, w);
            for (let i = 0; i < 5 && n + i < g.n; i++) out.push(bars[i]);
            pc = bars[4][3];
            j = rng.random() < 1 / 6 ? drawIndex(rng, lib) : (j + 1) % lib.lib.length;
          }
          return out;
        };
      }
      return (rng) => {
        const out = []; let pc = gapBoot(rng, spot0), j = drawIndex(rng, lib);
        for (let n = 0; n < g.n; n++) {
          const m = g.jStart + n * g.step, sq = Math.sqrt(clock.S[m + g.step] - clock.S[m]) * kappa, e = lib.lib[j], x = e[2] * sq;
          const mx = Math.max(e[0] * sq, x, 0), mn = Math.min(e[1] * sq, x, 0);
          out.push([pc, pc * Math.exp(mx), pc * Math.exp(mn), pc * Math.exp(x)]); pc *= Math.exp(x);
          j = rng.random() < 1 / 6 ? drawIndex(rng, lib) : (j + 1) % lib.lib.length;
        }
        return out;
      };
    };
    const tapesFor = (g) => {                      // each full session, from up to 30 minutes earlier in the day than now (live), or from the open
      const tapes = [], L5 = Math.round((TRADEMIN - g.jStart) / 5);
      for (let si = 0; si < sessions.length; si++) {
        const s = sessions[si];
        if (!s.full || s.date === sp.day) continue;
        const prev = si > 0 ? sessions[si - 1] : null, gapLog = prev && prev.bars[LAST5] ? Math.log(s.bars[0].o / prev.bars[LAST5].c) : null;
        if (sp.kind === "open") { if (gapLog !== null) tapes.push({ date: s.date, b: 0, gapLog, bars: s.bars.slice(0, L5) }); continue; }
        const b0 = g.jStart / 5;
        for (let b = Math.max(0, b0 - 6); b <= b0; b++) { if (b + L5 - 1 > LAST5) continue; tapes.push({ date: s.date, b, gapLog, bars: s.bars.slice(b, b + L5) }); }
      }
      return tapes;
    };
    const tapePath = (tp, g) => {
      const scale = (sp.kind === "open" ? spot0 * Math.exp(tp.gapLog) : spot0) / tp.bars[0].o;
      const b5 = tp.bars.map((b) => ({ o: b.o * scale, h: b.h * scale, l: b.l * scale, c: b.c * scale }));
      return (g.step === 15 ? aggregate(b5, 3) : b5).map((b) => [b.o, b.h, b.l, b.c]);
    };

    // ---- run each model
    const keys = (opts.variants || Object.keys(VARIANTS)).filter((x) => VARIANTS[x]);
    const ctx = { pricer, spec, rules, qLots, mode, posState, rates, exchange, valueNow, delta };
    const active = wanted.filter((m) => m === "implied" || (libMain && (m === "bootstrap" || m === "replay")));
    const models = {}, perf = {};
    const tol = opts.tolInr || Math.max(25, 0.01 * entryPlan * qLots * lotSize);
    let doneWork = 0;
    const totalWork = active.reduce((a, m) => a + (m === "replay" ? 40 : (auto ? maxPaths / 3 : paths)), 0) || 1;
    const stepOf = (m) => (m === "replay" ? (step === 15 ? 15 : 5) : step);
    for (const m of active) {
      const tm0 = Date.now(), rng = T.makeRng(seed + "-" + m), accs = newAcc(keys), fan = [];
      const g = gridOf(stepOf(m), m === "implied" ? step : Math.max(step, 5) === 15 ? 15 : 5);
      let tapes = null, gen = null;
      if (m === "replay") {
        tapes = tapesFor(g);
        if (!tapes.length) { models.replay = { n: 0, step: g.step, note: sp.kind === "open" ? "this needs two consecutive full sessions in the history" : "no full session in the history reaches back to this start" }; continue; }
      } else gen = m === "implied" ? genImplied(g) : genBoot(g);
      const todo = m === "replay" ? tapes.length : (auto ? maxPaths : paths), fanEvery = Math.max(1, Math.round(5 / g.step));
      let used = 0, entry = null, batchEnd = auto && m !== "replay" ? Math.min(minPaths, todo) : todo, stopEarly = false;
      const di = Math.max(keys.indexOf("desk"), 0);
      for (let p = 0; p < todo && !stopEarly; p++) {
        const bars = m === "replay" ? tapePath(tapes[p], g) : gen(rng);
        const res = playPath(ctx, bars, g, accs, keys, entryFixed);
        if (!res) continue;
        if (entry == null) entry = res.E;
        used++;
        if (used <= 1500) { const rowP = []; for (let n = 0; n < res.Ltr; n += fanEvery) rowP.push(res.pb(n).c); fan.push(rowP); }
        if ((++doneWork & 255) === 0) { progress(Math.min(0.95, doneWork / totalWork), m); await T.tick(); if (cancelled()) throw new T.TMError("cancelled"); }
        if (auto && m !== "replay" && p + 1 >= batchEnd) {
          const a = accs[di], mu = a.sum / a.n, se = Math.sqrt(Math.max(a.sumsq - a.n * mu * mu, 0) / Math.max(a.n - 1, 1)) / Math.sqrt(a.n);
          if (se <= tol || (Math.abs(mu) > 5 * se && se <= 3 * tol) || (budget && Date.now() - tm0 > budget)) stopEarly = true;
          batchEnd = Math.min(batchEnd + 500, todo);
        }
      }
      const ms = Date.now() - tm0;
      models[m] = { n: used, step: g.step, variants: summarise(accs, keys, rules, mode), fan: used ? fanOf(fan) : null, fan_start_min: g.m0, fan_step_min: g.step * fanEvery,
        entry, entry_minute: g.m0, ms };
      perf[m] = { ms, paths: used, bars_per_path: g.n - g.eIdx, step: g.step };
      if (auto && m !== "replay") models[m].se_target = tol;
    }
    function fanOf(store) {
      const cols = [], nn = store[0] ? store[0].length : 0;
      for (let n = 0; n < nn; n++) {
        const qs = T.quantiles(store.map((rw) => rw[n]), [0.05, 0.25, 0.5, 0.75, 0.95]);
        cols.push([qs.p5, qs.p25, qs.p50, qs.p75, qs.p95]);
      }
      return cols;
    }
    const stepWord = (s) => (s === 1 ? "1-minute" : s + "-minute");
    if (models.implied) models.implied.what = "risk-neutral paths at " + (100 * sigma * volMult).toFixed(1) + "% on the session clock (the chain quotes " + (100 * sigmaQuote).toFixed(1) + "% on a calendar" + (volMult !== 1 ? "; the market's pace is set at " + volMult.toFixed(2) + "x the option's, from what this app has measured" : "") + "), a U-shaped session, " + stepWord(models.implied.step) + " bars each bridged for its high and low";
    if (models.bootstrap) models.bootstrap.what = "paths stitched from " + libFor(5).lib.length + " real 5-minute bars (" + sessions.filter((s) => s.full).length + " full sessions) in blocks of about 30 minutes, drift removed, at the " + (volModeB === "implied" ? "implied" : "recent realised") + " volatility level" + (models.bootstrap.step === 1 ? "; each 5-minute move is bridged to the minute" : models.bootstrap.step === 15 ? "; regrouped into 15-minute bars" : "");
    if (models.replay && models.replay.n !== undefined) {
      const ts = tapesFor(gridOf(stepOf("replay"), stepOf("replay"))), ds = [...new Set(ts.map((t) => t.date))];
      if (ts.length) Object.assign(models.replay, { sessions: ds, independent: ds.length,
        what: "the plan on " + ds.length + " recent sessions' tapes (" + stepWord(models.replay.step) + " bars, the data's own resolution), started up to 30 minutes either side of this start; the paths overlap, so read it as an illustration, not a sample" });
    }
    if (step === 1 && models.replay && models.replay.step > 1) notes.push("the recent-sessions replay runs on 5-minute bars, the finest history the data feed gives");

    // ---- volatility sensitivity: the market moves at a multiple of the implied level, the option was bought at the quote
    const sens = [];
    if (opts.sensitivity !== false && active.includes("implied")) {
      const g = gridOf(step, step);
      for (const mult of [0.7, 0.85, 1.0, 1.2, 1.4]) {
        const rng = T.makeRng(seed + "-sens-" + mult), acc = newAcc(["desk"]), nP = Math.min(auto ? 1500 : paths, 1500), gen = genImplied(g);
        for (let p = 0; p < nP; p++) playPath(ctx, gen(rng, mult), g, acc, ["desk"], entryFixed);
        const a = acc[0];
        sens.push({ mult, n: a.n, expectancy: a.sum / a.n, p_profit: a.pnl.filter((x) => x > 0).length / a.n });
      }
    }
    progress(1, "done");
    const flat = pricer.bidAt(spot0, clock.Teff(TRADEMIN));      // the index unchanged all the way: only time has passed
    const gi = gridOf(step, step);
    spec.m0 = models.implied ? models.implied.entry_minute : gi.m0;
    return {
      meta: {
        mode, side: kind, strike: K, expiry: chain.expiry, underlying: chain.underlying, lot: lotSize, lots: qLots, qty: qLots * lotSize, exchange,
        spot0, forward: F, basis, start: sp, startLabel: sp.label, step, step_note: stepNote, tight, bars: gi.n - gi.eIdx, barsFrom: hm(gi.m0), entry_min: gi.m0, exitAt: rules.exitAt,
        sigma_calendar_pct: 100 * sigmaQuote, sigma_clock_pct: 100 * sigma, iv_overridden: !!opts.ivPct, vol_mult: volMult, phi, delta, mid, bid, ask, half_spread: half,
        days_to_expiry: clock.D, profile: prof.source, realised_ann_pct: realisedAnn ? 100 * realisedAnn : null,
        realised_vs_implied: realisedAnn ? realisedAnn / sigma : null, vol_mode: volModeB, quote_at: q,
        entry_plan: entryPlan, entry_flat: r05(entryNow), outlay: entryPlan * qLots * lotSize, bid_now: bidNow, value_now: valueNow, flat_at_exit: flat,
        flat_decay_pct: (flat - bidNow) / Math.max(bidNow, 0.05) * 100, flat_decay_inr: (flat - bidNow) * qLots * lotSize,
        position: posState, day_stop: rules.dayStop, ms: Date.now() - t0, perf, auto_paths: auto, tol_inr: tol, spec, bar_sd5_pts: barSd5, stop_pts: stopPts,
      },
      levels: Object.assign({}, planLv, { risk_inr: (entryPlan - planLv.sl) * qLots * lotSize }),
      rules, models, gate: mode === "new" ? gateOf(models) : null, blend: blendOf(models, opts.weights), sensitivity: sens, notes,
      variants: keys.map((kx) => ({ key: kx, label: VARIANTS[kx].label, note: VARIANTS[kx].note })),
    };
  }

  return { DAYMIN, TRADEMIN, BARS5, LAST5, STEPS, DEFAULT_RULES, VARIANTS, CLASSES, parseBars, packSession, unpackSession, mergeSessions,
    sessionProfile, blendProfile, paramShares, paramWeights, minuteShares, splitBar, startPoint, entryMinute, makeClock, quoteMoment, levelsFor, runDesk, makePricer,
    playTape, analyseIntraday, gateOf, blendOf, hm, minOf, r05, istParts, isTradingDay, nextTradingDay, barLibrary, aggregate, overnightGaps, bridgeBar };
});
