// ---------- the three discipline rules (adopted 21 Oct 2026, rule 2 amended 21 Nov 2026), wired in 26 Nov 2026 ----------
// The rules are pure functions in tmx-discipline.js (window.TMXD). This is the desk's side: it builds their inputs from the journal and
// the 5m candles, shows them (Now tab card, Trade tab lines, journal flags), asks for a second tap to log an entry that breaks one,
// and keeps a record of each A+ the finder confirms (collection "disc", one doc a day) so a trade can be judged A+ or not afterwards.
const TMD = window.TMXD;
const DISC_TEXT = `THREE DISCIPLINE RULES (adopted 21 Oct 2026; rule 2 amended 21 Nov 2026)
1. ONE RE-ENTRY PER SETUP. After an SL hit, the next entry in that idea needs a fresh A+ 5m close on both indices. No re-entering the same trade "to make it back". The second and later entries after an SL hit break this rule.
2. PROFIT LOCK. Once the day's realised P&L reaches ₹10,000, only A+ setups from then on, at the normal 4–6 lots (or stop for the day). Any non-A+ or oversized entry above the trigger is a rule break. Reason: after a good morning the aim must change from "more profit" to "protect the day".
3. RANGE FILTER. If the last 6–8 closed 5m candles sit inside a 400-point SENSEX range (about 120 points NIFTY), there are no entries until a 5m close breaks the range by the buffer (25 pts SENSEX, ~8 pts NIFTY). Call it "RANGE: no trade" and note the two break levels.
DEFINITIONS: A+ = a 5m close beyond the level on BOTH indices on the same candle, then the two-candle hold (25 Nov rule). Buffer = 0.035% of the index (25 pts SENSEX, ~8 pts NIFTY).`;
const discCfg = () => TMD.cfgWith(S.rules.disc);
const dirOfTrade = t => t.type === "PE" ? -1 : t.type === "CE" ? 1 : /put|pe\b/i.test(t.instrument || "") ? -1 : 1;
const dirWord = d => d > 0 ? "calls" : "puts";
const lotsOfTrade = t => { const ix = ixOfTrade(t), L = +t.lotSize || (S.rules.lotSizes && S.rules.lotSizes[ix]) || (IDX[ix] && IDX[ix].lot) || 1; return Math.round((+t.qty || 0) / L); };
const lastExitTime = t => { const x = (t.exits || []).map(e => e.time).filter(Boolean).sort(); return x.length ? x[x.length - 1] : null; };
const tradeClosedAt = t => openQty(t) > 0 ? null : (lastExitTime(t) || t.time || null);
/** An SL hit: closed at a loss, at or under the stop it carried (or, with no itemised exits, a loss of at least half a stop). */
function hitStop(t) {
  if (openQty(t) > 0 || realised(t) > -1) return false;
  const ex = (t.exits || []).filter(e => e.price != null), sl = num(t.sl), outlay = (+t.entry) * (+t.qty);
  if (t.pnlOverride == null && ex.length && sl != null) { const last = ex.slice().sort((a, b) => String(a.time || "").localeCompare(String(b.time || "")))[ex.length - 1]; return +last.price <= sl + 0.5; }
  return realised(t) <= -0.5 * (+S.rules.slPct || 10) / 100 * outlay;
}
/** Net realised P&L of one trade by a given time of day (an exit later than that does not count yet). */
function realisedBy(t, T) {
  if (t.pnlOverride != null) return openQty(t) === 0 && (!t.time || toMin(t.time) <= toMin(T)) ? realised(t) : 0;
  const all = t.exits || [], ex = all.filter(e => !e.time || toMin(e.time) <= toMin(T)), done = ex.length === all.length && openQty(t) === 0;
  const gross = ex.reduce((a, e) => a + ((+e.price) - (+t.entry)) * (+e.qty), 0) + (done ? (+t.realisedExtra || 0) : 0);
  return done ? gross - charges(t) : gross;
}
/** The A+ status of a journal trade: what it was stamped with when logged here, else the day's A+ record (an entry in the 20
 *  minutes after an A+ confirmed, same side), else null; undefined when the app has no A+ record for that day at all. */
