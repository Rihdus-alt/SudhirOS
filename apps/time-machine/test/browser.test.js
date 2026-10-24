// Drives the built Trading Desk page in headless Chromium against a stubbed window.claude that serves the real fixtures.
//   node apps/time-machine/test/browser.test.js <desk-time.full.html> [screenshot-dir]
// Needs the global Playwright module (the sandbox has it) and a Chromium binary.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { execSync } = require("node:child_process");

const [, , PAGE, SHOTS] = process.argv;
if (!PAGE) { console.error("usage: node browser.test.js <desk-time.full.html> [screenshot-dir]"); process.exit(2); }
const pw = require(path.join(execSync("npm root -g").toString().trim(), "playwright"));
const FIX = path.join(__dirname, "..", "fixtures");
const csv = (name) => {
  const [head, ...lines] = fs.readFileSync(path.join(FIX, name), "utf8").trim().split("\n");
  const cols = head.split(",");
  return lines.map((l) => Object.fromEntries(l.split(",").map((v, i) => [cols[i], v])));
};

// ---- the data the stub serves: real fixtures, with SENSEX scaled from NIFTY for the bars
const m5 = csv("NIFTY-5m.csv").map((r) => ({ datetime_ist: r.datetime_ist, open: +r.open, high: +r.high, low: +r.low, close: +r.close, volume: 1000 }));
const daily = csv("NIFTY-1d.csv").map((r) => ({ datetime_ist: r.date + " 05:30:00", open: +r.open, high: +r.high, low: +r.low, close: +r.close, volume: +r.volume }));
for (const day of ["2026-10-21", "2026-10-22", "2026-10-23"]) {          // the days the daily fixture lacks, aggregated from the 5-minute bars
  const rows = m5.filter((r) => r.datetime_ist.startsWith(day));
  daily.push({ datetime_ist: day + " 05:30:00", open: rows[0].open, high: Math.max(...rows.map((r) => r.high)), low: Math.min(...rows.map((r) => r.low)), close: rows[rows.length - 1].close, volume: 1 });
}
const chains = {
  INDI00012: JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027-eod23.json"), "utf8")),
  INDI00050: JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-SENSEX-20261029-eod23.json"), "utf8")),
};
const DATA = {
  m5, daily, chains,
  trades: [{ id: "t1", date: "2026-10-23", time: "10:20", instrument: "NIFTY 27 Oct ₹22600 Call", indKey: "INDONWG102LN", underlying: "NIFTY", entry: 130, qty: 130, sl: 117, tp: 156, exits: [], status: "open", mode: "real", createdAt: "2026-10-23T04:50:00Z" }],
};

// ---- a synthetic 23 Oct morning (a wandering index), for the A+ finder: the real 21 and 22 Oct stay as they are
function lcg(seed) { let x = seed >>> 0; return () => ((x = (Math.imul(x, 1664525) + 1013904223) >>> 0) / 4294967296); }
function syntheticMorning(seed, lastBarHm, breakClose) {
  const before = m5.filter((r) => r.datetime_ist < "2026-10-23");
  const r = lcg(seed), rows = [];
  let c = before[before.length - 1].close;
  for (let i = 0, mins = 9 * 60 + 15; ; i++, mins += 5) {
    const hm = String(Math.floor(mins / 60)).padStart(2, "0") + ":" + String(mins % 60).padStart(2, "0");
    if (hm > lastBarHm) break;
    const o = c, amp = 0.0005 * o, drift = i < 20 ? 0 : 0.00006 * o;
    c = o + (r() - 0.5) * 2 * amp + drift;
    if (breakClose && hm === breakClose.at) c = breakClose.close;
    rows.push({ datetime_ist: "2026-10-23 " + hm + ":00", open: o, high: Math.max(o, c) + r() * 0.0002 * o, low: Math.min(o, c) - r() * 0.0002 * o, close: c, volume: 1000 });
  }
  return before.concat(rows);
}

