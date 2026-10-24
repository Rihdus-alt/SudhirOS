// ---------- Time Machine: the desk's links ----------
// Everything that makes the Time Machine part of how the desk is used, not a tab on its own: one analysis function that every
// check goes through (Time tab, the Trade tab's plan, the scan, the coach's tool), the store for what it learns, the check on
// every plan before a trade is logged, and the Learning card. Runs in the desk's script, after tmx-ui.js.
const TMl = window.TMXL;
const LRN = { st: TMl.emptyState(), ready: false, rev: 0, dirty: false, timer: null, sess: { NIFTY: [], SENSEX: [] }, sessIds: new Set(), sessReady: false,
  feed: {}, daily: {}, obs: new Map(), obsSeen: new Set(), preds: new Map(), tradeSeen: new Set(), resetArmed: 0, bg: false };
const TMG = { last: null, timer: null, runId: 0, busy: false, confirmKey: null };
const lrnNow = () => new Date().toISOString();
const SESS_KEEP = 120;

// ----- the store: the learner's state, the saved sessions, the observations and predictions waiting for a tape
function lrnPut(path, data) { if (db && canWrite) queue(() => db.doc(path).set(data)); }
function lrnDel(path) { if (db && canWrite) queue(() => db.doc(path).delete()); }
function lrnInit(d) {
  d.doc("tmlearn/state").onSnapshot((s) => {
    const v = s.exists ? s.data() : null;
    if (v && !(v.rev && v.rev === LRN.rev)) {
      try {
        const st = TMl.normalise(JSON.parse(v.json));
        if (!LRN.ready || (st.updated || "") > (LRN.st.updated || "")) { LRN.st = st; LRN.rev = v.rev || 0; }
      } catch {}
    }
    LRN.ready = true; lrnResolve(); lrnRender();
  }, () => { LRN.ready = true; });
  d.collection("tmsess").onSnapshot((s) => {
    const by = { NIFTY: [], SENSEX: [] }, ids = new Set();
    s.docs.forEach((x) => { const v = x.data(); if (v && by[v.ix] && Array.isArray(v.o)) { try { by[v.ix].push(TMi.unpackSession(v)); ids.add(x.id); } catch {} } });
    for (const k of Object.keys(by)) { by[k] = TMi.mergeSessions(by[k]); for (const sx of by[k].splice(0, Math.max(0, by[k].length - SESS_KEEP))) lrnDel("tmsess/" + k + "_" + sx.date); }
    LRN.sess = by; LRN.sessIds = ids; LRN.sessReady = true;
    for (const ix of Object.keys(LRN.feed)) lrnSaveNew(ix);
    lrnResolve(); lrnRender();
  }, () => { LRN.sessReady = true; });
  d.collection("tmobs").onSnapshot((s) => {
    LRN.obs = new Map(); s.docs.forEach((x) => { const v = x.data(); if (v && !v.done) { LRN.obs.set(x.id, v); LRN.obsSeen.add(x.id); } });
    lrnResolve();
  }, () => {});
  d.collection("tmpred").onSnapshot((s) => {
    LRN.preds = new Map(); s.docs.forEach((x) => { const v = x.data(); if (v) LRN.preds.set(x.id, v); });
    lrnResolve(); lrnRender();
  }, () => {});
  window.addEventListener("pagehide", lrnFlush);
}
function lrnDirty(soft) {
  LRN.dirty = true;
  if (soft && Date.now() - (LRN.lastFlush || 0) < 600e3) return;          // speed figures alone are saved at most every ten minutes (and when the page closes)
  clearTimeout(LRN.timer); LRN.timer = setTimeout(lrnFlush, 2500); lrnRender();
}
function lrnFlush() {
  if (!LRN.dirty) return;
  if (!db || !canWrite) { LRN.dirty = false; return; }
  if (!LRN.ready) { clearTimeout(LRN.timer); LRN.timer = setTimeout(lrnFlush, 2000); return; }
  LRN.dirty = false; LRN.lastFlush = Date.now();
  const rev = LRN.rev = Date.now(), json = JSON.stringify(LRN.st);
  queue(() => db.doc("tmlearn/state").set({ v: TMl.VERSION, rev, updated: LRN.st.updated || null, json }));
}

// ----- sessions: the 5-minute feed holds about three sessions; every full one is saved, so the library grows by a day a day
const lrnSessions = (ix) => TMi.mergeSessions(LRN.sess[ix] || [], LRN.feed[ix] || []);
function lrnSaveNew(ix) {
  if (!LRN.sessReady || !LRN.feed[ix]) return;
  for (const s of LRN.feed[ix]) {
    const id = ix + "_" + s.date;
    if (!s.full || LRN.sessIds.has(id)) continue;
    LRN.sessIds.add(id); LRN.sess[ix] = TMi.mergeSessions(LRN.sess[ix], [s]);
    lrnPut("tmsess/" + id, Object.assign({ ix, savedAt: lrnNow() }, TMi.packSession(s)));
  }
}
function lrnAbsorb(ix, feed, daily) {
  if (!TMSYM[ix]) return;
  if (feed) { LRN.feed[ix] = feed; lrnSaveNew(ix); }
  if (daily) LRN.daily[ix] = daily;
  lrnResolve(); lrnRender();
}
/** Settle what the saved tapes can settle: pace observations (the tape's variance against the quote's), then stored
 *  predictions (the plan played on the real tape). Both are idempotent: the learner dedupes by id. */
