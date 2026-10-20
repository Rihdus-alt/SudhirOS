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
| `trader-skill-india` | `/plugin install trader-skill-india@sudhiros` | 12 skills for Indian stocks (NSE/BSE): copy a mutual fund or investor from SEBI disclosures, watch exchange filings, insider trades, pledges and bulk deals, read quarterly results, screen, size in whole shares with STT and charges, check SEBI and exchange rules, capital gains tax (FIFO, s.112A, grandfathering), price alerts on the NSE clock, rebalancing, and order tickets that need your yes. [README](plugins/trader-skill-india/README.md) |
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
