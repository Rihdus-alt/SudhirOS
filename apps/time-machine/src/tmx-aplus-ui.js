// ---------- A+ trades, found on their own ----------
// Watches the desk's levels. When a 5-minute candle on both indices closes beyond its A+ level (the desk's own rule), it picks
// the contract, sets the stop from the market (structure, noise, risk), runs the Time Machine and shows the trade with its
// target, the chance of reaching it, and what the plan needs from the signal to pay. Runs in the desk's script, after tmx-desk.js.
const TMA = window.TMXA;
const AP = { ev: null, plan: null, busy: false, err: "", computedKey: "", computedAt: 0, partial: null, cache: {}, open: {}, html: "" };
const apSLOn = () => { try { return localStorage.getItem("desk.autoSL") !== "off"; } catch { return true; } };
const apDirWord = (d) => (d > 0 ? "LONG" : "SHORT");
const apFmt = (v) => Math.round(v).toLocaleString("en-IN");

/** A typical 5-minute move of the index today, in points: a robust spread of the candle bodies over the last two hours (so the
 *  breakout candle itself does not inflate it). */
function apBarSd(closed, spot) {
  const xs = closed.slice(-24).map((c) => c.close - c.open);
  if (xs.length < 10) return 0.0006 * spot;
  const med = (a) => { const b = a.slice().sort((x, y) => x - y), n = b.length; return n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2; };
  const m = med(xs);
  return 1.4826 * med(xs.map((x) => Math.abs(x - m)));
}
function apLevelsAt(ix, closed, k) {
  const key = ix + "|" + closed[k].datetime_ist + "|" + k + "|" + ((S.chains[ix] || {}).at || 0);
  if (AP.cache[key] === undefined) {
    if (Object.keys(AP.cache).length > 80) AP.cache = {};
    AP.cache[key] = computeLevels(ix, { spot: closed[k - 1].close, c: closed.slice(0, k) });
  }
  return AP.cache[key];
}
/** The fresh A+ breaks on each index: a closed candle beyond the A+ trigger the levels showed one candle earlier. */
function apDetect() {
  const out = { 1: {}, "-1": {} };
  for (const ix of ["SENSEX", "NIFTY"]) {
    const closed = closedCandles(ix);
    if (closed.length < 6) continue;
    for (const dir of [1, -1]) {
      out[dir][ix] = TMA.findBreak(closed, dir, (k) => {
        const lv = apLevelsAt(ix, closed, k); if (!lv) return null;
        const P = dir > 0 ? lv.bull : lv.bear, a = P.aplus;
        return a ? { trigger: a.trigger, level: a.level, labels: a.labels, next: a.next, room: a.room, need: P.need, buf: lv.buf, trend: lv.trend } : null;
      }, 3);
    }
  }
  return out;
}

// ----- the loop: called whenever the desk's levels are rebuilt
function apTick() {
  if (!S.levels) return;
  const t = ist(), brk = apDetect();
  let ev = null, partial = null;
  for (const dir of [1, -1]) {
    const s = brk[dir].SENSEX, n = brk[dir].NIFTY;
    if (s && n) { const e = { dir, s, n, key: `${t.date}|${dir}|${s.t}|${n.t}`, ago: Math.min(s.ago, n.ago) }; if (!ev || e.ago < ev.ago) ev = e; }
    else if (s || n) { const b = s || n; if (!partial || b.ago < partial.b.ago) partial = { dir, ix: s ? "SENSEX" : "NIFTY", missing: s ? "NIFTY" : "SENSEX", b }; }
  }
  AP.partial = partial;
  if (ev) { if (!AP.ev || AP.ev.key !== ev.key) AP.ev = Object.assign(ev, { seenAt: Date.now() }); else AP.ev.ago = ev.ago; AP.ev.live = true; }
  else if (AP.ev) AP.ev.live = false;
  const early = marketOpen() && t.mins < toMin(S.rules.noEntryAfter);
  if (AP.ev && AP.ev.live && early && !AP.busy && mcp && (AP.computedKey !== AP.ev.key || Date.now() - AP.computedAt > 240e3)) apCompute(AP.ev);
  apRender();
}

