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
| `trader-skill-india` | `/plugin install trader-skill-india@sudhiros` | 16 skills for Indian stocks and F&O (NSE/BSE): copy a mutual fund or investor from SEBI disclosures; exchange filings, insider trades, pledges and bulk deals; quarterly results; a screener; F&O screening with stop-loss, TP1/TP2 and trailing stops, Black-76 option levels, option-chain analytics and an honest backtester; whole-share and whole-lot sizing with STT and charges; SEBI and exchange risk checks; capital gains and F&O tax; NSE alerts; rebalancing; order tickets that need your yes. [README](plugins/trader-skill-india/README.md) |
| `trader-skill` | `/plugin install trader-skill@sudhiros` | The original US pack by Jake Schincariol (13F copycat, Congress trades, SEC earnings and more), listed here from [Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill). Also available from its own marketplace: `/plugin marketplace add Jakeschincariol/trader-skill`. |

## Layout

```
.claude-plugin/marketplace.json   the marketplace: both plugins
.claude/settings.json             marketplaces known to this repo, the India pack enabled,
                                  broker order tools kept on "ask"
plugins/trader-skill-india/       the India edition (skills, tools, tests)
```

Run the India pack's tests (offline, standard library only):

```bash
cd plugins/trader-skill-india && python3 -m unittest discover -s tests -v
```

Not financial advice. Nothing in these plugins places an order without your
explicit yes for that one order.