function lrnResolve() {
  if (LRN.resolving) return;
  LRN.resolving = true;
  try {
    const at = lrnNow(), today = ist().date;
    let changed = false;
    for (const [id, o] of [...LRN.obs]) {
      let done = false;
      if (o.kind === "sess") {
        const s = lrnSessions(o.ix).find((x) => x.date === o.day && x.full);
        if (s) { const real = TMl.realisedVar(s, o.m5); if (real != null) { LRN.st = TMl.observePace(LRN.st, o.ix, "sess", real, o.pred, id, at); changed = true; } done = true; }
      } else {
        const dl = LRN.daily[o.ix] || [], i = dl.findIndex((c) => c.date === o.day);
        if (i > 0 && dl[i].open > 0 && dl[i - 1].close > 0) { const x = Math.log(dl[i].open / dl[i - 1].close); LRN.st = TMl.observePace(LRN.st, o.ix, "gap", x * x, o.pred, id, at); changed = true; done = true; }
      }
      if (!done && o.day < TMi.istParts(Date.now() - 8 * 864e5).date) done = true;          // never got its tape: let it go
      if (done) { LRN.obs.delete(id); lrnDel("tmobs/" + id); }
    }
    for (const [id, v] of [...LRN.preds]) {
      if (v.done) { if (v.day < TMi.istParts(Date.now() - 30 * 864e5).date) { LRN.preds.delete(id); lrnDel("tmpred/" + id); } continue; }
      let pred; try { pred = JSON.parse(v.json); } catch { pred = null; }
      const sessions = lrnSessions(v.ix), s = pred && sessions.find((x) => x.date === pred.day && x.full);
      let done = !pred;
      if (s) {
        const out = TMl.resolvePaper(pred, s, TMl.profileOf(sessions.filter((x) => x.date < pred.day)));
        if (out) { LRN.st = TMl.observeOutcome(LRN.st, pred, out, "paper", at); changed = true; }
        done = true;
      } else if (v.day < TMi.istParts(Date.now() - 8 * 864e5).date) done = true;
      if (done) { v.done = true; lrnPut("tmpred/" + id, v); }
    }
    if (changed) lrnDirty();
  } finally { LRN.resolving = false; }
}
/** A closed trade that carries its Time Machine check teaches the calibration what your own fills and exits did. */
function lrnTradesTick() {
  if (!S.loaded) return;
  let changed = false;
  for (const t of S.trades) {
    if (!t.tm || !t.tm.pred || LRN.tradeSeen.has(t.id) || openQty(t) > 0 || !(t.exits || []).length) continue;
    LRN.tradeSeen.add(t.id);
    const pred = Object.assign({}, t.tm.pred, { id: t.tm.pred.id || t.tmId || t.id });
    const out = TMl.resolveTrade(pred, { entry: +t.entry, exits: t.exits, realisedNet: realised(t) });
    if (out) { LRN.st = TMl.observeOutcome(LRN.st, pred, out, "trade", lrnNow()); changed = true; }
  }
  if (changed) lrnDirty();
}
function lrnObserve(rep, ix) {
  const m = rep.meta, p = m.pace;
  if (!p || m.iv_overridden) return;
  for (const [kind, val] of [["sess", p.sess_var], ["gap", p.gap_var]]) {
    if (!(val > 0)) continue;
    const id = kind + "_" + ix + "_" + p.day;
    if (LRN.obsSeen.has(id) || LRN.st.seen.includes(id)) continue;                      // one per index, day and kind: the first quote of the day
    LRN.obsSeen.add(id);
    const o = { ix, kind, day: p.day, m5: kind === "sess" ? p.m5 : 0, pred: val, at: lrnNow(), done: false };
    LRN.obs.set(id, o); lrnPut("tmobs/" + id, o);
  }
}
/** Keep a prediction until a saved tape can settle it (at most 40 a day). Returns the stored prediction. */
function lrnStorePred(rep, ix, kind) {
  const m = rep.meta;
  if (m.mode !== "new" || !rep.models.implied || !rep.models.implied.variants) return null;
  const id = "p_" + ix + "_" + m.start.day + "_" + m.side + m.strike + "_" + m.spec.m0 + "_" + m.lots;
  if (LRN.preds.has(id)) { try { return JSON.parse(LRN.preds.get(id).json); } catch { return null; } }
  const pred = TMl.predictionFrom(rep, { id, kind, ix });
  const today = [...LRN.preds.values()].filter((v) => v.at && v.at.slice(0, 10) === lrnNow().slice(0, 10)).length;
  if (today >= 40) return pred;
  const v = { ix, day: pred.day, kind, done: false, at: lrnNow(), json: JSON.stringify(pred) };
  LRN.preds.set(id, v); lrnPut("tmpred/" + id, v);
  return pred;
}

