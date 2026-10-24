// The intraday engine: its clock, its three models, and the desk's plan on premium bars.
// Run: node --test apps/time-machine/test/intraday.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const T = require("../src/tmx-core.js");
const I = require("../src/tmx-intraday.js");

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
const chainOf = (file, spotTime) => {
  const ch = T.parseChain(JSON.parse(fs.readFileSync(path.join(FIX, file), "utf8")));
  if (spotTime) ch.spot_time = spotTime;
  T.chainAnalytics(ch, ist("2026-10-23T15:46:00"), 0.065);
  return ch;
};
const FREE = { fno: { brokerage_per_order: 0, stt_futures_sell_pct: 0, stt_options_sell_pct: 0, stt_options_exercise_pct: 0,
  exchange_txn_pct: { NFO: { FUT: 0, OPT: 0 }, BFO: { FUT: 0, OPT: 0 } }, stamp_futures_buy_pct: 0, stamp_options_buy_pct: 0 },
  sebi_fee_per_crore: 0, gst_pct: 0, round_stt: false };

test("5-minute bars: sessions are cut at 15:10, the feed's closing prints are dropped", () => {
  assert.equal(sessions.length, 4);
  assert.deepEqual(sessions.map((s) => s.count), [22, 72, 72, 72]);
  assert.deepEqual(sessions.map((s) => s.full), [false, true, true, true]);
  assert.equal(sessions[3].bars[71].c, 22517);                  // the 15:10 bar
  assert.equal(I.hm(0), "09:15"); assert.equal(I.hm(72), "15:15");
});

test("the variance profile is a U that sums to one, and blends real sessions in", () => {
  const p = I.sessionProfile(sessions), flat = I.sessionProfile([]);
  near(p.shares.reduce((a, b) => a + b, 0), 1, 1e-12);
  near(flat.shares.reduce((a, b) => a + b, 0), 1, 1e-12);
  assert.equal(flat.empirical, false); assert.equal(p.empirical, true);
  assert.ok(p.shares[0] > 2 * p.shares[30], "the open spends more than midday");
  assert.ok(p.shares[70] > p.shares[40], "and so does the close");
  assert.ok(p.shares.every((x) => x > 0));
});

test("the clock spends variance: Teff falls by exactly the session's variance share, and a weekend arrives as the gap", () => {
  const prof = I.sessionProfile(sessions), phi = 0.25;
  const clk = I.makeClock(prof.shares, phi, "2026-10-26", "2026-10-27");     // Monday, the weekly expires Tuesday
  near(clk.Teff(0) - clk.Teff(75), (1 - phi) / 365, 1e-15);
  near(clk.Teff(75), 1 / 365, 1e-15);                                           // overnight to Tuesday's close: a full calendar day
  const fri = I.makeClock(prof.shares, phi, "2026-10-23", "2026-10-27");
  const gapTime = fri.Teff(75) - clk.Teff(0);                                  // Friday close -> Monday open
  near(gapTime, (3 - (1 - phi)) / 365, 1e-15);                                 // three calendar days, less one session's worth
  const sp = I.startPoint(ist("2026-10-24T11:00:00"), HOL, false);
  assert.equal(sp.kind, "open"); assert.equal(sp.day, "2026-10-26"); assert.equal(sp.b0, 3);   // 09:30 by default
  const live = I.startPoint(ist("2026-10-23T10:02:00"), HOL, false);
  assert.equal(live.kind, "live"); assert.equal(live.b0, 10); assert.equal(live.day, "2026-10-23");
  assert.equal(I.startPoint(ist("2026-10-21T08:00:00"), HOL, false).day, "2026-10-21");
  assert.equal(I.startPoint(ist("2026-10-19T16:00:00"), HOL, false).day, "2026-10-21");   // Dussehra on the 20th
});