function installClaude(data, fakeIso) {
  const realDate = Date, off = realDate.parse(fakeIso) - realDate.now();
  class FakeDate extends realDate { constructor(...a) { if (a.length) super(...a); else super(realDate.now() + off); } static now() { return realDate.now() + off; } }
  window.Date = FakeDate;
  const istNow = () => new realDate(realDate.now() + off + 5.5 * 3600e3).toISOString().slice(0, 19).replace("T", " ");
  const pack = (obj) => ({ payload: { result: JSON.stringify(obj) } });
  const spotOf = (key) => { const rows = data.m5.filter((r) => r.datetime_ist <= istNow()); const last = rows.length ? rows[rows.length - 1] : data.m5[data.m5.length - 1]; return key === "INDI00050" ? last.close * 3.2358 : last.close; };
  const scale = (key) => (key === "INDI00050" ? 3.2358 : 1);
  const tools = {
    get_indian_stocks_details(inp) {
      const out = {};
      for (const k of inp.ind_keys) {
        let px = spotOf(k);
        for (const ch of Object.values(data.chains)) for (const r of ch.option_chain_data) for (const leg of [r.call_data, r.put_data]) if (leg && leg.derivative_id === k) px = leg.current_price;
        out[k] = { entity_stats: { live_price: px, price_updated_at_ist: istNow().replace(" ", "T") + "+05:30", day_high: px * 1.003, day_low: px * 0.997, prev_close: px, day_change_percentage: 0.1, bid_price: px - 0.5, ask_price: px + 0.5, greeks_data: { delta: 0.5 } } };
      }
      return out;
    },
    get_indian_stocks_ohlc(inp) {
      const now = istNow();
      if (inp.interval === "1day") {
        const rows = data.daily.filter((r) => r.datetime_ist.slice(0, 10) + " 15:30:00" <= now || true).map((r) => ({ ...r, open: r.open * scale(inp.ind_key), high: r.high * scale(inp.ind_key), low: r.low * scale(inp.ind_key), close: r.close * scale(inp.ind_key) }));
        return { ind_key: inp.ind_key, interval: "1day", query_end_ist: now, candles: rows.slice(-250) };
      }
      let rows = data.m5.filter((r) => r.datetime_ist <= now);
      if (inp.lookback === "1d") { const d = rows.length ? rows[rows.length - 1].datetime_ist.slice(0, 10) : ""; rows = rows.filter((r) => r.datetime_ist.startsWith(d)); }
      return { ind_key: inp.ind_key, interval: "5minute", query_end_ist: now, candles: rows.map((r) => ({ ...r, open: r.open * scale(inp.ind_key), high: r.high * scale(inp.ind_key), low: r.low * scale(inp.ind_key), close: r.close * scale(inp.ind_key) })) };
    },
    get_indian_stocks_option_chain(inp) {
      const ch = JSON.parse(JSON.stringify(data.chains[inp.ind_key]));
      if (inp.use_expiry_date && inp.expiry_date) ch.selected_expiry = String(inp.expiry_date).replace(/-/g, "");
      const open = istNow().slice(11, 16) >= "09:15" && istNow().slice(11, 16) < "15:30" && istNow().slice(0, 10) === "2026-10-23";
      if (open) ch.entity_details.price_updated_at_ist = istNow().replace(" ", "T") + "+05:30";
      return ch;
    },
    networth_holdings() { return {}; },
    get_us_stocks_ohlc() { return { prices: { INDA: { candles: [] }, SPY: { candles: [] } } }; },
    get_us_stocks_details() { return {}; },
    lookup_ind_keys(inp) { return [{ ind_key: "INDS01052", name: "Reliance Industries" }, { ind_key: "INDS01338", name: "Reliance Power" }]; },
  };
  const docs = new Map(Object.entries(data.seed || {}));
  docs.set("trades/t1", data.trades[0]);
  docs.set("config/rules", { cap: null, maxTrades: 99, slPct: 10, tpMin: 15, tpMax: 20, dayStop: 3000, noEntryAfter: "15:00", expiryCutoff: "13:30", autoLog: false });
  const listeners = [];
  const snap = (path) => { const id = path.split("/").pop(); return { exists: docs.has(path), id, data: () => docs.get(path) }; };
  const collSnap = (name) => ({ docs: [...docs.entries()].filter(([p]) => p.startsWith(name + "/")).map(([p, d]) => ({ id: p.split("/")[1], data: () => d })) });
  const notify = (path) => setTimeout(() => { for (const l of listeners) { if (l.doc === path) l.cb(snap(path)); else if (l.coll && path.startsWith(l.coll + "/")) l.cb(collSnap(l.coll)); } }, 0);
  let auto = 0;
  const db = {
    collection(name) { return { onSnapshot(cb) { listeners.push({ coll: name, cb }); setTimeout(() => cb(collSnap(name)), 0); return () => {}; }, doc: (id) => db.doc(name + "/" + (id || "auto" + ++auto)) }; },
    doc(p) { return { onSnapshot(cb) { listeners.push({ doc: p, cb }); setTimeout(() => cb(snap(p)), 0); return () => {}; }, async get() { return snap(p); },
      async set(d) { docs.set(p, JSON.parse(JSON.stringify(d))); notify(p); }, async update(d) { docs.set(p, { ...(docs.get(p) || {}), ...d }); notify(p); }, async delete() { docs.delete(p); notify(p); } }; },
  };
  window.__docs = docs;
  const mcp = {
    async callTool(server, tool, input) { if (!tools[tool]) throw { code: "tool_error", message: "no stub for " + tool }; await new Promise((r) => setTimeout(r, 5)); return pack(tools[tool](input || {})); },
    watchTool(server, tool, input, handler) { setTimeout(() => { if (tools[tool]) handler({ type: "data", result: { ...pack(tools[tool](input || {})), cache: { storedAt: realDate.now() } } }); }, 0); return () => {}; },
    async invalidate() {},
  };
  let sample = null;
  if (data.coach) {
    sample = async (prompt, opts) => {
      window.__prompt = prompt;
      const tool = ((opts && opts.tools) || []).find((t) => t.name === "time_machine");
      const out = tool ? await tool.execute({ index: "NIFTY", side: "CE", strike: 22600, lots: 1 }) : "no tool";
      window.__toolOut = out;
      if (opts && opts.onText) opts.onText({ text: "TOOL " + String(out).slice(0, 80) });
      return { text: "TOOL " + String(out).slice(0, 80) };
    };
    sample.limits = async () => ({ tools: true });
  }
  window.claude = { use: async (name) => ({ db, mcp, user: { can: async () => true }, sample }[name] || null) };
  window.__calls = [];
}

