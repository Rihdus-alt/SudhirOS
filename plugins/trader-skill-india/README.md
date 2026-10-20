# The Trader skill, India edition

12 Claude skills for Indian stocks: copy a mutual fund or an investor from
SEBI disclosures, watch NSE/BSE filings and insider trades, size every trade
in whole shares with real charges, check SEBI and exchange rules, and never
place an order without your yes.

It is the Indian version of [Jake Schincariol's trader-skill](https://github.com/Jakeschincariol/trader-skill)
(MIT): the same safety design (proposals, a risk gate, one explicit yes per
order, paper first), rebuilt for NSE and BSE, SEBI's disclosure rules, the
Income-tax Act and rupees. Free, no signup, no API key. The tools are plain
Python that read public data: NSE and BSE disclosures, AMC portfolio files,
exchange XBRL, news feeds, and (if you have it) the INDmoney connector.

## Install

As a plugin, in Claude Code:

```
/plugin marketplace add rihdus-alt/SudhirOS
/plugin install trader-skill-india@sudhiros
```

Skills then show up as `/trader-skill-india:india-copycat` and so on. For plain
`/india-copycat`, copy the folders instead (copy ALL of them: budget, risk,
rebalance and broker use `india-costs` next to them):

```bash
git clone https://github.com/rihdus-alt/SudhirOS.git
cp -r SudhirOS/plugins/trader-skill-india/india-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. Nothing to pip install.

## The twelve

| command | job | what it does |
| --- | --- | --- |
| `/india-copycat` | Reverse Engineer | Reads a mutual fund's full monthly portfolio (AMC Excel, or the INDmoney connector) or an investor's named stakes from shareholding patterns, checks every ISIN, maps it to its NSE symbol, drops debt, cash and foreign stocks, diffs month to month, and writes target weights. |
| `/india-news` | News Monitor | NSE and BSE corporate filings (SEBI LODR Reg 30) and Indian headlines for what you hold; merges the two exchanges' copies, tags results, payouts, people, deals, and marks red flags: auditor resignations, defaults, downgrades, pledges, regulator orders. |
| `/india-budget` | Budget Manager | Sizes your rupee budget in whole shares, nearest-share rounding, worst-case costing with every charge, cash buffer and per-stock cap, tracking error; writes CNC limit orders. |
| `/india-insider` | Insider Tracker | Promoter and director trades (SEBI PIT disclosures), promoter pledges, NSE bulk and block deals; flags promoter market buys, insider clusters, late filings, named investors, same-day round trips. |
| `/india-analyst` | Earnings Analyst | Quarterly results from the exchange XBRL (Ind-AS, consolidated by default): revenue, operating EBITDA, margins, owners' profit, EPS, YoY and QoQ, TTM; flags other income, exceptional items, tax, restatements. |
| `/india-screener` | Stock Screener | Your rules on ROCE, growth, debt, promoter holding, pledge, P/E...; reads screener exports with Indian number formats (Rs 1,02,350, 4,512 Cr, 35 lakh). |
| `/india-risk` | Risk Manager | Blocks anything off-rules: fractional or malformed quantities, F&O/MIS/MTF unless allowed, limit prices outside the circuit band, ASM/GSM stocks, position cap, cash buffer after charges, selling shares you do not hold, duplicates, daily limit. |
| `/india-broker` | Order Executor | Paper fills by default; manual tickets for any broker app; or the exact `place_order` arguments for Zerodha's Kite MCP behind Claude's Allow once card. Records your yes and contract-note fills as tax lots. |
| `/india-tax` | Tax Optimizer | FIFO lots, STCG/LTCG at 12 months, the 23 July 2024 rate change, the Rs 1.25 lakh s.112A exemption, grandfathering for pre-2018 shares, set-off and carry forward, bonus/split adjustments, bonus stripping, loss and exemption harvesting, estimated tax. |
| `/india-alerts` | Price Alerts | Targets, stops, percent moves and near-circuit alerts on the NSE clock (IST, pre-open, the official 2026 holidays, Muhurat); quotes from INDmoney, NSE or Yahoo with cross-checks and stale-quote flags. |
| `/india-rebalance` | Rebalancer | Whole-share trades back to target for stocks outside the band, sells first, skips trades whose charges are not worth it. |
| `/india-costs` | Trade Costs | Contract-note arithmetic: brokerage, STT, exchange, SEBI, stamp duty, GST, DP charge, and the breakeven move. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
copycat -> budget -> risk -> broker                  (copy a fund or investor)
news / insider / alerts                               (watch, on a schedule)
analyst -> screener -> budget -> risk -> broker       (pick your own)
rebalance / tax -> risk -> broker                     (maintain)
costs                                                 (any time)
```

An example, copying Parag Parikh Flexi Cap's top five with Rs 1,00,000 (real
INDmoney data and prices of the 2026-10-19 close):