// ----- the one analysis every check goes through
const tmRound2 = (x) => Math.round(x * 100) / 100;
/** The desk's rules, with a plan's own entry, stop and targets turned into percentages of the entry. */
function tmRulesFor(p) {
  const r = tmRules();
  if (p && p.entry > 0) {
    if (p.sl > 0 && p.sl < p.entry) r.slPct = tmRound2((p.entry - p.sl) / p.entry * 100);
    if (p.tp2 > p.entry) r.tp2Pct = tmRound2((p.tp2 - p.entry) / p.entry * 100);
    if (p.tp1 > p.entry) r.tp1Pct = tmRound2((p.tp1 - p.entry) / p.entry * 100);
    if (r.tp1Pct > r.tp2Pct) r.tp1Pct = r.tp2Pct;
  }
  return r;
}
async function tmChainFor(ix, expiry) {
  const key = TMSYM[ix].key;
  let ch = await tmChain(key, expiry || null, true);
  if (!expiry) { const list = ch.expiry_list || [ch.expiry], want = defaultExpiry(list); if (want && want !== ch.expiry) ch = await tmChain(key, want, true); }
  return ch;
}
/** o: {ix, side, strike | id, lots, expiry | chain, entry, rules, step, paths, budget, minPaths, maxPaths, entryAt, volMode, ivPct, pos, sensitivity,
 *  variants, models, onProgress, isCancelled, kind, record}. Applies everything learned and returns the engine's report with
 *  .cal (calibrated odds), .learn (what was applied) and .ctx. */
async function tmAnalyse(o) {
  const ix = o.ix, key = TMSYM[ix].key;
  const ch = o.chain || await tmChainFor(ix, o.expiry);
  const [feed, daily] = await Promise.all([tm5m(key), tmDaily(key)]);
  lrnAbsorb(ix, feed, daily);
  const live = marketOpen();
  if (live) { const ct = TMa.chainTimeMs(ch); if (ct && Date.now() - ct > 20 * 60e3) throw new Error("The quote is " + Math.round((Date.now() - ct) / 60e3) + " minutes old: refresh and try again."); }
  const sessions = lrnSessions(ix), lr = TMl.learned(LRN.st, ix), profile = TMl.profileOf(sessions);
  const auto = o.paths === "auto" || !o.paths;
  const base = { sessions, chain: ch, daily, nowMs: Date.now(), holidays: tmHol(), rules: o.rules || tmRules(), seed: 7, profile, volMult: lr.volMult, gapMult: lr.gapMult, weights: lr.weights,
    step: o.step || "auto", paths: auto ? "auto" : +o.paths, minPaths: o.minPaths, maxPaths: o.maxPaths, timeBudgetMs: o.budget, volMode: o.volMode, ivPct: o.ivPct, entryAt: o.entryAt,
    entryPrice: o.entry || undefined, models: o.models, variants: o.variants, sensitivity: o.sensitivity !== false, onProgress: o.onProgress, isCancelled: o.isCancelled };
  const rep = o.pos
    ? await TMi.analyseIntraday({ ...base, mode: "position", derivativeId: o.pos.id, lots: o.pos.remainingLots, position: { entry: o.pos.entry, stop: o.pos.stop, t1Done: o.pos.t1Done, remainingLots: o.pos.remainingLots } })
    : await TMi.analyseIntraday({ ...base, ...(o.id ? { derivativeId: o.id } : { side: o.side, strike: +o.strike }), lots: Math.max(1, Math.round(o.lots || 1)) });
  rep.ctx = { ix, at: Date.now(), day: ist().date };
  rep.learn = lr;
  rep.cal = rep.blend ? TMl.calibrate(LRN.st, { profit: rep.blend.p_profit, t1: rep.blend.p_hit_t1, stop: rep.blend.p_stop }) : null;
  for (const [m, p] of Object.entries(rep.meta.perf || {})) LRN.st = TMl.observePerf(LRN.st, m, p.step, p.bars_per_path, p.paths, p.ms);
  const before = LRN.st.updated;
  lrnObserve(rep, ix);
  if (o.record) lrnStorePred(rep, ix, o.kind || "run");
  lrnDirty(LRN.st.updated === before);
  return rep;
}
/** The odds a report gives, with the calibrated figure beside the models' where the calibration is in use. */
function tmOdds(rep) {
  const b = rep.blend; if (!b) return null;
  const cal = rep.cal || {}, used = (e) => TMl.calibrationOf(LRN.st, e).applied;
  const row = (e, raw) => ({ raw, adj: used(e) && cal[e] != null ? cal[e] : null, now: used(e) && cal[e] != null ? cal[e] : raw });
  return { profit: row("profit", b.p_profit), t1: row("t1", b.p_hit_t1), stop: row("stop", b.p_stop), exp: b.expectancy, se: b.se };
}
const tmOddsTxt = (r) => r.adj != null && Math.abs(r.adj - r.raw) >= 0.005 ? `<b>${pc0(r.adj)}</b> <i class="muted">(models ${pc0(r.raw)})</i>` : `<b>${pc0(r.raw)}</b>`;
const tmGateName = { pass: "PASS", watch: "WATCH", fail: "FAIL", error: "NOT CHECKED", none: "NOT CHECKED" };