test("one clock: the option's sellable price is a martingale along the implied paths (no spread, no charges)", async () => {
  for (const [file, side, startIso, strike] of [
    ["indmoney-chain-NIFTY-20261027-eod23.json", "CE", "2026-10-23T10:01:00", 22600],
    ["indmoney-chain-NIFTY-20261027-eod23.json", "PE", "2026-10-23T10:01:00", 22550]]) {
    const ch = chainOf(file, "2026-10-23T10:00:00+05:30");
    for (const row of ch.strikes) for (const lg of [row.ce, row.pe]) if (lg) { lg.bid = null; lg.ask = null; }
    const rep = await I.analyseIntraday({ sessions, chain: ch, daily, side, strike, lots: 1, nowMs: ist(startIso), holidays: HOL,
      models: ["implied"], variants: ["hold"], rules: { slPct: 100, tp1Pct: 1e6, tp2Pct: 1e6 }, rates: FREE, paths: 20000, seed: 3, sensitivity: false });
    const v = rep.models.implied.variants.hold, qty = rep.meta.qty;
    assert.equal(rep.meta.start.kind, "live");
    // E is the ask (= the mid with no spread); the mean P&L per unit must be ~0 to within 4 standard errors
    near(v.expectancy / qty, 0, 4 * v.se / qty + 1e-9, side + " martingale (mean pnl per unit " + (v.expectancy / qty).toFixed(3) + ", se " + (v.se / qty).toFixed(3) + ")");
  }
});

test("one clock, from the open: the overnight gap keeps the martingale too (a weekend away)", async () => {
  const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json");
  for (const row of ch.strikes) for (const lg of [row.ce, row.pe]) if (lg) { lg.bid = null; lg.ask = null; }
  const rep = await I.analyseIntraday({ sessions, chain: ch, daily, side: "CE", strike: 22600, lots: 1, nowMs: ist("2026-10-24T11:00:00"), holidays: HOL,
    models: ["implied"], variants: ["hold"], rules: { slPct: 100, tp1Pct: 1e6, tp2Pct: 1e6 }, rates: FREE, paths: 30000, seed: 4, sensitivity: false });
  const v = rep.models.implied.variants.hold;
  assert.equal(rep.meta.start.kind, "open"); assert.equal(rep.meta.start.day, "2026-10-26");
  near(v.expectancy / rep.meta.qty, 0, 4 * v.se / rep.meta.qty + 1e-9, "martingale from the open");
  // the volatility solved on the clock is a little under the calendar-quoted one (a Friday-to-Tuesday option holds a weekend)
  assert.ok(rep.meta.sigma_clock_pct < rep.meta.sigma_calendar_pct + 1e-9);
});

test("the desk's levels: SL -10%, T1 +15%, T2 +20% on the 0.05 tick, and the ladder with breathing room", () => {
  const lv = I.levelsFor(130, I.DEFAULT_RULES, 22600, 0.5);
  assert.equal(lv.sl, 117); assert.equal(lv.t1, 149.5); assert.equal(lv.t2, 156);
  assert.equal(lv.breathPts, Math.round(22600 * 0.00085)); assert.equal(lv.breath, Math.max(5, Math.round(19 * 0.5)));
  assert.equal(lv.steps[0].stop, 130);                                        // the first step is breakeven
  assert.deepEqual(lv.steps.map((s) => s.at), [143, 149.5, 156, 169, 182, 195]);
  assert.ok(lv.steps.every((s, i) => s.stop >= 130 && (i === 0 || s.stop >= lv.steps[i - 1].stop)));
});

