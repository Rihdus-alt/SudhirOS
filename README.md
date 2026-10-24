# SudhirOS

A personal operating system for Claude Code: plugins and skills, shipped as a
Claude Code plugin marketplace.

## Install the marketplace

```
/plugin marketplace add rihdus-alt/SudhirOS
```

Opening this repository in Claude Code also offers it automatically
(`.claude/settings.json` lists it under `extraKnownMarketplaces` and enables
the India trader pack).

## Plugins

| plugin | install | what it is |
| --- | --- | --- |
| `trader-skill-india` | `/plugin install trader-skill-india@sudhiros` | 17 skills for Indian stocks and F&O (NSE/BSE): copy a mutual fund or investor from SEBI disclosures; exchange filings, insider trades, pledges and bulk deals; quarterly results; a screener; F&O screening with stop-loss, TP1/TP2 and trailing stops, Black-76 option levels, option-chain analytics, an honest backtester and a Time Machine that replays every way a trade can end with the odds of each; whole-share and whole-lot sizing with STT and charges; SEBI and exchange risk checks; capital gains and F&O tax; NSE alerts; rebalancing; order tickets that need your yes. [README](plugins/trader-skill-india/README.md) |
| `trader-skill` | `/plugin install trader-skill@sudhiros` | The original US pack by Jake Schincariol (13F copycat, Congress trades, SEC earnings and more), listed here from [Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill). Also available from its own marketplace: `/plugin marketplace add Jakeschincariol/trader-skill`. |

## Use it on claude.ai

The plugin marketplace serves Claude Code. For the Claude apps (claude.ai,
desktop, mobile), build one skill zip that holds all 17 India skills and
upload it in Claude's skills settings (Customize > Skills > Upload a skill;
older versions of the app call it Settings > Capabilities > Skills). Skills
need code execution turned on:

```bash
python3 scripts/build_claude_ai_skill.py      # writes dist/trader-india.zip
```

The zip is a single skill, `trader-india`, with the 17 skill folders inside
it (several tools load code from their sibling folders, so they travel
together) and a `SKILL.md` that routes each request to the right folder.

## The Trading Desk's Time tab

`apps/time-machine/` is the Time Machine as an app: the fifth tab of the Trading Desk page
(Claude artifact). Intraday mode replays the desk's own plan (SL, T1, T2, the trailing
ladder, out by 15:15) on 1-, 5- or 15-minute candles with the option repriced on every
candle; swing mode is the daily-bar tool above, for futures, options or shares. It is the
JavaScript port of the Python tool, held to it by tests, and it counts what a trade does on
paths from several models. Every plan goes through it: the Trade tab checks the plan before
it is logged, the scan ranks contracts by its gate, and the coach must run each setup
through it. A bounded, freezable learning layer corrects its pace, its odds and its model
weights from the real tapes the app saves. The Now tab finds A+ trades on its own, sets their stops
from the market, and shows the target with the chance of reaching it and what the plan needs from
its signal to pay. It does not predict.
See [apps/time-machine/README.md](apps/time-machine/README.md).

## Layout

```
.claude-plugin/marketplace.json   the marketplace: both plugins
.claude/settings.json             marketplaces known to this repo, the India pack enabled,
                                  broker order tools kept on "ask"
plugins/trader-skill-india/       the India edition (skills, tools, tests)
apps/time-machine/                the Time Machine as the Trading Desk's fifth tab (engine, UI, build, tests)
scripts/build_claude_ai_skill.py  packages the India pack as one claude.ai skill zip
```

Run the India pack's tests (offline, standard library only):

```bash
cd plugins/trader-skill-india && python3 -m unittest discover -s tests -v
```

And the Time Machine app's (Node 18+; the browser check also needs Playwright and Chromium):

```bash
node --test apps/time-machine/test/core.test.js apps/time-machine/test/intraday.test.js apps/time-machine/test/charts.test.js apps/time-machine/test/learn.test.js apps/time-machine/test/aplus.test.js
```

Not financial advice. Nothing in these plugins places an order without your
explicit yes for that one order.