// ----- the Trade tab: the plan is checked as it is written, and again before it is logged
function tmGateInputs() {
  const pk = S.pick; if (!pk || !pk.key) return null;
  const e = num($("f_entry").value); if (!(e > 0)) return null;
  const lots = Math.max(1, Math.round(num($("f_lots").value) || 1));
  if (!["NIFTY", "SENSEX"].includes(pk.ix)) return { skip: "The Time Machine replays NIFTY and SENSEX options; this one is not checked." };
  if (($("f_date").value || ist().date) !== ist().date) return { skip: "Logged for another day: the check runs on a plan before the trade, so this one is not checked." };
  if ($("f_inst").value.trim() !== pk.name) return { skip: "This instrument was typed by hand: pick it from the strike list (or the search) so the Time Machine can find the contract." };
  const lot = pk.lot || S.rules.lotSizes[pk.ix];
  const p = makePlan({ entry: e, sl: num($("f_sl").value), tp: num($("f_tp").value), delta: pk.delta, qty: lots * lot, ixKey: pk.ix, side: pk.side });
  const expiry = S.chain && S.chain.ix === pk.ix ? S.chain.expiry : null;
  return { ix: pk.ix, id: pk.key, side: pk.side, expiry, entry: e, lots, sl: p.sl, tp1: p.tp1, tp2: p.tp2, key: [pk.ix, pk.key, e, p.sl, p.tp2, lots].join("|") };
}
function tmGateSchedule() {
  clearTimeout(TMG.timer);
  const el = $("tmGate"); if (!el || S.view !== "trade") return;
  const inp = tmGateInputs();
  if (!inp) { TMG.last = null; TMG.confirmKey = null; el.innerHTML = ""; tmSaveBtn(); return; }
  if (inp.skip) { TMG.last = { skip: inp.skip }; el.innerHTML = `<div class="tmg tmg-none small">${esc(inp.skip)}</div>`; tmSaveBtn(); return; }
  const k = inp.key + "|" + Math.floor(Date.now() / 300000);
  if (TMG.last && TMG.last.k === k) return;                         // the same plan within the same five minutes: the answer stands
  if (!mcp) { el.innerHTML = `<div class="tmg tmg-none small">The Time Machine needs the live connection.</div>`; return; }
  el.innerHTML = `<div class="tmg tmg-none small">Checking this plan with the Time Machine…</div>`;
  TMG.timer = setTimeout(() => tmGateRun(inp, k), 900);
}
async function tmGateRun(inp, k) {
  const id = ++TMG.runId;
  TMG.busy = true;
  let res;
  try {
    const rep = await tmAnalyse({ ix: inp.ix, id: inp.id, expiry: inp.expiry, lots: inp.lots, entry: inp.entry, rules: tmRulesFor(inp), step: "auto", paths: "auto", budget: 2800, minPaths: 1200, maxPaths: 6000,
      sensitivity: false, variants: ["desk"], kind: "trade", isCancelled: () => id !== TMG.runId });
    res = { gate: rep.gate ? rep.gate.gate : "error", reason: rep.gate ? rep.gate.reason : "no result", rep, odds: tmOdds(rep) };
  } catch (e) {
    if (e && e.message === "cancelled") return;
    res = { gate: "error", reason: tmErr(e) };
  }
  if (id !== TMG.runId) return;
  Object.assign(res, { k, inp, at: Date.now() });
  TMG.busy = false; TMG.last = res;
  const el = $("tmGate"); if (el) el.innerHTML = tmGateHTML(res);
  tmSaveBtn();
}
function tmGateHTML(r) {
  const g = r.gate, name = tmGateName[g] || g.toUpperCase();
  if (g === "error") return `<div class="tmg tmg-none"><div class="row"><span class="tmg-chip">NOT CHECKED</span><b>Time Machine</b></div><div class="small">${esc(r.reason)}</div><div class="row" style="margin-top:6px"><button class="mini" data-tmg="again">Check again</button></div></div>`;
  const o = r.odds, m = r.rep.meta, n = (r.rep.models.implied || {}).n, lv = r.rep.levels;
  const models = ["implied", "bootstrap", "replay"].filter((x) => r.rep.models[x] && r.rep.models[x].variants);
  return `<div class="tmg tmg-${g}">
      <div class="row" style="justify-content:space-between"><div class="row"><span class="tmg-chip">${name}</span><b>Time Machine</b></div><span class="small muted">${n ? n.toLocaleString("en-IN") + " paths · " : ""}${m.step}-minute candles · ${(m.ms / 1000).toFixed(1)}s</span></div>
      <div class="tmg-reason">${esc(r.reason)}</div>
      ${o ? `<div class="small" style="margin-top:4px">Chance of a profit ${tmOddsTxt(o.profit)} · reaches T1 ${tmOddsTxt(o.t1)} · stopped out ${tmOddsTxt(o.stop)} · average <b class="${cls(o.exp)}">${inr(o.exp)}</b> ± ${inrA(1.96 * o.se)} after charges</div>` : ""}
      <div class="small muted" style="margin-top:4px">Your levels: entry ${px(r.inp.entry)}, stop ${px(lv.sl)} (−${r.rep.rules.slPct}%), T1 ${px(lv.t1)}, T2 ${px(lv.t2)} · ${models.map((x) => ({ implied: "Implied", bootstrap: "Bootstrap", replay: "Recent sessions" }[x])).join(", ")}</div>
      <div class="row" style="margin-top:6px"><button class="mini" data-tmg="full">Replay in full</button><button class="mini" data-tmg="again">Check again</button></div>
      ${g === "fail" ? `<div class="small" style="margin-top:6px">Logging it anyway is allowed, and recorded as such.</div>` : ""}
    </div>`;
}
function tmSaveBtn() {
  const b = $("save"); if (!b) return;
  b.textContent = TMG.last && TMG.last.gate === "fail" ? "Log anyway (Time Machine: fail)" : "Log trade";
}
/** Compact copy of the check for the trade document: what it said, what was learned at the time, and the prediction to settle later. */
function tmSnap(r, inp) {
  const o = r.odds || {}, pred = r.rep ? TMl.predictionFrom(r.rep, { id: "t_" + inp.ix + "_" + Date.now().toString(36), kind: "trade", ix: inp.ix }) : null;       // settled from the closed trade, not from the tape
  return { gate: r.gate, reason: r.reason, at: lrnNow(), entry: inp.entry, lots: inp.lots, sl: inp.sl, tp1: inp.tp1, tp2: inp.tp2, step: r.rep.meta.step, paths: (r.rep.models.implied || {}).n || null,
    exp: o.exp != null ? Math.round(o.exp) : null, se: o.se != null ? Math.round(o.se) : null,
    p: o.profit ? { profit: o.profit.raw, t1: o.t1.raw, stop: o.stop.raw } : null, pc: o.profit ? { profit: o.profit.now, t1: o.t1.now, stop: o.stop.now } : null,
    learn: { volMult: r.rep.learn.volMult, gapMult: r.rep.learn.gapMult, resolved: r.rep.learn.resolved }, pred };
}
/** Called by saveTrade just before the write. Returns null (nothing to check), "hold" (a failing plan needs a second tap) or {tm, id}. */
async function tmBeforeSave() {
  const inp = tmGateInputs();
  if (!inp) return null;
  if (inp.skip) return { tm: { gate: "none", reason: inp.skip, at: lrnNow() }, id: null };
  let r = TMG.last;
  const fresh = r && r.inp && r.inp.key === inp.key && Date.now() - r.at < 180e3 && r.gate !== "error";
  if (!fresh) {
    $("saveMsg").textContent = "Checking the plan with the Time Machine…";
    clearTimeout(TMG.timer);
    await tmGateRun(inp, inp.key + "|" + Math.floor(Date.now() / 300000));
    r = TMG.last;
  }
  if (!r || r.gate === "error") return { tm: { gate: "error", reason: (r && r.reason) || "could not run", at: lrnNow() }, id: null };
  if (r.gate === "fail" && TMG.confirmKey !== inp.key) {
    TMG.confirmKey = inp.key;
    $("saveMsg").textContent = "The Time Machine finds this plan negative after charges (" + r.reason + "). Tap “Log anyway” to record the trade as taken.";
    return "hold";
  }
  const snap = JSON.parse(JSON.stringify(tmSnap(r, inp)));          // plain data only: the database refuses undefined
  if (r.gate === "fail") snap.overridden = true;
  TMG.confirmKey = null;
  return { tm: snap, id: snap.pred && snap.pred.id };
}
function tmChip(t) {
  const g = t.tm && t.tm.gate;
  if (!g) return (t.createdAt || "") >= "2026-10-24" && t.source !== "broker" ? ` <span class="tm-chip tmg-none" title="This trade was logged without a Time Machine check">TM not checked</span>` : "";
  const txt = { pass: "TM pass", watch: "TM watch", fail: t.tm.overridden ? "TM fail, taken" : "TM fail", error: "TM not checked", none: "TM not checked" }[g] || "TM";
  return ` <span class="tm-chip tmg-${g}" title="${esc(t.tm.reason || "")}${t.tm.exp != null ? " · average " + inr(t.tm.exp) : ""}">${txt}</span>`;
}

