// Holds the JavaScript engine to the Python Time Machine (test/ref.json comes from ref_dump.py).
// Run: node --test apps/time-machine/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const T = require("../src/tmx-core.js");

const FIX = path.join(__dirname, "..", "fixtures");
const ref = JSON.parse(fs.readFileSync(path.join(__dirname, "ref.json"), "utf8"));
const csv = (name) => {
  const [head, ...lines] = fs.readFileSync(path.join(FIX, name), "utf8").trim().split("\n");
  const cols = head.split(",");
  return lines.map((l) => Object.fromEntries(l.split(",").map((v, i) => [cols[i], v])));
};
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, (msg || "") + " got " + a + " want " + b + " (tol " + tol + ")");
const fl = (x) => (Number.isInteger(x) ? x.toFixed(1) : String(x));   // Python prints 1.0, JavaScript 1
const CLOSE = Date.UTC(2026, 9, 19, 10, 0, 0);       // 15:30 IST on 19 Oct 2026

const candles = T.loadCandles(csv("NIFTY-1d.csv"));
const ind = T.indicators(candles);
const chainRaw = JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027.json"), "utf8"));

test("the normal CDF, Black-76 and implied volatility match Python", () => {
  for (const [x, want] of ref.ncdf) near(T.ncdf(x), want, 1e-13, "ncdf(" + x + ")");
  for (const [F, K, Tt, s, r, kind, want] of ref.black76) near(T.black76(F, K, Tt, s, r, kind), want, 1e-9 * Math.max(1, want), "black76");
  for (const [F, K, Tt, r, kind, price, want] of ref.implied) near(T.impliedVol(price, F, K, Tt, r, kind), want, 2e-7, "iv");
  for (const [F, K, Tt, s, r, kind, want] of ref.delta) near(T.delta76(F, K, Tt, s, r, kind), want, 1e-12, "delta");
  for (const [x, y, lo, hi] of ref.wilson) { const w = T.wilson(x, y); near(w[0], lo, 1e-12); near(w[1], hi, 1e-12); }
});

test("charges match india-costs for futures, options (NFO and BFO) and delivery shares", () => {
  for (const [side, v, kind, ex, want] of ref.costs) near(T.fnoCosts(side, v, kind, ex).total, want, 0.011, side + kind + ex + v);
  for (const [side, v, want] of ref.trade_costs) near(T.tradeCosts(side, v, "NSE").total, want, 0.011, "delivery " + side);
});

test("indicators match Python on the NIFTY year", () => {
  assert.equal(candles.length, ref.n_candles);
  assert.equal(candles[candles.length - 1].date, ref.last_date);
  for (const [key, pts] of Object.entries(ref.ind)) {
    for (const [i, want] of pts) {
      const got = ind[key][i];
      if (want === null) assert.equal(got, null, key + "[" + i + "]");
      else near(got, want, 1e-9 * Math.max(1, Math.abs(want)), key + "[" + i + "]");
    }
  }
});

test("every setup that fires matches Python's, bar for bar", () => {
  const got = [];
  for (let i = 60; i < candles.length; i++) for (const s of T.signalsAt(candles, ind, i)) got.push([i, s.setup, s.direction, s.score]);
  assert.deepEqual(got, ref.signals);
});

test("levels (structure stops, targets, walls) match Python", () => {
  const i0 = [80, 120, 180, candles.length - 1];
  let n = 0;
  for (const i of i0) for (const d of ["long", "short"]) {
    for (const mode of ["", "-limit"]) {
      const want = ref.levels[n++];
      const c = candles[i];
      const x = mode ? T.levels(candles, ind, i, d, null, c.close * 1.002, c.close * 1.02, c.close * 0.98) : T.levels(candles, ind, i, d, null);
      assert.equal(want[0], i); assert.equal(want[1], d + mode);
      near(x.entry, want[2], 0.011); near(x.stop, want[3], 0.011); near(x.tp1, want[4], 0.011); near(x.tp2, want[5], 0.011);
      near(x.risk_points, want[6], 0.011); near(x.reward_r, want[7], 0.011); assert.equal(x.ok, want[8]); assert.equal(x.stop_basis.replace(/\d+\.\d+$/, ""), want[9].replace(/\d+\.\d+$/, ""));
    }
  }
});

