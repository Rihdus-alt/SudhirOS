/* The Time Machine, intraday mode: the Trading Desk's own plan (SL, T1, T2, the trailing ladder, out by 15:15)
 * replayed on 5-minute index bars, with the option repriced with Black-76 on every bar.
 *
 * One variance clock. An option's IV is quoted on calendar time, but variance does not arrive on a calendar: it
 * arrives in the session (shaped like a U) and, overnight and over weekends, as the opening gap. The clock used here
 *     Teff(day, bar) = (1 - S[bar]) * (1 - phi) / 365  +  (calendar days from that day to the expiry) / 365
 * counts what is left to arrive: the unspent part of today's session, plus every later calendar day in full. The
 * volatility is solved from the quote's mid on that same clock and the index paths spend variance on it, so the
 * option price is a martingale along them (the tests check it). Nothing here predicts: it counts what the plan does
 * on paths from three models, and shows how far apart the models are. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./tmx-core.js"));
  else root.TMXI = factory(root.TMX);
})(typeof self !== "undefined" ? self : this, function (T) {
  "use strict";

  const BARS = 75;          // 09:15 .. 15:30, five minutes each
  const LAST = 71;          // the last bar traded: 15:10-15:15, out at its close
  const OPEN_MIN = 555;     // 09:15
  const DAY = 86400000, IST = 5.5 * 3600000;
  const sum = T.sum;
  const r05 = (n) => Math.round(n * 20) / 20;
  const hm = (j) => { const m = OPEN_MIN + 5 * j; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
  const barOf = (hhmm) => { const [h, m] = String(hhmm).split(":").map(Number); return Math.floor((h * 60 + (m || 0) - OPEN_MIN) / 5); };

  const DEFAULT_RULES = {
    slPct: 10, tp1Pct: 15, tp2Pct: 20, ladder: [1.10, 1.15, 1.20, 1.30, 1.40, 1.50], breathPct: 0.00085, minBreath: 5,
    noEntryAfter: "15:00", exitAt: "15:15", dayStop: 3000,
  };
  /** What happens at the targets. */
  const VARIANTS = {
    desk: { label: "Desk plan", note: "T1 books half (one lot: all of it), the stop goes to entry, the ladder trails, T2 takes the rest",
      t1: true, t2: "rest", ladder: true },
    ride: { label: "Ride past T2", note: "as the desk plan, but T2 is not taken: the rest rides the ladder to 15:15",
      t1: true, t2: false, ladder: true },
    allT1: { label: "All out at T1", note: "the whole position leaves at +15%", t1: true, t1All: true, t2: false, ladder: true },
    allT2: { label: "All out at T2", note: "no part at T1; the whole position leaves at +20%", t1: false, t2: "all", ladder: true },
    trail: { label: "Ladder only", note: "no targets: the ladder trails the stop, out at 15:15", t1: false, t2: false, ladder: true },
    hold: { label: "Stop only", note: "the stop-loss and nothing else, out at 15:15", t1: false, t2: false, ladder: false },
  };
  const CLASSES = [
    ["stop", "stop-loss, nothing booked"], ["t1_all", "T1: the whole position out"], ["t1_be", "T1, then stopped at entry"],
    ["t1_trail", "T1, then stopped by the ladder"], ["t2", "T2 taken"], ["trail_out", "stopped by the ladder"], ["time", "out at 15:15"],
  ];

  // ------------------------------------------------------------------ sessions and the variance profile
  /** INDmoney 5-minute rows -> sessions [{date, bars: Array(72) of {o,h,l,c} | null, count, full}]. Bars from 15:15 on are
   *  the feed's closing prints (the 15:10 value repeated, then one closing print), not trading: they are dropped. */
  function parseBars(rows) {
    const by = new Map();
    for (const r of rows || []) {
      const s = String(r.datetime_ist || r.date || ""), day = s.slice(0, 10), hh = +s.slice(11, 13), mm = +s.slice(14, 16);
      const j = (hh * 60 + mm - OPEN_MIN) / 5;
      if (!(j >= 0 && j <= LAST) || j !== Math.floor(j)) continue;
      const o = +r.open, h = +r.high, l = +r.low, c = +r.close;
      if (!(o > 0 && h > 0 && l > 0 && c > 0)) continue;
      if (!by.has(day)) by.set(day, { date: day, bars: new Array(LAST + 1).fill(null) });
      by.get(day).bars[j] = { o, h: Math.max(h, o, c), l: Math.min(l, o, c), c };
    }
    const out = [...by.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
    for (const s of out) { s.count = s.bars.filter(Boolean).length; s.full = s.count >= LAST + 1; }
    return out;
  }
  function paramWeights() {
    const w = [];
    for (let j = 0; j < BARS; j++) w.push(1 + 2.2 * Math.exp(-j / 5) + 0.7 * Math.exp(-(BARS - 1 - j) / 10));
    return w;
  }
  /** Each bar's share of a session's variance (summing to 1 over the 75 bars): a U shape, blended with what the full
   *  sessions in hand show (smoothed over five bars) when there are at least two. */
  function sessionProfile(sessions) {
    const pw = paramWeights(), pt = sum(pw), param = pw.map((x) => x / pt);
    const full = (sessions || []).filter((s) => s.full);
    if (full.length < 2) return { shares: param, source: "a standard U shape (fewer than two full sessions of 5-minute bars)", empirical: false };
    const e = new Array(LAST + 1).fill(0);
    for (const s of full) for (let j = 0; j <= LAST; j++) { const b = s.bars[j], x = Math.log(b.c / b.o); e[j] += x * x / full.length; }
    const sm = e.map((_, j) => {
      if (j < 2) return e[j];
      let a = 0, n = 0;
      for (let k = Math.max(0, j - 2); k <= Math.min(LAST, j + 2); k++) { a += e[k]; n++; }
      return a / n;
    });
    const st = sum(sm), pPart = sum(param.slice(0, LAST + 1)), shares = param.slice();
    for (let j = 0; j <= LAST; j++) shares[j] = 0.5 * param[j] + 0.5 * (sm[j] / st) * pPart;
    const tot = sum(shares);
    return { shares: shares.map((x) => x / tot), source: "a U shape blended with " + full.length + " full sessions of 5-minute bars", empirical: true };
  }

  // ------------------------------------------------------------------ the clock
  const daysBetween = (a, b) => Math.round((T.isoMs(b) - T.isoMs(a)) / DAY);
  function istParts(ms) { const d = new Date(ms + IST); return { date: d.toISOString().slice(0, 10), mins: d.getUTCHours() * 60 + d.getUTCMinutes() }; }
  function isTradingDay(iso, holidays) {
    const w = new Date(T.isoMs(iso)).getUTCDay();
    return w !== 0 && w !== 6 && !(holidays && holidays.has && holidays.has(iso));
  }
  function nextTradingDay(iso, holidays) {
    let d = T.isoMs(iso);
    for (let i = 0; i < 20; i++) { d += DAY; const x = T.msIso(d); if (isTradingDay(x, holidays)) return x; }
    return T.msIso(d);
  }
  /** Where the run starts: live (the next bar of today's session) or the next session's open (an overnight gap first). */
  function startPoint(nowMs, holidays, forceOpen, entryAt) {
    const t = istParts(nowMs);
    const open = isTradingDay(t.date, holidays) && t.mins >= OPEN_MIN && t.mins < OPEN_MIN + 5 * (LAST + 1);
    if (open && !forceOpen) {
      const b0 = Math.max(0, Math.ceil((t.mins - OPEN_MIN) / 5));
      return { kind: "live", day: t.date, b0, label: "from " + hm(b0) + " today" };
    }
    const before = isTradingDay(t.date, holidays) && t.mins < OPEN_MIN;
    const day = before ? t.date : nextTradingDay(t.date, holidays);
    const b0 = Math.max(0, Math.min(LAST, barOf(entryAt || "09:30")));
    return { kind: "open", day, b0, label: "the " + day + " session from the open, entering at " + hm(b0) };
  }
  function makeClock(shares, phi, day, expiryIso) {
    const S = [0];
    for (let j = 0; j < BARS; j++) S.push(S[j] + shares[j]);
    const D = Math.max(daysBetween(day, expiryIso), 0);
    return { S, D, phi, Teff: (b) => Math.max((1 - S[b]) * (1 - phi) / 365 + D / 365, 0) };
  }
  /** The moment the chain was struck, as a day and a bar boundary (75 = that day's close). */
  function quoteMoment(chain, holidays) {
    const ms = T.chainTimeMs(chain);
    if (ms == null) return null;
    const p = istParts(ms);
    const inSession = isTradingDay(p.date, holidays) && p.mins >= OPEN_MIN && p.mins < OPEN_MIN + 5 * BARS;
    return { day: p.date, b: inSession ? Math.max(0, Math.min(BARS, Math.round((p.mins - OPEN_MIN) / 5))) : (p.mins < OPEN_MIN ? 0 : BARS), ms };
  }

  // ------------------------------------------------------------------ index path models
  function bridgeBar(rng, O, v) {          // a Gaussian bar; a Brownian bridge gives its high and low
    const x = rng.gauss() * Math.sqrt(v) - 0.5 * v;
    const u1 = 1 - rng.random(), u2 = 1 - rng.random();
    const mx = 0.5 * (x + Math.sqrt(x * x - 2 * v * Math.log(u1))), mn = 0.5 * (x - Math.sqrt(x * x - 2 * v * Math.log(u2)));
    return [O, O * Math.exp(mx), O * Math.exp(mn), O * Math.exp(x)];
  }
  /** Real 5-minute bars as shapes: high, low and close from the open, each over the square root of the bar's variance
   *  share, drift taken out. rho is their rms, the recent realised level on the model's own scale. */
  function barLibrary(sessions, shares) {
    const lib = [];
    for (const s of sessions) for (let j = 0; j <= LAST; j++) {
      const b = s.bars[j];
      if (!b) continue;
      const sq = Math.sqrt(shares[j]);
      lib.push([Math.log(b.h / b.o) / sq, Math.log(b.l / b.o) / sq, Math.log(b.c / b.o) / sq]);
    }
    if (lib.length < 40) return null;
    const mu = sum(lib.map((x) => x[2])) / lib.length, out = lib.map((x) => [x[0] - mu, x[1] - mu, x[2] - mu]);
    return { lib: out, rho: Math.sqrt(sum(out.map((x) => x[2] * x[2])) / out.length) };
  }
  /** Overnight gaps (open over the last close, in logs) from the daily bars, per square root of the calendar days. */
  function overnightGaps(daily) {
    const g = [];
    for (let i = 1; daily && i < daily.length; i++) {
      const pc = daily[i - 1].close, o = daily[i].open;
      if (pc > 0 && o > 0) g.push({ x: Math.log(o / pc), days: Math.max(1, daysBetween(daily[i - 1].date, daily[i].date)) });
    }
    if (!g.length) return null;
    const z = g.map((a) => a.x / Math.sqrt(a.days)), mu = sum(z) / z.length, zc = z.map((v) => v - mu);
    return { z: zc, sd: Math.sqrt(sum(zc.map((v) => v * v)) / zc.length) };
  }

  // ------------------------------------------------------------------ the desk's plan on one premium path
  function levelsFor(E, rules, spot, delta) {
    const sl = r05(E * (1 - rules.slPct / 100)), t1 = r05(E * (1 + rules.tp1Pct / 100)), t2 = r05(E * (1 + rules.tp2Pct / 100));
    const breathPts = Math.round(spot * rules.breathPct), breath = Math.max(rules.minBreath, Math.round(breathPts * Math.abs(delta || 0.5)));
    const steps = rules.ladder.map((m, i) => { const at = r05(E * m); return { at, stop: i === 0 ? E : Math.max(E, r05(at - breath)) }; });
    return { sl, t1, t2, steps, breath, breathPts };
  }
  /** pb(n) -> {o, hi, lo, c}: the sellable (bid) premium on the run's bar n. state: {stop, t1Done}. Stops first when a bar touches
   *  both; a stop trigger that the bar opens through fills at the open; targets are limits (a better open fills better). */
  function runDesk(pb, L, E, lv, variant, qtyLots, state, rules) {
    const exits = [];
    let stop = state.stop, left = qtyLots, stage = state.t1Done ? "t1" : "open", stepsDone = 0, cls = null;
    let minLow = Infinity, maxHigh = -Infinity;
    const t1Lots = variant.t1 ? (variant.t1All || qtyLots < 2 ? qtyLots : Math.floor(qtyLots / 2)) : 0;
    const lastBar = Math.min(L - 1, barOf(rules.exitAt) - 1 - state.b0);
    let n = 0;
    for (; n <= lastBar && left > 0; n++) {
      const b = pb(n);
      if (b.lo < minLow) minLow = b.lo;
      if (b.hi > maxHigh) maxHigh = b.hi;
      if (b.lo <= stop) {
        exits.push({ q: left, px: b.o <= stop ? b.o : stop, n, why: "stop" });
        const locked = stop > E + 0.004;
        cls = stage === "open" ? (locked ? "trail_out" : "stop") : (locked ? "t1_trail" : "t1_be");
        left = 0; break;
      }
      if (stage === "open" && variant.t1 && b.hi >= lv.t1) {
        exits.push({ q: t1Lots, px: b.o >= lv.t1 ? b.o : lv.t1, n, why: "t1" });
        left -= t1Lots; stage = "t1";
        if (left <= 0) { cls = "t1_all"; break; }
        if (stop < E) stop = E;
      }
      if (variant.t2 && b.hi >= lv.t2 && (variant.t2 === "all" || stage === "t1")) {
        exits.push({ q: left, px: b.o >= lv.t2 ? b.o : lv.t2, n, why: "t2" });
        left = 0; cls = "t2"; break;
      }
      if (variant.ladder) while (stepsDone < lv.steps.length && b.hi >= lv.steps[stepsDone].at) { if (lv.steps[stepsDone].stop > stop) stop = lv.steps[stepsDone].stop; stepsDone++; }
      if (n === lastBar && left > 0) { exits.push({ q: left, px: b.c, n, why: "time" }); left = 0; cls = "time"; }
    }
    return { cls, exits, minLow, maxHigh };
  }

  // ------------------------------------------------------------------ the run
  /** opts.sessions (parseBars), opts.chain (T.parseChain), opts.side/strike or derivativeId, opts.nowMs; see the README. */
  async function analyseIntraday(opts) {
    const rules = Object.assign({}, DEFAULT_RULES, opts.rules || {});
    const k = T.cfgWith(opts.cfg), progress = opts.onProgress || (() => {}), cancelled = opts.isCancelled || (() => false);
    const nowMs = opts.nowMs, holidays = opts.holidays || null, rates = opts.rates || T.DEFAULT_RATES;
    const paths = Math.round(opts.paths || 4000), seed = opts.seed == null ? 7 : opts.seed;
    const chain = opts.chain, notes = [];
    if (!chain) throw new T.TMError("an option chain is needed: it carries the strike's price and the volatility");
    const sessions = (opts.sessions || []).filter((s) => s.count >= 20);
    const mode = opts.mode === "position" ? "position" : "new";

    // ---- the contract
    const row = opts.derivativeId
      ? chain.strikes.find((x) => (x.ce && x.ce.id === opts.derivativeId) || (x.pe && x.pe.id === opts.derivativeId))
      : chain.strikes.find((x) => x.strike === Number(opts.strike));
    if (!row) throw new T.TMError("that strike is not in the loaded chain: load the expiry with strikes around it");
    const kind = opts.derivativeId ? (row.ce && row.ce.id === opts.derivativeId ? "CE" : "PE") : opts.side;
    const leg = kind === "CE" ? row.ce : row.pe;
    if (!leg || !leg.ltp) throw new T.TMError("no price for that strike in the chain");
    const K = row.strike;
    const lotSize = Math.round(opts.lotSize || leg.lot_size || chain.lot_size || 0);
    if (!lotSize) throw new T.TMError("no lot size for this contract");
    const exchange = opts.exchange || (T.BSE_UNDERLYINGS[String(chain.underlying || "").toUpperCase()] ? "BFO" : "NFO");
    const r = k.risk_free_rate, expIso = T.expiryIso(chain.expiry);
    const qLots = mode === "position" ? Math.max(1, Math.round((opts.position || {}).remainingLots || opts.lots || 1)) : Math.max(1, Math.round(opts.lots || 1));

    // ---- the clock, and the volatility solved from the quote's mid on that clock
    const sp = startPoint(nowMs, holidays, !!opts.startAtOpen, opts.entryAt);
    if (sp.kind === "live" && sp.b0 > barOf(rules.noEntryAfter) && mode !== "position") throw new T.TMError("no new entries after " + rules.noEntryAfter + " (the desk rule): run it from the next open instead");
    if (sp.b0 > barOf(rules.exitAt) - 1 || (mode === "position" && sp.kind !== "live")) throw new T.TMError("an open position can be managed only while the session runs, and it is past " + rules.exitAt + " or the market is closed: the plan has no bars left today");
    if (expIso < sp.day) throw new T.TMError("this expiry (" + chain.expiry + ") is before the run starts: load a later expiry");
    const phi = opts.daily ? T.overnightShare(opts.daily) : 0.25;
    const prof = sessionProfile(sessions), clock = makeClock(prof.shares, phi, sp.day, expIso);
    const jStart = sp.kind === "open" ? 0 : sp.b0, L = LAST + 1 - jStart, eIdx = sp.b0 - jStart, Ltr = L - eIdx;
    const q = quoteMoment(chain, holidays) || { day: sp.day, b: sp.b0 };
    if (expIso < q.day) throw new T.TMError("the chain was struck after this expiry");
    const TeffQ = q.b >= BARS ? Math.max(daysBetween(q.day, expIso) / 365, 0) : makeClock(prof.shares, phi, q.day, expIso).Teff(q.b);
    const bid = leg.bid || null, ask = leg.ask || null, mid = bid && ask ? (bid + ask) / 2 : leg.ltp, half = bid && ask ? (ask - bid) / 2 : 0;
    const F = chain.forward || chain.spot, basis = F - chain.spot, spot0 = Number(opts.spot0 || chain.spot);
    let sigmaQuote = T.impliedVol(mid, F, K, TeffQ, r, kind) || T.impliedVol(leg.ltp, F, K, TeffQ, r, kind);
    if (!sigmaQuote) throw new T.TMError("the quote cannot be priced (below intrinsic value, or no time left)");
    const sigma = opts.ivPct ? opts.ivPct / 100 : sigmaQuote;
    const sessVar = sigma * sigma * (1 - phi) / 365;                  // the variance one full session spends
    const price = (level, Tt) => T.black76(level + basis, K, Tt, sigma, r, kind);
    const bidAt = (level, Tt) => Math.max(price(level, Tt) - half, 0.05);
    const delta = T.delta76(F, K, TeffQ, sigmaQuote, r, kind);
    const gapVar = sp.kind === "open" ? Math.max(sigma * sigma * (TeffQ - clock.Teff(0)), 0) : 0;
    const Tstart = [], Tend = [], Tmid = [];
    for (let n = 0; n < L; n++) { Tstart.push(clock.Teff(jStart + n)); Tend.push(clock.Teff(jStart + n + 1)); Tmid.push((Tstart[n] + Tend[n]) / 2); }
    // the plan's entry: the quote's ask now (live), or the quote's ask at the close (the open's gap moves it either way)
    const entryPlan = mode === "position" ? Number(opts.position.entry) : r05(sp.kind === "open" ? (ask || leg.ltp) : price(spot0, Tstart[0]) + half);
    const entryFlat = r05(price(spot0, Tstart[eIdx]) + half);        // if the index sat still until the entry bar
    const planLv = levelsFor(entryPlan, rules, spot0, delta);
    const posState = mode === "position"
      ? { entry: Number(opts.position.entry), stop: Number(opts.position.stop != null ? opts.position.stop : planLv.sl), t1Done: !!opts.position.t1Done, lots: qLots } : null;
    const bidNow = bidAt(spot0, Tstart[eIdx]);
    const valueNow = qLots * lotSize * bidNow - T.fnoCosts("sell", qLots * lotSize * bidNow, "OPT", exchange, rates).total;

    // ---- the three models of the index path
    const wanted = opts.models || ["implied", "bootstrap", "replay"];
    const gaps = overnightGaps(opts.daily), lib = barLibrary(sessions, prof.shares);
    if (!lib && (wanted.includes("bootstrap") || wanted.includes("replay"))) notes.push("the bootstrap and the replay need full sessions of 5-minute bars (about 3 are available); they are left out");
    const volMode = opts.volMode === "implied" ? "implied" : "realised";
    const realisedAnn = lib ? lib.rho / Math.sqrt((1 - phi) / 365) : null;
    const kappa = lib && volMode === "implied" ? Math.sqrt(sessVar) / lib.rho : 1;
    const nCalDays = sp.kind === "open" ? Math.max(1, daysBetween(q.day, sp.day)) : 1;
    const gapDraw = (rng, mult) => {
      const v = gapVar * (mult || 1) * (mult || 1);
      return rng.gauss() * Math.sqrt(v) - 0.5 * v;
    };
    const genImplied = (rng, mult) => {
      const out = []; let pc = spot0;
      if (sp.kind === "open") pc *= Math.exp(gapDraw(rng, mult));
      const sv = sessVar * (mult || 1) * (mult || 1);
      for (let n = 0; n < L; n++) { const b = bridgeBar(rng, pc, sv * prof.shares[jStart + n]); out.push(b); pc = b[3]; }
      return out;
    };
    const genBoot = (rng) => {
      const out = []; let pc = spot0;
      if (sp.kind === "open") {                  // a historical gap: per root-day, scaled to this gap's days, or to the implied gap's size
        if (gaps) { const z = gaps.z[rng.randrange(gaps.z.length)]; pc *= Math.exp(volMode === "implied" ? z / gaps.sd * Math.sqrt(gapVar) : z * Math.sqrt(nCalDays)); }
        else pc *= Math.exp(gapDraw(rng));
      }
      let j = rng.randrange(lib.lib.length);
      for (let n = 0; n < L; n++) {
        const e = lib.lib[j], sq = Math.sqrt(prof.shares[jStart + n]) * kappa, x = e[2] * sq;
        const mx = Math.max(e[0] * sq, x, 0), mn = Math.min(e[1] * sq, x, 0);
        out.push([pc, pc * Math.exp(mx), pc * Math.exp(mn), pc * Math.exp(x)]); pc *= Math.exp(x);
        j = rng.random() < 1 / 6 ? rng.randrange(lib.lib.length) : (j + 1) % lib.lib.length;
      }
      return out;
    };
    const tapes = [];                            // each full session, started up to 30 minutes earlier in the day than now
    for (let si = 0; si < sessions.length; si++) {
      const s = sessions[si];
      if (!s.full || s.date === sp.day) continue;
      const prev = si > 0 ? sessions[si - 1] : null;
      const gapLog = prev && prev.bars[LAST] ? Math.log(s.bars[0].o / prev.bars[LAST].c) : null;
      if (sp.kind === "open") { if (gapLog !== null) tapes.push({ date: s.date, b: 0, gapLog, bars: s.bars.slice(0, L) }); continue; }
      for (let b = Math.max(0, sp.b0 - 6); b <= sp.b0; b++) {
        if (b + L - 1 > LAST) continue;
        tapes.push({ date: s.date, b, gapLog, bars: s.bars.slice(b, b + L) });
      }
    }
    const tapePath = (tp) => {
      const scale = (sp.kind === "open" ? spot0 * Math.exp(tp.gapLog) : spot0) / tp.bars[0].o;
      return tp.bars.map((b) => [b.o * scale, b.h * scale, b.l * scale, b.c * scale]);
    };

    // ---- one path through each variant of the plan
    const newAcc = (keys) => keys.map(() => ({ n: 0, pnl: [], cls: {}, byCls: {}, byClsMin: {}, minutes: [], mae: [], mfe: [], t1: [] }));
    const runPath = (bars, acc, keys) => {
      const cache = new Array(L);
      const pb = (n) => {
        if (cache[n]) return cache[n];
        const b = bars[n], o = bidAt(b[0], Tstart[n]), c = bidAt(b[3], Tend[n]);
        const pH = bidAt(kind === "CE" ? b[1] : b[2], Tmid[n]), pL = bidAt(kind === "CE" ? b[2] : b[1], Tmid[n]);
        return (cache[n] = { o, hi: Math.max(pH, pL, o, c), lo: Math.min(pH, pL, o, c), c });
      };
      let E, lv, state;
      if (mode === "position") { E = posState.entry; lv = levelsFor(E, rules, spot0, delta); state = { stop: posState.stop, t1Done: posState.t1Done, b0: sp.b0 }; }
      else {
        E = r05(price(bars[eIdx][0], Tstart[eIdx]) + half);
        if (E < 0.1) return null;
        lv = levelsFor(E, rules, bars[eIdx][0], delta); state = { stop: lv.sl, t1Done: false, b0: sp.b0 };
      }
      const pbT = (m) => pb(eIdx + m);
      const buyCh = mode === "position" ? 0 : T.fnoCosts("buy", E * qLots * lotSize, "OPT", exchange, rates).total;
      keys.forEach((key, vi) => {
        const res = runDesk(pbT, Ltr, E, lv, VARIANTS[key], qLots, state, rules);
        let cash = 0, ch = buyCh, qty = 0;
        for (const e of res.exits) { const val = e.px * e.q * lotSize; cash += val; qty += e.q * lotSize; ch += T.fnoCosts("sell", val, "OPT", exchange, rates).total; }
        // a new trade: its profit. An open position: what following the plan yields over selling it now
        const pnl = mode === "position" ? cash - ch - valueNow : cash - E * qty - ch;
        const last = res.exits[res.exits.length - 1], a = acc[vi];
        a.n++; a.pnl.push(pnl); a.cls[res.cls] = (a.cls[res.cls] || 0) + 1; a.minutes.push(5 * (last.n + 1));
        a.mae.push(Math.max(0, (E - res.minLow) / E)); a.mfe.push(Math.max(0, (res.maxHigh - E) / E));
        (a.byCls[res.cls] = a.byCls[res.cls] || []).push(pnl); (a.byClsMin[res.cls] = a.byClsMin[res.cls] || []).push(5 * (last.n + 1));
        a.t1.push(res.exits.some((e) => e.why === "t1" || e.why === "t2") ? 1 : 0);
      });
      return { E, pb: pbT };
    };
    const summarise = (accs, keys) => {
      const out = {};
      keys.forEach((key, vi) => {
        const a = accs[vi], n = a.n;
        if (!n) { out[key] = { n: 0 }; return; }
        const mu = sum(a.pnl) / n, sd = n > 1 ? Math.sqrt(sum(a.pnl.map((x) => (x - mu) * (x - mu))) / (n - 1)) : 0;
        const qn = T.quantiles(a.pnl), wins = a.pnl.filter((x) => x > 0), loss = a.pnl.filter((x) => x <= 0), classes = {};
        for (const [ck] of CLASSES) {
          const m = a.cls[ck] || 0;
          if (!m) continue;
          const p = m / n, se = Math.sqrt(p * (1 - p) / n);
          classes[ck] = { count: m, p, ci95: [Math.max(0, p - 1.96 * se), Math.min(1, p + 1.96 * se)], avg: sum(a.byCls[ck]) / m, minutes: sum(a.byClsMin[ck]) / m };
        }
        const pw = wins.length / n, se = Math.sqrt(pw * (1 - pw) / n);
        out[key] = {
          n, label: VARIANTS[key].label, note: VARIANTS[key].note, classes, expectancy: mu, se: sd / Math.sqrt(n), sd,
          p_profit: pw, p_profit_ci95: [Math.max(0, pw - 1.96 * se), Math.min(1, pw + 1.96 * se)],
          p_day_stop: mode === "position" ? null : a.pnl.filter((x) => x <= -rules.dayStop).length / n, p_hit_t1: sum(a.t1) / n,
          avg_win: wins.length ? sum(wins) / wins.length : 0, avg_loss: loss.length ? sum(loss) / loss.length : 0,
          p5: qn.p5, p25: qn.p25, p50: qn.p50, p75: qn.p75, p95: qn.p95, worst: Math.min.apply(null, a.pnl), best: Math.max.apply(null, a.pnl),
          minutes_median: T.quantiles(a.minutes, [0.5]).p50, minutes_p90: T.quantiles(a.minutes, [0.9]).p90,
          mae_median: T.quantiles(a.mae, [0.5]).p50, mae_p90: T.quantiles(a.mae, [0.9]).p90, mfe_median: T.quantiles(a.mfe, [0.5]).p50, pnl: a.pnl,
        };
      });
      return out;
    };
    const fanOf = (store) => {
      const cols = [];
      for (let n = 0; n < Ltr; n++) {
        const qs = T.quantiles(store.map((rw) => rw[n]), [0.05, 0.25, 0.5, 0.75, 0.95]);
        cols.push([qs.p5, qs.p25, qs.p50, qs.p75, qs.p95]);
      }
      return cols;
    };

    const keys = (opts.variants || Object.keys(VARIANTS)).filter((x) => VARIANTS[x]);
    const models = {}, active = wanted.filter((m) => m === "implied" || lib);
    let done = 0;
    const totalWork = active.reduce((a, m) => a + (m === "replay" ? Math.max(tapes.length, 1) : paths), 0) || 1;
    for (const m of active) {
      const rng = T.makeRng(seed + "-" + m), accs = newAcc(keys), fan = [];
      const todo = m === "replay" ? tapes.length : paths;
      if (m === "replay" && !todo) { models.replay = { n: 0, note: sp.kind === "open" ? "this needs two consecutive full sessions in the history" : "no full session in the history reaches back to this start" }; continue; }
      let used = 0, entry = null;
      for (let p = 0; p < todo; p++) {
        const bars = m === "replay" ? tapePath(tapes[p]) : (m === "implied" ? genImplied(rng) : genBoot(rng));
        const res = runPath(bars, accs, keys);
        if (!res) continue;
        entry = entry == null ? res.E : entry; used++;
        if (used <= 1500) { const rowP = []; for (let n = 0; n < Ltr; n++) rowP.push(res.pb(n).c); fan.push(rowP); }
        if ((++done & 255) === 0) { progress(Math.min(0.95, done / totalWork), m); await T.tick(); if (cancelled()) throw new T.TMError("cancelled"); }
      }
      models[m] = { n: used, variants: summarise(accs, keys), fan: used ? fanOf(fan) : null, entry };
    }
    if (models.implied) models.implied.what = "risk-neutral paths at " + (100 * sigma).toFixed(1) + "% on the session clock (the chain quotes " + (100 * sigmaQuote).toFixed(1) + "% on a calendar), a U-shaped session, each bar bridged for its high and low";
    if (models.bootstrap) models.bootstrap.what = "paths stitched from " + lib.lib.length + " real 5-minute bars in blocks of about 30 minutes, drift removed, at the " + (volMode === "implied" ? "implied" : "recent realised") + " volatility level";
    if (models.replay && models.replay.n !== undefined && tapes.length) {
      const ds = [...new Set(tapes.map((t) => t.date))];
      Object.assign(models.replay, { sessions: ds, independent: ds.length,
        what: "the plan on " + ds.length + " recent sessions' tapes, started up to 30 minutes either side of this start; the paths overlap, so read it as an illustration, not a sample" });
    }

    // ---- volatility sensitivity: the market moves at a multiple of the implied level, the option was bought at the quote
    const sens = [];
    if (opts.sensitivity !== false && active.includes("implied")) {
      for (const mult of [0.7, 0.85, 1.0, 1.2, 1.4]) {
        const rng = T.makeRng(seed + "-sens-" + mult), acc = newAcc(["desk"]), nP = Math.min(paths, 1500);
        for (let p = 0; p < nP; p++) runPath(genImplied(rng, mult), acc, ["desk"]);
        const a = acc[0];
        sens.push({ mult, n: a.n, expectancy: sum(a.pnl) / a.n, p_profit: a.pnl.filter((x) => x > 0).length / a.n });
      }
    }
    progress(1, "done");
    const flat = bidAt(spot0, clock.Teff(barOf(rules.exitAt)));      // the index unchanged all the way: only time has passed
    return {
      meta: {
        mode, side: kind, strike: K, expiry: chain.expiry, underlying: chain.underlying, lot: lotSize, lots: qLots, qty: qLots * lotSize, exchange,
        spot0, forward: F, basis, start: sp, startLabel: sp.label, bars: Ltr, barsFrom: hm(sp.b0), exitAt: rules.exitAt,
        sigma_calendar_pct: 100 * sigmaQuote, sigma_clock_pct: 100 * sigma, iv_overridden: !!opts.ivPct, phi, delta, mid, bid, ask, half_spread: half,
        days_to_expiry: clock.D, profile: prof.source, realised_ann_pct: realisedAnn ? 100 * realisedAnn : null,
        realised_vs_implied: realisedAnn ? realisedAnn / sigma : null, vol_mode: volMode, quote_at: q, tapes: tapes.length,
        entry_plan: entryPlan, entry_flat: entryFlat, outlay: entryPlan * qLots * lotSize, bid_now: bidNow, value_now: valueNow, flat_at_exit: flat,
        flat_decay_pct: (flat - bidNow) / Math.max(bidNow, 0.05) * 100, flat_decay_inr: (flat - bidNow) * qLots * lotSize,
        position: posState, day_stop: rules.dayStop,
      },
      levels: Object.assign({}, planLv, { risk_inr: (entryPlan - planLv.sl) * qLots * lotSize }),
      rules, models, sensitivity: sens, notes, variants: keys.map((kx) => ({ key: kx, label: VARIANTS[kx].label, note: VARIANTS[kx].note })),
    };
  }

  return { BARS, LAST, DEFAULT_RULES, VARIANTS, CLASSES, parseBars, sessionProfile, paramWeights, startPoint, makeClock, quoteMoment, levelsFor,
    runDesk, analyseIntraday, hm, barOf, r05, istParts, isTradingDay, nextTradingDay, barLibrary, overnightGaps };
});
