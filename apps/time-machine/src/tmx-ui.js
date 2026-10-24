// ---------- Time Machine (5th tab) ----------
// Runs inside the Trading Desk's own script, so it uses the desk's helpers ($, esc, inr, px, S, IDX, callIND, ist, marketOpen,
// defaultExpiry, dayTrades, openQty ...). The engines (TMX, TMXI) and charts (TMXC) are loaded as globals by earlier <script> blocks.
const TMa = window.TMX, TMi = window.TMXI, TMc = window.TMXC;
const TMSYM = {
  NIFTY: { key: "INDI00012", lot: 65 }, SENSEX: { key: "INDI00050", lot: 20 }, BANKNIFTY: { key: "INDI00024", lot: 30 },
  FINNIFTY: { key: "INDI00311", lot: 60 }, MIDCPNIFTY: { key: "INDI00304", lot: 120 },
};
const TM = {
  mode: "intra", busy: false, runId: 0, cancel: false, cache: new Map(), exp: {}, ch: null, sessions: null, view: "implied", sview: "bootstrap",
  other: null, found: null, status: "", err: "", progress: 0,
  f: { ix: "NIFTY", expiry: "", side: "CE", strike: "", lots: 2, entryAt: "09:30", paths: 4000, volMode: "realised", iv: "", pos: null,
    sym: "NIFTY", dir: "auto", inst: "both", slots: 1, qty: 10, spaths: 10000, horizon: 40, sexpiry: "", sl: "", tp1: "", tp2: "", limit: "", siv: "",
    drift: false, grid: false },
  out: { intra: null, swing: null },
};
const tmHol = () => new Set(Object.keys({ ...HOLIDAYS, ...(S.rules.holidays || {}) }));
const tmRules = () => ({ slPct: +S.rules.slPct || 10, tp1Pct: +S.rules.tpMin || 15, tp2Pct: +S.rules.tpMax || 20,
  noEntryAfter: S.rules.noEntryAfter || "15:00", dayStop: +S.rules.dayStop || 3000, breathPct: BREATH_PCT });
const pc1 = (x) => x == null || isNaN(x) ? "—" : (100 * x).toFixed(1) + "%";
const pc0 = (x) => x == null || isNaN(x) ? "—" : Math.round(100 * x) + "%";
const rR = (x) => x == null || isNaN(x) ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(2) + "R";
const rR3 = (x) => x == null || isNaN(x) ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(3) + "R";
const inrA = (n) => inr(n, false);
const tmSave = () => { try { localStorage.setItem("desk.tm", JSON.stringify({ mode: TM.mode, f: { ...TM.f, pos: null } })); } catch {} };
const tmCached = (key, ttl, fn) => {
  const c = TM.cache.get(key);
  if (c && Date.now() - c.at < ttl) return c.p;
  const p = fn().catch((e) => { TM.cache.delete(key); throw e; });
  TM.cache.set(key, { at: Date.now(), p });
  return p;
};
// ----- data
const tmDaily = (key) => tmCached("d:" + key, 30 * 60e3, async () => {
  const d = await callIND("get_indian_stocks_ohlc", { ind_key: key, interval: "1day", lookback: "1y" });
  return TMa.loadCandles(d.candles || [], { queryEnd: d.query_end_ist });
});
const tm5m = (key) => tmCached("m:" + key, 25e3, async () => {
  const d = await callIND("get_indian_stocks_ohlc", { ind_key: key, interval: "5minute", lookback: "7d" });
  const t = ist(), open = marketOpen();
  const rows = (d.candles || []).filter((c) => {            // a bar still forming is not a bar yet
    const s = String(c.datetime_ist);
    return !(open && s.slice(0, 10) === t.date && (+s.slice(11, 13)) * 60 + (+s.slice(14, 16)) + 5 > t.mins);
  });
  return TMi.parseBars(rows);
});
const tmChain = (key, expiry, fresh) => {
  const ck = "c:" + key + ":" + (expiry || "");
  if (fresh) TM.cache.delete(ck);
  return tmCached(ck, 20e3, async () => {
    const d = await callIND("get_indian_stocks_option_chain", { ind_key: key, use_expiry_date: !!expiry, expiry_date: expiry || null, strikes_around_atm: 9 });
    const ch = TMa.parseChain(d);
    const ct = TMa.chainTimeMs(ch);
    TMa.chainAnalytics(ch, ct && ct < Date.now() ? ct : Date.now(), 0.065);
    return ch;
  });
};
const tmErr = (e) => e && e.code ? errInfo(e).long : (e && e.message) || "Something went wrong.";

