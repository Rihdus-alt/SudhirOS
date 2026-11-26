// The learning layer: what it learns, that it learns the right thing in a world whose answer is known, that it stays put when
// there is nothing to learn, and that every number it produces is bounded, dedupes and can be frozen.
// Run: node --test apps/time-machine/test/learn.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const T = require("../src/tmx-core.js");
const I = require("../src/tmx-intraday.js");
const L = require("../src/tmx-learn.js");

const FIX = path.join(__dirname, "..", "fixtures");
const readCsv = (name) => {
  const [head, ...lines] = fs.readFileSync(path.join(FIX, name), "utf8").trim().split("\n");
  const cols = head.split(",");
  return lines.map((l) => Object.fromEntries(l.split(",").map((v, i) => [cols[i], v])));
};
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, (msg || "") + " got " + a + " want " + b + " (tol " + tol + ")");
const ist = (iso) => Date.parse(iso + "+05:30");
const sessions = I.parseBars(readCsv("NIFTY-5m.csv"));
const daily = T.loadCandles(readCsv("NIFTY-1d.csv"));
const HOL = new Set(["2026-10-20", "2026-11-10"]);
const chainOf = (spotTime) => {
  const ch = T.parseChain(JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027-eod23.json"), "utf8")));
  if (spotTime) ch.spot_time = spotTime;
  T.chainAnalytics(ch, ist("2026-10-23T15:46:00"), 0.065);
  return ch;
};
const sigm = (z) => 1 / (1 + Math.exp(-z)), logit = (p) => Math.log(p / (1 - p));

/** A session whose true variance is `mult`^2 times the variance the quote predicts, spread by the clock's own profile. */
function synthSession(rng, spec, shares5, mult, date) {
  const clock = I.makeClock(shares5, spec.phi, spec.day, spec.expiryIso), bars = [];
  let c = 22500;
  for (let j = 0; j < 72; j++) {
    const v = mult * mult * spec.sigma * spec.sigma * (clock.Teff(5 * j) - clock.Teff(5 * j + 5)), o = c;
    c = o * Math.exp(rng.gauss() * Math.sqrt(v));
    bars.push({ o, h: Math.max(o, c) * (1 + 0.0002 * Math.abs(rng.gauss())), l: Math.min(o, c) * (1 - 0.0002 * Math.abs(rng.gauss())), c });
  }
  return { date, full: true, count: 72, bars };
}
const SPEC = { sigma: 0.15, phi: 0.3, day: "2026-10-26", expiryIso: "2026-10-27" };
const SHARES = I.sessionProfile(sessions).shares5;

/** n observations from a world whose variance runs at `ratio` times what was predicted. */
function paceWorld(ratio, n, id0) {
  const rng = T.makeRng("pace-world-" + ratio + "-" + n);
  let st = L.emptyState();
  for (let i = 0; i < n; i++) {
    const sigma = 0.11 + 0.1 * rng.random(), spec = Object.assign({}, SPEC, { sigma });
    const s = synthSession(rng, spec, SHARES, Math.sqrt(ratio), "2026-09-" + String(i));
    const pred = L.predictedVar(sigma, spec.phi, SHARES, spec.day, spec.expiryIso, 20), real = L.realisedVar(s, 20);
    st = L.observePace(st, "NIFTY", "sess", real, pred, (id0 || "w") + i, "t" + i);
  }
  return st;
}

test("the realised variance of a session matches what the quote predicted, on average, when the world runs at the implied pace", () => {
  const rng = T.makeRng("rv"), ratios = [];
  for (let i = 0; i < 300; i++) {
    const sigma = 0.1 + 0.1 * rng.random(), spec = Object.assign({}, SPEC, { sigma }), s = synthSession(rng, spec, SHARES, 1, "d" + i);
    ratios.push(L.realisedVar(s, 20) / L.predictedVar(sigma, spec.phi, SHARES, spec.day, spec.expiryIso, 20));
  }
  near(ratios.reduce((a, b) => a + b, 0) / ratios.length, 1, 0.04, "mean variance ratio");
  assert.equal(L.realisedVar({ bars: new Array(72).fill(null) }, 20), null, "a session with no bars is not an observation");
});

test("pace: recovers a world that runs 30% hotter in variance, with an interval that holds the truth, and applies only after 8 observations", () => {
  const st = paceWorld(1.3, 60);
  const p = L.paceOf(st, "NIFTY", "sess");
  assert.equal(p.n, 60); assert.ok(p.applied);
  near(p.mult, Math.sqrt(1.3), 0.05, "volatility multiplier");
  assert.ok(p.ci[0] <= Math.sqrt(1.3) + 0.02 && p.ci[1] >= Math.sqrt(1.3) - 0.02, "interval " + p.ci.map((x) => x.toFixed(3)));
  assert.ok(p.ci[1] - p.ci[0] < 0.12 && p.ci[1] > p.ci[0]);
  const few = L.paceOf(paceWorld(1.3, 5), "NIFTY", "sess");
  assert.equal(few.applied, false); assert.equal(few.mult, 1, "below 8 observations nothing is applied"); assert.ok(few.learned > 1, "but it reports what it has seen");
  assert.equal(L.learned(paceWorld(1.3, 5), "NIFTY").volMult, 1);
  near(L.learned(st, "NIFTY").volMult, p.mult, 1e-12);
  assert.equal(L.learned(st, "SENSEX").volMult, 1, "another index has learned nothing yet");
});

test("pace: a world that matches the model leaves the multiplier at 1; the shrinkage keeps a small sample near 1", () => {
  const p = L.paceOf(paceWorld(1, 60), "NIFTY", "sess");
  near(p.mult, 1, 0.05); assert.ok(p.ci[0] <= 1.0 + 0.02 && p.ci[1] >= 1.0 - 0.02);
  const hot = L.paceOf(paceWorld(1.6, 9), "NIFTY", "sess"), hotMore = L.paceOf(paceWorld(1.6, 90), "NIFTY", "sess");
  assert.ok(hot.mult > 1 && hot.mult < Math.sqrt(1.6) - 0.05, "nine observations are held back toward 1 (truth " + Math.sqrt(1.6).toFixed(3) + "): " + hot.mult.toFixed(3));
  near(hotMore.mult, Math.sqrt(1.6), 0.05, "ninety observations get there");
});

test("pace: bounded to [0.75, 1.35] however extreme the data, noise and bad input are ignored, ids dedupe, the buffer is capped", () => {
  const hi = L.paceOf(paceWorld(4, 80), "NIFTY", "sess"), lo = L.paceOf(paceWorld(0.2, 80), "NIFTY", "sess");
  assert.equal(hi.mult, 1.35); assert.equal(lo.mult, 0.75);
  let st = L.emptyState();
  const before = JSON.stringify(st);
  for (const [r, p] of [[-1, 1], [NaN, 1], [1, 0], [1, -2], [1, NaN]]) st = L.observePace(st, "NIFTY", "sess", r, p, null, "t");
  assert.equal(JSON.stringify(st), before, "bad observations change nothing");
  st = L.observePace(st, "NIFTY", "sess", 1.2, 1, "same", "t1"); st = L.observePace(st, "NIFTY", "sess", 1.2, 1, "same", "t2");
  assert.equal(st.pace.NIFTY.sess.length, 1, "the same id counts once");
  for (let i = 0; i < 300; i++) st = L.observePace(st, "NIFTY", "sess", 1, 1, "x" + i, "t");
  assert.equal(st.pace.NIFTY.sess.length, L.LIM.obs); assert.ok(st.seen.length <= L.LIM.seen);
});

test("pace: recent sessions count more, so a change of regime is followed", () => {
  let st = L.emptyState();
  const rng = T.makeRng("regime");
  for (let i = 0; i < 80; i++) {
    const ratio = i < 40 ? 0.8 : 1.3;
    st = L.observePace(st, "NIFTY", "sess", ratio * (1 + 0.1 * rng.gauss()), 1, "r" + i, "t");
  }
  const p = L.paceOf(st, "NIFTY", "sess");
  assert.ok(p.mult > Math.sqrt(1.05), "leans to the recent 1.3, not the all-time average (1.05): " + p.mult.toFixed(3));
  assert.ok(p.mult < Math.sqrt(1.3) + 0.02);
});

test("freezing stops the pace, the calibration and the weights; the state survives a JSON round trip and an unknown version resets", () => {
  let st = paceWorld(1.3, 30);
  st.frozen = true;
  const snap = JSON.stringify(st);
  const rng = T.makeRng("frozen");
  for (let i = 0; i < 20; i++) {
    st = L.observePace(st, "NIFTY", "sess", 2, 1, "z" + i, "t");
    st = L.observeOutcome(st, { id: "p" + i, blend: { profit: 0.5, t1: 0.5, stop: 0.5 }, models: { implied: { profit: 0.5, t1: 0.5, stop: 0.5 } } }, { profit: 1, t1: 1, stop: 0 }, "paper", "t");
  }
  assert.equal(JSON.stringify(st), snap);
  const back = L.normalise(JSON.parse(snap));
  assert.deepEqual(L.learned(back, "NIFTY").volMult, L.learned(st, "NIFTY").volMult);
  assert.equal(L.normalise({ v: 99 }).v, L.VERSION); assert.equal(L.normalise(null).pace && Object.keys(L.normalise(null).pace).length, 0);
  assert.equal(rng.random() >= 0, true);
});

// ---------------------------------------------------------------------------------------------------------------- calibration
function calibWorld(a, b, n, seed) {
  const rng = T.makeRng(seed), pairs = [];
  for (let i = 0; i < n; i++) { const p = 0.05 + 0.9 * rng.random(); pairs.push([p, rng.random() < sigm(a + b * logit(p)) ? 1 : 0]); }
  return pairs;
}
const stateWith = (pairs, bucket) => { const st = L.emptyState(); for (const e of L.EVENTS) st[bucket || "calib"][e] = pairs.slice(); return st; };

test("calibration: an over-confident model is pulled in, and the held-out score proves it helped", () => {
  const st = stateWith(calibWorld(-0.3, 0.6, 400, "over"));
  const c = L.calibrationOf(st, "profit");
  assert.ok(c.applied); near(c.b, 0.6, 0.15, "slope"); near(c.a, -0.3, 0.2, "intercept");
  assert.ok(c.held.n >= 100 && c.held.calibrated < c.held.raw, "held-out Brier " + c.held.raw.toFixed(4) + " -> " + c.held.calibrated.toFixed(4));
  assert.ok(c.fn(0.9) < 0.9 && c.fn(0.1) > 0.1 - 0.12 && c.fn(0.5) < 0.5, "high probabilities come down, the middle shifts with the intercept");
});

test("calibration: a model that is already right is left alone (slope near 1, intercept near 0, held-out score no worse)", () => {
  const st = stateWith(calibWorld(0, 1, 400, "right"));
  const c = L.calibrationOf(st, "stop");
  near(c.a, 0, 0.3); near(c.b, 1, 0.25);
  assert.ok(c.held.calibrated <= c.held.raw + 0.004, "no harm: " + c.held.raw.toFixed(4) + " vs " + c.held.calibrated.toFixed(4));
  for (const p of [0.2, 0.5, 0.8]) near(c.fn(p), p, 0.08);
});

test("calibration: nothing is applied below 30 resolved predictions, the prior holds a thin sample near 'no change', and 15 trades set a separate view", () => {
  const few = L.calibrationOf(stateWith(calibWorld(-0.3, 0.3, 29, "few")), "profit");
  assert.equal(few.applied, false); assert.equal(few.fn(0.7), 0.7); assert.equal(few.a, 0); assert.equal(few.b, 1);
  const thin = L.calibrationOf(stateWith(calibWorld(-0.3, 0.3, 30, "few")), "profit"), full = L.calibrationOf(stateWith(calibWorld(-0.3, 0.3, 400, "few2")), "profit");
  assert.ok(thin.applied && thin.b > full.b && thin.b < 1, "30 pairs shrink toward 1 (" + thin.b.toFixed(2) + ") more than 400 (" + full.b.toFixed(2) + ")");
  const tr = L.calibrationOf(stateWith(calibWorld(-0.3, 0.6, 20, "trades"), "calibTrades"), "profit");
  assert.equal(tr.applied, false, "trades alone never change the numbers the page shows"); assert.ok(tr.tradesBrier && tr.nTrades === 20);
  assert.equal(L.calibrationOf(stateWith(calibWorld(0, 1, 14, "t14"), "calibTrades"), "profit").tradesBrier, undefined);
});

test("calibration: extreme probabilities stay inside (0, 1), the map is monotone, and fitting is stable on a perfect or a useless model", () => {
  const c = { a: 0.4, b: 0.7 };
  let prev = -1;
  for (let p = 0; p <= 1.0001; p += 0.05) { const q = L.applyCal(c, Math.min(p, 1)); assert.ok(q > 0 && q < 1 && q >= prev); prev = q; }
  const perfect = [], useless = [], rng = T.makeRng("deg");
  for (let i = 0; i < 100; i++) { const y = rng.random() < 0.5 ? 1 : 0; perfect.push([y ? 0.97 : 0.03, y]); useless.push([0.9, rng.random() < 0.5 ? 1 : 0]); }
  const cp = L.fitCalibration(perfect), cu = L.fitCalibration(useless);
  assert.ok(Number.isFinite(cp.a) && Number.isFinite(cp.b) && cp.b > 1, "a model that is better than it says is sharpened");
  assert.ok(Number.isFinite(cu.a) && Math.abs(L.applyCal(cu, 0.9) - 0.5) < 0.1, "a constant 0.9 that is right half the time maps to about 0.5");
});

// ---------------------------------------------------------------------------------------------------------------- outcomes and weights
const pred = (id, p, models) => ({ id, blend: { profit: p, t1: p, stop: 1 - p }, models: models || { implied: { profit: p, t1: p, stop: 1 - p } } });
const out = (y) => ({ profit: y, t1: y, stop: 1 - y });

test("outcomes: a prediction counts once, paper and trade pairs are kept apart, model scores come from paper only", () => {
  let st = L.emptyState();
  const pr = pred("a", 0.7, { implied: { profit: 0.7, t1: 0.7, stop: 0.3 }, bootstrap: { profit: 0.6, t1: 0.6, stop: 0.4 }, replay: { profit: 0.5, t1: 0.5, stop: 0.5 } });
  st = L.observeOutcome(st, pr, out(1), "paper", "t1"); st = L.observeOutcome(st, pr, out(1), "paper", "t2");
  assert.equal(st.calib.profit.length, 1); assert.equal(st.scores.implied.n, 1);
  st = L.observeOutcome(st, pr, out(0), "trade", "t3");               // same prediction, now from your own trade: a different source
  assert.equal(st.calibTrades.profit.length, 1); assert.equal(st.scores.implied.n, 1, "a trade does not score the models: its fills and exits are not what they predicted");
  st = L.observeOutcome(st, pr, null, "paper", "t4"); assert.equal(st.calib.profit.length, 1, "an unresolved prediction adds nothing");
  assert.ok(st.scores.implied.ll < st.scores.replay.ll, "the sharper, right-leaning model scored better on a win");
  for (let i = 0; i < 500; i++) st = L.observeOutcome(st, pred("m" + i, 0.5), out(i % 2), "paper", "t");
  assert.equal(st.calib.profit.length, L.LIM.pairs);
});

test("model weights: stay at the prior until 25 resolved, then favour the model that scored best, never below 10% or above 70%", () => {
  const rng = T.makeRng("weights");
  let st = L.emptyState(), early = null;
  for (let i = 0; i < 140; i++) {
    const p = 0.15 + 0.7 * rng.random(), y = rng.random() < p ? 1 : 0;      // the implied model's p is the truth; the bootstrap is a blur of it; the replay says 0.5
    const blur = 0.5 + 0.4 * (p - 0.5);
    st = L.observeOutcome(st, pred("w" + i, p, { implied: { profit: p, t1: p, stop: 1 - p }, bootstrap: { profit: blur, t1: blur, stop: 1 - blur }, replay: { profit: 0.5, t1: 0.5, stop: 0.5 } }), out(y), "paper", "t");
    if (i === 23) early = L.weightsOf(st);
  }
  assert.equal(early.learned, false); assert.deepEqual(early.w, L.PRIOR_W);
  const w = L.weightsOf(st);
  assert.ok(w.learned && w.n === 140);
  assert.ok(w.w.implied > L.PRIOR_W.implied && w.w.implied > w.w.bootstrap && w.w.bootstrap > w.w.replay, JSON.stringify(w.w));
  near(w.w.implied + w.w.bootstrap + w.w.replay, 1, 1e-9);
  for (const k of L.MODELS) assert.ok(w.w[k] >= 0.1 - 1e-9 && w.w[k] <= 0.7 + 1e-9, k + " " + w.w[k]);
  // when the models score alike nothing moves: the prior stays
  let eq = L.emptyState();
  for (let i = 0; i < 100; i++) { const p = 0.2 + 0.6 * rng.random(); eq = L.observeOutcome(eq, pred("e" + i, p, { implied: { profit: p, t1: p, stop: 1 - p }, bootstrap: { profit: p, t1: p, stop: 1 - p }, replay: { profit: p, t1: p, stop: 1 - p } }), out(rng.random() < p ? 1 : 0), "paper", "t"); }
  const we = L.weightsOf(eq).w;
  for (const k of L.MODELS) near(we[k], L.PRIOR_W[k], 1e-9, "equal scores leave " + k + " at its prior");
  const b = L.bounded({ a: 0.95, b: 0.04, c: 0.01 }, 0.1, 0.7);
  near(b.a + b.b + b.c, 1, 1e-9); assert.ok(b.a <= 0.7 + 1e-9 && b.b >= 0.1 - 1e-9 && b.c >= 0.1 - 1e-9);
});

test("the stored state stays small at every limit (it lives in one database document)", () => {
  const rng = T.makeRng("size");
  let st = L.emptyState();
  for (const ix of ["NIFTY", "SENSEX"]) for (const kind of ["sess", "gap"]) for (let i = 0; i < 200; i++) st = L.observePace(st, ix, kind, 0.000012345678 * (1 + rng.random()), 0.000011234567, `${kind}:${ix}:2026-01-${i}`, "2026-10-24T10:00:00.000Z");
  for (let i = 0; i < 900; i++) {
    const p = rng.random(), pr = { id: "pred_NIFTY_2026-10-23_" + i, blend: { profit: p, t1: p * 0.7, stop: 1 - p }, models: { implied: { profit: p, t1: p, stop: 1 - p }, bootstrap: { profit: p, t1: p, stop: 1 - p }, replay: { profit: p, t1: p, stop: 1 - p } } };
    st = L.observeOutcome(st, pr, out(rng.random() < p ? 1 : 0), i % 3 ? "paper" : "trade", "2026-10-24T10:00:00.000Z");
  }
  for (const m of L.MODELS) for (const s of [1, 5, 15]) st = L.observePerf(st, m, s, 68, 1000, 1234.5678);
  const bytes = JSON.stringify(st).length;
  assert.ok(bytes < 200000, "state is " + bytes + " bytes");
  assert.ok(st.seen.length <= L.LIM.seen && st.log.length <= L.LIM.log);
});

// ---------------------------------------------------------------------------------------------------------------- real predictions on real tapes
async function realPrediction(extra, rules) {
  const rep = await I.analyseIntraday({ sessions: sessions.filter((s) => s.date < "2026-10-22"), chain: chainOf("2026-10-22T09:30:00+05:30"), daily, side: "PE", strike: 22600, lots: 1,
    nowMs: ist("2026-10-22T09:31:00"), holidays: HOL, step: 5, paths: 600, seed: 1, sensitivity: false, entryPrice: 130, rules });
  return L.predictionFrom(rep, Object.assign({ id: "real-1" }, extra || {}));
}

test("a stored prediction is plain data, replays on the real tape of its day, and the same plan on the same tape always gives the same answer", async () => {
  const pr = await realPrediction();
  assert.equal(pr.day, "2026-10-22"); assert.equal(pr.spec.m0, 20); assert.equal(pr.E, 130); assert.deepEqual(pr.levels, { sl: 117, t1: 149.5, t2: 156 });
  assert.deepEqual(JSON.parse(JSON.stringify(pr)), pr, "survives storage");
  for (const k of L.MODELS) { const m = pr.models[k]; if (m) for (const e of L.EVENTS) assert.ok(m[e] >= 0 && m[e] <= 1); }
  for (const e of L.EVENTS) assert.ok(pr.blend[e] >= 0 && pr.blend[e] <= 1);
  const day = sessions.find((s) => s.date === "2026-10-22"), profile = I.sessionProfile(sessions);
  const a = L.resolvePaper(pr, day, profile), b = L.resolvePaper(pr, day, profile);
  assert.deepEqual(a, b); assert.ok(a && [0, 1].includes(a.profit) && [0, 1].includes(a.t1) && [0, 1].includes(a.stop));
  assert.equal(L.resolvePaper(pr, sessions[0], profile), null, "a part session (the 19th's afternoon) cannot settle a morning entry");
  assert.equal(L.resolvePaper(pr, null, profile), null);
  assert.equal(L.resolvePaper(Object.assign({}, pr, { spec: null }), day, profile), null);
});

test("your own closed trade is judged against the levels it was planned with", async () => {
  const pr = await realPrediction();
  const win = L.resolveTrade(pr, { realisedNet: 1200, exits: [{ price: 150, qty: 65 }] });
  assert.deepEqual(win, { profit: 1, t1: 1, stop: 0, pnl: 1200 });
  const loss = L.resolveTrade(pr, { realisedNet: -900, exits: [{ price: 117, qty: 65 }] });
  assert.deepEqual(loss, { profit: 0, t1: 0, stop: 1, pnl: -900 });
  const scratch = L.resolveTrade(pr, { realisedNet: -60, exits: [{ price: 131, qty: 65 }] });
  assert.deepEqual([scratch.profit, scratch.t1, scratch.stop], [0, 0, 0]);
  assert.equal(L.resolveTrade(pr, { realisedNet: 0, exits: [] }), null);
  assert.equal(L.resolveTrade({ id: "x" }, { realisedNet: 1, exits: [{ price: 5, qty: 1 }] }), null);
});

test("end to end on the real fixtures: save a prediction, settle it on the stored tape, the state records it once and can be saved and loaded", async () => {
  const pr = await realPrediction();
  let st = L.emptyState();
  const o = L.resolvePaper(pr, sessions.find((s) => s.date === "2026-10-22"), I.sessionProfile(sessions));
  st = L.observeOutcome(st, pr, o, "paper", "2026-10-23T10:00");
  assert.equal(st.calib.profit.length, 1); assert.equal(st.updated, "2026-10-23T10:00"); assert.equal(st.created, "2026-10-23T10:00");
  const again = L.observeOutcome(st, pr, o, "paper", "later");
  assert.equal(JSON.stringify(again), JSON.stringify(st), "settling twice changes nothing");
  const back = L.normalise(JSON.parse(JSON.stringify(st)));
  assert.equal(L.learned(back, "NIFTY").resolved, 1);
  assert.equal(L.calibrate(back, { profit: 0.6, t1: 0.5, stop: 0.4 }).profit, 0.6, "one result changes no probability");
});

// ---------------------------------------------------------------------------------------------------------------- the saved sessions
test("the learned profile blends the saved full sessions in with recency weights, and falls back to the U shape with fewer than two", () => {
  const full = sessions.filter((s) => s.full);
  const w = L.weighted(full, 30);
  assert.deepEqual(w.map((s) => s.date), full.map((s) => s.date).sort());
  near(w[w.length - 1].w, 1, 1e-12); near(w[0].w, Math.pow(0.5, (w.length - 1) / 30), 1e-12);
  const p = L.profileOf(sessions);
  assert.equal(p.shares5.length, 75); near(p.shares5.reduce((a, b) => a + b, 0), 1, 1e-12); assert.ok(p.shares5.every((x) => x > 0));
  assert.match(p.source, /saved full sessions/);
  const one = L.profileOf([full[0]]);
  assert.equal(one.empirical !== undefined, true); assert.ok(!/recent ones weighted more/.test(one.source || ""));
  // a library of sessions that are quieter at the close pulls the profile's close share down
  const quiet = [], base = full[0];
  for (let i = 0; i < 12; i++) quiet.push(Object.assign({}, base, { date: "2026-09-" + String(10 + i), bars: base.bars.map((b, j) => (j >= 66 ? Object.assign({}, b, { c: b.o * (1 + 0.00002) }) : b)) }));
  assert.ok(L.profileOf(quiet).shares5[70] < L.profileOf([]).shares5[70], "learned from the saved sessions, not fixed");
});

// ---------------------------------------------------------------------------------------------------------------- speed
test("speed: an unmeasured device gets a safe guess, a measured one a smoothed per-bar cost, and the path count follows the time budget", () => {
  let st = L.emptyState();
  const guess = L.suggestPaths(st, 2000, 5, 68);
  assert.ok(guess >= 500 && guess <= 12000);
  for (let i = 0; i < 12; i++) st = L.observePerf(st, "implied", 5, 68, 1000, 68 * 1000 * 0.004);   // 4 microseconds per path-bar: slow
  st = L.observePerf(st, "bootstrap", 5, 68, 1000, 68 * 1000 * 0.004);
  near(st.perf["implied|5"].msPerBar, 0.004, 1e-9); assert.equal(st.perf["implied|5"].n, 12);
  const slow = L.suggestPaths(st, 2000, 5, 68), slowMore = L.suggestPaths(st, 4000, 5, 68);
  assert.ok(slowMore > slow && slow >= 500, slow + " -> " + slowMore);
  near(slow, Math.floor(2000 / (2 * 0.004 * 68)), 2);
  for (let i = 0; i < 30; i++) st = L.observePerf(st, "implied", 5, 68, 1000, 68 * 1000 * 0.001);  // the device got 4x faster: the estimate follows
  near(st.perf["implied|5"].msPerBar, 0.001, 0.0002);
  assert.equal(L.suggestPaths(st, 1e9, 5, 68), 12000, "capped");
  assert.equal(L.suggestPaths(st, 1, 5, 68), 500, "floored");
  const same = JSON.stringify(L.observePerf(st, "implied", 5, 0, 10, 10)); assert.equal(same, JSON.stringify(st));
});

// ---------------------------------------------------------------------------------------------------------------- the engine takes what is learned
const wide = { slPct: 40, tp1Pct: 50, tp2Pct: 80 };
const runCE = (extra) => I.analyseIntraday(Object.assign({ sessions: sessions.filter((s) => s.date < "2026-10-22"), chain: chainOf("2026-10-22T09:30:00+05:30"), daily, side: "CE", strike: 22600, lots: 1,
  nowMs: ist("2026-10-22T09:31:00"), holidays: HOL, step: 5, models: ["implied"], variants: ["desk"], paths: 8000, seed: 2, sensitivity: false, entryPrice: 130, rules: wide }, extra || {}));

test("the crown test: in a world that runs hotter than the quote says, the learned pace gives odds closer to that world's than the unlearned model; in a matched world it changes nothing", async () => {
  const hot = L.paceOf(paceWorld(1.3, 60), "NIFTY", "sess"), matched = L.paceOf(paceWorld(1, 60), "NIFTY", "sess");
  assert.ok(hot.applied && matched.applied);
  const truth = await runCE({ volMult: Math.sqrt(1.3) }), raw = await runCE({}), learned = await runCE({ volMult: hot.mult });
  const pt = truth.models.implied.variants.desk.p_stop, pr = raw.models.implied.variants.desk.p_stop, pl = learned.models.implied.variants.desk.p_stop;
  assert.equal(learned.meta.vol_mult, hot.mult);
  assert.ok(Math.abs(pl - pt) < Math.abs(pr - pt), "learned " + pl.toFixed(3) + " vs truth " + pt.toFixed(3) + " vs unlearned " + pr.toFixed(3));
  assert.ok(pt - pr > 0.02, "a hotter world stops this plan out more (wide stop, so it matters): " + pr.toFixed(3) + " -> " + pt.toFixed(3));
  const same = await runCE({ volMult: matched.mult });
  near(same.models.implied.variants.desk.p_stop, pr, 0.02, "a matched world: learned pace " + matched.mult.toFixed(3) + " leaves the odds where they were");
  // and the tight desk plan is nearly indifferent to pace, as first-passage theory says: the learner must not pretend otherwise
  const d1 = await runCE({ rules: {} , volMult: 1 }), d2 = await runCE({ rules: {}, volMult: Math.sqrt(1.3) });
  near(d1.models.implied.variants.desk.p_stop, d2.models.implied.variants.desk.p_stop, 0.04, "10%/15% desk levels");
});

test("the overnight pace widens the gap and only the gap: the premium's range at the open grows, a pace of 1 changes nothing, and a live run has no gap to widen", async () => {
  const base = { sessions, chain: chainOf(), daily, side: "CE", strike: 22600, lots: 1, nowMs: ist("2026-10-24T11:00:00"), holidays: HOL, step: 5, models: ["implied"],
    variants: ["hold"], rules: { slPct: 100, tp1Pct: 1e6, tp2Pct: 1e6 }, paths: 6000, seed: 6, sensitivity: false };
  const one = await I.analyseIntraday(base), also = await I.analyseIntraday(Object.assign({}, base, { gapMult: 1 })), wider = await I.analyseIntraday(Object.assign({}, base, { gapMult: 2 }));
  assert.equal(one.meta.gap_mult, 1); assert.equal(wider.meta.gap_mult, 2);
  assert.deepEqual(one.models.implied.fan, also.models.implied.fan, "1 is the default");
  const f1 = one.models.implied.fan[0], f2 = wider.models.implied.fan[0];       // the premium's quantiles on the first bar: the gap has just happened
  assert.ok(f2[4] > 1.6 * f1[4] && f2[3] > 1.6 * f1[3], "p95 " + f1[4].toFixed(0) + " -> " + f2[4].toFixed(0) + ", p75 " + f1[3].toFixed(0) + " -> " + f2[3].toFixed(0));
  const live = await I.analyseIntraday(Object.assign({}, base, { chain: chainOf("2026-10-22T09:30:00+05:30"), nowMs: ist("2026-10-22T09:31:00"), entryPrice: 130 }));
  const live2 = await I.analyseIntraday(Object.assign({}, base, { chain: chainOf("2026-10-22T09:30:00+05:30"), nowMs: ist("2026-10-22T09:31:00"), entryPrice: 130, gapMult: 2 }));
  assert.equal(live.meta.start.kind, "live"); assert.deepEqual(live.models.implied.fan, live2.models.implied.fan, "there is no overnight gap to widen in a live run");
});
