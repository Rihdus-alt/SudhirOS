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
  const docs = new Map();
  docs.set("trades/t1", data.trades[0]);
  docs.set("config/rules", { cap: null, maxTrades: 99, slPct: 10, tpMin: 15, tpMax: 20, dayStop: 3000, noEntryAfter: "15:00", expiryCutoff: "13:30", autoLog: false });
  const listeners = [];
  const snap = (path) => { const id = path.split("/").pop(); return { exists: docs.has(path), id, data: () => docs.get(path) }; };
  const collSnap = (name) => ({ docs: [...docs.entries()].filter(([p]) => p.startsWith(name + "/")).map(([p, d]) => ({ id: p.split("/")[1], data: () => d })) });
  const db = {
    collection(name) { return { onSnapshot(cb) { setTimeout(() => cb(collSnap(name)), 0); return () => {}; }, doc: (id) => db.doc(name + "/" + id) }; },
    doc(p) { return { onSnapshot(cb) { setTimeout(() => cb(snap(p)), 0); return () => {}; }, async get() { return snap(p); }, async set(d) { docs.set(p, d); }, async update(d) { docs.set(p, { ...(docs.get(p) || {}), ...d }); }, async delete() { docs.delete(p); } }; },
  };
  const mcp = {
    async callTool(server, tool, input) { if (!tools[tool]) throw { code: "tool_error", message: "no stub for " + tool }; await new Promise((r) => setTimeout(r, 5)); return pack(tools[tool](input || {})); },
    watchTool(server, tool, input, handler) { setTimeout(() => { if (tools[tool]) handler({ type: "data", result: { ...pack(tools[tool](input || {})), cache: { storedAt: realDate.now() } } }); }, 0); return () => {}; },
    async invalidate() {},
  };
  window.claude = { use: async (name) => ({ db, mcp, user: { can: async () => true }, sample: null }[name] || null) };
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
    await page.addInitScript(`(${installClaude.toString()})(${JSON.stringify(DATA)}, ${JSON.stringify(fakeIso)})`);
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
    check("live: the entry is the next bar", /next bar 11:05/.test(await page.locator("input#tm_entry").inputValue()), await page.locator("input#tm_entry").inputValue());
    await page.selectOption("#tm_paths", "2000");
    await page.click("#tm_run");
    await page.waitForSelector("#tmOut .card", { timeout: 90000 });
    await page.waitForFunction(() => !document.querySelector("#tm_cancel"), null, { timeout: 90000 });
    const txt = await page.locator("#tmOut").innerText();
    check("live run reports its start", /from 11:05 today/.test(txt), txt.slice(0, 200));
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
    check("no console or page errors (phone, dark)", errors.length === 0, errors.join(" | "));
    await ctx.close();
  }
  await browser.close();
  const failed = results.filter((r) => !r[1]);
  console.log("\n" + (results.length - failed.length) + " passed, " + failed.length + " failed");
  process.exit(failed.length ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