test("the exit engine on crafted premium paths: stop first, gap fills, ladder, T1 then T2, 15:15", () => {
  const rules = Object.assign({}, I.DEFAULT_RULES), E = 100, lv = I.levelsFor(E, rules, 22600, 0.5);
  const bars = (arr) => (n) => { const b = arr[Math.min(n, arr.length - 1)]; return { o: b[0], hi: b[1], lo: b[2], c: b[3] }; };
  const run = (arr, variant, lots) => I.runDesk(bars(arr), arr.length, E, lv, I.VARIANTS[variant], lots, { stop: lv.sl, t1Done: false, b0: 0 }, rules);
  // 1. straight to the stop
  let r = run([[100, 101, 89, 90], [90, 91, 80, 85]], "desk", 2);
  assert.equal(r.cls, "stop"); assert.equal(r.exits[0].px, 90); assert.equal(r.exits[0].q, 2);
  // 2. a bar that opens through the stop fills at the open
  r = run([[100, 101, 99, 100], [85, 86, 80, 82]], "desk", 1);
  assert.equal(r.cls, "stop"); assert.equal(r.exits[0].px, 85);
  // 3. both touched in one bar: the stop wins
  r = run([[100, 116, 89, 100]], "desk", 1);
  assert.equal(r.cls, "stop");
  // 4. one lot: T1 takes it all, at +15% (a better open fills better)
  r = run([[100, 116, 99, 115]], "desk", 1);
  assert.equal(r.cls, "t1_all"); assert.equal(r.exits[0].px, 115);
  r = run([[118, 119, 117, 118]], "desk", 1);
  assert.equal(r.exits[0].px, 118);
  // 5. two lots: half at T1; T1 is also the +15% ladder step, so the stop is already at (+15% - breathing room) = 105, not entry
  r = run([[100, 116, 99, 114], [114, 114, 99, 100]], "desk", 2);
  assert.equal(r.cls, "t1_trail"); assert.equal(r.exits.length, 2); assert.equal(r.exits[0].q, 1); assert.equal(r.exits[0].px, 115);
  assert.equal(r.exits[1].px, lv.steps[1].stop); assert.equal(lv.steps[1].stop, 105);
  // ... and where the breathing room is wide enough that the stop sits at entry, it is "stopped at entry"
  const wide = I.levelsFor(E, Object.assign({}, rules, { minBreath: 30 }), 22600, 0.5);
  r = I.runDesk(bars([[100, 116, 99, 114], [114, 114, 99, 100]]), 2, E, wide, I.VARIANTS.desk, 2, { stop: wide.sl, t1Done: false, b0: 0 }, rules);
  assert.equal(r.cls, "t1_be"); assert.equal(r.exits[1].px, 100);
  // 6. the ladder: +10% moves the stop to entry, +20% to (at - breath); T2 takes the rest
  r = run([[100, 112, 99, 111], [111, 125, 110, 124]], "desk", 2);
  assert.equal(r.cls, "t2"); assert.deepEqual(r.exits.map((e) => e.why), ["t1", "t2"]);
  // the same path on "ride": no T2, the ladder trails, then 15:15 (the last bar's close)
  r = run([[100, 112, 99, 111], [111, 125, 110, 124]], "ride", 2);
  assert.equal(r.cls, "time"); assert.equal(r.exits[r.exits.length - 1].why, "time");
  // 7. the ladder protects: up 25%, back through the locked stop
  const lock = lv.steps[2].stop;                                              // at +20%
  r = run([[100, 121, 99, 120], [120, 121, 90, 95]], "trail", 1);
  assert.equal(r.cls, "trail_out"); assert.equal(r.exits[0].px, Math.max(lv.steps[2].stop, lv.steps[1].stop));
  assert.ok(lock >= 100);
  // 8. "stop only" has no ladder: the same path hits the original stop instead
  r = run([[100, 121, 99, 120], [120, 121, 85, 90]], "hold", 1);
  assert.equal(r.cls, "stop"); assert.equal(r.exits[0].px, 90);
  // 9. nothing happens: out at the last bar's close
  r = run([[100, 104, 97, 101], [101, 105, 98, 103]], "desk", 2);
  assert.equal(r.cls, "time"); assert.equal(r.exits[0].px, 103); assert.equal(r.exits[0].q, 2);
  // 10. all@T2 books nothing at T1
  r = run([[100, 116, 99, 114], [114, 122, 110, 121]], "allT2", 2);
  assert.equal(r.cls, "t2"); assert.equal(r.exits.length, 1); assert.equal(r.exits[0].q, 2);
});

