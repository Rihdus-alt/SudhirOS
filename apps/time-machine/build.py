#!/usr/bin/env python3
"""Build the Trading Desk page with the Time Machine as its fifth tab.

    python3 apps/time-machine/build.py --base <the desk page as saved from Claude> --out-dir <folder>

The base is the live artifact's own HTML (read it with the Artifact tool's `read` action; it is saved to a file). Every
patch is anchored on text that must appear exactly once, so a changed base fails loudly instead of building a broken page.
Writes two files: desk-time.body.html (what the Artifact tool publishes: no document skeleton) and desk-time.full.html
(the same inside the skeleton, for opening in a browser or the test harness).

The base page itself is never committed; this script, the engine and the tests are."""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
BUILD = "2026-10-24b"


def read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return fh.read()


class Patcher:
    def __init__(self, text):
        self.s = text

    def once(self, old, new, label):
        n = self.s.count(old)
        if n != 1:
            sys.exit("build.py: anchor for %r found %d times (wanted 1): the base page has changed" % (label, n))
        self.s = self.s.replace(old, new)

    def sub_once(self, pattern, repl, label, flags=re.S):
        s, n = re.subn(pattern, repl, self.s, flags=flags)
        if n != 1:
            sys.exit("build.py: pattern for %r matched %d times (wanted 1)" % (label, n))
        self.s = s