test("the chart read matches Python", () => {
  const r = T.chartRead(candles, ind, null);
  for (const [k, want] of Object.entries(ref.chart_read)) {
    if (typeof want === "number") near(r[k], want, 0.011, k); else assert.equal(r[k], want, k);
  }
  near(T.overnightShare(candles), ref.overnight_share, 1e-12);
});

test("the exit engine reproduces Python on crafted paths", () => {
  const k = T.cfgWith(null);
  const base = { direction: "long", stop: 99, tp1: 101, tp2: 102, risk: 1 };
  const crafted = {
    tp2_then_trail: [[100, 100.4, 99.5, 100.2, 0.8], [100.2, 101.2, 100.1, 101.0, 0.8], [101.0, 102.3, 100.9, 102.2, 0.8], [102.2, 103.0, 102.0, 102.9, 0.8], [102.9, 103.1, 100.5, 100.8, 0.8]],
    gap_through_stop: [[100, 100.3, 99.6, 99.9, 0.8], [98.0, 98.5, 97.5, 98.1, 0.8]],
    stop_first_when_both: [[100, 101.5, 98.5, 100.0, 0.8]],
    time_stop: Array(12).fill([100, 100.4, 99.6, 100.1, 0.8]),
    tp1_gap: [[100, 100.3, 99.7, 100.1, 0.8], [101.6, 101.9, 101.2, 101.7, 0.8], [101.7, 101.9, 100.0, 100.2, 0.8]],
    no_entry: [[101.4, 101.6, 101.0, 101.5, 0.8]],
  };
  let checked = 0;
  for (const [name, bars] of Object.entries(crafted)) for (const frac of [0.5, 1.0, 0.0]) for (const d of ["long", "short"]) {
    const t = d === "long" ? Object.assign({}, base, { frac }) : { direction: "short", stop: 101, tp1: 99, tp2: 98, risk: 1, frac };
    const b = d === "long" ? bars : bars.map(([o, h, l, c, a]) => [200 - o, 200 - l, 200 - h, 200 - c, a]);
    const got = T.runTrade(b, t, k), want = ref.run_trade[name + "|" + d + "|" + fl(frac)];
    assert.equal(got.cls, want.cls, name + d + frac);
    assert.equal(!!got.entered, want.entered);
    if (want.entered) for (const f of ["entry", "exit", "pts", "r", "mae_r", "mfe_r"]) near(got[f], want[f], 1e-9, name + d + frac + f);
    if (want.entered) { assert.equal(got.exit_bar, want.exit_bar); assert.equal(got.bars, want.bars); assert.equal(got.tp1_bar, want.tp1_bar); assert.equal(got.tp2_bar, want.tp2_bar); }
    checked++;
  }
  assert.equal(checked, 36);
  const lim = Object.assign({}, base, { limit: 99.8, frac: 1.0 });
  const g = T.runTrade([[100.5, 101.5, 99.5, 100.0, 0.8], [100.0, 101.2, 99.9, 101.0, 0.8]], lim, k);
  assert.equal(g.cls, ref.run_trade.limit_inside.cls); near(g.entry, ref.run_trade.limit_inside.entry, 1e-9); near(g.pts, ref.run_trade.limit_inside.pts, 1e-9);
  assert.equal(T.runTrade([[100.5, 100.9, 99.8, 100.0, 0.8]], Object.assign({}, base, { limit: 99.5, frac: 1.0 }), k).cls, ref.run_trade.limit_nofill);
});

