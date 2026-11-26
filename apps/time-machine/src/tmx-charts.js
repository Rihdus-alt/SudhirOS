/* Time Machine charts: small SVG strings in the page's own colour tokens (see tmx.css). No dependencies.
 * Every chart is a role="img" with a text label, has native hover titles on its marks, and sits next to a table
 * that carries the same numbers. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.TMXC = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const f1 = (x) => (Math.round(x * 10) / 10).toString();
  const nf = (x) => Math.round(x).toLocaleString("en-IN");
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

  function niceTicks(lo, hi, n) {
    const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return out;
  }

  /** rows: [{label, segs: [{p, cls, label}]}]  ->  stacked horizontal bars, one per row, labelled inside when wide enough. */
  function stackBars(rows, aria) {
    const W = 420, lw = 92, bw = W - lw - 4, rh = 30, H = rows.length * rh + 6;
    let s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(aria || "How it ends") + '">';
    rows.forEach((r, i) => {
      const y = 4 + i * rh;
      s += '<text class="rowlab" x="0" y="' + (y + 16) + '">' + esc(r.label) + "</text>";
      let x = lw;
      for (const g of r.segs) {
        const w = g.p * bw;
        if (!(w > 0)) continue;
        s += '<rect class="' + g.cls + '" x="' + f1(x) + '" y="' + y + '" width="' + f1(Math.max(w - 1.5, 0.6)) + '" height="22" rx="3"><title>' + esc(g.label) + ": " + (100 * g.p).toFixed(1) + "%</title></rect>";
        if (w > 26) s += '<text class="inb" x="' + f1(x + w / 2) + '" y="' + (y + 15) + '" text-anchor="middle">' + Math.round(100 * g.p) + "%</text>";
        x += w;
      }
    });
    return s + "</svg>";
  }

  /** Histogram of results (rupees or R): red below zero, green above, a dashed zero line, a marker at the mean. */
  function histogram(vals, o) {
    o = o || {};
    const W = 420, H = 180, L = 6, R = 6, T = 14, B = 34;
    if (!vals || vals.length < 5) return '<div class="small muted">Too few results to draw.</div>';
    const s = vals.slice().sort((a, b) => a - b), q = (p) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))];
    let lo = q(0.005), hi = q(0.995);
    if (lo > 0) lo = 0; if (hi < 0) hi = 0;
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const nb = o.bins || 30, bwid = (hi - lo) / nb, cnt = new Array(nb).fill(0);
    for (const v of vals) cnt[clamp(Math.floor((v - lo) / bwid), 0, nb - 1)]++;
    const mx = Math.max.apply(null, cnt), pw = W - L - R, ph = H - T - B;
    const X = (v) => L + ((v - lo) / (hi - lo)) * pw, Y = (c) => T + ph - (c / mx) * ph;
    const fmt = o.fmt || nf;
    let svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.aria || "Distribution of results") + '">';
    svg += '<line class="ax" x1="' + L + '" y1="' + (T + ph) + '" x2="' + (W - R) + '" y2="' + (T + ph) + '"/>';
    cnt.forEach((c, i) => {
      if (!c) return;
      const a = lo + i * bwid, b = a + bwid, mid = (a + b) / 2;
      svg += '<rect class="' + (mid < 0 ? "k-neg" : "k-pos") + '" x="' + f1(X(a) + 0.5) + '" y="' + f1(Y(c)) + '" width="' + f1(Math.max(pw / nb - 1, 0.6)) + '" height="' + f1(T + ph - Y(c)) + '" rx="1.5"><title>' +
        esc(fmt(a) + " to " + fmt(b) + ": " + (100 * c / vals.length).toFixed(1) + "% of paths") + "</title></rect>";
    });
    if (lo < 0 && hi > 0) svg += '<line class="zero" x1="' + f1(X(0)) + '" y1="' + T + '" x2="' + f1(X(0)) + '" y2="' + (T + ph) + '"/>';
    for (const t of niceTicks(lo, hi, 5)) svg += '<text x="' + f1(X(t)) + '" y="' + (H - 18) + '" text-anchor="middle">' + esc(fmt(t)) + "</text>";
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    svg += '<path d="M' + f1(X(mean)) + " " + (T + ph + 3) + " l-5 8 h10 z" + '" fill="var(--ink)"><title>' + esc("average " + fmt(mean)) + "</title></path>";
    svg += '<text class="lab" x="' + f1(clamp(X(mean), 60, W - 60)) + '" y="' + (H - 2) + '" text-anchor="middle">average ' + esc(fmt(mean)) + "</text>";
    return svg + "</svg>";
  }

  /** A fan: bands (5-95 and 25-75), the median, and horizontal level lines, over a time axis.
   *  cols: [[p5,p25,p50,p75,p95], ...] per step; levels: [{y, label, cls}]; xlab(i) -> string. */
  function fan(cols, levels, xlab, o) {
    o = o || {};
    const W = 420, H = 215, L = 40, R = 66, T = 8, B = 22;
    const n = cols.length;
    if (n < 2) return '<div class="small muted">Not enough bars to draw.</div>';
    let lo = Infinity, hi = -Infinity;
    for (const c of cols) { lo = Math.min(lo, c[0]); hi = Math.max(hi, c[4]); }
    for (const l of levels) { lo = Math.min(lo, l.y); hi = Math.max(hi, l.y); }
    const pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
    const pw = W - L - R, ph = H - T - B;
    const X = (i) => L + (i / (n - 1)) * pw, Y = (v) => T + ph - ((v - lo) / (hi - lo)) * ph;
    const poly = (a, b) => cols.map((c, i) => X(i).toFixed(1) + "," + Y(c[a]).toFixed(1)).join(" ") + " " + cols.map((c, i) => X(n - 1 - i).toFixed(1) + "," + Y(cols[n - 1 - i][b]).toFixed(1)).join(" ");
    const fmt = o.fmt || ((v) => v.toFixed(1));
    let s = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(o.aria || "Range of paths") + '">';
    for (const t of niceTicks(lo, hi, 4)) s += '<line class="ax" x1="' + L + '" y1="' + f1(Y(t)) + '" x2="' + (W - R) + '" y2="' + f1(Y(t)) + '"/><text x="' + (L - 5) + '" y="' + f1(Y(t) + 3.5) + '" text-anchor="end">' + esc(fmt(t)) + "</text>";
    if (o.paths) for (const p of o.paths) s += '<polyline class="pth" points="' + p.map((v, i) => X(i).toFixed(1) + "," + Y(v).toFixed(1)).join(" ") + '"/>';
    s += '<polygon class="band1" points="' + poly(0, 4) + '"><title>' + esc("5% to 95% of paths") + "</title></polygon>";
    s += '<polygon class="band2" points="' + poly(1, 3) + '"><title>' + esc("25% to 75% of paths") + "</title></polygon>";
    s += '<polyline class="med" points="' + cols.map((c, i) => X(i).toFixed(1) + "," + Y(c[2]).toFixed(1)).join(" ") + '"><title>median</title></polyline>';
    const placed = [];
    for (const l of levels.slice().sort((a, b) => b.y - a.y)) {
      s += '<line class="lv ' + (l.cls || "") + '" x1="' + L + '" y1="' + f1(Y(l.y)) + '" x2="' + (W - R) + '" y2="' + f1(Y(l.y)) + '"/>';
      let ty = Y(l.y) + 3.5;
      for (const p of placed) if (Math.abs(ty - p) < 11) ty = p + 11;
      placed.push(ty);
      s += '<text class="lab" x="' + (W - R + 4) + '" y="' + f1(ty) + '">' + esc(l.label) + "</text>";
    }
    const step = Math.max(1, Math.round(n / 5));
    for (let i = 0; i < n; i += step) s += '<text x="' + f1(X(i)) + '" y="' + (H - 6) + '" text-anchor="middle">' + esc(xlab(i)) + "</text>";
    return s + "</svg>";
  }

  /** A grid of numbers coloured by sign and size: rows x cols, cells [{text, v, cur}]. Returns an HTML table. */
  function heat(rowLabels, colLabels, cells, corner) {
    let mx = 0;
    for (const r of cells) for (const c of r) mx = Math.max(mx, Math.abs(c.v));
    let h = '<div class="tbl"><table class="tm-grid"><tr><th>' + esc(corner || "") + "</th>" + colLabels.map((c) => "<th>" + esc(c) + "</th>").join("") + "</tr>";
    cells.forEach((r, i) => {
      h += "<tr><th>" + esc(rowLabels[i]) + "</th>";
      for (const c of r) {
        const a = mx ? Math.min(Math.abs(c.v) / mx, 1) * 0.42 : 0;
        const bg = c.v >= 0 ? "color-mix(in srgb, var(--up) " + Math.round(a * 100) + "%, transparent)" : "color-mix(in srgb, var(--down) " + Math.round(a * 100) + "%, transparent)";
        h += '<td class="' + (c.cur ? "cur" : "") + '" style="background:' + bg + '">' + esc(c.text) + "</td>";
      }
      h += "</tr>";
    });
    return h + "</table></div>";
  }

  return { esc, stackBars, histogram, fan, heat, niceTicks };
});