def build(base_text):
    m = re.match(r"^(<!doctype html>.*?<body>\n?)(.*?)(</body></html>\s*)$", base_text, re.S | re.I)
    if not m:
        sys.exit("build.py: the base is not a complete page in the Artifact skeleton")
    skeleton_head, body, skeleton_tail = m.group(1), m.group(2), m.group(3)
    if "<title>Trading Desk</title>" not in body[:200]:
        sys.exit("build.py: the base is not the Trading Desk page")
    p = Patcher(body)

    # 1. style, the view, the nav button
    p.once("@media (min-width:900px){.wrap{max-width:980px}}\n</style>", "@media (min-width:900px){.wrap{max-width:980px}}\n" + read("tmx.css") + "</style>", "css")
    p.once("""Coach reads are not financial advice.</p>
  </section>
</div>""", """Coach reads are not financial advice.</p>
  </section>

""" + read("tmx.html") + "</div>", "view")
    p.once("""  <button data-view="insights"><span>◔</span>Insights</button>
</nav>""", """  <button data-view="insights"><span>◔</span>Insights</button>
  <button data-view="time"><span>⟲</span>Time</button>
</nav>""", "nav")

    # 2. the engines and charts, before the desk's own script
    engines = "".join("<script>\n%s\n</script>\n" % read(f) for f in ("tmx-core.js", "tmx-intraday.js", "tmx-learn.js", "tmx-charts.js"))
    p.once("<script>\n(() => {\nconst IND = \"IndMoney\";", engines + "<script>\n(() => {\nconst IND = \"IndMoney\";", "engines")

    # 3. the tab's own code, before boot()
    ui = read("tmx-ui.js") + "\n" + read("tmx-desk.js")
    if "</script" in ui:
        sys.exit("build.py: the UI code contains a closing script tag")
    p.once("async function boot(){", ui + "\nasync function boot(){", "ui")

    # 4. wiring: the view list, entering the tab, binding, the connector arriving
    p.once('if(!["now","trade","journal","insights"].includes(v)) v="now";', 'if(!["now","trade","journal","insights","time"].includes(v)) v="now";', "setView list")
    p.once("""  if (v==="trade" && mcp && (!S.chain || S.chain.err || S.chain.ix!==S.ix)) loadChain(null); }""",
           """  if (v==="trade" && mcp && (!S.chain || S.chain.err || S.chain.ix!==S.ix)) loadChain(null);
  if (v==="time") tmEnter();
  if (v==="trade") tmGateSchedule(); }""", "setView hook")
    p.once("  bind(); renderAll();\n  const C=window.claude;", "  bind(); tmBind(); tmDeskBind(); renderAll();\n  const C=window.claude;", "boot bind")
    p.once("""S.gap.err="Live data isn't available in this view."; renderAll(); return; } syncWatches(true); loadGapHistory(); watchUS(); if (S.view==="trade") loadChain(null);""",
           """S.gap.err="Live data isn't available in this view."; renderAll(); if (S.view==="time") tmPrepare(); return; } syncWatches(true); loadGapHistory(); watchUS(); if (S.view==="trade") loadChain(null); if (S.view==="time") tmPrepare(); lrnBackground();""", "mcp arrives")
    p.once('const APP_BUILD = "2026-10-06a";', 'const APP_BUILD = "%s";' % BUILD, "build")

    # 4b. every plan goes through the Time Machine: the Trade tab's check, the journal's chip, the learning store
    p.once('<div id="plan"></div>', '<div id="plan"></div>\n      <div id="tmGate" aria-live="polite"></div>', "gate markup")
    p.once('function renderPlan(){\n  const e=num($("f_entry").value)', 'function renderPlan(){\n  tmGateSchedule();\n  const e=num($("f_entry").value)', "renderPlan hook")
    p.once('  $("save").disabled=true;\n  try{ await db.collection("trades").doc().set(doc);',
           '  const tmv=await tmBeforeSave(); if (tmv==="hold") return; if (tmv){ doc.tm=tmv.tm; if (tmv.id) doc.tmId=tmv.id; }\n  $("save").disabled=true;\n  try{ await db.collection("trades").doc().set(doc);', "save hook")
    p.once('<div>${f}</div></td><td><button class="mini" data-jedit', '<div>${f}${tmChip(t)}</div></td><td><button class="mini" data-jedit', "journal chip")
    p.once("function renderAll(){ detectClosed();", "function renderAll(){ lrnTradesTick(); detectClosed();", "renderAll hook")
    p.once('    subUsage();\n    d.doc("meta/app")', '    lrnInit(d);\n    subUsage();\n    d.doc("meta/app")', "db init")

    # 5. the daily bars keep their volume (the setups score volume)
    p.once("close:+c.close})).filter(c=>c.open>0&&c.close>0);", "close:+c.close,volume:+c.volume||0})).filter(c=>c.open>0&&c.close>0);", "norm volume")

    # 6. the coach's rules follow the saved settings instead of a copy of them
    p.once("const COACH_RULES = `You are", "const coachRules = () => `You are", "coach const")
    p.once("No capital cap per trade: size is his call, so always state the ₹ outlay and the ₹ risk to SL. Max 3 trades/day across both indices. SL 10% set before entry. TP 15–20% or trail to breakeven then behind structure. Daily max loss ₹3,000 → stop.",
           "${S.rules.cap?\"Capital cap per trade ₹\"+Number(S.rules.cap).toLocaleString(\"en-IN\")+\"; state the ₹ outlay and the ₹ risk to SL.\":\"No capital cap per trade: size is his call, so always state the ₹ outlay and the ₹ risk to SL.\"} ${S.rules.maxTrades>=99?\"No fixed limit on trades per day (his saved setting).\":\"Max \"+S.rules.maxTrades+\" trades/day across both indices.\"} SL ${S.rules.slPct}% set before entry. TP ${S.rules.tpMin}–${S.rules.tpMax}% or trail to breakeven then behind structure. Daily max loss ₹${(+S.rules.dayStop||3000).toLocaleString(\"en-IN\")} → stop.",
           "coach rules text")
    p.once("On an index's expiry day after 13:30 no new buys of that week's options (use next week's). No new entries after 15:00; F&O trading ends 15:30.",
           "On an index's expiry day after ${S.rules.expiryCutoff} no new buys of that week's options (use next week's). No new entries after ${S.rules.noEntryAfter}; F&O trading ends 15:30.", "coach cutoffs")
    p.once("const prompt=`${COACH_RULES}\\n\\n", "const prompt=`${coachRules()}\\n\\n", "coach use")
    p.once('Output: mobile-first, ≤10 short lines',
           'Every setup you propose, and every trade you assess, MUST first be run through the time_machine tool (index, side, strike, lots, and your entry, SL, T1, T2). Quote its gate (pass, watch or fail) and its chance of profit next to the setup. Never grade a setup the tool rates fail as A or A+: say it fails the Time Machine. If the tool is unavailable, say the setup is unchecked.\nOutput: mobile-first, ≤10 short lines', "coach time machine rule")
    p.once('If nothing qualifies or rules block entries, say so plainly.', 'Run each setup through the time_machine tool before showing it and drop any it rates fail. If nothing qualifies or rules block entries, say so plainly.', "coach scan ask")
    p.once('  ];\n  let useTools=false;', """    {name:"time_machine",description:"Replay a proposed NIFTY or SENSEX option trade through this app's Time Machine: the desk plan on simulated and real candles, the option repriced every candle, after charges. Returns a gate (pass, watch or fail), the chance of a profit, of reaching T1 and of being stopped out, and the average result. Call it for every setup before presenting it. entry, sl, tp1 and tp2 are option premiums; omit them to use the live ask and the desk rules.",inputSchema:{type:"object",properties:{index:{type:"string",enum:["NIFTY","SENSEX"]},side:{type:"string",enum:["CE","PE"]},strike:{type:"integer"},expiry_date:{type:"string"},lots:{type:"integer"},entry:{type:"number"},sl:{type:"number"},tp1:{type:"number"},tp2:{type:"number"}},required:["index","side","strike"]},
      execute:i=>wrap(()=>tmCoachTool(i))},
  ];
  let useTools=false;""", "coach tool")
    # the old hard-coded rules must be gone from the coach prompt
    for stale in ("Max 3 trades/day", "COACH_RULES"):
        if stale in p.s:
            sys.exit("build.py: %r is still in the page" % stale)
    return skeleton_head, p.s, skeleton_tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    with open(a.base, encoding="utf-8") as fh:
        base = fh.read()
    head, body, tail = build(base)
    os.makedirs(a.out_dir, exist_ok=True)
    body_path = os.path.join(a.out_dir, "desk-time.body.html")
    full_path = os.path.join(a.out_dir, "desk-time.full.html")
    with open(body_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    with open(full_path, "w", encoding="utf-8") as fh:
        fh.write(head + body + tail)
    # every script block must parse
    for i, blk in enumerate(re.findall(r"<script>\n(.*?)\n</script>", body, re.S)):
        tmp = os.path.join(a.out_dir, "_check%d.js" % i)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blk)
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        os.remove(tmp)
        if r.returncode:
            sys.exit("build.py: script block %d does not parse:\n%s" % (i, r.stderr))
    print("built", body_path, "(%d bytes)" % len(body.encode()), "and", full_path)


if __name__ == "__main__":
    main()