test("a full live run: three models, six variants, probabilities that add up", async () => {
  const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json", "2026-10-23T10:00:00+05:30");
  const rep = await I.analyseIntraday({ sessions, chain: ch, daily, side: "CE", strike: 22600, lots: 2, nowMs: ist("2026-10-23T10:01:00"), holidays: HOL,
    paths: 4000, seed: 9 });
  assert.deepEqual(Object.keys(rep.models).sort(), ["bootstrap", "implied", "replay"]);
  assert.equal(rep.meta.lots, 2); assert.equal(rep.meta.qty, 130);
  assert.equal(rep.meta.exchange, "NFO");
  near(rep.meta.entry_plan, 134.05, 2.5, "entry near the quoted ask");
  for (const m of ["implied", "bootstrap", "replay"]) {
    const M = rep.models[m];
    assert.ok(M.n > 0, m);
    for (const [key, v] of Object.entries(M.variants)) {
      let p = 0; for (const c of Object.values(v.classes)) p += c.p;
      near(p, 1, 1e-9, m + "/" + key + " classes sum to one");
      assert.ok(v.worst <= v.p5 && v.p5 <= v.p50 && v.p50 <= v.p95 && v.p95 <= v.best, m + "/" + key + " quantiles ordered");
      assert.ok(v.minutes_median >= 5 && v.minutes_median <= 5 * rep.meta.bars, m + "/" + key + " time in range");
      assert.ok(v.worst >= -(rep.levels.risk_inr * 2.2) - 1500 || key === "hold", m + "/" + key + " losses are bounded near the stop");
    }
    // desk, ride and allT1 are the same path until T1: the same stop-outs, to the path
    const d = M.variants.desk.classes.stop, r = M.variants.ride.classes.stop, a = M.variants.allT1.classes.stop;
    assert.deepEqual([d && d.count, r && r.count], [a && a.count, a && a.count]);
  }
  // today's session (the 23rd, the run's own day) is not replayed against itself: two sessions, seven starts each
  assert.equal(rep.models.replay.independent, 2);
  assert.equal(rep.models.replay.n, 14);
  // a call loses to time passing: flat index, lower premium at 15:15
  assert.ok(rep.meta.flat_decay_inr < 0);
  assert.equal(rep.sensitivity.length, 5);
  // more volatility than the option was bought at helps a long option
  assert.ok(rep.sensitivity[4].expectancy > rep.sensitivity[0].expectancy);
});

test("bootstrap at the implied level matches the implied model's variance (and the realised level is reported)", async () => {
  const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json", "2026-10-23T10:00:00+05:30");
  const base = { sessions, chain: ch, daily, side: "PE", strike: 22600, lots: 1, nowMs: ist("2026-10-23T10:01:00"), holidays: HOL, rates: FREE, paths: 12000, seed: 5,
    variants: ["hold"], rules: { slPct: 100, tp1Pct: 1e6, tp2Pct: 1e6 }, sensitivity: false, models: ["implied", "bootstrap"] };
  const real = await I.analyseIntraday(base), imp = await I.analyseIntraday(Object.assign({}, base, { volMode: "implied" }));
  assert.ok(real.meta.realised_ann_pct > 3 && real.meta.realised_ann_pct < 60, "a plausible annualised realised level: " + real.meta.realised_ann_pct);
  const a = imp.models.implied.variants.hold, b = imp.models.bootstrap.variants.hold;
  near(b.sd / a.sd, 1, 0.12, "same spread of outcomes at the same level (sd ratio)");
  near(b.expectancy / imp.meta.qty, a.expectancy / imp.meta.qty, 6 * Math.hypot(a.se, b.se) / imp.meta.qty + 1.5, "and about the same mean");
});

