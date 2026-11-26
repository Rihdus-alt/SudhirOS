// node --test apps/time-machine/test/discipline.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const D = require("../src/tmx-discipline.js");

const hm = (i) => { const m = 9 * 60 + 15 + 5 * i; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
// a candle from its open and close, wicks of `w` points
const bar = (i, o, c, w = 10) => ({ datetime_ist: "2026-11-26 " + hm(i) + ":00", open: o, high: Math.max(o, c) + w, low: Math.min(o, c) - w, close: c });
const series = (closes, o0 = 80000, w = 10) => closes.map((c, i) => bar(i, i ? closes[i - 1] : o0, c, w));

test("rule 3: six candles inside 400 SENSEX points are a range, with its two break levels", () => {
  const s = series([80010, 80040, 79990, 80060, 80020, 80050]);
  const r = D.rangeState(s, "SENSEX");
  assert.equal(r.active, true);
  assert.equal(r.n, 6);
  assert.ok(r.hi - r.lo <= 400);
  const buf = 0.00035 * 80050;                         // 28 points at this level (about 25 at 71,000)
  assert.ok(Math.abs(r.up - (r.hi + buf)) < 1e-6 && Math.abs(r.dn - (r.lo - buf)) < 1e-6);
  assert.equal(r.since, hm(0));
});

test("rule 3: five candles are not enough, and a range wider than 400 points is not a range", () => {
  assert.equal(D.rangeState(series([80010, 80040, 79990, 80060, 80020]), "SENSEX").active, false);
  const wide = D.rangeState(series([80000, 80150, 80300, 80450, 80600, 80750]), "SENSEX");
  assert.equal(wide.active, false);
});

test("rule 3: stretches back to 8 candles while they still fit, no further", () => {
  const s = series([80000, 80010, 80020, 80030, 80040, 80050, 80060, 80070, 80080, 80090]);
  const r = D.rangeState(s, "SENSEX");
  assert.equal(r.active, true);
  assert.ok(r.n >= 8);
});

test("rule 3: a 5m close beyond the band by the buffer breaks it; the breakout candle does not start the next range", () => {
  const base = [80010, 80040, 79990, 80060, 80020, 80050];
  const inside = D.rangeState(series(base.concat([80065])), "SENSEX");          // a close only just above the high, inside the buffer
  assert.equal(inside.active, true, "a close inside the buffer does not break it");
  const brk = D.rangeState(series(base.concat([80200])), "SENSEX");             // well beyond high + buffer
  assert.equal(brk.active, false);
  assert.equal(brk.broke.dir, 1);
  assert.equal(brk.broke.t, hm(6));
  const next = D.rangeState(series(base.concat([80200, 80210, 80190, 80220, 80205, 80215])), "SENSEX");   // 5 candles after the break: still not a range
  assert.equal(next.active, false);
  const again = D.rangeState(series(base.concat([80200, 80210, 80190, 80220, 80205, 80215, 80208])), "SENSEX");   // 6 after it: a new range
  assert.equal(again.active, true);
  assert.equal(again.since, hm(7));
  const down = D.rangeState(series(base.concat([79800])), "SENSEX");
  assert.equal(down.broke.dir, -1);
});

test("rule 3: NIFTY uses 120 points", () => {
  const s = series([24000, 24020, 24010, 24030, 24005, 24025], 24000, 3);
  assert.equal(D.rangeState(s, "NIFTY").active, true);
  const wide = series([24000, 24060, 24120, 24180, 24240, 24300], 24000, 3);
  assert.equal(D.rangeState(wide, "NIFTY").active, false);
  assert.equal(D.rangeState(wide, "SENSEX").active, true, "the same 300 points would be a SENSEX range");
});

test("the A+ hold: two more closes beyond the trigger; one back inside fails it", () => {
  const s = series([100, 101, 102, 110, 111, 112]);                 // the break candle is index 3 (close 110 > trigger 105)
  assert.deepEqual(D.holdStatus(s.slice(0, 4), 3, 1, 105, 2), { held: 0, need: 2, done: false, failed: false });
  assert.deepEqual(D.holdStatus(s.slice(0, 5), 3, 1, 105, 2), { held: 1, need: 2, done: false, failed: false });
  assert.deepEqual(D.holdStatus(s.slice(0, 6), 3, 1, 105, 2), { held: 2, need: 2, done: true, failed: false });
  const bad = series([100, 101, 102, 110, 104, 112]);
  assert.deepEqual(D.holdStatus(bad, 3, 1, 105, 2), { held: 0, need: 2, done: false, failed: true });
  assert.equal(D.holdStatus(series([100, 90, 89, 88]), 1, -1, 95, 2).done, true, "puts: closes below the trigger");
});

const stopped = (dir, time, closedAt) => ({ dir, time, closedAt, stopped: true });
const won = (dir, time, closedAt) => ({ dir, time, closedAt, stopped: false });
const entry = (over) => Object.assign({ date: "2026-11-26", time: "11:00", dir: 1, lots: 4, aplus: null, prior: [], realisedBefore: 0, range: null, lock: 10000 }, over);
const rule = (res, n) => res.find((r) => r.rule === n);

test("rule 1: a first entry is free; a re-entry after an SL hit needs a fresh A+; the second entry after it breaks the rule", () => {
  assert.equal(rule(D.checkEntry(entry({})), 1).ok, true);
  const prior = [stopped(1, "09:50", "10:10")];
  const no = rule(D.checkEntry(entry({ prior, time: "10:30" })), 1);
  assert.equal(no.ok, false);
  assert.deepEqual(no.flags, ["Re-entry after a stop-out without a fresh A+"]);
  const claimed = rule(D.checkEntry(entry({ prior, time: "10:30", aplus: { t: null } })), 1);
  assert.equal(claimed.ok, false, "an A+ the app did not see cannot be called fresh");
  const old = rule(D.checkEntry(entry({ prior, time: "10:30", aplus: { t: "09:45" } })), 1);
  assert.equal(old.ok, false, "an A+ from before the stop-out is not fresh");
  const fresh = rule(D.checkEntry(entry({ prior, time: "10:30", aplus: { t: "10:15" } })), 1);
  assert.equal(fresh.ok, true);
  const both = prior.concat([won(1, "10:30", "10:50")]);
  const second = rule(D.checkEntry(entry({ prior: both, time: "11:00", aplus: { t: "10:55" } })), 1);
  assert.equal(second.ok, false);
  assert.deepEqual(second.flags, ["Re-entry: second entry after a stop-out"]);
});

test("rule 1: the other side, a trade that did not hit its stop, and a trade still open do not count", () => {
  assert.equal(rule(D.checkEntry(entry({ prior: [stopped(-1, "09:50", "10:10")], time: "10:30" })), 1).ok, true, "a put stop-out does not touch a call entry");
  assert.equal(rule(D.checkEntry(entry({ prior: [won(1, "09:50", "10:10")], time: "10:30" })), 1).ok, true, "a winner is not an SL hit");
  assert.equal(rule(D.checkEntry(entry({ prior: [{ dir: 1, time: "09:50", closedAt: null, stopped: false }], time: "10:30" })), 1).ok, true, "still open");
  assert.equal(rule(D.checkEntry(entry({ prior: [stopped(1, "09:50", "10:40")], time: "10:30" })), 1).ok, true, "a stop-out after this entry is not before it");
});

test("rule 1 was adopted on 21 Oct 2026", () => {
  const prior = [stopped(1, "09:50", "10:10")];
  assert.equal(rule(D.checkEntry(entry({ prior, time: "10:30", date: "2026-10-20" })), 1).ok, true);
  assert.equal(rule(D.checkEntry(entry({ prior, time: "10:30", date: "2026-10-21" })), 1).ok, false);
});

test("rule 2: from 10,000 realised only A+ at 4-6 lots; the line itself counts", () => {
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 9999 })), 2).ok, true, "just under");
  const at = rule(D.checkEntry(entry({ realisedBefore: 10000 })), 2);
  assert.equal(at.ok, false);
  assert.deepEqual(at.flags, ["Profit lock: not A+"]);
  const a4 = rule(D.checkEntry(entry({ realisedBefore: 12500, aplus: { t: "10:55" }, lots: 4 })), 2);
  assert.equal(a4.ok, true);
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 12500, aplus: { t: "10:55" }, lots: 6 })), 2).ok, true);
  const big = rule(D.checkEntry(entry({ realisedBefore: 12500, aplus: { t: "10:55" }, lots: 8 })), 2);
  assert.equal(big.ok, false);
  assert.deepEqual(big.flags, ["Profit lock: oversized"]);
  const both = rule(D.checkEntry(entry({ realisedBefore: 12500, lots: 8 })), 2);
  assert.deepEqual(both.flags, ["Profit lock: not A+", "Profit lock: oversized"]);
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 12500, lots: 2 })), 2).flags.includes("Profit lock: oversized"), false, "smaller than normal is not oversized");
});