test("the trade on the last bar and the historical replay match Python", () => {
  const k = T.cfgWith(null);
  const ch = T.parseChain(chainRaw);
  const an = T.chainAnalytics(ch, CLOSE, k.risk_free_rate);
  for (const key of Object.keys(ref.chain)) {
    const want = ref.chain[key];
    if (typeof want === "number") near(an[key], want, key === "atm_iv_pct" || key === "days_to_expiry" ? 0.011 : 0.51, key); else assert.equal(an[key], want, key);
  }
  for (const [strike, ce, pe] of ref.chain_ivs) {
    const row = ch.strikes.find((x) => x.strike === strike);
    if (ce !== null) near(row.ce.iv, ce, 3e-6, "ce iv " + strike);
    if (pe !== null) near(row.pe.iv, pe, 3e-6, "pe iv " + strike);
  }
  const tr = T.tradeFrom(candles, ind, k, null, null, null, null, null, null, an);
  for (const kk of ["direction", "setup", "ok"]) assert.equal(tr[kk], ref.trade[kk], kk);
  for (const kk of ["entry_plan", "stop", "tp1", "tp2", "risk", "atr"]) near(tr[kk], ref.trade[kk], 0.011, kk);
  assert.deepEqual(tr.notes, ref.trade.notes);
  for (const tag of ["short_pullback", "long_any"]) {
    const dir = tag === "short_pullback" ? "short" : "long";
    const t2 = T.tradeFrom(candles, ind, k, dir, null, null, null, null, null, null);
    for (const frac of [0.5, 1.0]) for (const cond of [true, false]) {
      const want = ref.replay[tag + "|" + fl(frac) + "|" + (cond ? "True" : "False")];
      const [res, still] = T.replay(candles, ind, k, t2, frac, 40, cond);
      assert.equal(still, want.still, tag);
      assert.equal(T.episodes(res), want.episodes, tag);
      assert.equal(res.length, want.rows.length, tag + frac + cond);
      res.forEach((r, j) => {
        const w = want.rows[j];
        assert.equal(r.start, w[0]); assert.equal(r.cls, w[1], tag + " row " + j);
        if (r.entered) { near(r.r, w[2], 1e-5, "r"); near(r.pts, w[4], 1e-4, "pts"); assert.equal(r.bars, w[3]); }
      });
    }
  }
});

test("the option leg: strike pick, quote offset, repricing and per-order charges match Python", () => {
  const k = T.cfgWith(null);
  const ch = T.parseChain(chainRaw);
  T.chainAnalytics(ch, CLOSE, k.risk_free_rate);
  const op = T.pickOption("short", ch, k, CLOSE, null, "NIFTY");
  for (const kk of ["expiry", "kind", "strike", "lot_size"]) assert.equal(op[kk], ref.option[kk], kk);
  for (const kk of ["delta", "iv_pct", "spread_pct", "entry_premium", "forward"]) near(op[kk], ref.option[kk], 0.011, kk);
  const sessions = T.tradingDaysAfter(candles[candles.length - 1].date, 40, null);
  assert.deepEqual(sessions.slice(0, 8), ref.sessions);
  assert.deepEqual(T.tradingDaysAfter(candles[candles.length - 1].date, 12, new Set(ref.holidays)), ref.sessions_h);   // 20 Oct is Dussehra
  const leg = new T.OptionLeg(op, ch, sessions, CLOSE, k, null, T.DEFAULT_RATES, "NFO");
  near(leg.offset, ref.leg.offset, 0.02, "offset"); near(leg.spread, ref.leg.spread, 1e-6); near(leg.T0, ref.leg.T0, 1e-9); near(leg.Topen0, ref.leg.Topen0, 1e-9);
  near(leg.basis, ref.leg.basis, 0.011); assert.equal(leg.barsToExpiry, ref.leg.bars_to_expiry);
  for (const [lvl, b, want] of ref.leg.premium) near(leg.premium(lvl, b), want, 0.03, "premium " + lvl + "/" + b);
  for (const [lvl, want] of ref.leg.entry_premium) near(leg.entryPremium(lvl), want, 0.03, "entry " + lvl);
  const fake = { entered: true, entry: 22880, exit: 22400, exit_bar: 3, tp1_bar: 1, tp1_px: 22600 };
  for (const [fr, want] of ref.leg.pnl) near(leg.pnl(fake, { frac: fr }), want, 1.5, "pnl frac " + fr);
});