test("from the open: the SENSEX chain uses BFO charges; costs fall on each order on their own", async () => {
  const ch = chainOf("indmoney-chain-SENSEX-20261029-eod23.json");
  const rep = await I.analyseIntraday({ sessions: [], chain: ch, daily: null, side: "CE", strike: 73000, lots: 1, nowMs: ist("2026-10-24T11:00:00"), holidays: HOL,
    models: ["implied"], variants: ["desk"], paths: 600, seed: 2, sensitivity: false });
  assert.equal(rep.meta.exchange, "BFO"); assert.equal(rep.meta.lot, 20);
  assert.equal(rep.meta.start.kind, "open");
  assert.ok(rep.notes.length === 0, "implied alone needs no 5-minute bars");
  const E = rep.models.implied.entry;
  // one lot of an option that closes only on the stop: its loss is the premium fall plus charges, never less than the buy charges
  const v = rep.models.implied.variants.desk;
  assert.ok(v.worst < -T.fnoCosts("buy", E * 20, "OPT", "BFO").total);
});

test("an open position: the answer is what following the plan adds over selling now", async () => {
  const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json", "2026-10-23T11:00:00+05:30");
  const leg = ch.strikes.find((x) => x.strike === 22600).ce;
  const rep = await I.analyseIntraday({ sessions, chain: ch, daily, derivativeId: leg.id, lots: 2, nowMs: ist("2026-10-23T11:01:00"), holidays: HOL,
    mode: "position", position: { entry: 120, stop: 108, t1Done: false, remainingLots: 2 }, paths: 2500, seed: 6, models: ["implied", "bootstrap"], sensitivity: false });
  assert.equal(rep.meta.mode, "position"); assert.equal(rep.meta.side, "CE"); assert.equal(rep.meta.position.stop, 108);
  assert.ok(rep.meta.value_now > 0);
  const v = rep.models.implied.variants.desk;
  assert.equal(v.p_day_stop, null);
  // the premium now is ~132 against entry 120: the stop at 108 is well below the money in hand, so the worst case is losing the gap to it
  assert.ok(v.worst > -(rep.meta.value_now), "cannot lose more than the position is worth");
});

test("refusals: after 15:00, past 15:15, an expiry already gone, a strike that is not loaded", async () => {
  const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json", "2026-10-23T15:00:00+05:30");
  const base = { sessions, chain: ch, daily, side: "CE", strike: 22600, lots: 1, holidays: HOL, models: ["implied"], paths: 200 };
  await assert.rejects(I.analyseIntraday(Object.assign({}, base, { nowMs: ist("2026-10-23T15:03:00") })), /no new entries after 15:00/);
  await assert.rejects(I.analyseIntraday(Object.assign({}, base, { nowMs: ist("2026-10-23T15:20:00"), mode: "position", position: { entry: 100, stop: 90 } })), /no bars left/);
  await assert.rejects(I.analyseIntraday(Object.assign({}, base, { nowMs: ist("2026-10-28T09:30:00") })), /before the run starts/);
  await assert.rejects(I.analyseIntraday(Object.assign({}, base, { strike: 12345, nowMs: ist("2026-10-23T10:01:00") })), /not in the loaded chain/);
});

test("first-passage odds match the theory of a fair game: +15% before -10% is 40%, +10% before -10% is 50% (no spread, no ladder)", async () => {
  I.VARIANTS.fp = { label: "first passage", note: "", t1: true, t1All: true, t2: false, ladder: false };
  try {
    const ch = chainOf("indmoney-chain-NIFTY-20261027-eod23.json", "2026-10-23T10:00:00+05:30");
    for (const row of ch.strikes) for (const lg of [row.ce, row.pe]) if (lg) { lg.bid = null; lg.ask = null; }
    for (const [tp, want] of [[15, 0.4], [10, 0.5]]) {
      const rep = await I.analyseIntraday({ sessions, chain: ch, daily, side: "CE", strike: 22600, lots: 1, nowMs: ist("2026-10-23T10:01:00"), holidays: HOL,
        models: ["implied"], variants: ["fp"], rules: { slPct: 10, tp1Pct: tp, tp2Pct: 1e6 }, rates: FREE, paths: 20000, seed: 8, sensitivity: false });
      const c = rep.models.implied.variants.fp.classes;
      near(c.t1_all.p, want, 0.015, "P(+" + tp + "% before -10%)");
    }
  } finally { delete I.VARIANTS.fp; }
});