test("rule 2 as amended: in force from 21 Nov 2026", () => {
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 15000, date: "2026-11-20" })), 2).ok, true);
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 15000, date: "2026-11-21" })), 2).ok, false);
});

test("rule 3: an entry inside a range fails, with both levels in the message; unknown is not a failure", () => {
  const range = D.rangeState(series([80010, 80040, 79990, 80060, 80020, 80050]), "SENSEX");
  const r3 = rule(D.checkEntry(entry({ range })), 3);
  assert.equal(r3.ok, false);
  assert.match(r3.text, /RANGE: no trade/);
  assert.match(r3.text, new RegExp(Math.round(range.up).toString()));
  assert.match(r3.text, new RegExp(Math.round(range.dn).toString()));
  assert.deepEqual(r3.flags, ["Entered inside a range (RANGE: no trade)"]);
  const none = rule(D.checkEntry(entry({ range: null })), 3);
  assert.equal(none.ok, true);
  assert.equal(none.known, false);
  const broke = D.rangeState(series([80010, 80040, 79990, 80060, 80020, 80050, 80300]), "SENSEX");
  assert.equal(rule(D.checkEntry(entry({ range: broke })), 3).ok, true);
});

test("with no A+ record for the day (aplus undefined) A+ is not guessed: size and count are still judged", () => {
  const prior = [stopped(1, "09:50", "10:10")];
  const r1 = rule(D.checkEntry(entry({ prior, time: "10:30", aplus: undefined })), 1);
  assert.equal(r1.ok, true); assert.equal(r1.known, false); assert.deepEqual(r1.flags, []);
  const r1b = rule(D.checkEntry(entry({ prior: prior.concat([won(1, "10:30", "10:50")]), time: "11:00", aplus: undefined })), 1);
  assert.equal(r1b.ok, false, "a second entry after a stop-out needs no A+ record to be a break");
  const r2 = rule(D.checkEntry(entry({ realisedBefore: 12000, lots: 4, aplus: undefined })), 2);
  assert.equal(r2.ok, true); assert.equal(r2.known, false);
  const r2b = rule(D.checkEntry(entry({ realisedBefore: 12000, lots: 8, aplus: undefined })), 2);
  assert.deepEqual(r2b.flags, ["Profit lock: oversized"]);
});