// ----- the trade
async function apPlanFor(ix, ev) {
  const dir = ev.dir, bk = ix === "SENSEX" ? ev.s : ev.n, R = tmRules(), t = ist(), prev = !!ev.preview;
  const lv = S.levels[ix] || computeLevels(ix), closed = closedCandles(ix);
  const ch = await tmChainFor(ix), live = (stOf(IDX[ix].key) || {}).live_price || lv.spot;
  const spot = prev ? (dir > 0 ? Math.max(live, bk.trigger) : Math.min(live, bk.trigger)) : live;           // a preview assumes the break has just closed at its trigger
  const side = dir > 0 ? "CE" : "PE", misses = [];
  const cands = ch.strikes.map((r) => ({ r, leg: dir > 0 ? r.ce : r.pe })).filter((x) => x.leg && x.leg.bid && x.leg.ask && x.leg.delta != null)
    .map((x) => ({ ...x, d: Math.abs(x.leg.delta), spr: (x.leg.ask - x.leg.bid) / ((x.leg.ask + x.leg.bid) / 2) * 100 }));
  const inDelta = cands.filter((x) => x.d >= 0.40 && x.d <= 0.60), tight = inDelta.filter((x) => x.spr <= 4);
  const pick = (tight.length ? tight : inDelta).sort((a, b) => Math.abs(a.d - 0.5) - Math.abs(b.d - 0.5) || a.spr - b.spr)[0];
  if (!pick) return { ix, grade: "none", misses: ["no contract with delta 0.40 to 0.60 is quoted both ways"] };
  const leg = pick.leg, entry = leg.ask, delta = pick.d, lotSize = leg.lot_size || ch.lot_size || S.rules.lotSizes[ix];
  const barSd5 = apBarSd(closed, spot);
  const ss = TMA.structuralStop({ dir, spot, level: bk.level, buf: bk.buf, delta, entry, barSd5, minPct: 7, maxPct: 15 });
  if (!ss.ok) return { ix, grade: "none", misses: ss.why };
  const exch = ix === "SENSEX" ? "BFO" : "NFO";
  const chargesRT = TMa.fnoCosts("buy", entry * lotSize, "OPT", exch).total + TMa.fnoCosts("sell", ss.sl * lotSize, "OPT", exch).total;
  const riskPerLot = (entry - ss.sl) * lotSize + chargesRT, room = roomLeft(dayTrades(t.date));
  const budget = Math.min(room, 0.5 * (+S.rules.dayStop || 3000));          // one stop-out may use at most half the day stop, so a second attempt stays possible
  const sz = TMA.sizeLots({ riskPerLot, room: budget, cap: S.rules.cap, outlayPerLot: entry * lotSize, max: 3 });
  sz.fits = sz.fits || riskPerLot <= room;                                  // one lot is still allowed when it fits the day stop, though not the half
  const run = (rules) => tmAnalyse({ ix, id: leg.id, chain: ch, lots: sz.lots, entry, rules, step: "auto", paths: 4000, models: ["implied", "bootstrap", "replay"], variants: ["desk", "allT2"], sensitivity: false, kind: "aplus" });
  const rules = { ...R, slPct: ss.slPct }, rep = await run(rules);
  let alt = null;
  if (Math.abs(ss.slPct - R.slPct) >= 0.5) { try { const r2 = await run(R); alt = { slPct: R.slPct, sl: TMA.r05(entry * (1 - R.slPct / 100)), odds: apOdds(r2), be: apBreakeven(r2) }; } catch {} }
  const odds = apOdds(rep), be = apBreakeven(rep);
  const t1 = TMA.r05(entry * (1 + R.tp1Pct / 100)), t2 = TMA.r05(entry * (1 + R.tp2Pct / 100));
  const idx = (px) => spot + dir * (px - entry) / delta;
  const g = TMA.grade({ bothBroke: true, roomOk: true, stopFits: ss.fits, deltaOk: true, spreadOk: pick.spr <= 4, early: marketOpen() && t.mins < toMin(S.rules.noEntryAfter), riskFits: sz.fits,
    withTrend: !((dir > 0 && bk.trend === "down") || (dir < 0 && bk.trend === "up")) });
  const dd = tmDay(ch.expiry);
  const plan = { ix, side, dir, preview: prev, grade: g.grade, misses: g.misses, name: `${ix} ${dd} ₹${pick.r.strike} ${side === "CE" ? "Call" : "Put"}`, strike: pick.r.strike, expiry: ch.expiry, id: leg.id, delta: leg.delta, spr: pick.spr,
    entry, lotSize, lots: sz.lots, outlay: entry * sz.lots * lotSize, risk: riskPerLot * sz.lots, room, spot, ss, sl: ss.sl, t1, t2, idxSL: spot - dir * (entry - ss.sl) / delta, idxT1: idx(t1), idxT2: idx(t2),
    odds, be, alt, bk, barSd5, step: rep.meta.step, paths: (rep.models.implied || {}).n, ms: rep.meta.ms, learn: rep.learn,
    ladder: makePlan({ entry, sl: ss.sl, tp: t2, delta: leg.delta, qty: sz.lots * lotSize, ixKey: ix, side }).steps, at: Date.now() };
  if (!prev && g.grade === "A+" && odds) {                 // an A+ call is kept and settled on the real tape: that is the record
    lrnStorePred(rep, ix, "aplus", { id: "a_" + ix + "_" + ev.key.replace(/[^A-Za-z0-9-]+/g, "_"), extra: { aplus: { dir, p1: odds.t1.raw, p2: odds.t2.raw, ps: odds.stop.raw, need: be ? be.need : null, key: ev.key } } });
  }
  return plan;
}
/** What the plan needs: the shift in the chance of reaching T1 that brings its average to zero, from the simulated models' outcome
 *  classes; "need" is that shift added to the blend's own chance, so it is the hit rate a record can be held against. */
