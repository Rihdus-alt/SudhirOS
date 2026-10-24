// The A+ finder's pure parts: stops, what a plan needs from its signal, break detection, the record.
// Run: node --test apps/time-machine/test/aplus.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const T = require("../src/tmx-core.js");
const I = require("../src/tmx-intraday.js");
const L = require("../src/tmx-learn.js");
const A = require("../src/tmx-aplus.js");

const FIX = path.join(__dirname, "..", "fixtures");
const readCsv = (name) => {
  const [head, ...lines] = fs.readFileSync(path.join(FIX, name), "utf8").trim().split("\n");
  const cols = head.split(",");
  return lines.map((l) => Object.fromEntries(l.split(",").map((v, i) => [cols[i], v])));
};
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, (msg || "") + " got " + a + " want " + b + " (tol " + tol + ")");
const ist = (iso) => Date.parse(iso + "+05:30");

test("Wilson interval: known values, and it never leaves [0, 1]", () => {
  const w = A.wilson(5, 10);
  near(w.lo, 0.2366, 0.001); near(w.hi, 0.7634, 0.001); near(w.p, 0.5, 1e-12);
  const z = A.wilson(0, 20), o = A.wilson(20, 20);
  assert.equal(z.lo, 0); assert.ok(z.hi > 0.1 && z.hi < 0.2); assert.ok(o.hi > 0.999 && o.hi <= 1); assert.ok(o.lo > 0.8);
  assert.equal(A.wilson(0, 0).n, 0);
  const big = A.wilson(400, 1000); assert.ok(big.hi - big.lo < 0.07, "a thousand calls pin the rate to about three points either side");
});

test("the stop sits where the break has failed, no tighter than the day's noise, and inside 7-15% of the premium", () => {
  // NIFTY long: broke 22,600 with a 8-point buffer, spot 22,611, delta 0.5, premium 134; a 5-minute wiggle of 9 points
  const base = { dir: 1, spot: 22611, level: 22600, buf: 8, delta: 0.5, entry: 134, barSd5: 9 };
  const a = A.structuralStop(base);
  assert.ok(a.ok); near(a.structPct, (22611 - 22592) * 0.5 / 134 * 100, 1e-9);
  assert.equal(a.bound, "structure"); near(a.slPct, 7.1, 1e-9); assert.equal(a.sl, A.r05(134 * (1 - 0.071))); assert.ok(a.fits);
  const near_ = A.structuralStop(Object.assign({}, base, { spot: 22605 }));      // a break that has barely cleared: the failed-break line is 13 points away, 4.9%: raised to the 7% floor
  assert.equal(near_.bound, "floor"); assert.equal(near_.slPct, 7); assert.match(near_.why.join(" "), /inside the bid-ask/);
  const calm = A.structuralStop(Object.assign({}, base, { barSd5: 20 }));          // a livelier day: the noise floor (30 points = 11.2%) takes over
  assert.equal(calm.bound, "noise"); near(calm.slPct, 30 * 0.5 / 134 * 100, 0.06); assert.ok(calm.slPct > a.slPct);
  const wide = A.structuralStop(Object.assign({}, base, { spot: 22650 }));         // price ran 58 points past the level: a failed break is now 29% of the premium away
  assert.equal(wide.bound, "cap"); assert.equal(wide.slPct, 15); assert.equal(wide.fits, false); assert.match(wide.why.join(" "), /not A\+/); assert.match(wide.why[1], /^An ordinary/);
  const short = A.structuralStop({ dir: -1, spot: 22589, level: 22600, buf: 8, delta: -0.5, entry: 134, barSd5: 20 });
  assert.equal(short.bound, "noise"); near(short.slPct, calm.slPct, 1e-9, "a short mirrors a long");
  assert.equal(A.structuralStop(Object.assign({}, base, { spot: 22590 })).ok, false, "price already back through the failed-break line");
  for (const sd of [0, 3, 12, 25, 60]) { const s = A.structuralStop(Object.assign({}, base, { barSd5: sd })); assert.ok(s.slPct >= 7 && s.slPct <= 15 && s.sl < 134); }
});