function aplusOf(t) {
  if (t.aplus) return t.aplus;
  const d = S.disc && S.disc[t.date]; if (!d) return undefined;
  const T = toMin(t.time || "0:0"), dir = dirOfTrade(t), hit = (d.aplus || []).filter(x => x.dir === dir && toMin(x.conf) <= T && T < toMin(x.conf) + 20).pop();
  return hit ? { t: hit.t, key: hit.key } : null;
}
/** The range as it stood when an entry was made, from today's closed 5m candles; null when those candles are not the entry's day. */
function discRangeAt(ix, date, time) {
  const c = todayCandles(ix); if (!c.length || String(c[c.length - 1].datetime_ist).slice(0, 10) !== date) return null;
  const closed = c.filter(x => toMin(String(x.datetime_ist).slice(11, 16)) + 5 <= toMin(time));
  return closed.length ? TMD.rangeState(closed, ix, S.rules.disc) : null;
}
/** The three checks for an entry: e {date, time, dir, lots, ix, aplus}, against the day's trades made before it. */
function discCheck(e, prior) {
  const ps = prior.map(p => ({ dir: dirOfTrade(p), time: p.time || "00:00", closedAt: tradeClosedAt(p), stopped: hitStop(p) }));
  return TMD.checkEntry({ date: e.date, time: e.time, dir: e.dir, lots: e.lots, ix: e.ix, aplus: e.aplus, prior: ps,
    realisedBefore: prior.reduce((a, p) => a + realisedBy(p, e.time), 0), range: discRangeAt(e.ix, e.date, e.time), lock: +S.rules.profitLock || 0, cfg: S.rules.disc });
}
const discNowEntry = (dir, lots, ix, aplus) => { const t = ist(); return { date: t.date, time: t.hm, dir, lots, ix, aplus }; };
function discCheckNow(dir, lots, ix, aplus) {
  const e = discNowEntry(dir, lots, ix, aplus);
  return discCheck(e, dayTrades(e.date).filter(p => (p.time || "") <= e.time));
}
/** The labels for the journal: the rules a logged trade broke. */
function discFlags(t, idx) {
  try {
    if (!t.date || t.date < "2026-10-21") return [];
    const day = dayTrades(t.date), i = idx >= 0 && idx < day.length ? idx : day.indexOf(t);
    return discCheck({ date: t.date, time: t.time || "00:00", dir: dirOfTrade(t), lots: lotsOfTrade(t), ix: ixOfTrade(t), aplus: aplusOf(t) }, day.slice(0, i < 0 ? day.length : i)).flatMap(r => r.flags);
  } catch (e) { return []; }
}
/** The confirmed A+ the finder is showing for this side, if it is live now (what makes an entry now an A+ entry). */
function discAplusNow(dir) {
  const ev = AP && AP.ev; if (!ev || !ev.live || !ev.confirmed || ev.dir !== dir) return null;
  return { t: ev.s.t, key: ev.key };
}
/** The record of each confirmed A+ (one document a day), so a trade made later can be judged against it. */
function discLogAplus(ev) {
  if (!db || !canWrite || !ev || ev.preview) return;
  const date = ist().date, ref = db.doc("disc/" + date), conf = (() => { const m = toMin(ev.s.t) + 5 * (discCfg().hold + 1); return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); })();
  queue(async () => {
    const s = await ref.get(), cur = s.exists ? s.data() : { date, aplus: [] };
    if ((cur.aplus || []).some(x => x.key === ev.key)) return;
    await ref.set({ ...cur, date, aplus: [...(cur.aplus || []), { dir: ev.dir, t: ev.s.t, conf, key: ev.key, at: new Date().toISOString() }], updatedAt: new Date().toISOString() });
  });
}
/** Mark today as watched, so "no A+ in the record" can be told from "the app was not open". */
function discOpenDay() {
  const date = ist().date; if (!db || !canWrite || !marketOpen() || (S.disc && S.disc[date]) || S.discMarked === date) return;
  S.discMarked = date;
  queue(async () => { const ref = db.doc("disc/" + date), s = await ref.get(); if (!s.exists) await ref.set({ date, aplus: [], startedAt: new Date().toISOString() }); });
}
/** What the Trade tab and the save button need: the entry as typed, with its A+ status (ticked box, else the finder's). */
function discFormEntry() {
  const lots = Math.max(1, Math.round(num($("f_lots").value) || 0)), ix = (S.pick && S.pick.ix) || ixOfName($("f_inst").value) || S.ix;
  const side = (S.pick && S.pick.side) || (/put|pe\b/i.test($("f_inst").value) ? "PE" : "CE"), dir = side === "PE" ? -1 : 1;
  const date = $("f_date").value || ist().date, time = $("f_time").value || ist().hm;
  const auto = date === ist().date ? discAplusNow(dir) : null, box = $("f_aplus");
  const aplus = box && box.checked ? (auto || { t: null, self: true }) : null;
  return { date, time, dir, lots, ix: IDX[ix] ? ix : S.ix, aplus };
}
function discFormChecks() {
  const e = discFormEntry();
  return { e, res: discCheck(e, dayTrades(e.date).filter(p => (p.time || "") <= e.time)) };
}
function discPending() { try { if (!$("f_inst").value.trim()) return ""; const r = discFormChecks().res.filter(x => !x.ok); return r.length ? "breaks rule " + r.map(x => x.rule).join(" and ") : ""; } catch (e) { return ""; } }
/** The three lines for the Trade tab's checklist: [ok, text]. */
function discItems() {
  if (!$("f_inst") || !$("f_inst").value.trim()) return [];
  const { res } = discFormChecks();
  return res.map(r => [r.ok, "Rule " + r.rule + ": " + r.text]);
}
/** Keep the A+ tick in step with the finder until the trader touches it. */
function discSyncBox() {
  const box = $("f_aplus"); if (!box) return;
  const e = discFormEntry(), auto = (e.date === ist().date) ? discAplusNow(e.dir) : null;
  if (!box.dataset.touched) box.checked = !!auto;
  const note = $("f_aplusNote"); if (note) note.textContent = auto ? "the finder has a live A+ " + dirWord(e.dir).slice(0, -1) + " (break candle " + auto.t + ")" : box.checked ? "ticked by you: the app has not seen an A+ for this" : "";
}
/** The second tap: before the save, an entry that breaks a rule is held once. Returns true to stop the save. */
function discBeforeSave(doc) {
  const { e, res } = discFormChecks(), bad = res.filter(r => !r.ok), key = [e.date, e.time, e.dir, e.lots, doc.instrument].join("|");
  if (bad.length && S.discConfirm !== key) {
    S.discConfirm = key;
    $("saveMsg").innerHTML = bad.map(r => esc("Rule " + r.rule + ": " + r.text)).join("<br>") + "<br>Tap “Log anyway” to record the trade as taken; it will be flagged in the journal.";
    discSaveBtn(); return true;
  }
  S.discConfirm = null;
  doc.aplus = e.aplus ? JSON.parse(JSON.stringify(e.aplus)) : null;
  if (bad.length) doc.disc = { rules: bad.map(r => r.rule), overridden: true, at: ist().hm };
  return false;
}
function discSaveBtn() { if (typeof tmSaveBtn === "function") tmSaveBtn(); }

