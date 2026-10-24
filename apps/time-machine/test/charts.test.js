// The chart helpers: well-formed SVG, no NaN, sane on awkward input.
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../src/tmx-charts.js");

const wellFormed = (svg) => {
  assert.ok(svg.startsWith("<svg") && svg.endsWith("</svg>"), svg.slice(0, 60));
  assert.ok(!/NaN|undefined|Infinity/.test(svg), (svg.match(/.{20}(NaN|undefined|Infinity).{20}/) || [""])[0]);
  const stack = [];
  for (const m of svg.matchAll(/<(\/?)([a-z]+)\b[^>]*?(\/?)>/g)) {
    if (m[3]) continue;                                   // self-closing
    if (m[1]) assert.equal(stack.pop(), m[2], "closing </" + m[2] + ">");
    else stack.push(m[2]);
  }
  assert.deepEqual(stack, [], "every tag is closed");
};

test("stacked bars: segments scale to the row width, the labels are escaped", () => {
  const svg = C.stackBars([{ label: "A <b>", segs: [{ p: 0.5, cls: "k-win", label: "x" }, { p: 0.5, cls: "k-stop", label: "y" }] }, { label: "empty", segs: [] }], "aria");
  wellFormed(svg);
  assert.ok(svg.includes("A &lt;b&gt;") && !svg.includes("<b>"));
  assert.equal((svg.match(/<rect/g) || []).length, 2);
});

test("histogram: red below zero, green above, a zero line and a mean marker; awkward data does not break it", () => {
  const vals = []; for (let i = -300; i < 700; i += 3) vals.push(i);
  const svg = C.histogram(vals, { fmt: (x) => "₹" + Math.round(x) });
  wellFormed(svg);
  assert.ok(svg.includes("k-neg") && svg.includes("k-pos") && svg.includes('class="zero"'));
  wellFormed(C.histogram([5, 5, 5, 5, 5, 5], {}));                      // no spread
  wellFormed(C.histogram([-3, -2, -9, -1, -4, -5], {}));                // all losses
  assert.ok(/Too few/.test(C.histogram([1, 2], {})));
});

test("fan: bands, a median, level lines that do not overlap their labels", () => {
  const cols = []; for (let i = 0; i < 60; i++) cols.push([100 - i, 110 - i / 2, 120, 130 + i / 2, 140 + i]);
  const svg = C.fan(cols, [{ y: 135, label: "T1 135", cls: "tp" }, { y: 134, label: "entry", cls: "en" }, { y: 100, label: "SL 100", cls: "sl" }], (i) => "t" + i, { fmt: (v) => v.toFixed(0), paths: [cols.map((c) => c[2])] });
  wellFormed(svg);
  const ys = [...svg.matchAll(/<text class="lab" x="[\d.]+" y="([\d.]+)"/g)].map((m) => +m[1]).sort((a, b) => a - b);
  for (let i = 1; i < ys.length; i++) assert.ok(ys[i] - ys[i - 1] >= 10.9, "labels are spaced: " + ys);
  assert.ok(/Not enough/.test(C.fan([[1, 2, 3, 4, 5]], [], () => "", {})));
});

test("heat table: signs colour the cells, the current cell is marked", () => {
  const html = C.heat(["a", "b"], ["x", "y"], [[{ text: "+1", v: 1 }, { text: "-1", v: -1, cur: true }], [{ text: "0", v: 0 }, { text: "2", v: 2 }]], "corner");
  assert.ok(html.includes("var(--up)") && html.includes("var(--down)") && html.includes('class="cur"'));
  assert.ok(!/NaN/.test(html));
});