const apBreakeven = (rep) => {
  const w = rep.learn.weights, be = TMA.breakeven(["implied", "bootstrap"].filter((m) => rep.models[m] && rep.models[m].variants).map((m) => ({ v: rep.models[m].variants.desk, w: w[m] })));
  if (be && rep.blends && rep.blends.desk) be.need = Math.max(0, Math.min(1, rep.blends.desk.p_hit_t1 + be.shift));
  return be;
};
/** The odds shown for a plan: the models' (no edge assumed) with their 95% band, scaled by the app's own settled A+ record once it has
 *  20 calls, or else corrected by the general calibration once that has 30. Never both: the A+ calls are in the calibration too. */
function apOdds(rep) {
  const bl = rep.blends && rep.blends.desk, b2 = rep.blends && rep.blends.allT2;
  if (!bl || !b2) return null;
  const rec = TMA.record(LRN.st.aplus), cal = TMl.calibrationOf(LRN.st, "t1");
  let how = "the models, with no edge assumed", f = (x) => x;
  if (rec.applied) { f = (x) => Math.max(0.01, Math.min(0.95, x * rec.lift)); how = `the models, scaled ×${rec.lift.toFixed(2)} by what the app's ${rec.n} settled A+ calls did`; }
  else if (cal.applied) { f = (x) => cal.fn(x); how = `the models, corrected against ${cal.n} settled predictions`; }
  const one = (p, ci) => ({ raw: p, p: f(p), lo: f(ci[0]), hi: f(ci[1]) });
  return { t1: one(bl.p_hit_t1, bl.ci_t1), t2: one(b2.p_hit_t1, b2.ci_t1), stop: { raw: bl.p_stop, p: bl.p_stop, lo: bl.ci_stop[0], hi: bl.ci_stop[1] }, exp: bl.expectancy, se: bl.se, how };
}
async function apCompute(ev) {
  AP.busy = true; AP.err = ""; AP.computedKey = ev.key; apRender();
  try {
    const plans = [];
    for (const ix of ["SENSEX", "NIFTY"]) {
      try { plans.push(await apPlanFor(ix, ev)); } catch (e) { plans.push({ ix, grade: "none", misses: [tmErr(e)] }); }
    }
    const rank = { "A+": 0, A: 1, none: 2 };
    plans.sort((a, b) => rank[a.grade] - rank[b.grade] || ((a.be ? a.be.shift : 9) - (b.be ? b.be.shift : 9)));
    AP.plan = { key: ev.key, dir: ev.dir, ev, plans, at: Date.now() };
    AP.computedAt = Date.now();
  } catch (e) { AP.err = tmErr(e); }
  AP.busy = false; lrnDirty();
  apRender(); try { checkAlerts(); } catch {}
}
async function apPreview(dir) {
  if (!S.levels || (AP.prev && AP.prev.busy)) return;
  const bkOf = (ix) => { const lv = S.levels[ix], P = lv && (dir > 0 ? lv.bull : lv.bear), a = P && P.aplus; return a ? { trigger: a.trigger, level: a.level, labels: a.labels, next: a.next, room: a.room, need: P.need, buf: lv.buf, trend: lv.trend } : null; };
  const s = bkOf("SENSEX"), n = bkOf("NIFTY");
  if (!s || !n) return;
  AP.prev = { dir, busy: true }; apRender();
  const ev = { dir, s, n, key: "preview|" + dir, preview: true }, plans = [];
  for (const ix of ["SENSEX", "NIFTY"]) { try { plans.push(await apPlanFor(ix, ev)); } catch (e) { plans.push({ ix, grade: "none", misses: [tmErr(e)] }); } }
  const rank = { "A+": 0, A: 1, none: 2 };
  plans.sort((a, b) => rank[a.grade] - rank[b.grade] || ((a.be ? a.be.shift : 9) - (b.be ? b.be.shift : 9)));
  AP.prev = { dir, plans, at: Date.now() }; lrnDirty(true); apRender();
}
function apAlerts(list) {
  const p = AP.plan && AP.plan.plans && AP.plan.plans[0];
  if (!p || p.grade !== "A+" || !AP.ev || !AP.ev.live || AP.plan.key !== AP.ev.key) return;
  list.push({ id: "ap", kind: "ap", key: "ap|" + AP.plan.key, cls: "stop", btn: "Seen",
    msg: `A+ ${apDirWord(p.dir)} ${p.name}: entry ${px(p.entry)}, stop ${px(p.sl)}, T1 ${px(p.t1)} (${Math.round(100 * p.odds.t1.p)}% chance), ${p.lots} lot${p.lots > 1 ? "s" : ""}. Details on the Now tab.` });
}