// ----- the Now tab's card
function discSummary() {
  const t = ist(), todayTrades = dayTrades(t.date).filter(p => (p.time || "") <= t.hm), cfg = discCfg();
  const sides = [1, -1].map(dir => ({ dir, r: discCheck(discNowEntry(dir, 4, S.ix, discAplusNow(dir) || (S.disc && S.disc[t.date] ? null : undefined)), todayTrades)[0] }));
  const realisedNow = todayTrades.reduce((a, p) => a + realisedBy(p, t.hm), 0), lock = +S.rules.profitLock || 0;
  const ranges = ["SENSEX", "NIFTY"].map(ix => ({ ix, r: discRangeAt(ix, t.date, t.hm) }));
  return { t, cfg, sides, realisedNow, lock, lockOn: !!lock && realisedNow >= lock, ranges };
}
function discHTML() {
  const f = n => Math.round(n).toLocaleString("en-IN");
  if (!S.loaded) return `<div class="small muted">Loading your journal…</div>`;
  const d = discSummary(), open = marketOpen();
  const rangeLines = d.ranges.map(({ ix, r }) => !r ? `<div class="small"><b>${ix}</b>: ${open ? "waiting for candles" : "no candles for today yet"}.</div>`
    : r.active ? `<div class="small down"><b>${ix}: RANGE: no trade.</b> Last ${r.n} candles inside ${f(r.lo)}–${f(r.hi)} (since ${esc(r.since)}). Break above <b>${f(r.up)}</b> or below <b>${f(r.dn)}</b> on a 5m close first.</div>`
    : `<div class="small up"><b>${ix}</b>: not in a range${r.broke ? ` (broke ${r.broke.dir > 0 ? "up" : "down"} at ${esc(r.broke.t)})` : ""}.</div>`).join("");
  const sideLines = d.sides.map(({ dir, r }) => `<div class="small ${r.ok ? (r.known ? "up" : "muted") : "down"}"><b>${dirWord(dir)}</b>: ${esc(r.text)}</div>`).join("");
  const lockLine = d.lock ? `<div class="small ${d.lockOn ? "down" : ""}"><b>Profit lock</b>: realised ${inr(d.realisedNow)} of ${inr(d.lock, false)}. ${d.lockOn ? "<b>ON: only A+ setups, 4–" + d.cfg.lockMaxLots + " lots, or stop for the day.</b>" : "Not on."}</div>` : "";
  const pend = AP.pending ? `<div class="small"><b>A+ ${apDirWord(AP.pending.dir)}</b>: both indices closed ${AP.pending.dir > 0 ? "above" : "below"} their level at ${esc(AP.pending.s.t)}; held ${AP.pending.held} of ${AP.pending.need} candles. Not A+ until the hold is done.</div>` : "";
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">Discipline</h2><span class="pill ${d.lockOn ? "closed" : ""}">${d.lockOn ? "profit lock ON" : "3 rules"}</span></div>
    <div style="margin-top:6px"><div class="small muted" style="margin-bottom:2px">Rule 3, range filter</div>${rangeLines}</div>
    <div style="margin-top:6px"><div class="small muted" style="margin-bottom:2px">Rule 2, profit lock</div>${lockLine}</div>
    <div style="margin-top:6px"><div class="small muted" style="margin-bottom:2px">Rule 1, one re-entry per setup</div>${sideLines}${pend}</div>
    <details data-apd="disc" style="margin-top:6px"><summary class="small muted">The rules, as adopted</summary><div class="small" style="white-space:pre-wrap;margin-top:4px">${esc(DISC_TEXT)}</div>
      <div class="small muted" style="margin-top:4px">Readings: an SL hit is a closed trade at a loss, at or under its stop. An idea is one side (calls or puts) on one day, whichever index. The first entry after an SL hit is the re-entry; a further one breaks rule 1. A candle count of ${d.cfg.rangeMin} to ${d.cfg.rangeMax} decides the range, and a break needs a close beyond it by ${(d.cfg.bufPct * 100).toFixed(3)}% of the index. All of these are in config/rules.disc.</div></details>`;
}
function renderDisc() {
  const el = $("discCard"); if (!el) return;
  let h; try { h = discHTML(); } catch (e) { h = `<div class="small muted">The discipline card could not be built (${esc(e.message)}).</div>`; }
  if (h !== (renderDisc.last || "")) { renderDisc.last = h; el.innerHTML = h; }
}
/** For the coach: today's state in a few lines. */
function discCoachText() {
  try {
    const d = discSummary(), f = n => Math.round(n);
    return `Discipline now: ${d.ranges.map(({ ix, r }) => !r ? ix + " range unknown" : r.active ? `${ix} RANGE: no trade (${f(r.lo)}-${f(r.hi)}; break above ${f(r.up)} or below ${f(r.dn)})` : ix + " not in a range").join("; ")}. Day realised ${f(d.realisedNow)}${d.lock ? `, profit lock ${d.lockOn ? "ON (A+ only, 4-" + d.cfg.lockMaxLots + " lots)" : "off"}` : ""}. ${d.sides.map(({ dir, r }) => dirWord(dir) + ": " + r.text).join(" ")}${AP.pending ? ` A+ ${apDirWord(AP.pending.dir)} pending: held ${AP.pending.held} of ${AP.pending.need}.` : ""}`;
  } catch (e) { return ""; }
}