test("every rule returns one entry, in order, so the page can show all three", () => {
  assert.deepEqual(D.checkEntry(entry({})).map((r) => r.rule), [1, 2, 3]);
});

test("the numbers can be changed in config/rules.disc without touching code", () => {
  const prior = [stopped(1, "09:50", "10:10"), won(1, "10:30", "10:50")];
  assert.equal(rule(D.checkEntry(entry({ prior, time: "11:00", aplus: { t: "10:55" }, cfg: { reentries: 2 } })), 1).ok, true);
  assert.equal(rule(D.checkEntry(entry({ realisedBefore: 12500, aplus: { t: "10:55" }, lots: 8, cfg: { lockMaxLots: 8 } })), 2).ok, true);
  const s = series([80010, 80040, 79990, 80060, 80020, 80050]);
  assert.equal(D.rangeState(s, "SENSEX", { range: { SENSEX: { width: 50 } } }).active, false);
});

test("on the real NIFTY 5-minute tapes of 21-23 Oct the filter is on part of the day, not all of it", () => {
  const f = path.join(__dirname, "..", "fixtures", "NIFTY-5m.csv");
  if (!fs.existsSync(f)) return;
  const [head, ...lines] = fs.readFileSync(f, "utf8").trim().split("\n"), cols = head.split(",");
  const rows = lines.map((l) => Object.fromEntries(l.split(",").map((v, i) => [cols[i], v]))).map((r) => ({ datetime_ist: r.datetime_ist, open: +r.open, high: +r.high, low: +r.low, close: +r.close }));
  let on = 0, all = 0;
  for (const day of ["2026-10-21", "2026-10-22", "2026-10-23"]) {
    const d = rows.filter((r) => r.datetime_ist.startsWith(day));
    for (let i = 6; i <= d.length; i++) { all++; if (D.rangeState(d.slice(0, i), "NIFTY").active) on++; }
  }
  assert.ok(all > 100);
  assert.ok(on > 0 && on < all, "range filter on " + on + " of " + all + " candle closes");
  console.log("  range filter on for " + on + " of " + all + " NIFTY 5m closes on 21-23 Oct (" + Math.round(100 * on / all) + "%)");
});