// ----- the card
function apRecordHTML() {
  const r = TMA.record(LRN.st.aplus);
  if (!r.n) return `No A+ call has been settled yet: the odds above are the models' and nothing more. Each call is kept and settled on the real tape once it is saved; the record builds from here.`;
  const rate = `${r.k1} of ${r.n} reached T1 (${Math.round(100 * r.rate1)}%, 95% band ${Math.round(100 * r.lo1)}–${Math.round(100 * r.hi1)}%) against ${Math.round(100 * r.expectedRate)}% the models gave them${r.need != null ? ` and ${Math.round(100 * r.need)}% they needed` : ""}`;
  const v = { thin: " Too few to judge.", above: " The signal has beaten what the plans needed.", below: " The signal has fallen short of what the plans needed.", unclear: " The band includes the break-even: it cannot be told apart from it yet." }[r.verdict] || "";
  return `Record of the app's own A+ calls: ${rate}.${v}`;
}
function apPlanHTML(p, primary, i) {
  const dirCls = p.dir > 0 ? "up" : "down", o = p.odds, ss = p.ss, be = p.be;
  if (p.grade === "none" && !p.entry) return `<div class="small"><b>${esc(p.ix)}</b>: no trade. ${esc(p.misses.join("; "))}</div>`;
  const band = (x) => `${Math.round(100 * x.lo)}–${Math.round(100 * x.hi)}%`;
  const gap = be ? Math.round(100 * (be.need - o.t1.p)) : null;
  const verdict = o ? `If the market has no edge, this plan averages <strong class="${cls(o.exp)}">${inr(o.exp)}</strong> a trade after charges and spread, because the models give the stop ${Math.round(100 * o.stop.p)}% and T1 ${Math.round(100 * o.t1.p)}%.
      ${be ? (be.shift > 0 ? ` To pay, the A+ signal has to lift the chance of reaching T1 from <strong>${pc0(o.t1.p)}</strong> to <strong>${pc0(be.need)}</strong> (${gap >= 0 ? "+" : ""}${gap} points).` : ` It already pays on the models, with a cushion of ${-gap} points of T1 odds.`) : ""}` : "";
  const alt = p.alt && p.alt.odds ? `<li>The desk's fixed ${p.alt.slPct}% stop (₹${p.alt.sl}), on the same simulated paths: T1 ${pc0(p.alt.odds.t1.p)}, stopped ${pc0(p.alt.odds.stop.p)}, average ${inr(p.alt.odds.exp)}${p.alt.be ? `, needs T1 at ${pc0(p.alt.be.need)}` : ""}. The adjusted stop is set by structure and noise, not by which scored better: with no edge in the models, searching for the best stop would only fit the simulation's noise.</li>` : "";
  return `<div class="ap-plan">
    <div class="row" style="justify-content:space-between"><div class="row"><span class="ap-grade ${p.preview ? "" : p.grade === "A+" ? "aplus" : "a"}">${p.preview ? "PREVIEW" : esc(p.grade)}</span><b class="${dirCls}">${apDirWord(p.dir)}</b><b>${esc(p.name)}</b></div>
      <span class="small muted">priced ${tmClock(p.at)} · ${p.step}-minute candles · ${p.paths ? p.paths.toLocaleString("en-IN") + " paths" : ""}</span></div>
    ${p.preview ? `<div class="small" style="margin-top:4px">Not fired. This is the trade as it would stand if the break closed now, at today's prices: nothing is recorded and no alert is raised.${p.grade !== "A+" ? ` <span class="down">It would not be A+: ${esc(p.misses.join("; "))}.</span>` : ""}</div>` : p.grade !== "A+" ? `<div class="small down" style="margin-top:4px">Not A+: ${esc(p.misses.join("; "))}.</div>` : ""}
    <div class="grid tm-tiles" style="margin-top:8px">
      <div class="stat"><span>Entry (the ask)</span><b>${px(p.entry)}</b><span>${p.lots} lot${p.lots > 1 ? "s" : ""} · ${p.lots * p.lotSize} qty · outlay ${inrA(p.outlay)}</span></div>
      <div class="stat"><span>Stop-loss</span><b class="down">${px(p.sl)}</b><span>−${ss.slPct}% · risk ${inrA(p.risk)} · index ≈ ${apFmt(p.idxSL)}</span></div>
      <div class="stat"><span>Target 1</span><b class="up">${px(p.t1)}</b><span>+${S.rules.tpMin}% · index ≈ ${apFmt(p.idxT1)} · book half</span></div>
      <div class="stat"><span>Target 2</span><b class="up">${px(p.t2)}</b><span>+${S.rules.tpMax}% · index ≈ ${apFmt(p.idxT2)}</span></div>
    </div>
    ${o ? `<div class="grid tm-tiles" style="margin-top:8px">
      <div class="stat"><span>Chance of reaching T1</span><b>${pc0(o.t1.p)}</b><span class="by">95% band ${band(o.t1)}${o.t1.p !== o.t1.raw ? `<br>the models said ${pc0(o.t1.raw)}` : ""}</span></div>
      <div class="stat"><span>Chance of reaching T2</span><b>${pc0(o.t2.p)}</b><span class="by">95% band ${band(o.t2)}</span></div>
      <div class="stat"><span>Stopped out first</span><b>${pc0(o.stop.p)}</b><span class="by">95% band ${band(o.stop)}</span></div>
      <div class="stat"><span>Average, after charges</span><b class="${cls(o.exp)}">${inr(o.exp)}</b><span class="by">± ${inrA(1.96 * o.se)} · what taking it costs if there is no edge</span></div></div>` : ""}
    <div class="tm-verdict mix"><b>What this trade needs</b>${verdict}</div>
    <div class="small muted" style="margin-top:6px">Odds: ${esc(o ? o.how : "")}. The band counts the paths' sampling error and the models' disagreement. ${apRecordHTML()}</div>
    <ul class="tm-notes">
      <li>Stop: ${esc(ss.why[0])}. ${esc(ss.why[1])}${ss.bound === "floor" ? ". " + esc(ss.why[2] || "") : ""}</li>${alt}
      <li>Room: ${p.bk.next ? `${Math.round(p.bk.room)} points to the next level (${apFmt(p.bk.next.level)}), about ${inrA(p.bk.room * Math.abs(p.delta))} on the option, against the ${S.rules.tpMin}% (${inrA(p.entry * S.rules.tpMin / 100)}) T1 needs` : "open beyond the chain"}. ${p.bk.trend === "up" || p.bk.trend === "down" ? `The index trend is ${esc(p.bk.trend)}.` : ""}</li>
      ${p.lots * p.lotSize && p.room != null ? `<li>Sized so one stop-out uses at most half of your ${inrA(S.rules.dayStop)} day stop; today's room left is ${inrA(p.room)}${p.risk > p.room ? ` <b class="down">(this risks more than that: skip)</b>` : ""}.</li>` : ""}
    </ul>
    <details data-apd="ladder" ${AP.open.ladder ? "open" : ""}><summary class="small muted">Trailing-stop ladder</summary>
      <table class="ladder small" style="margin-top:4px"><tr><th>When price ≥</th><th>Move stop to</th><th>Locks</th></tr>${p.ladder.map((s) => `<tr><td>${px(s.at)}</td><td>${px(s.stop)}</td><td class="${cls(s.lock)}">${inr(s.lock)}</td></tr>`).join("")}</table></details>
    ${p.preview ? "" : primary ? `<div class="row" style="margin-top:8px"><button class="primary" data-ap="use:${i}">Use on the Trade tab</button><button data-ap="full:${i}">Replay in full</button><button data-ap="refresh">Refresh</button></div>` : `<div class="row" style="margin-top:6px"><button data-ap="use:${i}">Use on the Trade tab</button><button data-ap="full:${i}">Replay in full</button></div>`}
  </div>`;
}
function apWaitHTML() {
  const rows = [1, -1].map((dir) => {
    const parts = ["SENSEX", "NIFTY"].map((ix) => {
      const lv = S.levels && S.levels[ix]; if (!lv) return `${ix}: waiting for data`;
      const a = (dir > 0 ? lv.bull : lv.bear).aplus;
      return a ? `${ix} 5-minute close ${dir > 0 ? "above" : "below"} <b>${apFmt(a.trigger)}</b> (${apFmt(Math.abs(a.trigger - lv.spot))} points away)` : `${ix}: no A+ level ${dir > 0 ? "above" : "below"}`;
    });
    return `<div class="small"><b class="${dir > 0 ? "up" : "down"}">${apDirWord(dir)}</b> needs ${parts.join(" and ")}.</div>`;
  }).join("");
  const can = (dir) => ["SENSEX", "NIFTY"].every((ix) => S.levels && S.levels[ix] && (dir > 0 ? S.levels[ix].bull : S.levels[ix].bear).aplus);
  const btns = [1, -1].filter(can).map((dir) => `<button class="mini" data-ap="prev:${dir}" ${AP.prev && AP.prev.busy ? "disabled" : ""}>Preview the ${apDirWord(dir)} trade</button>`).join(" ");
  const pv = AP.prev && AP.prev.busy ? `<div class="small muted" style="margin-top:6px">Working out the ${apDirWord(AP.prev.dir)} preview…</div>`
    : AP.prev && AP.prev.plans ? `<details data-apd="prev" ${AP.open.prev !== false ? "open" : ""} style="margin-top:8px"><summary class="small muted">Preview of the ${apDirWord(AP.prev.dir)} trade (priced ${tmClock(AP.prev.at)})</summary>${AP.prev.plans[0] && AP.prev.plans[0].entry ? apPlanHTML(AP.prev.plans[0], false, 0) : `<div class="small">${esc((AP.prev.plans[0] || {}).misses ? AP.prev.plans[0].misses.join("; ") : "no trade")}</div>`}</details>` : "";
  const pt = AP.partial ? `<div class="small" style="margin-top:4px">${esc(AP.partial.ix)} closed ${AP.partial.dir > 0 ? "above" : "below"} its A+ level at ${esc(AP.partial.b.t)}; ${esc(AP.partial.missing)} has not: not A+ yet.</div>` : "";
  return rows + pt + (btns ? `<div class="row" style="margin-top:8px">${btns}<span class="small muted">the target and the odds, before it fires</span></div>` : "") + pv;
}
function apHTML() {
  const t = ist(), open = marketOpen(), early = open && t.mins < toMin(S.rules.noEntryAfter), P = AP.plan;
  let head = "", body = "";
  if (!open) {
    head = `<span class="pill">market closed</span>`;
    body = `<div class="small muted">The A+ finder runs while the market is open (9:15 to ${esc(S.rules.noEntryAfter)}); it will pick up the first A+ break by itself.</div>`
      + (P && P.plans && P.plans[0] && P.plans[0].entry ? `<details data-apd="last" ${AP.open.last ? "open" : ""} style="margin-top:8px"><summary class="small muted">Last A+ call: ${apDirWord(P.dir)} ${esc(P.plans[0].name)}, entry ${px(P.plans[0].entry)}</summary>${apPlanHTML(P.plans[0], false, 0)}</details>` : "")
      + `<div class="small muted" style="margin-top:6px">${apRecordHTML()}</div>`;
  } else if (!S.levels || (!S.levels.SENSEX && !S.levels.NIFTY)) body = `<div class="small muted">${open ? "Waiting for candles and option chains…" : "The A+ finder runs while the market is open (9:15 to " + esc(S.rules.noEntryAfter) + ")."}</div>`;
  else if (AP.ev && AP.ev.live) {
    const ev = AP.ev;
    head = `<span class="pill ${ev.dir > 0 ? "live" : "closed"}">A+ ${apDirWord(ev.dir)} fired · ${ev.ago === 0 ? "this candle" : ev.ago * 5 + " min ago"}</span>`;
    const when = `SENSEX closed ${ev.dir > 0 ? "above" : "below"} ${apFmt(ev.s.trigger)} at ${esc(ev.s.t)}, NIFTY ${ev.dir > 0 ? "above" : "below"} ${apFmt(ev.n.trigger)} at ${esc(ev.n.t)}`;
    if (!early) body = `<div class="small">${when}, but it is past ${esc(S.rules.noEntryAfter)}: no new entries.</div>`;
    else if (AP.busy && (!P || P.key !== ev.key)) body = `<div class="small">${when}. Working out the trade: picking the contract, setting the stop, running the Time Machine (about ten seconds)…</div><div class="tm-prog"><div style="width:60%"></div></div>`;
    else if (AP.err) body = `<div class="small down">${esc(AP.err)}</div>`;
    else if (P && P.key === ev.key) {
      body = `<div class="small" style="margin-bottom:6px">${when}.${AP.busy ? " Refreshing…" : ""}</div>${apPlanHTML(P.plans[0], true, 0)}`;
      if (P.plans[1] && P.plans[1].entry) body += `<details data-apd="other" ${AP.open.other ? "open" : ""} style="margin-top:8px"><summary class="small muted">${esc(P.plans[1].ix)} instead: ${esc(P.plans[1].grade === "none" ? "not a trade" : P.plans[1].grade)}</summary>${apPlanHTML(P.plans[1], false, 1)}</details>`;
      else if (P.plans[1]) body += `<div class="small muted" style="margin-top:6px">${esc(P.plans[1].ix)}: no trade (${esc(P.plans[1].misses.join("; "))}).</div>`;
    }
  } else {
    head = `<span class="pill">no A+ now</span>`;
    body = apWaitHTML();
    if (P && P.plans && P.plans[0] && P.plans[0].entry && AP.ev) body += `<details data-apd="last" ${AP.open.last ? "open" : ""} style="margin-top:8px"><summary class="small muted">Last A+ call: ${apDirWord(P.dir)} ${esc(P.plans[0].name)}, entry ${px(P.plans[0].entry)} (expired)</summary>${apPlanHTML(P.plans[0], false, 0)}</details>`;
    body += `<div class="small muted" style="margin-top:6px">${apRecordHTML()}</div>`;
  }
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">A+ trade</h2>${head}</div>
    <div class="small muted" style="margin:2px 0 6px">Found on its own, free of Claude usage: it waits for the desk's A+ rule (both indices close beyond their A+ level), then picks the contract, sets the stop and shows the target with the chance of reaching it. The chance assumes the market has no edge; the record below is how the signal has actually done.</div>${body}`;
}
function apRender() {
  const el = $("apCard"); if (!el) return;
  const h = apHTML();
  if (h !== AP.html) { AP.html = h; el.innerHTML = h; }
}