// ----------------------------------------------------------------------------------------- what a plan needs from its signal
const moveMass = (v, delta) => {                // move `delta` of probability from "stopped" to the T1 classes (in proportion), and return the new class table's average
  const cls = JSON.parse(JSON.stringify(v.classes)), t1 = A.T1_CLASSES.filter((c) => cls[c]), p1 = t1.reduce((a, c) => a + cls[c].p, 0);
  cls.stop.p -= delta; for (const c of t1) cls[c].p += delta * cls[c].p / p1;
  return Object.values(cls).reduce((a, c) => a + c.p * c.avg, 0);
};
test("break-even: moving that much probability from 'stopped' to 'reached T1' brings the average to exactly zero", () => {
  const v = { n: 1000, expectancy: -110, se: 20, classes: { stop: { p: 0.56, avg: -1650 }, t1_all: { p: 0.30, avg: 1750 }, t1_be: { p: 0.04, avg: -90 }, t2: { p: 0.06, avg: 2400 }, time: { p: 0.04, avg: -300 } } };
  v.expectancy = Object.values(v.classes).reduce((a, c) => a + c.p * c.avg, 0);
  const b = A.breakeven([{ v, w: 1 }]);
  near(b.p1, 0.40, 1e-12); assert.ok(b.shift > 0 && b.need > b.p1 && b.need < 1);
  near(moveMass(v, b.shift), 0, 1e-9, "the average at the break-even shift");
  const rich = JSON.parse(JSON.stringify(v)); rich.classes.t1_all.avg = 9000; rich.expectancy = Object.values(rich.classes).reduce((a, c) => a + c.p * c.avg, 0);
  assert.ok(A.breakeven([{ v: rich, w: 1 }]).shift < 0, "a plan that already pays has a cushion: the shift is negative");
  assert.equal(A.breakeven([{ v: { n: 0 }, w: 1 }]), null); assert.equal(A.breakeven([]), null);
  const two = A.breakeven([{ v, w: 1 }, { v: rich, w: 1 }]);
  near(two.shift, (A.breakeven([{ v, w: 1 }]).shift + A.breakeven([{ v: rich, w: 1 }]).shift) / 2, 1e-12, "models are averaged by weight");
});