// ----- the Time tab's scan: the desk plan on the contracts near the money, ranked by the Time Machine
function tmCandidates(ch) {
  const out = [];
  for (const side of ["CE", "PE"]) {
    const k = side === "CE" ? "ce" : "pe";
    ch.strikes.filter((r) => r[k] && r[k].ltp && r[k].bid && r[k].ask && r[k].ask >= 1 && r[k].delta != null && Math.abs(r[k].delta) >= 0.35 && Math.abs(r[k].delta) <= 0.65)
      .sort((a, b) => Math.abs(Math.abs(a[k].delta) - 0.5) - Math.abs(Math.abs(b[k].delta) - 0.5)).slice(0, 3)
      .forEach((r) => out.push({ side, strike: r.strike, id: r[k].id, ask: r[k].ask, delta: r[k].delta }));
  }
  return out;
}
async function tmScan() {
  if (TM.scan && TM.scan.running) return;
  const sc = TM.scan = { running: true, cancel: false, rows: [], errs: [], status: "Loading the chains…", done: 0, total: 0, at: Date.now() };
  tmScanRender();
  const lots = Math.max(1, Math.round(+TM.f.lots || 1));
  try {
    const plans = [];
    for (const ix of ["NIFTY", "SENSEX"]) {
      try { const ch = await tmChainFor(ix); for (const c of tmCandidates(ch)) plans.push({ ix, ch, ...c }); } catch (e) { sc.errs.push(ix + ": " + tmErr(e)); }
    }
    sc.total = plans.length;
    for (const [i, p] of plans.entries()) {
      if (sc.cancel) break;
      sc.status = `Replaying ${p.ix} ${p.strike} ${p.side}…`; sc.done = i; tmScanRender();
      try {
        const rep = await tmAnalyse({ ix: p.ix, chain: p.ch, id: p.id, lots, step: "auto", paths: "auto", budget: 1500, minPaths: 600, maxPaths: 3000, entryAt: TM.f.entryAt, sensitivity: false, variants: ["desk"], kind: "scan" });
        const o = tmOdds(rep), lv = rep.levels;
        sc.rows.push({ ix: p.ix, side: p.side, strike: p.strike, expiry: p.ch.expiry, ask: p.ask, delta: p.delta, gate: rep.gate.gate, reason: rep.gate.reason, odds: o, risk: lv.risk_inr, step: rep.meta.step, n: (rep.models.implied || {}).n, rep });
      } catch (e) { sc.errs.push(`${p.ix} ${p.strike} ${p.side}: ${tmErr(e)}`); }
    }
    sc.rows.sort((a, b) => TMa_RANK[a.gate] - TMa_RANK[b.gate] || (b.odds.exp / Math.max(b.risk, 1)) - (a.odds.exp / Math.max(a.risk, 1)));
    sc.rows.slice(0, 6).forEach((r) => lrnStorePred(r.rep, r.ix, "scan"));          // the best few are kept to be settled on the tape
    sc.rows.forEach((r) => { delete r.rep; });
  } catch (e) { sc.errs.push(tmErr(e)); }
  sc.running = false; sc.status = ""; sc.done = sc.total;
  lrnDirty(); tmScanRender();
}
const TMa_RANK = { pass: 0, watch: 1, fail: 2, error: 3 };
function tmScanRender() {
  const el = $("tmScanOut"); if (!el) return;
  const sc = TM.scan;
  if (!sc || TM.mode !== "intra") { el.innerHTML = ""; return; }
  const rows = sc.rows, n = (g) => rows.filter((r) => r.gate === g).length;
  const body = rows.map((r) => `<tr><td>${esc(r.ix)} ${r.strike} ${r.side === "CE" ? "Call" : "Put"}<small>Δ${Math.abs(r.delta).toFixed(2)} · ask ${px(r.ask)} · ${r.step}-min</small></td>
      <td><span class="tm-chip tmg-${r.gate}" title="${esc(r.reason)}">${tmGateName[r.gate]}</span></td>
      <td class="num">${tmOddsTxt(r.odds.profit)}<small>T1 ${pc0(r.odds.t1.now)}</small></td>
      <td class="num ${cls(r.odds.exp)}">${inr(r.odds.exp)}<small>± ${inrA(1.96 * r.odds.se)}</small></td>
      <td><button class="mini" data-scanrun="${esc(r.ix + "|" + r.side + "|" + r.strike + "|" + r.expiry)}">Replay in full</button></td></tr>`).join("");
  el.innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Scan through the Time Machine</h2>
      <span class="small muted">${sc.running ? esc(sc.status) + " " + sc.done + " of " + sc.total : rows.length ? "done · " + new Date(sc.at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : ""}${sc.running ? ` <button class="mini" id="tm_scancancel">Stop</button>` : ""}</span></div>
    ${rows.length ? `<div class="small" style="margin:6px 0">${n("pass")} pass · ${n("watch")} watch · ${n("fail")} fail, of ${rows.length} contracts near the money (Δ 0.35–0.65), ${TM.f.lots} lot${TM.f.lots > 1 ? "s" : ""}, your desk plan, after charges.${n("pass") ? "" : " <b>Nothing passes right now.</b> That is the Time Machine's answer: a contract is not worth taking just because it is on this list."}</div>
      <div class="tbl"><table class="tm-table"><tr><th>Contract</th><th>Gate</th><th class="num">Chance of a profit</th><th class="num">Average result</th><th></th></tr>${body}</table></div>` : sc.running ? "" : `<div class="small muted">No contract could be replayed.</div>`}
    ${sc.errs.length ? `<div class="small down" style="margin-top:6px">${sc.errs.slice(0, 3).map(esc).join("<br>")}</div>` : ""}
    <div class="small muted" style="margin-top:6px">Each contract is replayed with the same engine as the Time tab, with fewer paths so the scan is quick. The best few are kept and settled on the real tape once it is saved, which is how the odds learn.</div></div>`;
}

// ----- the coach's tool
async function tmCoachTool(i) {
  const ix = String(i.index || "").toUpperCase(), side = String(i.side || "").toUpperCase(), strike = +i.strike;
  if (!["NIFTY", "SENSEX"].includes(ix) || !["CE", "PE"].includes(side) || !(strike > 0)) return { error: "give index (NIFTY or SENSEX), side (CE or PE) and strike" };
  const entry = +i.entry > 0 ? +i.entry : null;
  const rules = tmRulesFor({ entry, sl: +i.sl, tp1: +i.tp1, tp2: +i.tp2 });
  const rep = await tmAnalyse({ ix, side, strike, expiry: i.expiry_date ? String(i.expiry_date).replace(/-/g, "") : null, lots: Math.max(1, Math.round(+i.lots || 1)), entry, rules, step: "auto", paths: "auto",
    budget: 2500, minPaths: 1000, maxPaths: 5000, sensitivity: false, variants: ["desk"], kind: "coach", record: true });
  const o = tmOdds(rep), lv = rep.levels, lr = rep.learn;
  return { time_machine: { contract: `${ix} ${strike} ${side}`, gate: rep.gate.gate, reason: rep.gate.reason,
    chance_of_profit_pct: Math.round(100 * o.profit.now), chance_reach_T1_pct: Math.round(100 * o.t1.now), chance_stopped_pct: Math.round(100 * o.stop.now),
    average_result_inr: Math.round(o.exp), average_95pct_band_inr: Math.round(1.96 * o.se), entry: rep.meta.entry_plan, sl: lv.sl, t1: lv.t1, t2: lv.t2, lots: rep.meta.lots,
    candle_minutes: rep.meta.step, paths: (rep.models.implied || {}).n,
    learned: `market pace x${lr.volMult.toFixed(2)}${lr.pace.applied ? "" : " (not yet used)"}, calibration ${lr.calibration.profit.applied ? "in use" : "not yet in use"}`,
    note: "A model's odds after charges, not a forecast. 'fail' means the plan loses money on average in the models: do not grade such a setup A or A+." } };
}

// ----- the Learning card
const pcn = (x) => (x == null ? "—" : (100 * x).toFixed(1) + "%");
function lrnHTML() {
  const st = LRN.st, ixs = ["NIFTY", "SENSEX"], Ls = Object.fromEntries(ixs.map((ix) => [ix, TMl.learned(st, ix)]));
  const paceCell = (p) => p.n ? `×${p.mult.toFixed(2)}<small>${p.applied ? "in use" : "seen ×" + p.learned.toFixed(2) + ", used from " + TMl.N_MIN.pace} · ${p.n} obs · ${p.ci[0].toFixed(2)}–${p.ci[1].toFixed(2)}</small>` : `—<small>no observations yet</small>`;
  const sessCell = (ix) => { const l = LRN.sess[ix] || []; return l.length ? `${l.length}<small>${l[0].date.slice(5)} to ${l[l.length - 1].date.slice(5)}</small>` : `0<small>saved as they arrive</small>`; };
  const calRows = TMl.EVENTS.map((e) => {
    const c = TMl.calibrationOf(st, e), name = { profit: "Chance of a profit", t1: "Reaches T1", stop: "Stopped out" }[e];
    return `<tr><td>${name}</td><td class="num">${c.n}${c.nTrades ? `<small>+ ${c.nTrades} of your trades</small>` : ""}</td><td class="num">${c.applied ? "in use" : "from " + TMl.N_MIN.calibPaper}</td>
      <td class="num">${c.applied ? "slope " + c.b.toFixed(2) + ", shift " + c.a.toFixed(2) : "—"}</td><td class="num">${c.held ? c.held.raw.toFixed(3) + " → " + c.held.calibrated.toFixed(3) + `<small>on ${c.held.n} held out</small>` : "—"}</td></tr>`;
  }).join("");
  const w = TMl.weightsOf(st);
  const perf = Object.entries(st.perf).map(([k, p]) => `${k.replace("|", " ")}-min: ${(p.msPerBar * 1000).toFixed(1)} µs per path-bar`).join(" · ");
  const pend = [...LRN.preds.values()].filter((v) => !v.done).length, pendObs = LRN.obs.size;
  const mine = S.trades.filter((t) => t.tm && t.tm.gate && t.tm.gate !== "none" && t.tm.gate !== "error" && openQty(t) === 0 && (t.exits || []).length);
  const byGate = ["pass", "watch", "fail"].map((g) => { const xs = mine.filter((t) => t.tm.gate === g); return xs.length ? `<tr><td>${tmGateName[g]}</td><td class="num">${xs.length}</td><td class="num">${xs.filter((t) => realised(t) > 0).length}</td><td class="num ${cls(xs.reduce((a, t) => a + realised(t), 0))}">${inr(xs.reduce((a, t) => a + realised(t), 0))}</td></tr>` : ""; }).join("");
  return `<div class="card" id="tmLearnCard"><div class="row" style="justify-content:space-between"><h2 style="margin:0">What it has learned</h2>
      <span class="small muted">${st.frozen ? "frozen" : "learning"}${st.updated ? " · updated " + esc(st.updated.slice(0, 16).replace("T", " ")) + " UTC" : ""}${db ? "" : " · not saved in this view"}</span></div>
    <p class="small tm-fine" style="margin:6px 0">It learns three things from data the app collects itself: how fast the market really moves against what options price, how far its own odds have been from what happened, and which of its models has been closer. It also speeds up on this device. It cannot learn where the market goes next, and it never changes its rules, its code or your orders. Every figure is bounded, shrunk toward "no change" while evidence is thin, and shown here. It advances when the app is opened.</p>
    <div class="tbl"><table class="tm-table"><tr><th></th>${ixs.map((ix) => `<th class="num">${ix}</th>`).join("")}</tr>
      <tr><td>Saved 5-minute sessions<small>the bootstrap, the replay and the volatility profile draw on these</small></td>${ixs.map((ix) => `<td class="num">${sessCell(ix)}</td>`).join("")}</tr>
      <tr><td>Pace of the market against the option's<small>×1 = as the option prices it</small></td>${ixs.map((ix) => `<td class="num">${paceCell(Ls[ix].pace)}</td>`).join("")}</tr>
      <tr><td>Pace of the overnight gap</td>${ixs.map((ix) => `<td class="num">${paceCell(Ls[ix].gap)}</td>`).join("")}</tr></table></div>
    <h3>Are its odds honest?</h3>
    <div class="tbl"><table class="tm-table"><tr><th>Odds of</th><th class="num">Settled on the tape</th><th class="num">Correction</th><th class="num">How</th><th class="num">Brier, held out<small>lower is better</small></th></tr>${calRows}</table></div>
    <div class="small muted" style="margin-top:6px">${pend} prediction${pend === 1 ? "" : "s"} and ${pendObs} observation${pendObs === 1 ? "" : "s"} waiting for the tape to be saved. The odds are only corrected once ${TMl.N_MIN.calibPaper} are settled, and the correction is judged on predictions it was not fitted to. The average result is not corrected.</div>
    <h3>Which model to trust</h3>
    <div class="small">Implied <b>${pc0(w.w.implied)}</b> · Bootstrap <b>${pc0(w.w.bootstrap)}</b> · Recent sessions <b>${pc0(w.w.replay)}</b> ${w.learned ? `<span class="muted">(learned from ${w.n} settled predictions; each stays between 10% and 70%)</span>` : `<span class="muted">(the default mix; it moves after ${TMl.N_MIN.weights} settled predictions)</span>`}</div>
    ${perf ? `<h3>Speed on this device</h3><div class="small">${esc(perf)}</div>` : ""}
    ${byGate ? `<h3>Your trades against the gate</h3><div class="tbl"><table class="tm-table"><tr><th>Gate when logged</th><th class="num">Closed trades</th><th class="num">Winners</th><th class="num">Net</th></tr>${byGate}</table></div><div class="small muted" style="margin-top:4px">A handful of trades says little either way.</div>` : ""}
    ${st.log.length ? `<h3>What changed</h3><ul class="tm-notes">${st.log.slice(-6).reverse().map((l) => `<li>${esc((l.t || "").slice(0, 10))} · ${esc(l.msg)}</li>`).join("")}</ul>` : ""}
    <div class="row" style="margin-top:10px"><button class="mini" id="lrn_freeze">${st.frozen ? "Resume learning" : "Freeze learning"}</button>
      <button class="mini ${LRN.resetArmed && Date.now() - LRN.resetArmed < 5000 ? "danger" : ""}" id="lrn_reset">${LRN.resetArmed && Date.now() - LRN.resetArmed < 5000 ? "Tap again to reset" : "Reset what it learned"}</button>
      <span class="small muted">Freezing keeps the pace, the calibration and the weights as they are. The saved sessions are data, and keep growing. Resetting keeps them too.</span></div></div>`;
}
function lrnRender() {
  const el = $("tmLearn"); if (!el || S.view !== "time") return;
  const ae = document.activeElement; if (ae && el.contains(ae) && /INPUT|SELECT/.test(ae.tagName)) return;
  el.innerHTML = lrnHTML();
}

// ----- the first look in the background: save the day's sessions and settle what they settle, without opening the Time tab
function lrnBackground() {
  if (LRN.bg || !mcp) return;
  LRN.bg = true;
  setTimeout(async () => {
    for (const ix of ["NIFTY", "SENSEX"]) {
      try { const feed = await tm5m(TMSYM[ix].key); lrnAbsorb(ix, feed, null); } catch {}
      if ([...LRN.obs.values()].some((o) => o.ix === ix && o.kind === "gap")) { try { lrnAbsorb(ix, null, await tmDaily(TMSYM[ix].key)); } catch {} }
    }
  }, 5000);
}

// ----- wiring
function tmDeskBind() {
  const gate = $("tmGate");
  if (gate) gate.addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-tmg]"); if (!b) return;
    if (b.dataset.tmg === "again") { TMG.last = null; tmGateSchedule(); return; }
    if (b.dataset.tmg === "full") {
      const inp = TMG.last && TMG.last.inp; if (!inp) return;
      TM.mode = "intra"; TM.f.pos = null; TM.f.ix = inp.ix; TM.f.side = inp.side; TM.f.lots = inp.lots; TM.f.plan = { entry: inp.entry, sl: inp.sl, tp1: inp.tp1, tp2: inp.tp2 };
      TM.f.expiry = inp.expiry || ""; TM.f.strike = ""; TM.f.useId = inp.id; TM.ch = null; tmSave();
      setView("time");
    }
  });
  const sc = $("tmScanOut");
  if (sc) sc.addEventListener("click", (ev) => {
    if (ev.target.closest("#tm_scancancel")) { if (TM.scan) TM.scan.cancel = true; return; }
    const b = ev.target.closest("button[data-scanrun]"); if (!b) return;
    const [ix, side, strike, expiry] = b.dataset.scanrun.split("|");
    Object.assign(TM.f, { ix, side, strike: +strike, expiry, pos: null, plan: null, useId: null }); TM.ch = null;
    tmRender(); tmPrepare().then(() => tmRun());
  });
  const ln = $("tmLearn");
  if (ln) ln.addEventListener("click", (ev) => {
    if (ev.target.closest("#lrn_freeze")) { LRN.st.frozen = !LRN.st.frozen; LRN.st.updated = lrnNow(); lrnDirty(); return; }
    if (ev.target.closest("#lrn_reset")) {
      if (!LRN.resetArmed || Date.now() - LRN.resetArmed > 5000) { LRN.resetArmed = Date.now(); lrnRender(); setTimeout(lrnRender, 5100); return; }
      LRN.resetArmed = 0; const fr = LRN.st.frozen; LRN.st = TMl.emptyState(); LRN.st.frozen = fr; LRN.st.updated = lrnNow(); LRN.st.created = LRN.st.updated; lrnDirty();
    }
  });
}