// ----- using it
function apUse(i) {
  const p = AP.plan && AP.plan.plans[i]; if (!p || !p.entry) return;
  setIx(p.ix);
  S.pick = { key: p.id, name: p.name, side: p.side, ix: p.ix, lot: p.lotSize, delta: p.delta };
  $("f_inst").value = p.name; $("f_entry").value = p.entry; $("f_sl").value = p.sl; $("f_tp").value = p.t2; $("f_lots").value = p.lots;
  const note = $("apSLnote"); if (note) note.innerHTML = `Stop from the A+ finder: <b>${p.ss.slPct}%</b> (₹${p.sl}). ${esc(p.ss.why[0])}. Edit the stop to override.`;
  setView("trade"); renderPlan(); syncWatches();
}
function apFull(i) {
  const p = AP.plan && AP.plan.plans[i]; if (!p || !p.entry) return;
  TM.mode = "intra"; TM.f.pos = null; TM.f.ix = p.ix; TM.f.side = p.side; TM.f.lots = p.lots; TM.f.plan = { entry: p.entry, sl: p.sl, tp1: p.t1, tp2: p.t2 };
  TM.f.expiry = p.expiry; TM.f.strike = ""; TM.f.useId = p.id; TM.ch = null; tmSave();
  setView("time");
}

