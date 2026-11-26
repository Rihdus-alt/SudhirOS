/* The A+ trade finder's pure parts: where a stop belongs, what a plan needs from its signal to pay, the record of past A+ calls,
 * and how a fresh break is told from a stale one. The page (tmx-aplus-ui.js) feeds it the desk's levels, candles and chain and
 * the Time Machine's odds. Nothing here predicts: the odds it shows are the models' (no edge assumed), and the record is
 * what the app's own A+ calls actually did on the real tapes.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.TMXA = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const r05 = (x) => Math.round(x * 20) / 20;
  const T1_CLASSES = ["t1_all", "t1_be", "t1_trail", "t2"];

  /** Wilson score interval for k hits in n tries. */
  function wilson(k, n, z) {
    z = z || 1.96;
    if (!n) return { p: null, lo: 0, hi: 1, n: 0 };
    const p = k / n, d = 1 + z * z / n, c = p + z * z / (2 * n), h = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n));
    return { p, lo: Math.max(0, (c - h) / d), hi: Math.min(1, (c + h) / d), n };
  }

  /** Where the stop goes, from the market and not from a score: the point where the break has failed (a close back through the
   *  level by the desk's buffer), made at least as wide as the day's noise (1.5 typical 5-minute moves, so an ordinary wiggle
   *  does not take it), kept inside [minPct, maxPct] of the premium. A break that needs more than maxPct does not fit, and is
   *  not A+. This is a rule, not a search: picking the best of many stops on a simulation would only fit the simulation's noise.
   *  dir 1 long, -1 short; spot the index now; level the broken level; buf the desk's buffer (index points). */
  function structuralStop(o) {
    const d = Math.max(Math.abs(o.delta || 0.5), 0.05), minPct = o.minPct == null ? 7 : o.minPct, maxPct = o.maxPct == null ? 15 : o.maxPct, nm = o.noiseMult == null ? 1.5 : o.noiseMult;
    const inval = o.dir > 0 ? o.level - o.buf : o.level + o.buf;
    const structPts = o.dir > 0 ? o.spot - inval : inval - o.spot;
    if (!(structPts > 0) || !(o.entry > 0)) return { ok: false, why: ["the index is already through the point where the break has failed"] };
    const structPct = structPts * d / o.entry * 100, noisePts = nm * (o.barSd5 || 0), noisePct = noisePts * d / o.entry * 100;
    const raw = Math.max(structPct, noisePct), slPct = Math.round(clamp(raw, minPct, maxPct) * 10) / 10;
    const bound = raw > maxPct ? "cap" : raw < minPct ? "floor" : noisePct > structPct ? "noise" : "structure";
    const why = [
      "the break has failed if the index closes back through " + Math.round(inval) + " (the level " + Math.round(o.level) + " less the " + Math.round(o.buf) + "-point buffer): " + Math.round(structPts) + " points, " + structPct.toFixed(1) + "% of the premium at delta " + d.toFixed(2),
      "An ordinary 5-minute wiggle is about " + Math.round(o.barSd5 || 0) + " points; the stop is kept at least " + nm + " of them away (" + noisePct.toFixed(1) + "%)",
    ];
    if (bound === "cap") why.push("that needs " + raw.toFixed(1) + "%, beyond the " + maxPct + "% limit: the stop does not fit, so this is not A+");
    if (bound === "floor") why.push("Below " + minPct + "% a stop is inside the bid-ask and the charges: raised to " + minPct + "%");
    return { ok: true, slPct, sl: r05(o.entry * (1 - slPct / 100)), structPct, noisePct, raw, bound, fits: raw <= maxPct, pts: slPct / 100 * o.entry / d, inval, why };
  }

  /** What a plan needs from its signal to pay. From the simulated outcome classes: move probability from "stopped" to "reached T1"
   *  and the average result rises by (average when T1 is reached - average when stopped) per point moved; the shift that brings
   *  the average to zero is what the signal has to supply. variants: [{v (a model's variant summary), w (weight)}]. */
  function breakeven(items) {
    const rows = [];
    for (const { v, w } of items) {
      if (!v || !v.n || !v.classes) continue;
      const t1 = T1_CLASSES.filter((c) => v.classes[c]), p1 = sum(t1.map((c) => v.classes[c].p));
      if (!(p1 > 0) || !v.classes.stop) continue;
      const gWin = sum(t1.map((c) => v.classes[c].p * v.classes[c].avg)) / p1, gStop = v.classes.stop.avg, gap = gWin - gStop;
      if (!(gap > 0)) continue;
      rows.push({ w, p1, exp: v.expectancy, se: v.se, gWin, gStop, gap, shift: -v.expectancy / gap, shiftSe: v.se / gap });
    }
    if (!rows.length) return null;
    const tw = sum(rows.map((r) => r.w)), f = (k) => sum(rows.map((r) => r.w * r[k])) / tw;
    const p1 = f("p1"), shift = f("shift");
    return { p1, shift, shiftSe: Math.sqrt(sum(rows.map((r) => r.w * r.w * r.shiftSe * r.shiftSe))) / tw, need: clamp(p1 + shift, 0, 1), gWin: f("gWin"), gStop: f("gStop"), n: rows.length };
  }

  /** The most recent fresh break: a closed candle beyond the trigger the levels had one candle earlier, whose own predecessor was
   *  not already beyond it, within the last `window` candles. levelsAt(k) gives the trigger the levels showed as of candle k-1.
   *  Candles: [{close, datetime_ist}] (closed ones only). Returns the break with its candle, or null. */
  function findBreak(closed, dir, levelsAt, window) {
    const n = closed.length;
    for (let k = n - 1; k >= Math.max(1, n - (window || 3)); k--) {
      const lv = levelsAt(k);
      if (!lv || lv.trigger == null) continue;
      const beyond = (c) => (dir > 0 ? c > lv.trigger : c < lv.trigger);
      if (beyond(closed[k].close) && !beyond(closed[k - 1].close)) return Object.assign({ k, t: String(closed[k].datetime_ist).slice(11, 16), close: closed[k].close, ago: n - 1 - k }, lv);
    }
    return null;
  }

  /** The record of the app's own A+ calls: what each said (the models' chance of reaching T1 and T2 and of being stopped, and
   *  what the plan needed) against what happened on the real tape. rows: [{p1, y1, p2, y2, ps, ys, need}]. */
  function record(rows, n0) {
    n0 = n0 == null ? 15 : n0;
    const n = rows.length, k1 = sum(rows.map((r) => r.y1)), e1 = sum(rows.map((r) => r.p1)), k2 = sum(rows.map((r) => r.y2 || 0)), e2 = sum(rows.map((r) => r.p2 || 0));
    const ks = sum(rows.map((r) => r.ys || 0)), es = sum(rows.map((r) => r.ps || 0));
    const needs = rows.map((r) => r.need).filter((x) => x != null), need = needs.length ? sum(needs) / needs.length : null, w1 = wilson(k1, n), pbar = n ? e1 / n : 0;
    const lift = n ? clamp((k1 + n0 * pbar) / (e1 + n0 * pbar), 0.6, 1.4) : 1;
    const applied = n >= 20;
    let verdict = "none";
    if (n && n < 20) verdict = "thin";
    else if (n >= 20) verdict = need == null ? "unclear" : w1.lo >= need ? "above" : w1.hi < need ? "below" : "unclear";
    return { n, k1, e1, rate1: w1.p, lo1: w1.lo, hi1: w1.hi, k2, e2, ks, es, need, lift, applied, verdict, expectedRate: n ? e1 / n : null };
  }

  /** Lots: as many as fit the money left to the day stop (and the capital cap, when one is set), at most `max`; at least one is
   *  always shown with a flag when even that risks more than is left. */
  function sizeLots(o) {
    const max = o.max || 3, byRisk = o.riskPerLot > 0 ? Math.floor(o.room / o.riskPerLot) : max, byCap = o.cap && o.outlayPerLot > 0 ? Math.floor(o.cap / o.outlayPerLot) : max;
    const lots = clamp(Math.min(byRisk, byCap, max), 1, max);
    return { lots, fits: byRisk >= 1 && byCap >= 1, byRisk, byCap };
  }

  /** The A+ checklist: A+ only if every check passes; one miss in trend alone makes it an A (a reversal, stricter). */
  function grade(c) {
    const misses = [];
    if (!c.bothBroke) misses.push("only one index has closed beyond its A+ level");
    if (!c.roomOk) misses.push("not enough room to the next level");
    if (!c.stopFits) misses.push("the stop that survives the failed break is wider than the limit");
    if (!c.deltaOk) misses.push("no contract with delta 0.40-0.60");
    if (!c.spreadOk) misses.push("the bid-ask spread is too wide");
    if (!c.early) misses.push("after the entry cut-off");
    if (!c.riskFits) misses.push("one lot risks more than is left to the day stop");
    if (misses.length) return { grade: "none", misses };
    if (!c.withTrend) return { grade: "A", misses: ["against the current trend: a reversal, which the desk treats more strictly"] };
    return { grade: "A+", misses: [] };
  }

  return { wilson, structuralStop, breakeven, findBreak, record, sizeLots, grade, T1_CLASSES, r05 };
});