test("break-even on the engine's own output: the classes add up to the average, and the figures are sane", async () => {
  const sessions = I.parseBars(readCsv("NIFTY-5m.csv")), daily = T.loadCandles(readCsv("NIFTY-1d.csv"));
  const ch = T.parseChain(JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027-eod23.json"), "utf8")));
  ch.spot_time = "2026-10-22T09:30:00+05:30"; T.chainAnalytics(ch, ist("2026-10-23T15:46:00"), 0.065);
  const rep = await I.analyseIntraday({ sessions: sessions.filter((s) => s.date < "2026-10-22"), chain: ch, daily, side: "CE", strike: 22600, lots: 2, nowMs: ist("2026-10-22T09:31:00"),
    holidays: new Set(["2026-10-20", "2026-11-10"]), step: 5, models: ["implied", "bootstrap"], variants: ["desk", "allT2"], paths: 3000, seed: 3, sensitivity: false, entryPrice: 130 });
  for (const m of ["implied", "bootstrap"]) {
    const v = rep.models[m].variants.desk, e = Object.values(v.classes).reduce((a, c) => a + c.p * c.avg, 0);
    near(e, v.expectancy, 1e-6, m + ": the outcome classes partition the paths");
    near(A.T1_CLASSES.filter((c) => v.classes[c]).reduce((a, c) => a + v.classes[c].p, 0), v.p_hit_t1, 1e-9, m + ": the T1 classes are 'reached T1'");
  }
  const be = A.breakeven(["implied", "bootstrap"].map((m) => ({ v: rep.models[m].variants.desk, w: 0.5 })));
  assert.ok(be.need > 0 && be.need < 1 && Number.isFinite(be.shift) && be.shiftSe >= 0);
  assert.ok(rep.blends.desk && rep.blends.allT2, "a blend per exit rule");
  const b = rep.blends.desk;
  for (const k of ["ci_profit", "ci_t1", "ci_stop"]) assert.ok(b[k][0] <= b[k][1] && b[k][0] >= 0 && b[k][1] <= 1);
  assert.ok(b.ci_t1[0] <= b.p_hit_t1 && b.p_hit_t1 <= b.ci_t1[1]);
  assert.ok(rep.blends.allT2.p_hit_t1 < b.p_hit_t1, "reaching +20% is rarer than reaching +15%: " + rep.blends.allT2.p_hit_t1.toFixed(3) + " < " + b.p_hit_t1.toFixed(3));
});

// ----------------------------------------------------------------------------------------- the interval holds its coverage
test("the 95% band on a probability covers the truth about 95% of the time (a world with a known answer)", () => {
  const rng = T.makeRng("coverage");
  let hit = 0, runs = 600;
  for (let r = 0; r < runs; r++) {
    // two 'models' that each estimate the same true probability 0.4 from 1500 paths; the blend's band is built as blendOf builds it
    const n = 1500, ps = [0.4, 0.4].map(() => { let k = 0; for (let i = 0; i < n; i++) if (rng.random() < 0.4) k++; return k / n; });
    const models = { implied: { variants: { desk: { n, expectancy: 0, se: 1, p_profit: ps[0], p_hit_t1: ps[0], p_stop: 1 - ps[0] } } }, bootstrap: { variants: { desk: { n, expectancy: 0, se: 1, p_profit: ps[1], p_hit_t1: ps[1], p_stop: 1 - ps[1] } } } };
    const b = I.blendOf(models);
    if (b.ci_t1[0] <= 0.4 && 0.4 <= b.ci_t1[1]) hit++;
  }
  assert.ok(hit / runs > 0.93, "coverage " + (hit / runs).toFixed(3));
});

// ----------------------------------------------------------------------------------------- fresh breaks
const candles = (closes) => closes.map((c, i) => ({ close: c, datetime_ist: "2026-10-23 " + String(9 + Math.floor((15 + i * 5) / 60)).padStart(2, "0") + ":" + String((15 + i * 5) % 60).padStart(2, "0") + ":00" }));
test("a break is the candle that closes beyond the trigger when its predecessor had not; a stale one is not a break", () => {
  const c = candles([100, 101, 102, 103, 106, 107]), at = () => ({ trigger: 105, level: 104, labels: ["x"] });
  const b = A.findBreak(c, 1, at);
  assert.equal(b.k, 4); assert.equal(b.ago, 1); assert.equal(b.close, 106); assert.equal(b.t, "09:35");
  assert.equal(A.findBreak(candles([100, 106, 107, 108, 109, 110]), 1, at), null, "it broke five candles ago: stale");
  assert.equal(A.findBreak(candles([100, 101, 102, 103, 104, 105]), 1, at), null, "closing exactly on the trigger is not beyond it");
  const s = A.findBreak(candles([110, 109, 108, 107, 104, 103]), -1, at);
  assert.equal(s.k, 4); assert.equal(s.close, 104);
  assert.equal(A.findBreak(c, 1, () => null), null);
  const moving = A.findBreak(candles([100, 101, 102, 103, 104, 106]), 1, (k) => ({ trigger: k === 5 ? 105 : 120 }));
  assert.equal(moving.k, 5, "the trigger is the one the levels showed one candle earlier");
});

// ----------------------------------------------------------------------------------------- the record
test("the record: a thin one says so; a long one is judged against what the plans needed, by interval", () => {
  const rng = T.makeRng("record");
  const world = (n, rate, p, need) => Array.from({ length: n }, () => ({ p1: p, y1: rng.random() < rate ? 1 : 0, p2: 0.2, y2: 0, ps: 0.55, ys: 0, need }));
  assert.equal(A.record([]).verdict, "none"); assert.equal(A.record([]).n, 0);
  const thin = A.record(world(12, 0.7, 0.4, 0.45)); assert.equal(thin.verdict, "thin"); assert.equal(thin.applied, false);
  const good = A.record(world(240, 0.62, 0.40, 0.45)), bad = A.record(world(240, 0.30, 0.40, 0.45)), edge = A.record(world(240, 0.45, 0.40, 0.45));
  assert.equal(good.verdict, "above"); assert.ok(good.applied && good.lift > 1.1 && good.lift <= 1.4, "lift " + good.lift);
  assert.equal(bad.verdict, "below"); assert.ok(bad.lift < 0.9 && bad.lift >= 0.6);
  assert.equal(edge.verdict, "unclear");
  const calibrated = A.record(world(1000, 0.40, 0.40, 0.45)); near(calibrated.lift, 1, 0.1, "a model that is right gets no lift");
  assert.ok(A.record(world(19, 0.5, 0.4, 0.45)).applied === false && A.record(world(20, 0.5, 0.4, 0.45)).applied === true);
  const few = A.record(world(5, 1, 0.4, 0.45)); assert.ok(few.lift < 1.4, "five hits do not move the lift to its limit: " + few.lift.toFixed(2));
});

test("the learner keeps the record of A+ calls once they are settled, once each, and a trade of the same call does not add to it", () => {
  const pred = (id) => ({ id, kind: "aplus", day: "2026-10-23", ix: "NIFTY", aplus: { dir: 1, p1: 0.4, p2: 0.2, ps: 0.55, need: 0.46 }, blend: { profit: 0.4, t1: 0.4, stop: 0.55 }, models: { implied: { profit: 0.4, t1: 0.4, stop: 0.55 } } });
  let st = L.emptyState();
  st = L.observeOutcome(st, pred("a1"), { profit: 1, t1: 1, stop: 0, t2: 0 }, "paper", "t");
  st = L.observeOutcome(st, pred("a1"), { profit: 1, t1: 1, stop: 0, t2: 0 }, "paper", "t");
  st = L.observeOutcome(st, pred("a1"), { profit: 0, t1: 0, stop: 1 }, "trade", "t");
  st = L.observeOutcome(st, pred("a2"), { profit: 0, t1: 0, stop: 1, t2: 0 }, "paper", "t");
  assert.equal(st.aplus.length, 2); assert.deepEqual(st.aplus.map((r) => r.y1), [1, 0]); assert.equal(st.aplus[0].need, 0.46);
  const r = A.record(st.aplus); assert.equal(r.n, 2); assert.equal(r.k1, 1);
  const frozen = L.emptyState(); frozen.frozen = true;
  const fz = L.observeOutcome(frozen, pred("f1"), { profit: 1, t1: 1, stop: 0, t2: 0 }, "paper", "t");
  assert.equal(fz.aplus.length, 1, "the record is a measurement: it keeps counting while learning is frozen"); assert.equal(fz.calib.profit.length, 0, "but nothing else learns");
  const thawed = (fz.frozen = false, L.observeOutcome(fz, pred("f1"), { profit: 1, t1: 1, stop: 0, t2: 0 }, "paper", "t"));
  assert.equal(thawed.aplus.length, 1, "counted once"); assert.equal(thawed.calib.profit.length, 1, "and its calibration pair still arrives after the thaw");
  const other = L.observeOutcome(L.emptyState(), Object.assign(pred("s1"), { kind: "scan" }), { profit: 1, t1: 1, stop: 0 }, "paper", "t");
  assert.equal(other.aplus.length, 0, "only A+ calls count");
  for (let i = 0; i < 200; i++) st = L.observeOutcome(st, pred("b" + i), { profit: 1, t1: 1, stop: 0, t2: 1 }, "paper", "t");
  assert.equal(st.aplus.length, L.LIM.aplus);
});

test("an A+ call is settled on the real tape: T1, T2 on its own, and the stop", async () => {
  const sessions = I.parseBars(readCsv("NIFTY-5m.csv")), daily = T.loadCandles(readCsv("NIFTY-1d.csv"));
  const ch = T.parseChain(JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027-eod23.json"), "utf8")));
  ch.spot_time = "2026-10-22T09:30:00+05:30"; T.chainAnalytics(ch, ist("2026-10-23T15:46:00"), 0.065);
  const rep = await I.analyseIntraday({ sessions: sessions.filter((s) => s.date < "2026-10-22"), chain: ch, daily, side: "PE", strike: 22600, lots: 2, nowMs: ist("2026-10-22T09:31:00"),
    holidays: new Set(["2026-10-20", "2026-11-10"]), step: 5, models: ["implied"], variants: ["desk"], paths: 300, seed: 1, sensitivity: false, entryPrice: 130 });
  const pred = L.predictionFrom(rep, { id: "ap1", ix: "NIFTY", kind: "aplus", aplus: { dir: -1, p1: 0.4, p2: 0.2, ps: 0.5, need: 0.45 } });
  const out = L.resolvePaper(pred, sessions.find((s) => s.date === "2026-10-22"), I.sessionProfile(sessions));
  assert.ok(out && [0, 1].includes(out.t1) && [0, 1].includes(out.t2) && [0, 1].includes(out.stop));
  assert.ok(out.t2 <= out.t1, "a plan cannot reach +20% without having passed +15%");
  assert.equal(L.resolvePaper(Object.assign({}, pred, { kind: "scan" }), sessions.find((s) => s.date === "2026-10-22"), I.sessionProfile(sessions)).t2, undefined);
});

test("lots fit the money left to the day stop; one lot is always shown, flagged when it does not fit", () => {
  assert.deepEqual(A.sizeLots({ riskPerLot: 800, room: 3000 }), { lots: 3, fits: true, byRisk: 3, byCap: 3 });
  assert.equal(A.sizeLots({ riskPerLot: 1500, room: 3000 }).lots, 2);
  const none = A.sizeLots({ riskPerLot: 1500, room: 1000 }); assert.equal(none.lots, 1); assert.equal(none.fits, false);
  assert.equal(A.sizeLots({ riskPerLot: 100, room: 3000, max: 4 }).lots, 4);
  assert.equal(A.sizeLots({ riskPerLot: 800, room: 3000, cap: 10000, outlayPerLot: 6000 }).lots, 1, "a capital cap limits it too");
});

test("the A+ checklist: every check must pass; trend alone makes it an A", () => {
  const ok = { bothBroke: true, roomOk: true, stopFits: true, deltaOk: true, spreadOk: true, early: true, riskFits: true, withTrend: true };
  assert.equal(A.grade(ok).grade, "A+");
  assert.equal(A.grade(Object.assign({}, ok, { withTrend: false })).grade, "A");
  for (const k of ["bothBroke", "roomOk", "stopFits", "deltaOk", "spreadOk", "early", "riskFits"]) { const g = A.grade(Object.assign({}, ok, { [k]: false })); assert.equal(g.grade, "none", k); assert.equal(g.misses.length, 1); }
});