// ----- the Trade tab's stop: set from the market when a contract is picked (the desk's 10% otherwise)
function apFillSL(entry) {
  const note = $("apSLnote"); if (note) note.innerHTML = "";
  const pk = S.pick; if (!pk || !apSLOn() || !S.levels || !S.levels[pk.ix] || !(entry > 0)) return;
  const lv = S.levels[pk.ix], dir = pk.side === "CE" ? 1 : -1, opp = dir > 0 ? lv.bear.first : lv.bull.first;
  if (!opp) return;
  const closed = closedCandles(pk.ix);
  const ss = TMA.structuralStop({ dir, spot: lv.spot, level: opp.level, buf: lv.buf, delta: pk.delta == null ? 0.5 : pk.delta, entry, barSd5: apBarSd(closed, lv.spot) });
  if (!ss.ok) return;
  const def = TMA.r05(entry * (1 - S.rules.slPct / 100));
  if (!ss.fits) { if (note) note.innerHTML = `Structure puts the stop ${ss.raw.toFixed(1)}% away, beyond the 15% limit, so the desk's ${S.rules.slPct}% (₹${def}) is kept: this entry does not fit the market's structure.`; return; }
  $("f_sl").value = ss.sl;
  if (note) note.innerHTML = `Stop set from the market: <b>${ss.slPct}%</b> (₹${ss.sl}) instead of the desk's ${S.rules.slPct}% (₹${def}). ${esc(ss.why[0])}. Edit the stop to override.`;
}
function apBind() {
  const c = $("apCard");
  if (c) {
    c.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-ap]"); if (!b) return;
      const [k, i] = b.dataset.ap.split(":");
      if (k === "use") apUse(+i); else if (k === "full") apFull(+i); else if (k === "prev") apPreview(+i);
      else if (k === "refresh" && AP.ev && !AP.busy) apCompute(AP.ev);
    });
    c.addEventListener("toggle", (ev) => { const d = ev.target; if (d && d.dataset && d.dataset.apd) AP.open[d.dataset.apd] = d.open; }, true);
  }
  const on = $("apSLon");
  if (on) { on.checked = apSLOn(); on.addEventListener("change", () => { try { localStorage.setItem("desk.autoSL", on.checked ? "on" : "off"); } catch {} const e = num($("f_entry").value); if (S.pick && e) { $("f_sl").value = ""; autoFill(); } else apFillSL(0); }); }
}

window.__tmx = { AP, LRN, TM, S };          // a read-only handle for the tests and for looking things up in the console
