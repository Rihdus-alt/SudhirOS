#!/usr/bin/env python3
"""Package the India trader pack as one skill zip for claude.ai (Customize > Skills > Upload a skill).

    python3 scripts/build_claude_ai_skill.py            # writes dist/trader-india.zip
    python3 scripts/build_claude_ai_skill.py --out x.zip

claude.ai takes one skill per upload, and several of the tools load code from their sibling folders
(../india-costs/costs.py, ../india-fno-plan/fnocore.py). So the skill folders go into a single skill,
"trader-india", side by side as they sit in the plugin, under a SKILL.md that routes to the right folder.
The tests stay out: they belong to the repo. Standard library only.
"""

import argparse
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "trader-skill-india")
NAME = "trader-india"
SKIP_DIRS = {"tests", "__pycache__", ".claude-plugin", "trader"}

DESCRIPTION = (
    "Indian stocks and F&O toolkit (NSE/BSE, SEBI rules, Income-tax Act, rupees): 17 tools in one skill. "
    "F&O screening with stop-loss, TP1/TP2 and trailing stops, option-chain analytics, Black-76 option "
    "levels, an honest backtester and a Time Machine that replays every way a trade can end; whole-share and whole-lot sizing with STT and every charge; SEBI and "
    "exchange risk checks; capital gains and F&O tax; copy a mutual fund or investor from SEBI disclosures; "
    "insider trades, pledges and bulk deals; exchange filings; quarterly results; a stock screener; NSE "
    "price alerts; rebalancing; order tickets that need the user's yes. Use for any Indian market request "
    "about trades, F&O levels, SL and targets, charges, tax, portfolios, filings, results or alerts."
)


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return m.group(1) if m else ""


def first_sentence(fm):
    m = re.search(r"description: >-\n((?:  .*\n?)+)", fm + "\n")
    desc = " ".join(m.group(1).split()) if m else ""
    head = desc.split(". ")
    return (head[0] + ". " + head[1] + ".") if len(head) > 1 else desc


def skills():
    out = []
    for d in sorted(os.listdir(PLUGIN)):
        path = os.path.join(PLUGIN, d, "SKILL.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                out.append((d, first_sentence(frontmatter(fh.read()))))
    return out


def router(rows):
    table = "\n".join("| `%s/` | %s |" % (d, s.replace("|", "/")) for d, s in rows)
    return """---
name: %s
description: >-
  %s
---

# %s

This one skill holds the %d skills of the `trader-skill-india` plugin
(github.com/rihdus-alt/SudhirOS), each in its own folder next to this file.

## How to use it

1. Pick the folder for the request from the table below, read that folder's
   `SKILL.md` in full, and follow it. Several requests use more than one
   (a trade goes plan, then risk, then broker).
2. In those files `${CLAUDE_SKILL_DIR}` means that folder: the folder this
   file sits in, plus `/india-...`. Put the real path in before running a
   command.
3. A reference like `/india-risk` means: read `india-risk/SKILL.md` and follow it.
4. The tools are Python 3.8+, standard library only: run them with `python3`.
   Keep the folders together, because several tools load code from their
   sibling folders.
5. Keep the user's own files (`trader/config.json`, `trader/orders.json`,
   portfolios, journals) in the working or output folder, never inside this
   skill's folder.
6. Live NSE, BSE or Yahoo fetches may be blocked here. When one fails, use
   the INDmoney connector if it is connected, or ask the user to paste or
   upload the data, as each `SKILL.md` describes.

## The folders

| Folder | What it does |
|---|---|
%s

## Rules that always apply

- Education and tooling, not financial advice. Never call a trade a sure thing.
- No order is ever placed without the user's explicit yes to that one order.
- Never ask for a password, PIN, OTP or broker API secret in the chat.
- For F&O, say once and early: about 91%% of individual F&O traders lost money
  in FY2024-25 (SEBI's study; 87.7%% in FY2025-26). Quote only measured track
  records with their confidence intervals, never an "accuracy".
""" % (NAME, DESCRIPTION, NAME, len(rows), table)


def build(out):
    rows = skills()
    if len(DESCRIPTION) > 1024:
        raise SystemExit("description is %d characters; claude.ai allows 1024" % len(DESCRIPTION))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(NAME + "/SKILL.md", router(rows))
        for base, dirs, files in os.walk(PLUGIN):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                src = os.path.join(base, f)
                rel = os.path.relpath(src, PLUGIN)
                if os.sep not in rel and f not in ("README.md", "LICENSE"):
                    continue
                z.write(src, NAME + "/" + rel.replace(os.sep, "/"))
                n += 1
    return rows, n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Package the India trader pack as one claude.ai skill zip.")
    ap.add_argument("--out", default=os.path.join(REPO, "dist", NAME + ".zip"))
    a = ap.parse_args(argv)
    rows, n = build(a.out)
    print("%s: %d skill folders, %d files, %d bytes" % (a.out, len(rows), n + 1, os.path.getsize(a.out)))
    print("Upload it on claude.ai: Customize > Skills > Upload a skill (older apps: Settings > Capabilities).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
