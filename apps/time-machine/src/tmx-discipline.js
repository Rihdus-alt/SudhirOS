/* The three discipline rules (adopted 21 Oct 2026; rule 2 amended 21 Nov 2026) and the A+ hold, as pure functions.
 * The page (tmx-discipline-ui.js) feeds them its candles and journal; nothing here touches the page or the network.
 *
 *   1. ONE RE-ENTRY PER SETUP. After an SL hit, the next entry in that idea needs a fresh A+ 5m close on both indices. The second
 *      and later entries after an SL hit break the rule.
 *   2. PROFIT LOCK. Once the day's realised P&L reaches the lock (10,000), only A+ setups from then on, at the normal 4-6 lots.
 *      Any non-A+ or oversized entry above the trigger is a rule break.
 *   3. RANGE FILTER. If the last 6-8 closed 5m candles sit inside a 400-point SENSEX range (120 NIFTY), no entries until a 5m close
 *      breaks the range by the buffer (0.035% of the index: 25 points SENSEX, about 8 NIFTY).
 *   A+ = a 5m close beyond the level on both indices on the same candle, then the two-candle hold (25 Nov rule).
 *
 * Where the wording leaves room, the reading taken is written next to the code and shown on the page; the numbers live in
 * config/rules.disc so they can be changed without touching code.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.TMXD = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const toMin = (hm) => { const p = String(hm || "0:0").split(":").map(Number); return p[0] * 60 + (p[1] || 0); };
  const DEFAULTS = {
    reentries: 1,                                    // rule 1: entries allowed after an SL hit
    lockMaxLots: 6,                                  // rule 2: the normal 4-6 lots; above this is oversized once the lock is on
    hold: 2,                                         // the A+ hold: this many further 5m closes beyond the level, on both indices
    rangeMin: 6, rangeMax: 8,                        // rule 3: the last 6-8 closed candles
    range: { SENSEX: { width: 400 }, NIFTY: { width: 120 } },
    bufPct: 0.00035,                                 // 0.035% of the index
    from: { 1: "2026-10-21", 2: "2026-11-21", 3: "2026-10-21" },   // when each rule came into force (rule 2: as amended)
  };
  const cfgWith = (c) => {
    const o = Object.assign({}, DEFAULTS, c || {});
    o.range = { SENSEX: Object.assign({}, DEFAULTS.range.SENSEX, ((c || {}).range || {}).SENSEX), NIFTY: Object.assign({}, DEFAULTS.range.NIFTY, ((c || {}).range || {}).NIFTY) };
    o.from = Object.assign({}, DEFAULTS.from, (c || {}).from);
    return o;
  };

  /** Rule 3 over the day's closed 5m candles [{high, low, close, datetime_ist}] (oldest first).
   *  A range forms when the last `rangeMin` candles sit inside `width` points; it is stretched back to as many as `rangeMax` that
   *  still fit, and its band widens with later candles that stay inside the width. A close beyond the band by the buffer breaks it,
   *  and a new one can only be formed from candles after the break (so the breakout candle never makes the next range).
   *  Returns {active, lo, hi, up, dn, n, since, broke}: up/dn are the two break levels, broke the latest break {dir, t, level}. */
  function rangeState(closed, ix, cfg) {
    const c = cfgWith(cfg), W = (c.range[ix] || c.range.SENSEX).width, n = closed.length;
    let rng = null, broke = null;
    for (let i = 0; i < n; i++) {
      const b = closed[i], buf = c.bufPct * b.close;
      if (rng) {
        if (b.close > rng.hi + buf) { broke = { dir: 1, i, t: String(b.datetime_ist || "").slice(11, 16), level: rng.hi + buf }; rng = null; }
        else if (b.close < rng.lo - buf) { broke = { dir: -1, i, t: String(b.datetime_ist || "").slice(11, 16), level: rng.lo - buf }; rng = null; }
        else { const nh = Math.max(rng.hi, b.high), nl = Math.min(rng.lo, b.low); if (nh - nl <= W) { rng.hi = nh; rng.lo = nl; } rng.n++; }
      }
      if (!rng) {
        const first = broke ? broke.i + 1 : 0;
        let best = null, hi = -Infinity, lo = Infinity;
        for (let len = 1; len <= c.rangeMax && i - len + 1 >= first; len++) {
          const w = closed[i - len + 1]; hi = Math.max(hi, w.high); lo = Math.min(lo, w.low);
          if (hi - lo > W) break;
          if (len >= c.rangeMin) best = { hi, lo, n: len, from: i - len + 1 };
        }
        if (best) rng = best;
      }
    }
    const last = n ? closed[n - 1].close : 0, buf = c.bufPct * last;
    if (!rng) return { active: false, n: 0, broke, width: W, buf };
    return { active: true, lo: rng.lo, hi: rng.hi, up: rng.hi + buf, dn: rng.lo - buf, n: rng.n, since: String(closed[rng.from].datetime_ist || "").slice(11, 16), broke, width: W, buf };
  }

  /** The A+ hold: after the break candle k, every later closed candle must still close beyond the trigger, `need` of them.
   *  done: held enough; failed: one closed back inside first (the break did not hold). */
  function holdStatus(closed, k, dir, trigger, need) {
    let held = 0;
    for (let j = k + 1; j < closed.length; j++) {
      const beyond = dir > 0 ? closed[j].close > trigger : closed[j].close < trigger;
      if (!beyond) return { held, need, done: false, failed: true };
      held++;
    }
    return { held, need, done: held >= need, failed: false };
  }

  /** The three checks for one entry. o: {date, time (entry, HH:MM), dir (1 call, -1 put), lots, ix,
   *    aplus: null | {t: HH:MM of the A+ break candle, or null when only claimed},
   *    prior: [{dir, time, closedAt (HH:MM or null while open), stopped}]  (the day's earlier entries, any index)
   *    realisedBefore: the day's realised P&L when this entry was made,
   *    range: the rangeState at that moment, or null when it cannot be known,
   *    lock: the profit lock in rupees, cfg}
   *  Returns [{rule, ok, known, text, flags: [short journal labels when not ok]}], one per rule (1, 2, 3). */
  function checkEntry(o) {
    const c = cfgWith(o.cfg), out = [];
    const rup = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
    // ---- rule 1
    {
      const r = { rule: 1, ok: true, known: true, text: "", flags: [] };
      if (o.date && o.date < c.from[1]) r.text = "Rule 1 was not in force yet.";
      else {
        const same = (o.prior || []).filter((p) => p.dir === o.dir && toMin(p.time) <= toMin(o.time));
        const stops = same.filter((p) => p.stopped && p.closedAt && toMin(p.closedAt) <= toMin(o.time)).sort((a, b) => toMin(a.closedAt) - toMin(b.closedAt));
        if (!stops.length) r.text = "No stop-out on this side today: a first entry.";
        else {
          const first = stops[0], last = stops[stops.length - 1];
          const after = same.filter((p) => toMin(p.time) >= toMin(first.closedAt)).length;
          if (after >= c.reentries) { r.ok = false; r.text = "Entry " + (after + 1) + " after the " + first.closedAt + " stop-out: one re-entry per setup, and it is used. No re-entering to make it back."; r.flags.push("Re-entry: second entry after a stop-out"); }
          else if (o.aplus && o.aplus.t && toMin(o.aplus.t) + 5 > toMin(last.closedAt)) r.text = "One re-entry after the " + last.closedAt + " stop-out, on a fresh A+ (candle " + o.aplus.t + ").";
          else if (o.aplus === undefined) { r.known = false; r.text = "Stopped out at " + last.closedAt + ": a re-entry needs a fresh A+ after that. The app has no A+ record for this day to check it."; }
          else { r.ok = false; r.text = "Stopped out at " + last.closedAt + ": the next entry needs a fresh A+ close on both indices after that" + (o.aplus ? " (the app has not seen one: tick A+ only if it has)." : "."); r.flags.push("Re-entry after a stop-out without a fresh A+"); }
        }
      }
      out.push(r);
    }
    // ---- rule 2
    {
      const r = { rule: 2, ok: true, known: true, text: "", flags: [] }, lock = +o.lock || 0;
      if (o.date && o.date < c.from[2]) r.text = "Rule 2 (as amended 21 Nov) was not in force yet.";
      else if (!lock) r.text = "No profit lock set.";
      else if (!((o.realisedBefore || 0) >= lock)) r.text = "Day realised " + rup(o.realisedBefore || 0) + ": the " + rup(lock) + " lock is not on.";
      else {
        const bad = [];
        if (o.aplus === undefined) r.known = false;               // no A+ record for the day: A+ cannot be judged, size still can
        else if (!o.aplus) { bad.push("not A+"); r.flags.push("Profit lock: not A+"); }
        if (o.lots > c.lockMaxLots) { bad.push(o.lots + " lots, above the normal " + c.lockMaxLots); r.flags.push("Profit lock: oversized"); }
        r.ok = !bad.length;
        r.text = "Profit lock is ON (day realised " + rup(o.realisedBefore) + "): " + (r.ok ? (r.known ? "A+ at " + o.lots + " lots, allowed." : "size is fine; the app has no A+ record for this day to check A+.") : bad.join(" and ") + ". Only A+ at 4-" + c.lockMaxLots + " lots, or stop for the day.");
      }
      out.push(r);
    }
    // ---- rule 3
    {
      const r = { rule: 3, ok: true, known: !!o.range, text: "", flags: [] };
      if (o.date && o.date < c.from[3]) { r.text = "Rule 3 was not in force yet."; r.known = true; }
      else if (!o.range) r.text = "The range cannot be told for this entry (no candles for it).";
      else if (o.range.active) { r.ok = false; r.text = "RANGE: no trade. The last " + o.range.n + " candles sit inside " + Math.round(o.range.lo) + " to " + Math.round(o.range.hi) + ". Break above " + Math.round(o.range.up) + " or below " + Math.round(o.range.dn) + " on a 5m close first."; r.flags.push("Entered inside a range (RANGE: no trade)"); }
      else r.text = o.range.broke ? "Not in a range (the last one broke " + (o.range.broke.dir > 0 ? "up" : "down") + " at " + o.range.broke.t + ")." : "Not in a range.";
      out.push(r);
    }
    return out;
  }

  return { DEFAULTS, cfgWith, rangeState, holdStatus, checkEntry, toMin };
});