1. **`/india-copycat`** "copy Parag Parikh Flexi Cap, top 5". HDFC Bank 7.8%,
   ICICI Bank 5.2%, M&M 3.9%, Axis Bank 2.8%, TCS 2.5%; Amazon.com (2.3%) is a
   US stock and is not copied. Weights 35.14 / 23.42 / 17.57 / 12.61 / 11.26.
   It notes that INDmoney lists only the top rows, rounded to 0.1%, and that
   the AMC's monthly file is the exact source.
2. **`/india-alerts`** imports live prices and circuit limits from the
   INDmoney connector into `trader/prices.json`.
3. **`/india-budget`** with a 5% buffer and a 25% cap: 35 HDFC Bank, 18 ICICI
   Bank (both capped), 6 M&M, 11 Axis Bank, 5 TCS; Rs 93,120.50 invested, Rs
   111.44 of charges, tracking error 1.34%. CNC limit orders to
   `trader/orders.json`.
4. **`/india-risk`**: all five PASS; cash after, worst case, Rs 6,304.01.
5. **`/india-broker`**: one ticket at a time; "yes to all" is refused, "haan,
   35 HDFCBANK" is recorded; on paper, 35 fill at Rs 712 and the tax lot costs
   Rs 24,924.64 (charges in, STT out).
6. Then on a schedule: `/india-news` and `/india-insider` each evening,
   `/india-alerts` in market hours; each month `/india-copycat diff` and
   `/india-rebalance`; before 31 March, `/india-tax harvest`.

## What changed from the US pack, for accuracy

- **Whole shares.** Indian exchanges have no fractional shares. Sizing rounds
  each stock to its NEAREST share count (not always down), then trims where
  it costs the weights least, and reports the tracking error.
- **Real costs.** Every buy is sized at its worst-case price (limit, or price
  plus slippage) plus STT, stamp duty, exchange, SEBI and GST charges, so the
  money always suffices; sells include the DP charge; rebalancing skips
  trades whose charges are not worth it.
- **Exchange rules.** Limit prices on a tick grid valid under NSE's tick
  rules and inside the day's circuit band; surveillance (ASM/GSM) and
  trade-to-trade stocks caught; same-day buy and sell flagged as intraday.
- **Indian tax law.** FIFO is mandatory for demat shares, so there is no lot
  picking; the 2024 rate change by sale date; the s.112A exemption per
  financial year; grandfathering; set-off order (short-term losses against
  the 20% gains first); bonus and split lots; no wash-sale rule but no
  same-day re-buy.
- **Better sources.** A mutual fund's monthly portfolio is complete and
  exact (a 13F is long-only and partial); insider disclosures arrive within
  days (a congressional PTR within 45).
- **Verified identifiers.** Every ISIN's check digit is validated; a holding
  without an exact symbol match is never guessed into a copy.
- **Two-source prices.** `--verify` compares sources; stale quotes are
  flagged; INDmoney's live quotes bring circuit limits.
