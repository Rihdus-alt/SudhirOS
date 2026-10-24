/* The Time Machine's learning layer. A pure state machine: state in, state out, no storage and no network (the page keeps
 * the state in its database). What it learns, and what it does not:
 *
 *   It learns from data the app collects itself: 5-minute sessions it saves, the option chain's volatility at the time of
 *   each run, and what each prediction did afterwards (on the real index tape, and in your own closed trades).
 *     1. the market's PACE against the option's implied pace (realised variance / predicted variance, shrunk toward 1,
 *        bounded): the implied model runs at that pace once there are enough observations;
 *     2. CALIBRATION of the probabilities the models give (profit, reaches T1, stopped out): a logistic recalibration
 *        with a strong prior at "no change", applied only once there are enough resolved predictions, and reported against
 *        a held-out slice so it cannot flatter itself;
 *     3. MODEL WEIGHTS: which of the three models' probabilities have scored best (log loss), as a bounded Hedge update;
 *     4. SPEED: milliseconds per path and bar, to choose how many paths a time budget buys.
 *   It does not learn where the market goes next, and it never changes its own rules, code or any order. Every number it
 *   learns is bounded, shrunk toward the unlearned value, shown to you and can be frozen or reset.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./tmx-core.js"), require("./tmx-intraday.js"));
  else root.TMXL = factory(root.TMX, root.TMXI);
})(typeof self !== "undefined" ? self : this, function (T, I) {
  "use strict";

  const VERSION = 1;
  const EVENTS = ["profit", "t1", "stop"];
  const MODELS = ["implied", "bootstrap", "replay"];
  const PRIOR_W = { implied: 0.4, bootstrap: 0.4, replay: 0.2 };
  const LIM = { pace: [0.75, 1.35], pairs: 400, obs: 120, seen: 800, log: 60 };
  const N_MIN = { pace: 8, calibPaper: 30, calibTrades: 15, weights: 25 };
  const sum = T.sum;
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const logit = (p) => Math.log(p / (1 - p));
  const sigm = (z) => 1 / (1 + Math.exp(-z));
  const cp = (p) => clamp(p, 0.02, 0.98);

  function emptyState() {
    return { v: VERSION, created: null, updated: null, frozen: false,
      pace: {},                                       // by index: {sess: [[realisedVar, predictedVar], ...], gap: [...]}
      calib: { profit: [], t1: [], stop: [] },        // [p, outcome] on the real tape, by event (the blend's probability)
      calibTrades: { profit: [], t1: [], stop: [] },  // the same for trades you actually took
      scores: { implied: { n: 0, ll: 0 }, bootstrap: { n: 0, ll: 0 }, replay: { n: 0, ll: 0 } },
      perf: {},                                       // by "model|step": {msPerBar, n}
      seen: [], log: [] };
  }
  function normalise(s) {
    const e = emptyState();
    if (!s || s.v !== VERSION) return e;
    for (const k of Object.keys(e)) if (s[k] === undefined) s[k] = e[k];
    return s;
  }
  const note = (st, at, msg) => { st.log.push({ t: at, msg }); if (st.log.length > LIM.log) st.log.splice(0, st.log.length - LIM.log); };
  function stamp(st, at) { st.updated = at; if (!st.created) st.created = at; }
  function seenAdd(st, id) { st.seen.push(id); if (st.seen.length > LIM.seen) st.seen.splice(0, st.seen.length - LIM.seen); }

  // ------------------------------------------------------------------ the saved sessions: profile and weights by recency
  /** Sessions with a recency weight: 0.97 per session of age, so a month counts about half (the saved library is data, not
   *  state: this is all the "learning" the profile and the bootstrap library need). */
  function weighted(sessions, half) {
    const list = (sessions || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1)), lam = Math.pow(0.5, 1 / (half || 30)), n = list.length;
    return list.map((s, i) => Object.assign({}, s, { w: Math.pow(lam, n - 1 - i) }));
  }
  /** The learned variance profile: the U shape blended with the mean squared 5-minute move of each bar over the saved full
   *  sessions (recency-weighted). */
  function profileOf(sessions) {
    const full = weighted((sessions || []).filter((s) => s.full)), param = I.paramShares();
    if (full.length < 2) return I.sessionProfile(sessions);
    const e = new Array(72).fill(0), tw = sum(full.map((s) => s.w));
    for (const s of full) for (let j = 0; j < 72; j++) { const b = s.bars[j], x = Math.log(b.c / b.o); e[j] += s.w * x * x / tw; }
    const nEff = tw * tw / sum(full.map((s) => s.w * s.w));
    const p = I.blendProfile(param, e, nEff);
    p.source = "a U shape blended with " + full.length + " saved full sessions, recent ones weighted more (" + Math.round(100 * nEff / (nEff + 6)) + "% weight)";
    return p;
  }

  // ------------------------------------------------------------------ pace: the market's variance against the option's
  /** The variance the quote predicted for the rest of the session, from the 5-minute boundary m5 on (a function of the
   *  contract spec, the profile and the minute): sigma^2 * (Teff(m5) - Teff(15:15)). */
  function predictedVar(sigma, phi, shares5, day, expiryIso, m5) {
    const clock = I.makeClock(shares5, phi, day, expiryIso);
    return sigma * sigma * (clock.Teff(m5) - clock.Teff(I.TRADEMIN));
  }
  /** The variance that actually arrived on a session from the 5-minute boundary m5 to 15:15: the sum of squared 5-minute
   *  moves (open to close of each bar). */
  function realisedVar(session, m5) {
    let v = 0, n = 0;
    for (let j = Math.ceil(m5 / 5); j < 72; j++) { const b = session.bars[j]; if (b) { const x = Math.log(b.c / b.o); v += x * x; n++; } }
    return n >= 6 ? v : null;
  }
  /** Fold in one resolved observation (kind "sess" or "gap") for an index. Idempotent by id. */
  function observePace(st, ix, kind, real, pred, id, at) {
    st = normalise(st);
    if (st.frozen || (id && st.seen.includes(id)) || !(real >= 0) || !(pred > 0)) return st;
    const p = st.pace[ix] || (st.pace[ix] = { sess: [], gap: [] });
    const before = paceOf(st, ix, kind).mult;
    p[kind].push([real, pred]);
    if (p[kind].length > LIM.obs) p[kind].splice(0, p[kind].length - LIM.obs);
    if (id) seenAdd(st, id);
    stamp(st, at || null);
    const after = paceOf(st, ix, kind);
    if (after.applied && Math.abs(after.mult - before) > 0.02) note(st, at, ix + " " + (kind === "sess" ? "session" : "overnight") + " pace " + before.toFixed(2) + "x -> " + after.mult.toFixed(2) + "x (" + after.n + " observations)");
    return st;
  }
  /** The learned pace multiplier (volatility, so the square root of the variance ratio), shrunk toward 1 by n0 pseudo
   *  observations of ratio 1, recency-weighted, bounded; applied only from N_MIN.pace observations. A bootstrap interval says
   *  how sure it is. */
  function paceOf(st, ix, kind, rng) {
    const arr = ((normalise(st).pace[ix] || {})[kind]) || [], n = arr.length;
    if (!n) return { mult: 1, n: 0, applied: false, ci: [1, 1], ratio: 1 };
    const lam = 0.97, n0 = 6;
    const est = (a) => {
      let r = 0, p = 0, w = 0;
      a.forEach(([re, pr], i) => { const wi = Math.pow(lam, a.length - 1 - i); r += wi * re; p += wi * pr; w += wi; });
      const pbar = p / w;
      return (r + n0 * pbar) / (p + n0 * pbar);
    };
    const ratio = est(arr), mult = clamp(Math.sqrt(ratio), LIM.pace[0], LIM.pace[1]);
    const rg = rng || T.makeRng("pace-" + ix + kind + n), bs = [];
    for (let b = 0; b < 300; b++) { const res = []; for (let i = 0; i < n; i++) res.push(arr[rg.randrange(n)]); bs.push(Math.sqrt(est(res))); }
    bs.sort((a, b) => a - b);
    return { mult: n >= N_MIN.pace ? mult : 1, learned: mult, n, applied: n >= N_MIN.pace, ratio, ci: [clamp(bs[7], LIM.pace[0], LIM.pace[1]), clamp(bs[292], LIM.pace[0], LIM.pace[1])] };
  }

  // ------------------------------------------------------------------ calibration
  /** Logistic recalibration p' = sigmoid(a + b * logit(p)) fitted by Newton's method with a ridge prior at (a, b) = (0, 1),
   *  strength lam (equivalent to lam observations that say "no change"). */
  function fitCalibration(pairs, lam) {
    lam = lam == null ? 6 : lam;
    let a = 0, b = 1;
    for (let it = 0; it < 40; it++) {
      let g0 = -lam * a, g1 = -lam * (b - 1), h00 = lam, h01 = 0, h11 = lam;
      for (const [p, y] of pairs) {
        const z = logit(cp(p)), q = sigm(a + b * z), w = q * (1 - q);
        g0 += y - q; g1 += (y - q) * z; h00 += w; h01 += w * z; h11 += w * z * z;
      }
      const det = h00 * h11 - h01 * h01;
      if (Math.abs(det) < 1e-12) break;
      const da = (h11 * g0 - h01 * g1) / det, db = (-h01 * g0 + h00 * g1) / det;
      a += da; b += db;
      if (Math.abs(da) + Math.abs(db) < 1e-9) break;
    }
    return { a, b };
  }
  const applyCal = (c, p) => sigm(c.a + c.b * logit(cp(p)));
  const brier = (pairs, f) => (pairs.length ? sum(pairs.map(([p, y]) => Math.pow((f ? f(p) : p) - y, 2))) / pairs.length : null);
  /** The calibration of one event: fitted on the real-tape pairs (and, when there are enough, nudged by your own trades'),
   *  with a held-out check (fit on the first 70%, score the rest). */
  function calibrationOf(st, event) {
    st = normalise(st);
    const paper = st.calib[event], trades = st.calibTrades[event];
    const out = { n: paper.length, nTrades: trades.length, applied: false, a: 0, b: 1, held: null };
    if (paper.length >= N_MIN.calibPaper) {
      const c = fitCalibration(paper);
      out.a = c.a; out.b = c.b;
      const cut = Math.floor(paper.length * 0.7), train = paper.slice(0, cut), test = paper.slice(cut);
      if (test.length >= 10) { const ct = fitCalibration(train); out.held = { n: test.length, raw: brier(test), calibrated: brier(test, (p) => applyCal(ct, p)) }; }
      out.applied = true;
    }
    if (trades.length >= N_MIN.calibTrades) {          // your own execution: what your fills and exits did against the plan
      const ct = fitCalibration(trades, 12);
      out.tradesFit = ct; out.tradesBrier = { raw: brier(trades), calibrated: brier(trades, (p) => applyCal(ct, p)) };
    }
    out.fn = (p) => (out.applied ? applyCal(out, p) : p);
    return out;
  }

  // ------------------------------------------------------------------ predictions and their outcomes
  /** A stored prediction from an engine report (what the models said, and the contract needed to play it on a tape later). */
  function predictionFrom(rep, extra) {
    const m = rep.meta, models = {};
    for (const k of MODELS) {
      const v = rep.models[k] && rep.models[k].variants && rep.models[k].variants.desk;
      if (v && v.n) models[k] = { profit: v.p_profit, t1: v.p_hit_t1, stop: v.p_stop, exp: v.expectancy, n: v.n };
    }
    const spec = Object.assign({}, m.spec, { m0: Math.ceil(((rep.models.implied || {}).entry_minute || m.entry_min) / 5) * 5 });
    const E = m.start.kind === "open" ? null : (rep.models.implied && rep.models.implied.entry) || m.entry_plan;     // from the open the fill is whatever the open gives
    return Object.assign({ day: m.start.day, kind: "scan", spec, E, lots: m.lots, rules: { slPct: rep.rules.slPct, tp1Pct: rep.rules.tp1Pct, tp2Pct: rep.rules.tp2Pct, breathPct: rep.rules.breathPct },
      levels: { sl: rep.levels.sl, t1: rep.levels.t1, t2: rep.levels.t2 }, step: m.step, models, blend: rep.blend ? { profit: rep.blend.p_profit, t1: rep.blend.p_hit_t1, stop: rep.blend.p_stop, exp: rep.blend.expectancy } : null }, extra || {});
  }
  /** What the plan would have done on the real tape of that day (5-minute bars from the entry on). */
  function resolvePaper(pred, session, profile, rates) {
    if (!session || !session.full || !pred.spec) return null;
    const m0 = pred.spec.m0, bars = session.bars.slice(m0 / 5);
    if (bars.some((b) => !b)) return null;
    const out = I.playTape(pred.spec, bars, profile, Object.assign({}, I.DEFAULT_RULES, pred.rules), rates, pred.lots, pred.E);
    return out ? { profit: out.profit, t1: out.t1, stop: out.stop, pnl: out.pnl } : null;
  }
  /** What your closed trade did against the levels it was planned with: profit, any exit at or above T1, the first exit at or
   *  below the stop. trade: {entry, exits: [{price, qty}], realisedNet}. */
  function resolveTrade(pred, trade) {
    const ex = (trade.exits || []).filter((e) => +e.qty > 0 && +e.price > 0);
    if (!ex.length || !pred.levels) return null;
    const tol = 0.05 + 1e-9;
    return { profit: trade.realisedNet > 0 ? 1 : 0, t1: ex.some((e) => +e.price >= pred.levels.t1 - tol) ? 1 : 0, stop: +ex[0].price <= pred.levels.sl + tol ? 1 : 0, pnl: trade.realisedNet };
  }
  const logLoss = (p, y) => -(y ? Math.log(cp(p)) : Math.log(1 - cp(p)));
  /** Fold a resolved prediction into calibration pairs (paper or trade) and the models' scores (paper only). Idempotent by id. */
  function observeOutcome(st, pred, outcome, source, at) {
    st = normalise(st);
    const id = "out:" + source + ":" + pred.id;
    if (st.frozen || !outcome || st.seen.includes(id)) return st;
    const before = EVENTS.map((e) => calibrationOf(st, e).b);
    const bucket = source === "trade" ? st.calibTrades : st.calib;
    for (const e of EVENTS) {
      const p = pred.blend ? pred.blend[e] : null;
      if (p != null) { bucket[e].push([p, outcome[e]]); if (bucket[e].length > LIM.pairs) bucket[e].splice(0, bucket[e].length - LIM.pairs); }
    }
    if (source !== "trade") for (const k of MODELS) {
      if (!pred.models[k]) continue;
      st.scores[k].n++; st.scores[k].ll += sum(EVENTS.map((e) => logLoss(pred.models[k][e], outcome[e]))) / EVENTS.length;
    }
    seenAdd(st, id); stamp(st, at || null);
    EVENTS.forEach((e, i) => { const c = calibrationOf(st, e); if (c.applied && Math.abs(c.b - before[i]) > 0.05) note(st, at, "calibration of '" + e + "': slope " + before[i].toFixed(2) + " -> " + c.b.toFixed(2) + " (" + c.n + " resolved)"); });
    return st;
  }

  // ------------------------------------------------------------------ model weights (bounded Hedge)
  /** Scale the weights until clamping each into [lo, hi] leaves them summing to one (so no model is ever switched off, or
   *  allowed to carry the blend alone). */
  function bounded(w, lo, hi) {
    const keys = Object.keys(w), f = (c) => sum(keys.map((k) => clamp(w[k] * c, lo, hi)));
    let a = 1e-12, b = 1e12;
    for (let i = 0; i < 200; i++) { const mid = Math.sqrt(a * b); if (f(mid) < 1) a = mid; else b = mid; }
    const out = {}, c = Math.sqrt(a * b); let tot = 0;
    for (const k of keys) { out[k] = clamp(w[k] * c, lo, hi); tot += out[k]; }
    for (const k of keys) out[k] /= tot;
    return out;
  }
  function weightsOf(st) {
    st = normalise(st);
    const done = Math.min.apply(null, MODELS.map((k) => st.scores[k].n));
    if (done < N_MIN.weights) return { w: Object.assign({}, PRIOR_W), learned: false, n: done };
    const L = {}; for (const k of MODELS) L[k] = st.scores[k].ll / st.scores[k].n;
    const best = Math.min.apply(null, MODELS.map((k) => L[k])), eta = 12 * Math.min(1, done / 100);      // grows with the evidence
    const raw = {}; for (const k of MODELS) raw[k] = PRIOR_W[k] * Math.exp(-eta * (L[k] - best));
    const w = bounded(raw, 0.1, 0.7);
    return { w, learned: true, n: done, loss: L };
  }

  // ------------------------------------------------------------------ speed
  function observePerf(st, model, step, bars, paths, ms) {
    st = normalise(st);
    if (!(ms > 0) || !(paths > 0) || !(bars > 0)) return st;
    const key = model + "|" + step, x = ms / (paths * bars), p = st.perf[key] || { msPerBar: x, n: 0 };
    p.msPerBar = p.n ? 0.8 * p.msPerBar + 0.2 * x : x; p.n++;
    st.perf[key] = p;
    return st;
  }
  /** How many paths per model a time budget buys at this step, from what this device has measured (a fresh guess until then). */
  function suggestPaths(st, budgetMs, step, bars, models) {
    st = normalise(st);
    const per = (models || MODELS.slice(0, 2)).reduce((a, m) => a + ((st.perf[m + "|" + step] || {}).msPerBar || 0.0016 * (step === 1 ? 1.2 : 1)) * bars, 0);
    return clamp(Math.floor(budgetMs / per), 500, 12000);
  }

  // ------------------------------------------------------------------ what the page applies
  function learned(st, ix) {
    st = normalise(st);
    const pace = paceOf(st, ix, "sess"), gap = paceOf(st, ix, "gap"), wts = weightsOf(st), cal = {};
    for (const e of EVENTS) cal[e] = calibrationOf(st, e);
    return { volMult: pace.applied ? pace.mult : 1, gapMult: gap.applied ? gap.mult : 1, weights: wts.w, weightsLearned: wts.learned, pace, gap, calibration: cal,
      resolved: st.calib.profit.length, trades: st.calibTrades.profit.length, frozen: st.frozen };
  }
  const calibrate = (st, probs) => {                 // {profit, t1, stop} through the calibration
    const out = {};
    for (const e of EVENTS) out[e] = probs[e] == null ? null : calibrationOf(st, e).fn(probs[e]);
    return out;
  };

  return { VERSION, EVENTS, MODELS, PRIOR_W, N_MIN, LIM, emptyState, normalise, weighted, profileOf, predictedVar, realisedVar, observePace, paceOf,
    fitCalibration, applyCal, brier, bounded, calibrationOf, predictionFrom, resolvePaper, resolveTrade, observeOutcome, weightsOf, observePerf, suggestPaths, learned, calibrate };
});