// ----- controls
function tmLead() {
  $("tmLead").textContent = TM.mode === "intra"
    ? "Your desk plan (SL, T1, T2, the trailing ladder, out by 15:15) replayed on 5-minute bars, with the option repriced every bar. It counts how the trade ends; it does not predict the next move."
    : "A daily-bar trade (ATR stops, TP1/TP2, a trailing stop) in futures, an option or shares, replayed through the chart's own history, a bootstrap of its real days and the volatility the option chain prices.";
}
const tmOpt = (v, label, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`;
const tmDay = (e) => e ? `${e.slice(6, 8)} ${MON[+e.slice(4, 6) - 1]}` : "";
function tmOpenTrades() {
  return active(S.trades.filter((t) => openQty(t) > 0 && t.indKey && t.date === ist().date)).sort(byDT);
}
function tmIntraCtl() {
  const f = TM.f, ch = TM.ch, rules = tmRules(), open = marketOpen() && ist().mins < 915;
  const exps = TM.exp[f.ix] || [];
  const rows = ch ? ch.strikes : [];
  const sideKey = f.side === "CE" ? "ce" : "pe";
  const spot = ch && ch.spot, atm = rows.length ? rows.reduce((a, b) => Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a) : null;
  const strikeOpts = rows.filter((r) => r[sideKey] && r[sideKey].ltp && (!atm || Math.abs(rows.indexOf(r) - rows.indexOf(atm)) <= 7)).map((r) => {
    const g = r[sideKey]; const dl = g.delta != null ? Math.abs(g.delta) : null;
    return tmOpt(r.strike, `${r.strike} · ₹${g.ask || g.ltp}${dl != null ? " · Δ" + dl.toFixed(2) : ""}`, f.strike);
  }).join("");
  const times = ["09:20", "09:30", "09:45", "10:00", "10:15", "10:30", "11:00", "11:30", "12:00", "13:00", "14:00", "14:30", "15:00"];
  const t = ist(), nextBar = TMi.hm(Math.max(0, Math.ceil((t.mins - 555) / 5)));
  const trades = tmOpenTrades();
  const picked = S.pick && S.pick.key;
  const pos = f.pos;
  return `<div class="row" style="margin-top:6px">
      <div class="seg" id="tm_ix">${["NIFTY", "SENSEX"].map((k) => `<button data-v="${k}" class="${f.ix === k ? "on" : ""}">${k}</button>`).join("")}</div>
      <select id="tm_exp" aria-label="Expiry" style="width:auto" ${pos ? "disabled" : ""}>${exps.slice(0, 5).map((e) => tmOpt(e, tmDay(e) + " expiry", f.expiry)).join("") || "<option>…</option>"}</select>
      <div class="seg" id="tm_side">${["CE", "PE"].map((k) => `<button data-v="${k}" class="${f.side === k ? "on" : ""}" ${pos ? "disabled" : ""}>${k === "CE" ? "Call" : "Put"}</button>`).join("")}</div>
    </div>
    <div class="presets"><span class="small muted">Start from</span>
      ${trades.map((x) => `<button data-pos="${esc(x.id)}" title="Replay what happens next to this open trade">My open trade: ${esc(String(x.instrument || "").replace(/^(SENSEX|NIFTY)\s*/, "$1 ").slice(0, 28))}</button>`).join("")}
      ${picked ? `<button id="tm_fromtrade" title="Use the strike chosen on the Trade tab">The Trade tab's pick</button>` : ""}
      ${!trades.length && !picked ? `<span class="small muted">a strike below; open trades and the Trade tab's pick appear here</span>` : ""}
    </div>
    ${pos ? `<div class="tm-pos small"><b>Managing ${esc(pos.name)}</b> · entry ${px(pos.entry)} · stop ${px(pos.stop)}${pos.t1Done ? " · T1 done" : ""} · ${pos.remainingLots} lot${pos.remainingLots > 1 ? "s" : ""} left
        <button class="mini" id="tm_clearpos" style="margin-left:8px">Back to a new trade</button></div>` : ""}
    <div class="form">
      <div><label for="tm_strike">Strike</label><select id="tm_strike" ${pos ? "disabled" : ""}>${strikeOpts || "<option>loading…</option>"}</select></div>
      <div><label for="tm_lots">Lots ${ch ? `<span class="muted">× ${ch.lot_size || S.rules.lotSizes[f.ix]}</span>` : ""}</label><input id="tm_lots" type="number" min="1" inputmode="numeric" value="${esc(pos ? pos.remainingLots : f.lots)}" ${pos ? "disabled" : ""}></div>
      <div><label for="tm_entry">Entry</label>${open
        ? `<input id="tm_entry" value="live: next bar ${nextBar}" disabled>`
        : `<select id="tm_entry" aria-label="Entry time">${times.filter((x) => TMi.barOf(x) <= TMi.barOf(rules.noEntryAfter)).map((x) => tmOpt(x, "next session, " + x, f.entryAt)).join("")}</select>`}</div>
      <div><label for="tm_paths">Paths</label><select id="tm_paths">${[[2000, "2,000 (quick)"], [4000, "4,000"], [10000, "10,000"]].map(([v, l]) => tmOpt(v, l, f.paths)).join("")}</select></div>
    </div>
    <details style="margin-top:8px"><summary class="small muted">Options</summary>
      <div class="form" style="margin-top:8px">
        <div><label for="tm_vol">Recent-bars model runs at</label><select id="tm_vol">${tmOpt("realised", "the recent realised pace", f.volMode)}${tmOpt("implied", "the option's implied pace", f.volMode)}</select></div>
        <div><label for="tm_iv">IV override, % a year</label><input id="tm_iv" type="number" step="0.1" inputmode="decimal" placeholder="the chain's" value="${esc(f.iv)}"></div>
      </div>
      <div class="small muted" style="margin-top:6px">Rules come from your saved settings: SL ${rules.slPct}%, T1 +${rules.tp1Pct}%, T2 +${rules.tp2Pct}%, no entries after ${rules.noEntryAfter}, day stop ${inrA(rules.dayStop)}, out at 15:15.</div>
    </details>
    <div class="row" style="margin-top:10px"><button class="primary" id="tm_run" ${TM.busy ? "disabled" : ""}>${TM.busy ? "Replaying…" : pos ? "Replay what happens next" : "Replay the trade"}</button>
      ${TM.busy ? `<button id="tm_cancel">Stop</button>` : ""}<span class="small muted tm-status" id="tm_status">${esc(TM.status)}</span></div>
    <div class="tm-prog" ${TM.busy ? "" : "hidden"}><div id="tm_prog" style="width:${Math.round(100 * TM.progress)}%"></div></div>
    ${TM.err ? `<div class="small down" style="margin-top:6px">${esc(TM.err)}</div>` : ""}`;
}
function tmSwingCtl() {
  const f = TM.f, ch = TM.ch;
  const syms = Object.keys(TMSYM), cur = TM.other && f.sym === TM.other.sym ? TM.other : null;
  const exps = (ch && ch.expiry_list) || [];
  const eq = f.inst === "equity";
  return `<div class="row" style="margin-top:6px">
      <select id="tm_sym" aria-label="Symbol" style="width:auto">${syms.map((k) => tmOpt(k, k, f.sym)).join("")}${cur ? tmOpt(cur.sym, cur.sym, f.sym) : ""}</select>
      <button class="mini" id="tm_other">Another stock…</button>
      <div class="seg" id="tm_dir">${[["auto", "Setup on the last bar"], ["long", "Long"], ["short", "Short"]].map(([k, l]) => `<button data-v="${k}" class="${f.dir === k ? "on" : ""}">${l}</button>`).join("")}</div>
    </div>
    <div id="tm_find" class="row" style="margin-top:6px" hidden><input id="tm_q" placeholder="e.g. RELIANCE" style="flex:1;min-width:160px"><button id="tm_go">Find</button></div>
    <div id="tm_found" class="results small">${(TM.found || []).map((x) => `<button data-key="${esc(x.ind_key)}" data-name="${esc(x.name)}">${esc(x.name)}</button>`).join("")}</div>
    <div class="form">
      <div><label for="tm_inst">Instrument</label><select id="tm_inst">${[["both", "Futures and option"], ["future", "Futures"], ["option", "Option"], ["equity", "Shares (delivery)"]].map(([v, l]) => tmOpt(v, l, f.inst)).join("")}</select></div>
      ${eq ? `<div><label for="tm_qty">Shares</label><input id="tm_qty" type="number" min="1" inputmode="numeric" value="${esc(f.qty)}"></div>`
        : `<div><label for="tm_slots">Lots</label><input id="tm_slots" type="number" min="1" inputmode="numeric" value="${esc(f.slots)}"></div>`}
      <div><label for="tm_sexp">Option expiry</label><select id="tm_sexp" ${eq || f.inst === "future" ? "disabled" : ""}>${exps.slice(0, 8).map((e) => tmOpt(e, tmDay(e) + " " + e.slice(0, 4), f.sexpiry)).join("") || "<option value=''>auto</option>"}</select></div>
      <div><label for="tm_spaths">Paths</label><select id="tm_spaths">${[[5000, "5,000 (quick)"], [10000, "10,000"], [20000, "20,000"]].map(([v, l]) => tmOpt(v, l, f.spaths)).join("")}</select></div>
    </div>
    <details style="margin-top:8px"><summary class="small muted">Your own levels, horizon and options</summary>
      <div class="form" style="margin-top:8px">
        <div><label for="tm_sl">Stop-loss</label><input id="tm_sl" type="number" step="0.05" inputmode="decimal" placeholder="the chart's" value="${esc(f.sl)}"></div>
        <div><label for="tm_tp1">TP1</label><input id="tm_tp1" type="number" step="0.05" inputmode="decimal" placeholder="1R" value="${esc(f.tp1)}"></div>
        <div><label for="tm_tp2">TP2</label><input id="tm_tp2" type="number" step="0.05" inputmode="decimal" placeholder="2R" value="${esc(f.tp2)}"></div>
        <div><label for="tm_limit">Limit entry (day order)</label><input id="tm_limit" type="number" step="0.05" inputmode="decimal" placeholder="next open" value="${esc(f.limit)}"></div>
        <div><label for="tm_hz">Follow a trade for (bars)</label><input id="tm_hz" type="number" min="5" max="120" value="${esc(f.horizon)}"></div>
        <div><label for="tm_siv">IV override, % a year</label><input id="tm_siv" type="number" step="0.1" inputmode="decimal" placeholder="the chain's" value="${esc(f.siv)}"></div>
      </div>
      <div class="row" style="margin-top:8px;gap:16px"><label class="row small" style="gap:6px;color:var(--ink)"><input type="checkbox" id="tm_drift" ${f.drift ? "checked" : ""}> keep the history's trend in the bootstrap</label>
        <label class="row small" style="gap:6px;color:var(--ink)"><input type="checkbox" id="tm_grid" ${f.grid ? "checked" : ""}> show how other stops and targets would do</label></div>
    </details>
    <div class="row" style="margin-top:10px"><button class="primary" id="tm_run" ${TM.busy ? "disabled" : ""}>${TM.busy ? "Replaying…" : "Replay the trade"}</button>
      ${TM.busy ? `<button id="tm_cancel">Stop</button>` : ""}<span class="small muted tm-status" id="tm_status">${esc(TM.status)}</span></div>
    <div class="tm-prog" ${TM.busy ? "" : "hidden"}><div id="tm_prog" style="width:${Math.round(100 * TM.progress)}%"></div></div>
    ${TM.err ? `<div class="small down" style="margin-top:6px">${esc(TM.err)}</div>` : ""}`;
}
function tmRender() {
  tmLead();
  document.querySelectorAll("#tmMode button").forEach((b) => b.classList.toggle("on", b.dataset.m === TM.mode));
  $("tmCtl").innerHTML = TM.mode === "intra" ? tmIntraCtl() : tmSwingCtl();
  $("tmOut").innerHTML = "";
  const o = TM.out[TM.mode];
  if (o) tmShow(o);
}
const tmStatus = (msg) => { TM.status = msg; const el = $("tm_status"); if (el) el.textContent = msg; };
const tmProg = (p, label) => { TM.progress = p; const el = $("tm_prog"); if (el) el.style.width = Math.round(100 * p) + "%"; if (label) tmStatus("Replaying " + label + "…"); };

// ----- loading what a mode needs
async function tmPrepare() {
  if (!mcp) { TM.err = S.quoteErr ? S.quoteErr.long : ""; TM.status = S.quoteErr ? "" : "Connecting to INDmoney…"; tmRender(); return; }
  TM.err = "";
  try {
    if (TM.mode === "intra") {
      const f = TM.f, key = TMSYM[f.ix].key;
      if (!f.pos) await tmLoadIntraChain(f.ix, f.expiry || null);
      tm5m(key).then((s) => { TM.sessions = s; }).catch(() => {});
    } else {
      const f = TM.f, sy = f.sym === (TM.other && TM.other.sym) ? TM.other : TMSYM[f.sym];
      if (sy && f.inst !== "equity" && f.inst !== "future") {
        const ch0 = await tmChain(sy.key, f.sexpiry || null);
        if (!f.sexpiry) {
          const want = tmSwingExpiry(ch0);
          f.sexpiry = want;
          TM.ch = want === ch0.expiry ? ch0 : await tmChain(sy.key, want);
        } else TM.ch = ch0;
      } else TM.ch = null;
    }
  } catch (e) { TM.err = tmErr(e); }
  tmRender();
}
function tmSwingExpiry(ch) {      // the first expiry at least 12 days out, so a swing can play out inside it
  const today = ist().date, list = ch.expiry_list || [];
  const ok = list.find((e) => (Date.parse(TMa.expiryIso(e)) - Date.parse(today)) / 864e5 >= 12);
  return ok || list[list.length - 1] || ch.expiry;
}
async function tmLoadIntraChain(ix, expiry) {
  const ch = await tmChain(TMSYM[ix].key, expiry);
  TM.exp[ix] = ch.expiry_list || [ch.expiry];
  if (!expiry) { const want = defaultExpiry(TM.exp[ix]); if (want && want !== ch.expiry) return tmLoadIntraChain(ix, want); }
  TM.ch = ch; TM.f.expiry = ch.expiry;
  tmFixStrike();
  return ch;
}
function tmFixStrike() {            // the strike closest to a 0.50 delta that is quoted both ways
  const ch = TM.ch, f = TM.f; if (!ch) return;
  const k = f.side === "CE" ? "ce" : "pe";
  const ok = ch.strikes.filter((r) => r[k] && r[k].ltp && r[k].bid && r[k].ask);
  if (!ok.length) return;
  if (f.strike && ok.some((r) => String(r.strike) === String(f.strike))) return;
  const best = ok.reduce((a, b) => Math.abs(Math.abs(b[k].delta || 0.5) - 0.5) < Math.abs(Math.abs(a[k].delta || 0.5) - 0.5) ? b : a);
  f.strike = best.strike;
}

// ----- the runs
async function tmRun() {
  if (TM.busy) return;
  TM.busy = true; TM.cancel = false; TM.err = ""; TM.progress = 0; TM.status = "Fetching live data…";
  const id = ++TM.runId;
  tmRender();
  let ok = false;
  try {
    const opts = { onProgress: (p, l) => { if (id === TM.runId) tmProg(p, l); }, isCancelled: () => TM.cancel || id !== TM.runId };
    if (TM.mode === "intra") await tmRunIntra(opts, id); else await tmRunSwing(opts, id);
    ok = true;
  } catch (e) {
    if (e && e.message === "cancelled") TM.status = "Stopped."; else { TM.err = tmErr(e); TM.status = ""; }
  }
  TM.busy = false;
  if (ok) TM.status = "";
  tmSave(); tmRender();
  if (ok) { try { $("tmOut").scrollIntoView({ block: "start", behavior: "smooth" }); } catch {} }
}
async function tmRunIntra(opts, id) {
  const f = TM.f, key = TMSYM[f.ix].key, pos = f.pos;
  const ch = await tmChain(key, f.expiry || null, true);
  const [sessions, daily] = await Promise.all([tm5m(key), tmDaily(key)]);
  TM.ch = ch; TM.sessions = sessions;
  const t = ist(), live = marketOpen();
  if (live) { const ct = TMa.chainTimeMs(ch); if (ct && Date.now() - ct > 20 * 60e3) throw new Error("The quote is " + Math.round((Date.now() - ct) / 60e3) + " minutes old: refresh and try again."); }
  const base = { sessions, chain: ch, daily, nowMs: Date.now(), holidays: tmHol(), rules: tmRules(), paths: +f.paths, seed: 7, volMode: f.volMode,
    ivPct: f.iv ? +f.iv : null, entryAt: f.entryAt, ...opts };
  tmStatus("Replaying…");
  const rep = pos
    ? await TMi.analyseIntraday({ ...base, mode: "position", derivativeId: pos.id, lots: pos.remainingLots, position: { entry: pos.entry, stop: pos.stop, t1Done: pos.t1Done, remainingLots: pos.remainingLots } })
    : await TMi.analyseIntraday({ ...base, side: f.side, strike: +f.strike, lots: Math.max(1, +f.lots || 1) });
  if (id !== TM.runId) return;
  rep.ctx = { barRange: tmBarRange(sessions), day: t.date, room: roomLeft(dayTrades(t.date)), ix: f.ix, at: Date.now() };
  TM.out.intra = rep;
}
async function tmRunSwing(opts, id) {
  const f = TM.f, sy = TM.other && f.sym === TM.other.sym ? TM.other : TMSYM[f.sym];
  if (!sy) throw new Error("Choose a symbol.");
  const daily = await tmDaily(sy.key);
  let ch = null;
  if (f.inst !== "equity" && f.inst !== "future") {
    try { ch = await tmChain(sy.key, f.sexpiry || null, true); } catch (e) { if (f.inst === "option") throw e; }
  }
  tmStatus("Replaying…");
  const rep = await TMa.analyseSwing(daily, {
    direction: f.dir === "auto" ? null : f.dir, instrument: f.inst, symbol: f.sym, lots: Math.max(1, +f.slots || 1), qty: +f.qty || 1, paths: +f.spaths, horizon: +f.horizon || 40,
    sl: f.sl ? +f.sl : null, tp1: f.tp1 ? +f.tp1 : null, tp2: f.tp2 ? +f.tp2 : null, entry: f.limit ? +f.limit : null, iv: f.siv ? +f.siv : null,
    keepDrift: !!f.drift, grid: !!f.grid, chain: ch, holidays: tmHol(), lot: sy.lot, seed: 7, ...opts });
  if (id !== TM.runId) return;
  rep.ctx = { at: Date.now(), sym: f.sym };
  TM.out.swing = rep;
}
function tmBarRange(sessions) {
  const r = [];
  for (const s of sessions || []) for (let j = 3; j < s.bars.length; j++) { const b = s.bars[j]; if (b) r.push(b.h - b.l); }
  if (r.length < 20) return null;
  r.sort((a, b) => a - b);
  return r[Math.floor(r.length / 2)];
}
function tmShow(o) { $("tmOut").innerHTML = TM.mode === "intra" ? tmIntraHTML(o) : tmSwingHTML(o); }

// ----- shared bits of the results
const TM_K = { stop: "k-stop", trail_out: "k-lock", t1_be: "k-lock", t1_trail: "k-lock", time: "k-time", t1_all: "k-win", t2: "k-win" };
const TM_ORDER = ["stop", "trail_out", "t1_be", "t1_trail", "time", "t1_all", "t2"];
const TM_NAME = Object.fromEntries(TMi.CLASSES);
const tmWord = (v) => { const h = 1.96 * v.se; return v.expectancy - h > 0 ? "pos" : v.expectancy + h < 0 ? "neg" : "zero"; };
const tmWordTxt = { pos: "positive", neg: "negative", zero: "indistinguishable from zero" };
function tmLegend() {
  return `<div class="tm-legend"><span><i class="k-stop"></i>stopped, nothing booked</span><span><i class="k-lock"></i>stopped by the ladder or at entry</span><span><i class="k-win"></i>target taken</span><span><i class="k-time"></i>out at 15:15</span></div>`;
}
function tmCopy(text) {
  const done = () => tmStatus("Copied.");
  try { navigator.clipboard.writeText(text).then(done, () => tmStatus("Select the summary and copy it by hand.")); } catch { tmStatus("Select the summary and copy it by hand."); }
}

// ----- intraday results
function tmIntraHTML(rep) {
  const m = rep.meta, lv = rep.levels, pos = m.mode === "position", names = { implied: "Implied", bootstrap: "Bootstrap", replay: "Recent sessions" };
  const mc = { implied: "m1", bootstrap: "m2", replay: "m3" };
  const keys = ["implied", "bootstrap", "replay"].filter((k) => rep.models[k] && rep.models[k].variants);
  const sims = keys.filter((k) => k !== "replay");
  const D = (k) => rep.models[k].variants.desk;
  const title = `${m.underlying || ""} ${m.strike} ${m.side === "CE" ? "Call" : "Put"} · ${m.lots} lot${m.lots > 1 ? "s" : ""} (${m.qty})`;
  const word = (k) => tmWord(D(k));
  const chip = (k) => `<span class="tm-chip ${mc[k]}">${names[k]}</span>`;
  const by = (fn) => keys.map((k) => `<i>${fn(D(k), k)}</i> ${names[k]}${k === "replay" ? " (n=" + rep.models[k].n + ")" : ""}`).join("<br>");
  const unit = pos ? "vs selling now" : "";
  // the verdict, in plain words
  const words = sims.map(word);
  let vcls = "mix", head, body;
  if (!sims.length) { head = "Only the recent sessions could be replayed"; body = "Too thin to judge: three sessions are an illustration, not a sample."; }
  else if (words.every((w) => w === "neg")) { vcls = "neg"; head = pos ? "Following the plan is expected to do worse than selling now" : "On average this plan loses money after charges"; body = sims.map((k) => `${names[k]}: ${inr(D(k).expectancy)} (${tmWordTxt.neg})`).join(" · ") + ". " + (pos ? "" : "Both models agree. Any edge has to come from your entry, not from these exits."); }
  else if (words.every((w) => w === "pos")) { vcls = "pos"; head = pos ? "Following the plan is expected to do better than selling now" : "On average this plan makes money in the models"; body = sims.map((k) => `${names[k]}: ${inr(D(k).expectancy)} (${tmWordTxt.pos})`).join(" · ") + ". That rests on the volatility each model assumes: see how sure we are below."; }
  else { head = words.includes("zero") && !words.includes("neg") && !words.includes("pos") ? "No average result this replay can tell from zero" : "The models do not agree"; body = sims.map((k) => `${names[k]}: ${inr(D(k).expectancy)} ± ${inrA(1.96 * D(k).se)} (${tmWordTxt[word(k)]})`).join(" · ") + ". They differ in the volatility they assume, so the answer depends on which pace the market keeps."; }
  const tiles = `<div class="grid tm-tiles" style="margin-top:8px">
      <div class="stat"><span>${pos ? "Plan beats selling now" : "Chance of a profit"}</span><b>${pc0(D(keys[0]).p_profit)}${keys.length > 1 ? "–" + pc0(D(keys[1]).p_profit) : ""}</b><span class="by">${by((v) => pc0(v.p_profit))}</span></div>
      <div class="stat"><span>Average result ${unit}</span><b class="${cls(D(keys[0]).expectancy)}">${inr(D(keys[0]).expectancy)}</b><span class="by">${by((v) => inr(v.expectancy))}</span></div>
      <div class="stat"><span>${pos ? "Reaches T1" : "Reaches T1 (+" + rep.rules.tp1Pct + "%)"}</span><b>${pc0(D(keys[0]).p_hit_t1)}</b><span class="by">${by((v) => pc0(v.p_hit_t1))}</span></div>
      ${pos ? "" : `<div class="stat"><span>Loses ≥ ${inrA(rep.rules.dayStop)} (day stop)</span><b class="${D(keys[0]).p_day_stop > 0.1 ? "down" : ""}">${pc0(D(keys[0]).p_day_stop)}</b><span class="by">${by((v) => pc0(v.p_day_stop))}</span></div>`}
    </div>`;
  // what stands out
  const notes = [];
  const ipts = lv.risk_inr / m.qty / Math.max(Math.abs(m.delta), 0.05);
  if (!pos) {
    const br = rep.ctx && rep.ctx.barRange;
    notes.push(`The ${rep.rules.slPct}% stop is ${inrA((m.entry_plan - lv.sl))} on the option, about <b>${Math.round(ipts)} ${esc(m.underlying)} points</b> at Δ${Math.abs(m.delta).toFixed(2)}${br ? `; a typical 5-minute bar spans ${Math.round(br)} points, so ordinary noise reaches it` : ""}.`);
    const fair = rep.rules.slPct / (rep.rules.slPct + rep.rules.tp1Pct);
    const d0 = D(keys[0]);
    notes.push(`T1 is reached in ${pc0(d0.p_hit_t1)} of paths${keys.length > 1 ? " (" + pc0(D(keys[1]).p_hit_t1) + " in the next model)" : ""}. A fair game with these levels would reach it ${pc0(fair)} of the time; the bid-ask spread (${m.half_spread ? "₹" + (2 * m.half_spread).toFixed(2) : "—"} here), the charges and the 15:15 exit take the rest.`);
  }
  notes.push(`If the index did not move at all, time alone would take the option from ${px(m.bid_now)} to ${px(m.flat_at_exit)} by 15:15: <b>${m.flat_decay_pct.toFixed(1)}%</b> (${inr(m.flat_decay_inr)} on ${m.qty}).`);
  if (m.realised_ann_pct) {
    const r = m.realised_vs_implied;
    notes.push(`The option prices ${esc(m.underlying)} at <b>${m.sigma_calendar_pct.toFixed(1)}%</b> a year; the recent 5-minute bars ran at <b>${m.realised_ann_pct.toFixed(1)}%</b> (${r.toFixed(1)}×). The Bootstrap model uses ${m.vol_mode === "implied" ? "the implied pace" : "the recent pace"}, the Implied model the option's.`);
  }
  if (m.start.kind === "open") notes.push(`The chain was struck before the session. From the close to the open the paths first take an overnight gap (${(100 * m.phi).toFixed(0)}% of a day's variance is overnight in this chart's own history), so the entry price varies path by path. Levels are set from each path's own fill.`);
  const rt = TMa.fnoCosts("buy", m.entry_plan * m.qty, "OPT", m.exchange).total + TMa.fnoCosts("sell", m.entry_plan * m.qty, "OPT", m.exchange).total;
  notes.push(`Charges for a round trip at this size are about <b>${inrA(rt)}</b> (brokerage on each order, STT on the sale, exchange fees, GST, stamp duty).`);
  if (rep.ctx && rep.ctx.room != null && !pos) notes.push(`Room left to today's ${inrA(rep.rules.dayStop)} stop: <b>${inrA(rep.ctx.room)}</b>. The plan's loss at the stop is about ${inrA(lv.risk_inr)} plus charges.`);
  for (const n of rep.notes || []) notes.push(esc(n));
  const head1 = `<div class="card">
      <div class="row" style="justify-content:space-between"><h2 style="margin:0">${esc(title)}</h2><span class="small muted">${esc(m.startLabel)}</span></div>
      <div class="small muted" style="margin-top:2px">${pos ? `Entry ${px(m.position.entry)}, stop ${px(m.position.stop)}${m.position.t1Done ? ", T1 done" : ""}; the option sells at ${px(m.bid_now)} now.` :
        `Entry about ${px(m.entry_plan)} (the ask) · SL ${px(lv.sl)} · T1 ${px(lv.t1)} · T2 ${px(lv.t2)} · outlay ${inrA(m.outlay)} · ${esc(m.underlying)} ${px(m.spot0)}, ${m.days_to_expiry === 0 ? "expires today" : m.days_to_expiry + " day" + (m.days_to_expiry > 1 ? "s" : "") + " to expiry"}`}</div>
      ${tiles}
      <div class="tm-verdict ${vcls}"><b>${esc(head)}</b>${esc(body)}</div>
      <ul class="tm-notes">${notes.map((n) => `<li>${n}</li>`).join("")}</ul>
      <div class="row" style="margin-top:8px"><button class="mini" id="tm_copy">Copy a summary</button></div>
    </div>`;
  // how it ends
  const rowsFor = (variant) => keys.map((k) => {
    const v = rep.models[k].variants[variant];
    return { label: (k === "replay" ? "Recent (n=" + rep.models[k].n + ")" : names[k]), segs: TM_ORDER.filter((c) => v.classes[c]).map((c) => ({ p: v.classes[c].p, cls: TM_K[c], label: TM_NAME[c] })) };
  });
  const allCls = TM_ORDER.filter((c) => keys.some((k) => D(k).classes[c]));
  const ends = `<div class="card">
      <h2>How the desk plan ends</h2>
      <div class="tm-chart">${TMc.stackBars(rowsFor("desk"), "How the desk plan ends in each model")}</div>${tmLegend()}
      <div class="tbl" style="margin-top:8px"><table class="tm-table"><tr><th>Outcome</th>${keys.map((k) => `<th class="num">${names[k]}</th>`).join("")}</tr>
        ${allCls.map((c) => `<tr><td>${esc(TM_NAME[c])}</td>${keys.map((k) => {
          const x = D(k).classes[c];
          return x ? `<td class="num">${pc1(x.p)}<small>${pc0(x.ci95[0])}–${pc0(x.ci95[1])} · ${inr(x.avg)} · ${Math.round(x.minutes)} min</small></td>` : `<td class="num muted">—</td>`;
        }).join("")}</tr>`).join("")}
        <tr class="sub"><td>Average · chance of a profit</td>${keys.map((k) => `<td class="num">${inr(D(k).expectancy)} ± ${inrA(1.96 * D(k).se)}<small>${pc1(D(k).p_profit)}</small></td>`).join("")}</tr>
        <tr class="sub"><td>Worst 5% · median · best 5%</td>${keys.map((k) => `<td class="num">${inr(D(k).p5)}<small>${inr(D(k).p50)} · ${inr(D(k).p95)}</small></td>`).join("")}</tr>
        <tr class="sub"><td>Held (median · 90%)</td>${keys.map((k) => `<td class="num">${Math.round(D(k).minutes_median)} · ${Math.round(D(k).minutes_p90)} min</td>`).join("")}</tr>
        <tr class="sub"><td>Option's worst dip (median · 90%)</td>${keys.map((k) => `<td class="num">−${pc0(D(k).mae_median)} · −${pc0(D(k).mae_p90)}</td>`).join("")}</tr></table></div>
      <div class="tm-fine" style="margin-top:6px">${keys.map((k) => `<div>${chip(k)} ${esc(rep.models[k].what || "")}</div>`).join("")}</div>
    </div>`;
  const charts = `<div class="card" id="tmCharts">${tmIntraCharts(rep)}</div>`;
  // the exits compared
  const vk = rep.variants.map((x) => x.key);
  const exps = (k, v) => rep.models[k].variants[v].expectancy;
  const variants = `<div class="card"><h2>Other ways to exit</h2>
      <div class="small muted" style="margin-bottom:6px">The same paths, the same entry, a different exit rule. Differences smaller than the ± are noise.</div>
      <div class="tbl"><table class="tm-table"><tr><th>Exit rule</th>${keys.map((k) => `<th class="num">${names[k]}</th>`).join("")}<th class="num">Profit<small>${names[keys[0]]}</small></th></tr>
      ${vk.map((v) => {
        const row = rep.variants.find((x) => x.key === v);
        return `<tr><td>${esc(row.label)}<small>${esc(row.note)}</small></td>${keys.map((k) => {
          const vv = rep.models[k].variants[v], dd = D(k);
          const diff = vv.expectancy - dd.expectancy, tol = 1.96 * Math.hypot(vv.se, dd.se) * 0.7;
          return `<td class="num">${inr(vv.expectancy)}<small>± ${inrA(1.96 * vv.se)}${v !== "desk" && Math.abs(diff) > tol ? " · " + (diff > 0 ? "better" : "worse") + " than desk" : ""}</small></td>`;
        }).join("")}<td class="num">${pc0(rep.models[keys[0]].variants[v].p_profit)}</td></tr>`;
      }).join("")}</table></div>
      ${m.lots < 2 ? `<div class="small muted" style="margin-top:6px">With one lot the desk plan cannot book half: it takes the whole position out at T1, which is why it matches "All out at T1".</div>` : ""}
    </div>`;
  // how sure
  const sens = (rep.sensitivity || []);
  const sure = `<div class="card"><h2>How sure is this?</h2>
      <div class="small" style="max-width:74ch">The paths are drawn, not known. The biggest unknown is how fast the market actually moves, so here is the desk plan on the Implied model with the market moving at a multiple of the option's pace (the option is still bought at today's quote):</div>
      ${sens.length ? TMc.heat(["Average result", "Chance of a profit"], sens.map((s) => s.mult.toFixed(2).replace(/0$/, "") + "×" + (s.mult === 1 ? " (implied)" : "")),
        [sens.map((s) => ({ text: inr(s.expectancy), v: s.expectancy })), sens.map((s) => ({ text: pc0(s.p_profit), v: s.p_profit - 0.5 }))], "market speed") : ""}
      <ul class="tm-notes">
        <li>Each probability carries a 95% interval from the number of paths (shown under it above). It does not include the models being wrong.</li>
        <li>The recent sessions row replays ${rep.models.replay && rep.models.replay.n ? rep.models.replay.independent + " real sessions' 5-minute tapes" : "no session"}: it shows what really happened on those days, with the option repriced, but the paths overlap and ${rep.models.replay && rep.models.replay.n ? "two or three sessions" : "it"} cannot support a probability.</li>
        <li>Inside a 5-minute bar the order of the high and low is unknown: a bar that touches both the stop and a target counts as the stop.</li>
        <li>Stops and targets are checked on the price you could sell at (the bid) and fill there; stop-loss orders can slip further in a fast market. Charges are counted per order.</li>
      </ul>
      <details style="margin-top:6px"><summary class="small muted">How this was worked out</summary>
        <div class="tm-fine" style="margin-top:6px">
          <p>Volatility is solved from the quote's mid on a session clock: an option's IV is quoted on calendar time, but variance arrives mostly in the session (a U shape: ${esc(m.profile)}) and the rest overnight as the opening gap. Solved on that clock the chain's ${m.sigma_calendar_pct.toFixed(1)}% a year is ${m.sigma_clock_pct.toFixed(1)}%${m.iv_overridden ? " (overridden)" : ""}. The index paths spend variance on the same clock, so the option's price is a martingale along them: no drift is invented (the tests check this).</p>
          <p>Implied: Gaussian 5-minute bars, each bridged for its high and low. Bootstrap: real 5-minute bars (their shapes, fat tails and clustering) reshuffled in blocks of about 30 minutes with the drift taken out. Recent sessions: the actual tapes of the last full sessions, started up to 30 minutes either side of this start, scaled to today's level.</p>
          <p>The option is bought at the ask and sold at the bid (half the quoted spread each way, held constant); the forward keeps today's basis to the index; Black-76 on the NSE/BSE calendar; European exercise; nothing is held past 15:15.</p>
        </div>
      </details>
    </div>`;
  return head1 + ends + charts + variants + sure;
}
function tmIntraCharts(rep) {
  const m = rep.meta, lv = rep.levels, keys = ["implied", "bootstrap", "replay"].filter((k) => rep.models[k] && rep.models[k].variants);
  const names = { implied: "Implied", bootstrap: "Bootstrap", replay: "Recent sessions" };
  if (!keys.includes(TM.view)) TM.view = keys[0];
  const M = rep.models[TM.view], v = M.variants.desk, pos = m.mode === "position";
  const start = TMi.barOf(m.barsFrom), xlab = (i) => TMi.hm(start + i);
  const levels = pos ? [{ y: m.position.stop, label: "stop " + m.position.stop.toFixed(1), cls: "sl" }, { y: lv.t1, label: "T1 " + lv.t1.toFixed(1), cls: "tp" }, { y: lv.t2, label: "T2 " + lv.t2.toFixed(1), cls: "tp" }, { y: m.position.entry, label: "entry " + m.position.entry.toFixed(1), cls: "en" }]
    : [{ y: lv.sl, label: "SL " + lv.sl.toFixed(1), cls: "sl" }, { y: lv.t1, label: "T1 " + lv.t1.toFixed(1), cls: "tp" }, { y: lv.t2, label: "T2 " + lv.t2.toFixed(1), cls: "tp" }, { y: m.entry_plan, label: "entry " + m.entry_plan.toFixed(1), cls: "en" }];
  const seg = `<div class="seg" id="tm_view">${keys.map((k) => `<button data-v="${k}" class="${TM.view === k ? "on" : ""}">${names[k]}</button>`).join("")}</div>`;
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">What you make or lose</h2>${seg}</div>
    <div class="small muted" style="margin:4px 0 0">${esc(M.what || "")}</div>
    <div class="tm-chart">${TMc.histogram(v.pnl, { aria: "Distribution of the desk plan's result in rupees, " + names[TM.view], fmt: (x) => inr(x) })}</div>
    <h3>The option's price through the session if you simply held it</h3>
    <div class="small muted">Sellable (bid) price, the middle 50% and 90% of paths, with your levels. ${pos ? "" : "Levels are for an entry at the plan price."}</div>
    <div class="tm-chart">${M.fan ? TMc.fan(M.fan, levels, xlab, { aria: "The option's bid price through the session, " + names[TM.view], fmt: (x) => "₹" + Math.round(x) }) : '<div class="small muted">No paths to draw.</div>'}</div>`;
}

// ----- swing results
function tmSwingHTML(rep) {
  const tr = rep.trade, ch = rep.chart, ms = rep.methods, sy = rep.symbol || "";
  const cols = ["replay", "baseline", "bootstrap", "implied"].filter((k) => ms[k] && ms[k].n);
  const hd = { replay: (s) => `Past occurrences · ${s.n} (${s.independent} indep.)`, baseline: (s) => `Every bar · ${s.n} (${s.independent} indep.)`, bootstrap: (s) => `Bootstrap · ${s.n.toLocaleString("en-IN")}`, implied: (s) => `Implied IV · ${s.n.toLocaleString("en-IN")}` };
  const showAfter = tr.frac < 1;
  const sgn = (x) => (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(1);
  const fut = rep.instrument === "equity" ? "equity_inr" : "futures_inr_per_lot";
  const unit = rep.instrument === "equity" ? `the ${rep.qty || 1} shares` : "a lot";
  const sims = ["bootstrap", "implied"].filter((k) => ms[k] && ms[k].entered);
  const lines = rep.verdict || [];
  const worst = lines.find((l) => l.kind === "bootstrap") || lines[0];
  let vcls = "mix", vhead = "No result to judge";
  if (worst) { vcls = worst.word === "positive" ? "pos" : worst.word === "negative" ? "neg" : "mix"; vhead = worst.word === "positive" ? "The bootstrap finds a positive average" : worst.word === "negative" ? "The bootstrap finds a negative average" : "The bootstrap cannot tell the average from zero"; }
  const header = `<div class="card">
      <div class="row" style="justify-content:space-between"><h2 style="margin:0">${esc(sy)} ${tr.direction.toUpperCase()}${tr.setup ? " · " + esc(tr.setup.replace(/_/g, " ")) : ""}${tr.ok ? "" : ` <span class="pill stale">not worth it</span>`}</h2><span class="small muted">on the ${esc(ch.date)} close</span></div>
      <div class="grid tm-tiles" style="margin-top:8px">
        <div class="stat"><span>Entry</span><b>${px(tr.limit || tr.entry_plan)}</b><span>${tr.limit ? "day limit" : "next open"}</span></div>
        <div class="stat"><span>Stop-loss</span><b class="down">${px(tr.stop)}</b><span>${tr.risk.toFixed(1)} pts · ${(tr.stop_atr || 0).toFixed(2)} ATR</span></div>
        <div class="stat"><span>TP1</span><b class="up">${px(tr.tp1)}</b><span>${tr.tp1_r.toFixed(2)}R</span></div>
        <div class="stat"><span>TP2</span><b class="up">${px(tr.tp2)}</b><span>${tr.tp2_r.toFixed(2)}R</span></div>
      </div>
      <div class="small muted" style="margin-top:6px">${tr.why ? esc(tr.why) + ". " : ""}${tr.frac >= 1 ? "At TP1 the whole position exits (one lot cannot be split)." : tr.frac > 0 ? `At TP1 ${Math.round(100 * tr.frac)}% is booked and the stop moves to entry;` : "Nothing is booked at TP1;"}${tr.frac < 1 ? ` then a trailing stop ${tr.rules.trail_atr} ATR behind the best price (${tr.rules.trail_after_tp2_atr} ATR after TP2).` : ""} Time stop ${tr.rules.time_stop_bars} bars, followed for up to ${rep.horizon_bars}; charges ${tr.cost_points} points a round trip. With no edge, P(TP1 before SL) = ${pc0(tr.no_edge_p_tp1)}, P(TP2 before SL) = ${pc0(tr.no_edge_p_tp2)}.</div>
      ${tr.option ? `<div class="small" style="margin-top:6px"><b>Option leg:</b> ${esc(tr.option.expiry)} ${tr.option.strike} ${tr.option.kind} at ₹${tr.option.entry_premium} (Δ${tr.option.delta}, IV ${tr.option.iv_pct}%, spread ${tr.option.spread_pct}%), lot ${tr.option.lot_size}${tr.option.bars_to_expiry ? ", " + tr.option.bars_to_expiry + " sessions to expiry" : ""}.</div>` : ""}
      ${worst ? `<div class="tm-verdict ${vcls}"><b>${esc(vhead)}</b>${lines.map((l) => esc(l.text)).join("<br>")}</div>` : ""}
      ${(rep.notes || []).length ? `<ul class="tm-notes">${rep.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <div class="small muted" style="margin-top:8px;max-width:74ch">About 91% of individual F&amp;O traders lost money in FY2024-25 (SEBI's study; 87.7% in FY2025-26). These are counts from models, with the models' intervals: they are not a forecast and not a sure thing.</div>
      <div class="row" style="margin-top:8px"><button class="mini" id="tm_copy">Copy a summary</button></div>
    </div>`;
  const chartRead = `<div class="card"><h2>The chart</h2>
      <div class="grid tm-tiles">
        <div class="stat"><span>Close</span><b>${px(ch.close)}</b><span>${ch.change_pct["1"] >= 0 ? "+" : ""}${ch.change_pct["1"]}% · 5d ${ch.change_pct["5"]}% · 20d ${ch.change_pct["20"]}%</span></div>
        <div class="stat"><span>Trend</span><b>${esc(ch.trend)}</b><span>EMA20 ${ch.ema20} vs EMA50 ${ch.ema50}, ${esc(ch.ema50_slope)} · Supertrend ${esc(ch.supertrend)}</span></div>
        <div class="stat"><span>ADX · RSI</span><b>${ch.adx} · ${ch.rsi}</b><span>structure: swing low ${ch.swing_low == null ? "—" : ch.swing_low}, high ${ch.swing_high == null ? "—" : ch.swing_high}</span></div>
        <div class="stat"><span>Volatility</span><b>ATR ${ch.atr}</b><span>${ch.atr_pct}% of price · ${ch.atr_percentile}th pctile of the year · 20-day realised ${ch.realized_vol_20d_pct}%</span></div>
        <div class="stat"><span>${ch.bars >= 245 ? "52-week" : ch.bars + "-bar"} range</span><b>${ch.from_high_pct}%</b><span>from the high ${px(ch.high_52w)} (low ${px(ch.low_52w)})</span></div>
        ${ch.chain ? `<div class="stat"><span>Options ${esc(ch.chain.expiry)}</span><b>IV ${ch.chain.atm_iv_pct || "—"}%</b><span>1-SD move ±${ch.chain.expected_move_1sd || "—"} · PCR ${ch.chain.pcr_oi || "—"} · max pain ${ch.chain.max_pain || "—"} · OI wall ${ch.chain.resistance || "—"} / ${ch.chain.support || "—"}</span></div>` : ""}
      </div></div>`;
  const allKeys = Object.keys(TMa.OUTCOMES.reduce((a, [k]) => (a[k] = 1, a), {})).filter((k) => cols.some((c) => ms[c].outcomes && ms[c].outcomes[k]));
  const label = Object.fromEntries(TMa.OUTCOMES);
  const swCls = { gap: "k-gap", nofill: "k-gap", stop: "k-stop", time: "k-lock", tp1_all: "k-win", tp1_be: "k-lock", tp1_trail: "k-lock", tp2_trail: "k-win", horizon: "k-time", expiry: "k-time" };
  const rowsStack = cols.map((c) => ({ label: { replay: "Past occurrences", baseline: "Every bar", bootstrap: "Bootstrap", implied: "Implied IV" }[c],
    segs: allKeys.filter((k) => ms[c].outcomes[k]).map((k) => ({ p: ms[c].outcomes[k].p, cls: swCls[k], label: label[k] })) }));
  const table = `<div class="card"><h2>How it ends</h2>
      <div class="tm-chart">${TMc.stackBars(rowsStack, "How the trade ends, by method")}</div>
      <div class="tm-legend"><span><i class="k-stop"></i>stop-loss</span><span><i class="k-lock"></i>time stop, or stopped at entry / trailed after TP1</span><span><i class="k-win"></i>TP1 taken, or trailed out after TP2</span><span><i class="k-time"></i>still open</span><span><i class="k-gap"></i>no entry</span></div>
      <div class="tbl" style="margin-top:8px"><table class="tm-table"><tr><th>Outcome</th>${cols.map((c) => `<th class="num">${esc(hd[c](ms[c]))}</th>`).join("")}</tr>
      ${allKeys.filter((k) => !(tr.frac >= 1 && ["tp1_be", "tp1_trail", "tp2_trail"].includes(k))).map((k) => `<tr><td>${esc(label[k])}${k === "stop" ? "<small>of which the open gapped through</small>" : ""}</td>${cols.map((c) => {
        const o = ms[c].outcomes[k];
        return o ? `<td class="num">${pc1(o.p)}<small>${pc0(o.ci95[0])}–${pc0(o.ci95[1])}${o.avg_r != null ? " · " + rR(o.avg_r) : ""}${k === "stop" && o.gapped_through ? " · " + pc1(o.gapped_through) + " gapped" : ""}</small></td>` : `<td class="num muted">0%</td>`;
      }).join("")}</tr>`).join("")}
      <tr class="sub"><td>Average per trade (after charges)</td>${cols.map((c) => `<td class="num">${ms[c].entered ? rR3(ms[c].expectancy_r) + (ms[c].expectancy_se ? `<small>± ${(1.96 * ms[c].expectancy_se).toFixed(3)}</small>` : "") : "—"}</td>`).join("")}</tr>
      <tr class="sub"><td>Chance of a profit</td>${cols.map((c) => `<td class="num">${ms[c].entered ? pc1(ms[c].p_profit) + `<small>${pc0(ms[c].p_profit_ci95[0])}–${pc0(ms[c].p_profit_ci95[1])}</small>` : "—"}</td>`).join("")}</tr>
      <tr class="sub"><td>Average win · loss</td>${cols.map((c) => `<td class="num">${ms[c].entered ? "+" + ms[c].avg_win_r.toFixed(2) + "R · −" + ms[c].avg_loss_r.toFixed(2) + "R" : "—"}</td>`).join("")}</tr>
      <tr class="sub"><td>Worst 5% · median · best 5%</td>${cols.map((c) => `<td class="num">${ms[c].entered ? rR(ms[c].r_quantiles.p5) + `<small>${rR(ms[c].r_quantiles.p50)} · ${rR(ms[c].r_quantiles.p95)}</small>` : "—"}</td>`).join("")}</tr>
      <tr class="sub"><td>Went against you first (median · 90%)</td>${cols.map((c) => `<td class="num">${ms[c].entered ? ms[c].mae_r_median.toFixed(2) + "R · " + ms[c].mae_r_p90.toFixed(2) + "R" : "—"}</td>`).join("")}</tr>
      <tr class="sub"><td>Bars held (median · 90%)</td>${cols.map((c) => `<td class="num">${ms[c].entered ? ms[c].bars_median + " · " + ms[c].bars_p90 : "—"}</td>`).join("")}</tr>
      </table></div>
      ${ms.replay && ms.replay.independent < 20 ? `<div class="small muted" style="margin-top:6px">The past-occurrence column has ${ms.replay.independent} independent occurrence${ms.replay.independent === 1 ? "" : "s"} (the rest overlap them): too few to judge, whatever the average says.</div>` : ""}
    </div>`;
  const moneyKinds = [[fut, rep.instrument === "equity" ? `Shares: rupees for the ${rep.qty || 1} after charges` : "Futures: rupees per lot after charges"], ["option_inr_per_lot", "Option: rupees per lot after charges"]];
  const money = moneyKinds.filter(([k]) => cols.some((c) => ms[c][k])).map(([k, t]) => `<h3>${esc(t)}</h3><div class="tbl"><table class="tm-table"><tr><th></th>${cols.filter((c) => ms[c][k]).map((c) => `<th class="num">${{ replay: "Past occurrences", baseline: "Every bar", bootstrap: "Bootstrap", implied: "Implied IV" }[c]}</th>`).join("")}</tr>
      ${[["expectancy", "Average", inr], ["p_loss", "Chance of a loss", pc1], ["p5", "Worst 5%", inr], ["p50", "Median", inr], ["p95", "Best 5%", inr], ["worst", "Worst path", inr], ["premium_paid", "Premium paid (the quote)", inrA], ["p_lose_half_premium", "Loses at least half the premium", pc1], ["p_held_to_expiry", "Still held at expiry", pc1]]
        .filter(([s]) => cols.some((c) => ms[c][k] && ms[c][k][s] != null)).map(([s, l, fn]) => `<tr><td>${l}</td>${cols.filter((c) => ms[c][k]).map((c) => `<td class="num">${ms[c][k][s] == null ? "—" : fn(ms[c][k][s])}</td>`).join("")}</tr>`).join("")}</table></div>`).join("");
  const moneyCard = money ? `<div class="card"><h2>In rupees</h2>${money}<div class="small muted" style="margin-top:6px">Exits are valued at the session's close, which counts a little more decay than an intraday exit. Option prices keep today's IV unless you override it.</div></div>` : "";
  const sel = ms[TM.sview] && ms[TM.sview].entered ? TM.sview : (sims[0] || "bootstrap");
  const chartCard = `<div class="card" id="tmCharts">${tmSwingCharts(rep, sel)}</div>`;
  let grid = "";
  if (rep.grid) {
    const g = rep.grid, tps = [...new Set(g.cells.map((x) => x.tp2_r))].sort((a, b) => a - b), sts = [...new Set(g.cells.map((x) => x.stop_atr))].sort((a, b) => a - b);
    grid = `<div class="card"><h2>Other stops and targets</h2><div class="small muted" style="margin-bottom:6px">Average per trade (R) on the same ${g.paths.toLocaleString("en-IN")} bootstrap paths. Picking the best cell is fitting this chart's past: read it as a range, not a setting. Now: stop ${g.current.stop_atr} ATR, TP2 ${g.current.tp2_r}R.</div>
      ${TMc.heat(sts.map((s) => s + " ATR"), tps.map((t) => t + "R"), sts.map((s) => tps.map((t) => { const c = g.cells.find((x) => x.stop_atr === s && x.tp2_r === t); return { text: rR3(c.expectancy_r), v: c.expectancy_r, cur: Math.abs(s - g.current.stop_atr) < 0.13 && Math.abs(t - g.current.tp2_r) < 0.13 }; })), "stop \\ TP2")}</div>`;
  }
  const how = `<div class="card"><details><summary class="small muted">How this was worked out</summary><div class="tm-fine" style="margin-top:6px">
      <p><b>Past occurrences</b>: every bar of this chart's own history where the same setup fired the same way, entered at the next open with that bar's own levels; the baseline enters on every bar. Consecutive occurrences overlap, so the header counts independent ones.</p>
      <p><b>Bootstrap</b>: ${esc((ms.bootstrap || {}).what || "")}. The year's drift is measured with a pilot run and taken out; the verdict also counts that one year of history is a single sample.</p>
      <p><b>Implied IV</b>: ${esc((ms.implied || {}).what || "no option chain, so no implied volatility")}.</p>
      <p>All of them use the pack's exit rules: the stop is checked first when a bar touches both levels; a gap through the stop fills at the open; a session that opens past TP1 fills at that better open; TP1 books its part in whole lots; the stop then moves to entry and a chandelier trail only tightens; the time stop applies before TP1. Options are bought at the ask and sold at the bid, repriced with Black-76 on the real calendar, settled at intrinsic value at expiry.</p>
      <p>Limits: daily bars cannot show the order of a day's high and low (the stop is assumed first); liquidity, slippage beyond the charges and futures roll costs are not counted; the option's IV is held constant unless you override it.</p></div></details></div>`;
  return header + chartRead + table + chartCard + moneyCard + grid + how;
}
function tmSwingCharts(rep, sel) {
  const ms = rep.methods, tr = rep.trade, names = { bootstrap: "Bootstrap", implied: "Implied IV" };
  const keys = ["bootstrap", "implied"].filter((k) => ms[k] && ms[k].entered);
  const s = ms[sel];
  if (!s) return `<h2>What you make or lose</h2><div class="small muted">No simulated paths to draw (the bootstrap and implied methods were not run).</div>`;
  const seg = `<div class="seg" id="tm_sview">${keys.map((k) => `<button data-v="${k}" class="${sel === k ? "on" : ""}">${names[k]}</button>`).join("")}</div>`;
  const paths = (rep.samples && rep.samples[sel]) || [];
  const n = paths.length ? paths[0].length : 0;
  let fanSvg = '<div class="small muted">No paths to draw.</div>';
  if (n > 1) {
    const cols = []; for (let i = 0; i < n; i++) { const col = paths.map((p) => p[i][3]); const q = TMa.quantiles(col, [0.05, 0.25, 0.5, 0.75, 0.95]); cols.push([q.p5, q.p25, q.p50, q.p75, q.p95]); }
    const thin = paths.slice(0, 24).map((p) => p.map((b) => b[3]));
    const nl = (v) => Math.round(v).toLocaleString("en-IN");
    fanSvg = TMc.fan(cols, [{ y: tr.stop, label: "SL " + nl(tr.stop), cls: "sl" }, { y: tr.tp1, label: "TP1 " + nl(tr.tp1), cls: "tp" }, { y: tr.tp2, label: "TP2 " + nl(tr.tp2), cls: "tp" }, { y: tr.entry_plan, label: "entry", cls: "en" }],
      (i) => "day " + (i + 1), { paths: thin, aria: "Simulated closing prices day by day, " + names[sel], fmt: (v) => Math.round(v).toLocaleString("en-IN") });
  }
  const moneyKey = rep.instrument === "equity" ? "result_inr" : (rep.methods[sel].option_result_inr ? "option_result_inr" : "result_inr");
  const useR = !(s.result_inr || s.option_result_inr);
  const vals = useR ? s.result_r : (moneyKey === "option_result_inr" && s.option_result_inr ? s.option_result_inr : s.result_inr);
  const label = useR ? "R" : (moneyKey === "option_result_inr" && s.option_result_inr ? "the option, rupees per lot" : rep.instrument === "equity" ? "the shares, rupees" : "futures, rupees per lot");
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">What you make or lose</h2>${seg}</div>
    <div class="small muted" style="margin:4px 0 0">${esc(s.what || "")}. Distribution of ${esc(label)} after charges.</div>
    <div class="tm-chart">${TMc.histogram(vals, { aria: "Distribution of results, " + names[sel], fmt: useR ? ((x) => (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(1) + "R") : ((x) => inr(x)) })}</div>
    <h3>Where the price goes</h3><div class="small muted">Closing prices of ${paths.length} sampled paths: the middle 50% and 90%, with your levels.</div>
    <div class="tm-chart">${fanSvg}</div>`;
}

// ----- text summaries (the Copy button)
function tmSummaryText() {
  const rep = TM.out[TM.mode]; if (!rep) return "";
  if (TM.mode === "intra") {
    const m = rep.meta, keys = ["implied", "bootstrap", "replay"].filter((k) => rep.models[k] && rep.models[k].variants), L = [];
    L.push(`Time Machine (intraday) · ${m.underlying} ${m.strike} ${m.side} · ${m.lots} lot(s) · ${m.startLabel}`);
    L.push(`Entry ~${m.entry_plan.toFixed(2)} · SL ${rep.levels.sl} · T1 ${rep.levels.t1} · T2 ${rep.levels.t2}`);
    for (const k of keys) { const v = rep.models[k].variants.desk; L.push(`${k}: avg ${inr(v.expectancy)} ± ${inrA(1.96 * v.se)}, profit ${pc0(v.p_profit)}, T1 ${pc0(v.p_hit_t1)}${v.p_day_stop != null ? ", loses ≥ day stop " + pc0(v.p_day_stop) : ""} (n=${rep.models[k].n})`); }
    L.push("Models, not forecasts. Not financial advice.");
    return L.join("\n");
  }
  const tr = rep.trade, L = [`Time Machine (swing) · ${rep.symbol} ${tr.direction} · entry ${tr.entry_plan} · SL ${tr.stop} · TP1 ${tr.tp1} · TP2 ${tr.tp2}`];
  for (const l of rep.verdict || []) L.push(l.text);
  L.push("Models, not forecasts. Not financial advice.");
  return L.join("\n");
}

// ----- wiring
async function tmPickPosition(id) {
  const t = S.trades.find((x) => x.id === id); if (!t) return;
  const ix = ixOfTrade(t); if (!TMSYM[ix] || !["NIFTY", "SENSEX"].includes(ix)) { TM.err = "The Time Machine replays NIFTY and SENSEX options only."; return tmRender(); }
  TM.err = ""; TM.status = "Finding the contract…"; tmRender();
  try {
    let ch = await tmChain(TMSYM[ix].key, null), list = ch.expiry_list || [ch.expiry], found = null;
    for (const e of [ch.expiry, ...list.filter((x) => x !== ch.expiry)].slice(0, 5)) {
      const c = e === ch.expiry ? ch : await tmChain(TMSYM[ix].key, e);
      const row = c.strikes.find((r) => (r.ce && r.ce.id === t.indKey) || (r.pe && r.pe.id === t.indKey));
      if (row) { found = { c, row }; break; }
    }
    if (!found) throw new Error("That contract isn't in the nearest expiries' strikes around the money: replay it from the strike list instead.");
    const side = found.row.ce && found.row.ce.id === t.indKey ? "CE" : "PE", lot = found.row.ce ? found.row.ce.lot_size : found.row.pe.lot_size;
    TM.exp[ix] = found.c.expiry_list || [found.c.expiry]; TM.ch = found.c;
    Object.assign(TM.f, { ix, expiry: found.c.expiry, side, strike: found.row.strike });
    TM.f.pos = { id: t.indKey, name: String(t.instrument || ""), entry: +t.entry, stop: num(t.sl) || r05((+t.entry) * (1 - S.rules.slPct / 100)), t1Done: !!t.t1Done, remainingLots: Math.max(1, Math.round(openQty(t) / (lot || S.rules.lotSizes[ix]))) };
    TM.status = "";
  } catch (e) { TM.err = tmErr(e); TM.status = ""; }
  tmRender();
}
function tmFromTradePick() {
  const p = S.pick; if (!p) return;
  const ix = p.ix; if (!["NIFTY", "SENSEX"].includes(ix)) return;
  TM.f.pos = null; TM.f.ix = ix; TM.f.side = p.side || "CE";
  if (S.chain && S.chain.ix === ix && S.chain.expiry) TM.f.expiry = S.chain.expiry;
  const row = S.chain && S.chain.rows ? S.chain.rows.find((r) => (r.call_data && r.call_data.derivative_id === p.key) || (r.put_data && r.put_data.derivative_id === p.key)) : null;
  TM.f.strike = row ? row.strike_price : "";
  const lots = num($("f_lots").value); if (lots) TM.f.lots = lots;
  tmPrepare();
}
function tmBind() {
  try { const v = JSON.parse(localStorage.getItem("desk.tm") || "null"); if (v && (v.mode === "intra" || v.mode === "swing")) { TM.mode = v.mode; Object.assign(TM.f, v.f || {}, { pos: null }); } } catch {}
  $("tmMode").addEventListener("click", (ev) => { const b = ev.target.closest("button[data-m]"); if (!b || b.dataset.m === TM.mode) return; TM.mode = b.dataset.m; TM.err = ""; TM.ch = null; tmSave(); tmRender(); tmPrepare(); });
  const ctl = $("tmCtl");
  ctl.addEventListener("click", (ev) => {
    const t = ev.target, seg = t.closest(".seg button[data-v]");
    if (seg && !seg.disabled) {
      const id = seg.parentElement.id, v = seg.dataset.v;
      if (id === "tm_ix") { TM.f.ix = v; TM.f.expiry = ""; TM.f.strike = ""; TM.f.pos = null; TM.ch = null; tmRender(); tmPrepare(); }
      else if (id === "tm_side") { TM.f.side = v; TM.f.strike = ""; tmFixStrike(); tmRender(); }
      else if (id === "tm_dir") { TM.f.dir = v; tmRender(); }
      return;
    }
    const pb = t.closest("button[data-pos]"); if (pb) return tmPickPosition(pb.dataset.pos);
    if (t.closest("#tm_fromtrade")) return tmFromTradePick();
    if (t.closest("#tm_clearpos")) { TM.f.pos = null; tmPrepare(); return; }
    if (t.closest("#tm_run")) return tmRun();
    if (t.closest("#tm_cancel")) { TM.cancel = true; return; }
    if (t.closest("#tm_other")) { $("tm_find").hidden = !$("tm_find").hidden; return; }
    if (t.closest("#tm_go")) {
      const q = $("tm_q").value.trim(); if (!q) return;
      $("tm_found").textContent = "Searching…";
      callIND("lookup_ind_keys", { names: [q], filter_type: "IN_STOCKS" }).then((r) => { TM.found = Array.isArray(r) ? r.filter((x) => x.ind_key).slice(0, 8) : []; $("tm_found").innerHTML = TM.found.length ? TM.found.map((x) => `<button data-key="${esc(x.ind_key)}" data-name="${esc(x.name)}">${esc(x.name)}</button>`).join("") : "No match."; })
        .catch((e) => { $("tm_found").textContent = tmErr(e); });
      return;
    }
    const fb = t.closest("#tm_found button[data-key]");
    if (fb) { const sym = fb.dataset.name.toUpperCase().replace(/\s+(LTD|LIMITED)\.?$/, "").replace(/[^A-Z0-9&-]+/g, " ").trim().slice(0, 18); TM.other = { sym, key: fb.dataset.key, lot: null }; TM.f.sym = sym; TM.f.sexpiry = ""; TM.ch = null; TM.found = null; tmRender(); tmPrepare(); }
  });
  ctl.addEventListener("change", (ev) => {
    const id = ev.target.id, v = ev.target.value, f = TM.f;
    if (id === "tm_exp") { f.expiry = v; f.strike = ""; TM.ch = null; tmLoadIntraChain(f.ix, v).then(tmRender).catch((e) => { TM.err = tmErr(e); tmRender(); }); }
    else if (id === "tm_strike") f.strike = v;
    else if (id === "tm_entry") f.entryAt = v;
    else if (id === "tm_paths") f.paths = +v;
    else if (id === "tm_vol") f.volMode = v;
    else if (id === "tm_sym") { f.sym = v; f.sexpiry = ""; TM.ch = null; tmRender(); tmPrepare(); }
    else if (id === "tm_inst") { f.inst = v; f.sexpiry = ""; tmRender(); tmPrepare(); }
    else if (id === "tm_sexp") { f.sexpiry = v; tmPrepare(); }
    else if (id === "tm_spaths") f.spaths = +v;
    else if (id === "tm_drift") f.drift = ev.target.checked;
    else if (id === "tm_grid") f.grid = ev.target.checked;
  });
  ctl.addEventListener("input", (ev) => {
    const id = ev.target.id, v = ev.target.value, f = TM.f;
    const map = { tm_lots: "lots", tm_iv: "iv", tm_slots: "slots", tm_qty: "qty", tm_sl: "sl", tm_tp1: "tp1", tm_tp2: "tp2", tm_limit: "limit", tm_hz: "horizon", tm_siv: "siv" };
    if (map[id]) f[map[id]] = v;
  });
  $("tmOut").addEventListener("click", (ev) => {
    const b = ev.target.closest("#tm_view button[data-v]");
    if (b) { TM.view = b.dataset.v; const rep = TM.out.intra; if (rep) $("tmCharts").innerHTML = tmIntraCharts(rep); return; }
    const s = ev.target.closest("#tm_sview button[data-v]");
    if (s) { TM.sview = s.dataset.v; const rep = TM.out.swing; if (rep) $("tmCharts").innerHTML = tmSwingCharts(rep, TM.sview); return; }
    if (ev.target.closest("#tm_copy")) tmCopy(tmSummaryText());
  });
  tmRender();
}
function tmEnter() { tmRender(); tmPrepare(); }