- **A verified calendar.** NSE's 2026 holidays from its circulars; unknown
  years and Muhurat times are reported as unknown, never guessed.

## The tools

All standard-library Python.

```bash
python3 india-alerts/alerts.py clock                                      # NSE open or closed, IST
python3 india-alerts/alerts.py import indmoney details.json --write       # INDmoney quotes -> prices
python3 india-copycat/copycat.py copy portfolio.xlsx --sheet "Flexi Cap" --top 10
python3 india-budget/size.py --budget 100000 --write                     # -> trader/orders.json
python3 india-risk/check.py                                               # PASS / BLOCK per order
python3 india-broker/ticket.py show                                       # the tickets
python3 india-tax/lots.py sell AXISBANK 50 --price 1236                   # FIFO gain and its tax
python3 india-tax/lots.py harvest                                         # loss + exemption harvesting
python3 india-insider/insider.py pit --promoters --buys --days 30
python3 india-news/news.py --symbols RELIANCE --bse 500325
python3 india-analyst/results.py fetch INFY && python3 india-analyst/results.py read trader/state/results/INFY-*.xml
python3 india-costs/costs.py roundtrip 24920 --exit 26000
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline. `tests/fixtures/README.md` says which fixtures are real
(the INDmoney connector's quotes and fund holdings, read 2026-10-20) and which
are written by hand in the documented shape of a source (NSE, BSE, Yahoo, AMC
files, XBRL), with made-up companies.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It is not a SEBI-registered investment adviser or research
analyst, it does not know your situation, and it cannot see the future.
Copying a fund does not copy its results. You can lose money.

**No trade goes through until you say yes.** Only `/india-broker` can lead to
an order, only after `/india-risk` passes that exact order and you type yes
for it, and then you either place it yourself or approve Claude's "Allow
once" card. Set any broker order tool to "Needs approval", never "Always
allow". Delivery (CNC) only unless you change it: no F&O, intraday, MTF or
short selling.

**Paper first.** `"paper": true` is the default: fills are simulated and no
broker is touched. Broker logins happen on the broker's own page; never type
a password, PIN or TOTP into the chat.

**Disclosures are old and partial.** A fund's portfolio is a month-end
snapshot published up to 10 days later. Investors appear only above 1% of a
company, once a quarter. Insider trades and bulk deals are published after
the trade. Nothing here is inside information.

**Live sources were not reachable from the machine this pack was built on.**
NSE, BSE, Yahoo and Google News blocked the build environment, so those
parsers are written to the fields those sources publish and tested on
hand-made fixtures; the INDmoney data is real. NSE in particular changes its
site and refuses scripts at times: each SKILL.md says what to do then (the
same page in a browser, or a downloaded file). Check anything that matters
against your broker and the exchange.

**Taxes.** `/india-tax` does the Income-tax Act's arithmetic for a resident
individual on listed shares. It is not tax advice; rates and rules are as of
October 2026 (Budget 2026 left equity capital gains rates unchanged).

**Charges** are defaults as of 2026-10-20. Set your broker's brokerage and DP
fee in `trader/config.json` and compare one contract note.

## Files

```
india-copycat/    SKILL.md, copycat.py
india-news/       SKILL.md, news.py
india-budget/     SKILL.md, size.py, config.example.json, targets.example.json
india-insider/    SKILL.md, insider.py, investors.example.json
india-analyst/    SKILL.md, results.py
india-screener/   SKILL.md, screen.py, rules.example.json
india-risk/       SKILL.md, check.py
india-broker/     SKILL.md, ticket.py, claude_desktop_config.example.json
india-tax/        SKILL.md, lots.py, portfolio.example.json
india-alerts/     SKILL.md, alerts.py, alerts.example.json
india-rebalance/  SKILL.md, rebalance.py
india-costs/      SKILL.md, costs.py
tests/            unittest suite + offline fixtures
```

MIT. The original pack by [Jake Schincariol](https://github.com/Jakeschincariol/trader-skill);
the Indian edition for SudhirOS.