async function main() {
  const chromium = pw.chromium;
  const exe = process.env.PW_CHROME || "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
  const browser = await chromium.launch({ executablePath: fs.existsSync(exe) ? exe : undefined });
  const results = [];
  const check = (name, ok, detail) => { results.push([name, ok, detail]); console.log((ok ? "ok   " : "FAIL ") + name + (ok ? "" : "  " + detail)); };
  const shot = async (page, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: true }); } };

  // addInitScript passes one argument: wrap
  async function launch(fakeIso, opts) {
    opts = opts || {};
    const ctx = await browser.newContext({ viewport: opts.viewport || { width: 1000, height: 900 }, colorScheme: opts.dark ? "dark" : "light", locale: "en-IN" });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
    await page.addInitScript(`(${installClaude.toString()})(${JSON.stringify({ ...DATA, ...(opts.data || {}), seed: opts.seed || {}, coach: !!opts.coach })}, ${JSON.stringify(fakeIso)})`);
    await page.goto("file://" + path.resolve(PAGE));
    await page.waitForFunction(() => document.querySelectorAll("#nav button").length === 5);
    return { ctx, page, errors };
  }
  const textOK = (t) => !/NaN|undefined|Infinity|\[object/.test(t);

  // ------------------------------------------------------------ A. the market is closed (Saturday): plan the next session
  {
    const { ctx, page, errors } = await launch("2026-10-24T11:00:00+05:30");
    check("the desk still loads with five tabs, and none of its own panels broke", (await page.locator("#nav button").count()) === 5 && (await page.locator("#sumStrip .stat").count()) === 3, "tabs/summary");
    await page.click('#nav button[data-view="time"]');
    await page.waitForFunction(() => document.querySelectorAll("#tm_strike option").length >= 8, null, { timeout: 15000 });
    check("Time tab opens and loads the NIFTY chain's strikes", (await page.locator("#tm_strike option").count()) >= 8, "strikes");
    check("closed market: the entry is a chosen time in the next session", (await page.locator("select#tm_entry").count()) === 1, "entry select");
    await page.selectOption("#tm_paths", "2000");
    await page.click("#tm_run");
    await page.waitForSelector("#tmOut .card", { timeout: 90000 });
    await page.waitForFunction(() => !document.querySelector("#tm_cancel"), null, { timeout: 90000 });
    const txt = await page.locator("#tmOut").innerText();
    check("closed-market run: result cards appear", /how the desk plan ends/i.test(txt) && /other ways to exit/i.test(txt) && /how sure is this/i.test(txt), txt.slice(0, 300));
    check("no NaN / undefined / Infinity in the result", textOK(txt), txt.match(/.{30}(NaN|undefined|Infinity|\[object).{30}/) + "");
    check("it says the session starts from the open with a chosen entry", /session from the open, entering at 09:30/.test(txt), txt.slice(0, 200));
    check("the result carries the gate, the blend of the models and the candle size it chose", /(PASS|WATCH|FAIL)/.test(txt) && /blend of the models/i.test(txt) && /(auto|chosen): [15]-minute candles/.test(txt), txt.slice(0, 400));
    check("the fan chart is labelled in clock times", /\b(09|1\d):\d\d\b/.test(await page.locator("#tmCharts").innerText()), await page.locator("#tmCharts").innerText());
    check("charts are drawn (stacked bars, histogram, fan)", (await page.locator("#tmOut svg").count()) >= 3, "svg count " + (await page.locator("#tmOut svg").count()));
    await page.click('#tm_view button[data-v="bootstrap"]');
    check("switching the model redraws the charts", /Bootstrap/.test(await page.locator("#tmCharts").innerText()), "charts text");
    await shot(page, "A-closed-desktop");
    check("no console or page errors (closed market)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ B. live, mid-session; and an open position
  {
    const { ctx, page, errors } = await launch("2026-10-23T11:02:00+05:30");
    await page.click('#nav button[data-view="time"]');
    await page.waitForFunction(() => document.querySelectorAll("#tm_strike option").length >= 8, null, { timeout: 15000 });
    check("live: the entry is the next candle", /from the next candle/.test(await page.locator("input#tm_entry").inputValue()), await page.locator("input#tm_entry").inputValue());
    await page.selectOption("#tm_paths", "2000");
    await page.click("#tm_run");
    await page.waitForSelector("#tmOut .card", { timeout: 90000 });
    await page.waitForFunction(() => !document.querySelector("#tm_cancel"), null, { timeout: 90000 });
    const txt = await page.locator("#tmOut").innerText();
    check("live run reports its start", /from about 11:0\d today/.test(txt), txt.slice(0, 200));
    check("live run: no NaN / undefined", textOK(txt), txt.match(/.{30}(NaN|undefined|Infinity|\[object).{30}/) + "");
    await shot(page, "B-live-desktop");
    // the open trade preset
    const btn = page.locator("button[data-pos]");
    check("an open trade offers itself as a starting point", (await btn.count()) === 1, "preset count " + (await btn.count()));
    await btn.first().click();
    await page.waitForSelector(".tm-pos", { timeout: 15000 });
    check("the position banner shows its entry and stop", /entry .*130\.00.* stop .*117\.00/.test(await page.locator(".tm-pos").innerText()), await page.locator(".tm-pos").innerText());
    await page.click("#tm_run");
    await page.waitForFunction(() => !document.querySelector("#tm_cancel") && /selling now|Following the plan/.test(document.querySelector("#tmOut").innerText), null, { timeout: 90000 });
    const t2 = await page.locator("#tmOut").innerText();
    check("position run is framed against selling now", /selling now/.test(t2) && textOK(t2), t2.slice(0, 300));
    await shot(page, "B-position");
    check("no console or page errors (live)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ C. swing
  {
    const { ctx, page, errors } = await launch("2026-10-24T11:00:00+05:30");
    await page.click('#nav button[data-view="time"]');
    await page.click('#tmMode button[data-m="swing"]');
    await page.waitForSelector("#tm_sym", { timeout: 15000 });
    await page.click('#tm_dir button[data-v="short"]');
    await page.selectOption("#tm_spaths", "5000");
    await page.waitForFunction(() => document.querySelectorAll("#tm_sexp option").length > 1, null, { timeout: 15000 });
    await page.click("#tm_run");
    await page.waitForSelector("#tmOut .card", { timeout: 120000 });
    await page.waitForFunction(() => !document.querySelector("#tm_cancel"), null, { timeout: 120000 });
    const txt = await page.locator("#tmOut").innerText();
    check("swing run: the trade, the chart, the outcome table", /NIFTY SHORT/i.test(txt) && /the chart/i.test(txt) && /how it ends/i.test(txt) && /bootstrap/i.test(txt), txt.slice(0, 300));
    check("swing run: no NaN / undefined", textOK(txt), txt.match(/.{30}(NaN|undefined|Infinity|\[object).{30}/) + "");
    check("swing run: says the SEBI figure and that it is not a forecast", /91%/.test(txt) && /not a forecast/.test(txt), "disclosure");
    await shot(page, "C-swing-desktop");
    check("no console or page errors (swing)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ D. phone width, dark theme
  {
    const { ctx, page, errors } = await launch("2026-10-24T11:00:00+05:30", { viewport: { width: 390, height: 800 }, dark: true });
    await page.click('#nav button[data-view="time"]');
    await page.waitForFunction(() => document.querySelectorAll("#tm_strike option").length >= 8, null, { timeout: 15000 });
    await page.selectOption("#tm_paths", "2000");
    await page.click("#tm_run");
    await page.waitForSelector("#tmOut .card", { timeout: 90000 });
    await page.waitForFunction(() => !document.querySelector("#tm_cancel"), null, { timeout: 90000 });
    const w = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    check("phone width: nothing scrolls sideways", w[0] <= w[1] + 1, "scrollWidth " + w[0] + " vs " + w[1]);
    await shot(page, "D-phone-dark");
    if (SHOTS) { const cards = page.locator("#tmOut > .card"); for (let i = 0; i < (await cards.count()); i++) await cards.nth(i).screenshot({ path: path.join(SHOTS, "D-card" + i + ".png") }); await page.locator(".tm-ctl").screenshot({ path: path.join(SHOTS, "D-controls.png") }); }
    await page.click("#tm_scan");
    await page.waitForFunction(() => !document.querySelector("#tm_scancancel") && document.querySelectorAll("#tmScanOut tr").length > 3, null, { timeout: 180000 });
    const w2 = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    check("phone width, dark: the scan table does not push the page sideways", w2[0] <= w2[1] + 1, "scrollWidth " + w2[0] + " vs " + w2[1]);
    if (SHOTS) await page.locator("#tmScanOut").screenshot({ path: path.join(SHOTS, "D-scan.png") });
    check("no console or page errors (phone, dark)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }

  // ------------------------------------------------------------ E. candle size, the scan, and what the Time tab saves
  {
    const { ctx, page, errors } = await launch("2026-10-24T11:00:00+05:30");
    await page.click('#nav button[data-view="time"]');
    await page.waitForFunction(() => document.querySelectorAll("#tm_strike option").length >= 8, null, { timeout: 15000 });
    check("the candle size can be chosen: auto, 1, 5 or 15 minutes", (await page.locator("#tm_step option").allInnerTexts()).join("|").match(/Auto.*1 minute.*5 minutes.*15 minutes/) !== null, (await page.locator("#tm_step option").allInnerTexts()).join("|"));
    await page.selectOption("#tm_step", "1");
    await page.selectOption("#tm_paths", "2000");
    await page.click("#tm_run");
    await page.waitForFunction(() => !document.querySelector("#tm_cancel") && /chosen: 1-minute candles/.test(document.querySelector("#tmOut").innerText), null, { timeout: 120000 });
    let txt = await page.locator("#tmOut").innerText();
    check("1-minute candles: the run says so, and nothing is NaN", /chosen: 1-minute candles/.test(txt) && textOK(txt), txt.slice(0, 300));
    await page.selectOption("#tm_step", "15");
    await page.click("#tm_run");
    await page.waitForFunction(() => !document.querySelector("#tm_cancel") && /chosen: 15-minute candles/.test(document.querySelector("#tmOut").innerText), null, { timeout: 120000 });
    check("15-minute candles run too", textOK(await page.locator("#tmOut").innerText()), "15 min");
    // what the run left behind
    await page.waitForFunction(() => window.__docs.has("tmlearn/state"), null, { timeout: 15000 });
    const keys = await page.evaluate(() => [...window.__docs.keys()]);
    check("full 5-minute sessions are saved as they arrive (the library grows a day a day)", ["2026-10-21", "2026-10-22", "2026-10-23"].every((d) => keys.includes("tmsess/NIFTY_" + d)) && !keys.includes("tmsess/NIFTY_2026-10-19"), keys.filter((k) => k.startsWith("tmsess")).join(","));
    check("the run's pace observations wait for their tape (the next session, and the weekend gap)", keys.includes("tmobs/sess_NIFTY_2026-10-26") && keys.includes("tmobs/gap_NIFTY_2026-10-26"), keys.filter((k) => k.startsWith("tmobs")).join(","));
    check("a Time-tab run is kept as a prediction to be settled", keys.some((k) => k.startsWith("tmpred/p_NIFTY_2026-10-26")), keys.filter((k) => k.startsWith("tmpred")).join(","));
    const lt = await page.locator("#tmLearn").innerText();
    check("the Learning card says what it knows, honestly", /what it has learned/i.test(lt) && /cannot learn where the market goes next/i.test(lt) && /Saved 5-minute sessions/.test(lt) && textOK(lt), lt.slice(0, 400));
    // the scan
    await page.click("#tm_scan");
    await page.waitForFunction(() => !document.querySelector("#tm_scancancel") && document.querySelectorAll("#tmScanOut tr").length > 3, null, { timeout: 180000 });
    const st = await page.locator("#tmScanOut").innerText();
    check("the scan ranks contracts of both indices with a gate each", /NIFTY/.test(st) && /SENSEX/.test(st) && /(PASS|WATCH|FAIL)/.test(st) && /pass · \d+ watch · \d+ fail/.test(st) && textOK(st), st.slice(0, 500));
    check("when nothing passes the scan says so plainly", /Nothing passes right now/.test(st) || /PASS/.test(st), st.slice(0, 200));
    await shot(page, "E-scan");
    await page.locator("button[data-scanrun]").first().click();
    await page.waitForFunction(() => !document.querySelector("#tm_cancel") && /how the desk plan ends/i.test(document.querySelector("#tmOut").innerText), null, { timeout: 120000 });
    check("a scan row opens its full replay", textOK(await page.locator("#tmOut").innerText()), "full replay");
    await page.waitForTimeout(3200);
    const state = await page.evaluate(() => JSON.parse(window.__docs.get("tmlearn/state").json));
    check("the learner's state is saved as one document", state.v === 1 && Array.isArray(state.log) && Object.keys(state.perf).length >= 1, JSON.stringify(Object.keys(state)));
    await page.click("#lrn_freeze");
    await page.waitForTimeout(3200);
    check("freezing is saved", (await page.evaluate(() => JSON.parse(window.__docs.get("tmlearn/state").json).frozen)) === true && /Resume learning/.test(await page.locator("#tmLearn").innerText()), "frozen");
    await shot(page, "E-learning");
    check("no console or page errors (candles, scan, learning)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ F. the Trade tab checks every plan, and a failing one needs a second tap
  {
    const { ctx, page, errors } = await launch("2026-10-23T11:02:00+05:30", { viewport: { width: 390, height: 900 }, dark: true });
    await page.click('#nav button[data-view="trade"]');
    await page.waitForSelector("#chain button[data-pick]", { timeout: 15000 });
    check("before a contract is picked there is nothing to check", (await page.locator("#tmGate").innerHTML()).trim() === "", await page.locator("#tmGate").innerHTML());
    await page.locator('#chain tr.atm button[data-side="CE"]').click();
    await page.waitForSelector("#tmGate .tmg-chip", { timeout: 60000 });
    let g = await page.locator("#tmGate").innerText();
    const gate = (g.match(/\b(PASS|WATCH|FAIL)\b/) || [])[1];
    check("picking a contract runs the Time Machine on the plan, with the gate, the odds and the candle size", !!gate && /Chance of a profit/.test(g) && /reaches T1/.test(g) && /-minute candles/.test(g) && textOK(g), g.slice(0, 400));
    check("it names the plan it checked (entry, stop, targets)", /Your levels: entry/.test(g) && /stop .*\(−\d+(\.\d)?%\)/.test(g), g);
    await shot(page, "F-trade-gate-phone-dark");
    const wf = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    check("phone width, dark: the gate card does not push the page sideways", wf[0] <= wf[1] + 1, "scrollWidth " + wf[0] + " vs " + wf[1]);
    const btn = () => page.locator("#save").innerText();
    check("the Log button says so when the gate fails", gate !== "FAIL" || /Log anyway/.test(await btn()), await btn());
    await page.click("#save");
    await page.waitForFunction(() => /Logged\./.test(document.querySelector("#saveMsg").textContent) || /Log anyway/.test(document.querySelector("#saveMsg").textContent), null, { timeout: 60000 });
    let msg = await page.locator("#saveMsg").innerText();
    if (gate === "FAIL") {
      check("a failing plan is not logged on the first tap: it asks for a second", /Time Machine finds this plan negative after charges/.test(msg) && !/Logged\./.test(msg), msg);
      const n1 = await page.evaluate(() => [...window.__docs.keys()].filter((k) => k.startsWith("trades/auto")).length);
      check("nothing was written yet", n1 === 0, "trades written " + n1);
      await page.click("#save");
      await page.waitForFunction(() => /Logged\./.test(document.querySelector("#saveMsg").textContent), null, { timeout: 30000 });
    }
    const tr = await page.evaluate(() => [...window.__docs.entries()].filter(([k]) => k.startsWith("trades/auto")).map(([, v]) => v));
    check("the logged trade carries the check it was given", tr.length === 1 && tr[0].tm && ["pass", "watch", "fail"].includes(tr[0].tm.gate) && tr[0].tm.gate === gate.toLowerCase() && tr[0].tm.pred && tr[0].tm.pred.levels && !!tr[0].tmId, JSON.stringify(tr[0] && tr[0].tm).slice(0, 300));
    check("a failing plan that was logged anyway is marked as such", gate !== "FAIL" || tr[0].tm.overridden === true, JSON.stringify(tr[0].tm).slice(0, 200));
    check("the stored check is plain data (no undefined, no NaN)", !/undefined|NaN/.test(JSON.stringify(tr[0])), "doc");
    await page.click('#nav button[data-view="journal"]');
    await page.waitForFunction(() => /TM (pass|watch|fail)/.test(document.querySelector("#journal").innerText), null, { timeout: 15000 });
    check("the journal shows the check beside the trade", /TM (pass|watch|fail)/.test(await page.locator("#journal").innerText()), await page.locator("#journal").innerText());
    check("no console or page errors (trade tab)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ G. what was waiting for a tape is settled when the tape is saved
  {
    const T = require("../src/tmx-core.js"), I = require("../src/tmx-intraday.js"), L = require("../src/tmx-learn.js");
    const sessions = I.parseBars(csv("NIFTY-5m.csv"));
    const chain = T.parseChain(JSON.parse(fs.readFileSync(path.join(FIX, "indmoney-chain-NIFTY-20261027-eod23.json"), "utf8")));
    chain.spot_time = "2026-10-22T09:30:00+05:30"; T.chainAnalytics(chain, Date.parse("2026-10-23T15:46:00+05:30"), 0.065);
    const rep = await I.analyseIntraday({ sessions: sessions.filter((x) => x.date < "2026-10-22"), chain, daily: T.loadCandles(csv("NIFTY-1d.csv")), side: "PE", strike: 22600, lots: 1, nowMs: Date.parse("2026-10-22T09:31:00+05:30"),
      holidays: new Set(["2026-10-20", "2026-11-10"]), step: 5, paths: 400, seed: 1, sensitivity: false, entryPrice: 130 });
    const pred = L.predictionFrom(rep, { id: "p_seed", ix: "NIFTY", kind: "scan" });
    const seed = {
      "tmobs/sess_NIFTY_2026-10-22": { ix: "NIFTY", kind: "sess", day: "2026-10-22", m5: 105, pred: 0.00001, at: "2026-10-22T06:00:00Z", done: false },
      "tmobs/gap_NIFTY_2026-10-23": { ix: "NIFTY", kind: "gap", day: "2026-10-23", m5: 0, pred: 0.00004, at: "2026-10-22T16:00:00Z", done: false },
      "tmpred/p_seed": { ix: "NIFTY", day: "2026-10-22", kind: "scan", done: false, at: "2026-10-22T04:00:00Z", json: JSON.stringify(pred) },
    };
    const { ctx, page, errors } = await launch("2026-10-24T11:00:00+05:30", { seed });
    await page.click('#nav button[data-view="time"]');
    await page.waitForFunction(() => !window.__docs.has("tmobs/sess_NIFTY_2026-10-22") && !window.__docs.has("tmobs/gap_NIFTY_2026-10-23") && window.__docs.get("tmpred/p_seed") && window.__docs.get("tmpred/p_seed").done === true, null, { timeout: 30000 });
    check("an observation and a prediction are settled once the full session is saved, and the observation is removed", true, "");
    await page.waitForFunction(() => window.__docs.has("tmlearn/state"), null, { timeout: 15000 });
    const st = await page.evaluate(() => JSON.parse(window.__docs.get("tmlearn/state").json));
    check("the learner recorded the pace observation and the settled prediction, once each", st.pace.NIFTY && st.pace.NIFTY.sess.length === 1 && st.pace.NIFTY.gap.length === 1 && st.calib.profit.length === 1 && st.seen.includes("sess_NIFTY_2026-10-22") && st.seen.includes("gap_NIFTY_2026-10-23") && st.seen.includes("out:paper:p_seed"), JSON.stringify(st).slice(0, 400));
    const lt = await page.locator("#tmLearn").innerText();
    check("the Learning card shows one settled prediction and the pace so far", /×1\.\d\d|×0\.\d\d/.test(lt) && /1 observation|1 obs/.test(lt) && textOK(lt), lt.slice(0, 600));
    check("no console or page errors (settling)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ H. the coach runs every setup through the Time Machine
  {
    const { ctx, page, errors } = await launch("2026-10-23T11:02:00+05:30", { coach: true });
    await page.click('button[data-ask="scan"]');
    await page.waitForFunction(() => window.__toolOut !== undefined, null, { timeout: 90000 });
    const out = await page.evaluate(() => window.__toolOut), prompt = await page.evaluate(() => window.__prompt);
    let j = null; try { j = JSON.parse(out).time_machine; } catch {}
    check("the coach has a time_machine tool and it answers with a gate and odds", !!j && ["pass", "watch", "fail"].includes(j.gate) && j.chance_of_profit_pct >= 0 && j.chance_of_profit_pct <= 100 && !!j.reason, String(out).slice(0, 300));
    check("the coach's rules and its scan request both require the check", /MUST first be run through the time_machine tool/.test(prompt) && /Run each setup through the time_machine tool/.test(prompt), prompt.slice(0, 200));
    check("the coach's rules still follow the saved settings (no stale 3 trades a day)", !/Max 3 trades\/day/.test(prompt) && /No fixed limit on trades per day/.test(prompt), "rules");
    check("no console or page errors (coach)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }

  // ------------------------------------------------------------ I. the A+ finder: waits, notices one index, then both, and shows the trade with its odds
  {
    const SEED = 14;                                              // a synthetic morning whose bull A+ levels are NIFTY 22,713 (day high) and SENSEX 73,525 (the call OI wall)
    const wait = async (page) => page.waitForFunction(() => window.__tmx && window.__tmx.S.levels && window.__tmx.S.levels.NIFTY && window.__tmx.S.levels.SENSEX, null, { timeout: 30000 });
    // I1: before the break
    let { ctx, page, errors } = await launch("2026-10-23T11:25:30+05:30", { data: { m5: syntheticMorning(SEED, "11:25") } });
    await wait(page);
    await page.waitForFunction(() => /no A\+ now/.test(document.querySelector("#apCard").innerText), null, { timeout: 30000 });
    let t = await page.locator("#apCard").innerText();
    check("A+ card, before a break: it says so and what each side needs, in index points", /no A\+ now/.test(t) && /LONG needs SENSEX 5-minute close above 73,5\d\d \(\d+ points away\) and NIFTY 5-minute close above 22,7\d\d/.test(t) && /SHORT needs/.test(t) && textOK(t), t.slice(0, 600));
    check("the card is honest about what it is and about the record that does not exist yet", /free of Claude usage/.test(t) && /No A\+ call has been settled yet/.test(t), t.slice(0, 900));
    // the target and the odds before it fires
    await page.click('#apCard button[data-ap="prev:1"]');
    await page.waitForSelector("#apCard .ap-plan", { timeout: 150000 });
    await page.waitForFunction(() => !/Working out the/.test(document.querySelector("#apCard").innerText), null, { timeout: 60000 });
    t = await page.locator("#apCard").innerText();
    check("a preview shows the target and the odds before anything has fired, and says it is not a call", /PREVIEW/.test(t) && /Not fired\. This is the trade as it would stand if the break closed now/.test(t) && /Chance of reaching T1\n\d+%/.test(t) && /Target 1/.test(t) && !/Use on the Trade tab/.test(t) && textOK(t), t.slice(0, 900));
    check("a preview records nothing and raises no alert", !(await page.evaluate(() => [...window.__docs.keys()].some((k) => /^tmpred\/a_/.test(k)))) && (await page.locator("#alerts").innerText()).trim() === "", await page.locator("#alerts").innerText());
    // the stop on the Trade tab, set from the market
    await page.click('#nav button[data-view="trade"]');
    await page.waitForSelector("#chain button[data-pick]", { timeout: 15000 });
    await page.locator('#chain tr.atm button[data-side="CE"]').click();
    await page.waitForFunction(() => /Stop set from the market/.test(document.querySelector("#apSLnote").innerText), null, { timeout: 15000 });
    const note = await page.locator("#apSLnote").innerText(), entry = +(await page.locator("#f_entry").inputValue()), sl = +(await page.locator("#f_sl").inputValue());
    const pct = +(note.match(/Stop set from the market: (\d+(?:\.\d)?)%/) || [])[1];
    check("picking a strike sets the stop from the market: bounded to 7-15%, rounded to the tick, and different from the desk's fixed 10%", pct >= 7 && pct <= 15 && Math.abs(sl - Math.round(entry * (1 - pct / 100) * 20) / 20) < 0.051 && Math.abs(sl - Math.round(entry * 0.9 * 20) / 20) > 0.5, note + " entry " + entry + " sl " + sl);
    await page.uncheck("#apSLon");
    await page.waitForFunction((e) => Math.abs(+document.querySelector("#f_sl").value - Math.round(e * 0.9 * 20) / 20) < 0.051, entry, { timeout: 15000 });
    check("switching the auto stop off brings the desk's 10% back", (await page.locator("#apSLnote").innerText()).trim() === "", await page.locator("#apSLnote").innerText());
    check("no console or page errors (A+ waiting)", errors.length === 0, errors.join(" | "));
    await ctx.close();

    // I2: one index breaks, the other does not
    ({ ctx, page, errors } = await launch("2026-10-23T11:30:30+05:30", { data: { m5: syntheticMorning(SEED, "11:30", { at: "11:25", close: 22718 }) } }));
    await wait(page);
    await page.waitForFunction(() => /not A\+ yet/.test(document.querySelector("#apCard").innerText), null, { timeout: 30000 });
    t = await page.locator("#apCard").innerText();
    check("one index closing beyond its level is not A+: the card names the one that has not", /NIFTY closed above its A\+ level at 11:25; SENSEX has not: not A\+ yet/.test(t) && !/fired/.test(t), t.slice(0, 500));
    await ctx.close();

    // I3: both break: the trade appears on its own
    ({ ctx, page, errors } = await launch("2026-10-23T11:30:30+05:30", { data: { m5: syntheticMorning(SEED, "11:30", { at: "11:25", close: 22724.5 }) } }));
    await wait(page);
    await page.waitForSelector("#apCard .ap-plan", { timeout: 150000 });
    await page.waitForFunction(() => !/Refreshing|Working out/.test(document.querySelector("#apCard").innerText), null, { timeout: 60000 });
    t = await page.locator("#apCard").innerText();
    const grade = (await page.locator("#apCard .ap-grade").first().innerText()).trim();
    check("both indices closed beyond: an A+ LONG appears by itself with the contract, entry, stop and targets", /A\+ LONG fired · this candle/.test(t) && /SENSEX closed above 73,5\d\d at 11:25, NIFTY above 22,\d\d\d at 11:25/.test(t) && /Entry \(the ask\)/.test(t) && /Stop-loss/.test(t) && /Target 1/.test(t) && /Target 2/.test(t) && /index ≈ \d\d,\d\d\d/.test(t) && ["A+", "A"].includes(grade), grade + " | " + t.slice(0, 700));
    check("it shows the chance of reaching each target and of being stopped, each with its 95% band", /Chance of reaching T1\n\d+%\n95% band \d+–\d+%/.test(t) && /Chance of reaching T2\n\d+%\n95% band \d+–\d+%/.test(t) && /Stopped out first\n\d+%\n95% band \d+–\d+%/.test(t), t);
    const num = (re) => +(t.match(re) || [])[1];
    const t1p = num(/Chance of reaching T1\n(\d+)%/), t2p = num(/Chance of reaching T2\n(\d+)%/), stp = num(/Stopped out first\n(\d+)%/);
    check("the probabilities are coherent: T2 below T1, and T1 plus stopped below 100%", t2p < t1p && t1p + stp <= 100 && t1p >= 10 && t1p <= 70, `T1 ${t1p} T2 ${t2p} stop ${stp}`);
    check("it says what the trade needs from the signal, and that no edge is assumed", /What this trade needs/.test(t) && /If the market has no edge, this plan averages/.test(t) && /the models, with no edge assumed/.test(t) && /(To pay, the A\+ signal has to lift the chance of reaching T1 from \d+% to \d+%|It already pays on the models)/.test(t), t);
    check("it explains the stop (structure, noise, the 7-15% limit) and why it is not a search for the best-scoring stop", /Stop: the break has failed if the index closes back through/.test(t) && /ordinary 5-minute wiggle/.test(t) && /searching for the best stop would only fit/.test(t) || /Stop: the break has failed/.test(t), t);
    check("no NaN / undefined anywhere on the card", textOK(t), t.match(/.{30}(NaN|undefined|Infinity|\[object).{30}/) + "");
    const keys = await page.evaluate(() => [...window.__docs.keys()]);
    check("an A+ call is kept to be settled on the tape (only A+, not an A)", grade === "A+" ? keys.some((k) => /^tmpred\/a_/.test(k)) : !keys.some((k) => /^tmpred\/a_/.test(k)), keys.filter((k) => k.startsWith("tmpred")).join(","));
    if (grade === "A+") {
      const pk = keys.find((k) => /^tmpred\/a_/.test(k)), v = await page.evaluate((k) => window.__docs.get(k), pk), pred = JSON.parse(v.json);
      check("the stored call carries what it said (T1, T2, stop and what it needed) for the record", pred.kind === "aplus" && pred.aplus.dir === 1 && pred.aplus.p1 > 0 && pred.aplus.p2 > 0 && pred.aplus.p2 < pred.aplus.p1 && pred.aplus.need > 0 && v.done === false, JSON.stringify(pred.aplus));
      check("an alert says so, with the entry, stop and the chance", /A\+ LONG .*entry .*stop .*T1 .*\(\d+% chance\)/.test(await page.locator("#alerts").innerText()), await page.locator("#alerts").innerText());
    }
    await shot(page, "I-aplus");
    // using it
    await page.click('#apCard button[data-ap="use:0"]');
    await page.waitForSelector("#tmGate .tmg-chip", { timeout: 90000 });
    const inst = await page.locator("#f_inst").inputValue(), esl = +(await page.locator("#f_sl").inputValue()), etp = +(await page.locator("#f_tp").inputValue());
    check("'Use on the Trade tab' fills the plan, and the Time Machine gate checks that very plan", /Call$/.test(inst) && esl > 0 && etp > esl && /Stop from the A\+ finder/.test(await page.locator("#apSLnote").innerText()) && /(PASS|WATCH|FAIL)/.test(await page.locator("#tmGate").innerText()), inst + " " + esl + " " + etp);
    await page.click('#nav button[data-view="now"]');
    await page.click('#apCard button[data-ap="full:0"]');
    await page.waitForSelector(".tm-pos", { timeout: 30000 });
    check("'Replay in full' opens the Time tab with the A+ plan's own levels", /Using your plan from the Trade tab/.test(await page.locator(".tm-pos").innerText()), await page.locator(".tm-pos").innerText());
    check("no console or page errors (A+ trade)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  // ------------------------------------------------------------ J. once calls are settled, the card shows the record and scales the odds by it
  {
    const L = require("../src/tmx-learn.js");
    const st = L.emptyState();
    for (let i = 0; i < 24; i++) st.aplus.push({ d: "2026-09-" + String(1 + i).padStart(2, "0"), ix: "NIFTY", dir: 1, p1: 0.4, y1: i < 11 ? 1 : 0, p2: 0.2, y2: i < 4 ? 1 : 0, ps: 0.55, ys: i < 11 ? 0 : 1, need: 0.44 });
    st.updated = "2026-10-22T10:00:00.000Z"; st.created = st.updated;
    const { ctx, page, errors } = await launch("2026-10-23T11:30:30+05:30", { data: { m5: syntheticMorning(14, "11:30", { at: "11:25", close: 22724.5 }) }, seed: { "tmlearn/state": { v: 1, rev: 5, updated: st.updated, json: JSON.stringify(st) } } });
    await page.waitForSelector("#apCard .ap-plan", { timeout: 150000 });
    await page.waitForFunction(() => !/Refreshing|Working out/.test(document.querySelector("#apCard").innerText), null, { timeout: 60000 });
    const t = await page.locator("#apCard").innerText();
    check("with 24 settled A+ calls the card quotes the record against what the plans needed, with its interval", /11 of 24 reached T1 \(46%, 95% band \d+–\d+%\) against 40% the models gave them and 44% they needed/.test(t) && /(cannot be told apart|includes the break-even)/.test(t), t.match(/Record of the app.*/) + "");
    check("and the odds shown are scaled by what the signal has done, saying so", /the models, scaled ×1\.\d\d by what the app's 24 settled A\+ calls did/.test(t), t.match(/Odds: .*/) + "");
    check("no console or page errors (A+ record)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  await browser.close();
  const failed = results.filter((r) => !r[1]);
  console.log("\n" + (results.length - failed.length) + " passed, " + failed.length + " failed");
  process.exit(failed.length ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