test("a run repeats exactly for the same seed, and differs for another", async () => {
  const ch = T.parseChain(chainRaw);
  const run = (seed) => T.analyseSwing(candles, { chain: ch, nowMs: CLOSE, symbol: "NIFTY", paths: 1500, lots: 2, seed });
  const a = await run(5), b = await run(5), c = await run(6);
  assert.deepEqual(a.methods.bootstrap.outcomes, b.methods.bootstrap.outcomes);
  assert.equal(a.methods.implied.expectancy_r, b.methods.implied.expectancy_r);
  assert.notEqual(a.methods.implied.expectancy_r, c.methods.implied.expectancy_r);
});

test("the bootstrap and implied simulations agree with Python within sampling error", async () => {
  const ch = T.parseChain(chainRaw);
  const rep = await T.analyseSwing(candles, { chain: ch, nowMs: CLOSE, symbol: "NIFTY", paths: 40000, lots: 2, seed: 11, holidays: new Set(ref.holidays) });
  assert.equal(rep.trade.frac, ref.sim_trade_frac);
  near(rep.trade.cost_points, ref.sim_cost_points, 0.011, "cost points");
  for (const m of ["bootstrap", "implied"]) {
    const s = rep.methods[m], w = ref.sim[m];
    assert.equal(s.n, w.n);
    // two independent samples of 40,000 paths: 4 standard errors of a difference at p = 0.5 is about 0.014
    for (const [key, p] of Object.entries(w.outcomes)) near(s.outcomes[key] ? s.outcomes[key].p : 0, p, 0.016, m + " " + key);
    near(s.expectancy_r, w.expectancy_r, 0.025, m + " expectancy");
    near(s.p_profit, w.p_profit, 0.016, m + " p_profit");
    near(s.avg_win_r, w.avg_win_r, 0.05, m + " avg win"); near(s.avg_loss_r, w.avg_loss_r, 0.05, m + " avg loss");
    near(s.bars_median, w.bars_median, 1.01, m + " bars");
    near(s.futures_inr_per_lot.expectancy, w.futures.expectancy, 0.025 * 65 * 412 / 2.5, m + " futures");
    near(s.option_inr_per_lot.expectancy, w.option.expectancy, 150, m + " option");
    near(s.option_inr_per_lot.p_held_to_expiry, w.option.p_held_to_expiry, 0.016, m + " held to expiry");
    near(s.mae_r_p90, w.mae_p90, 0.08, m + " mae");
  }
  const rp = rep.methods.replay, w = ref.sim_replay;
  assert.equal(rp.n, w.n); assert.equal(rp.entered, w.entered); assert.equal(rp.independent, w.independent);
  near(rp.expectancy_r, w.expectancy_r, 1e-3, "replay expectancy");
  near(rep.methods.baseline.expectancy_r, ref.sim_baseline.expectancy_r, 1e-3, "baseline expectancy");
  assert.equal(rep.methods.baseline.independent, ref.sim_baseline.independent);
  // the bootstrap's one-year uncertainty is computed, and about the size Python finds
  near(rep.methods.bootstrap.history_se, ref.sim.bootstrap.history_se, 0.05, "history se");
});

test("honest refusals: wrong-side stop, intraday candles, wrong chain, too little history", async () => {
  await assert.rejects(T.analyseSwing(candles, { direction: "long", sl: candles[candles.length - 1].close + 10, paths: 200, methods: ["bootstrap"] }), /wrong side/);
  await assert.rejects(T.analyseSwing(candles, { direction: "long", paths: 200, chain: Object.assign(T.parseChain(chainRaw), { spot: 30000 }), nowMs: CLOSE }), /same instrument/);
  await assert.rejects(T.analyseSwing(candles.slice(0, 70), { direction: "long", paths: 200 }), /at least 80/);
  assert.throws(() => T.loadCandles([{ date: "2026-01-01", open: 1, high: 2, low: 0.5, close: 1.5 }]), /need at least 30/);
  const eq = await T.analyseSwing(candles, { direction: "long", instrument: "equity", qty: 10, paths: 300, methods: ["bootstrap"], symbol: "NIFTY" });
  assert.ok(eq.methods.bootstrap.equity_inr, "equity money");
  await assert.rejects(T.analyseSwing(candles, { direction: "short", instrument: "equity", qty: 10, paths: 300 }), /cannot be held short/);
});
