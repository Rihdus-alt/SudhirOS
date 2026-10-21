---
name: india-analyst
description: >-
  The Earnings Analyst (India). Reads a listed company's quarterly results
  as filed with NSE and BSE (the Ind-AS results XBRL under SEBI LODR Reg 33,
  consolidated by default) and lines the quarters up: revenue, operating
  EBITDA (before other income and exceptional items), margins, profit for
  the company's own shareholders, diluted EPS, growth against the same
  quarter last year and the quarter before, and the last four quarters.
  Flags profit that leans on other income, exceptional items, odd tax rates,
  restatements and mixed consolidated/standalone numbers. Writes a metrics
  row for the screener. Use when the user says "how were Infosys's results",
  "analyse the quarter", "is revenue growing", "check the Q2 numbers",
  "compare margins", or "run the analyst on my holdings".
argument-hint: "[symbol or XBRL files]"
---

# india-analyst

Results season moves Indian stocks, and the press release leads with the best
number. This skill reads what the company actually filed with the exchanges,
in XBRL, lines the quarters up and says plainly what changed.

Tool in this folder (standard library Python):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
python3 "${CLAUDE_SKILL_DIR}/results.py" fetch INFY --quarters 5            # NSE's results list -> XBRL files
python3 "${CLAUDE_SKILL_DIR}/results.py" read trader/state/results/INFY-*.xml
python3 "${CLAUDE_SKILL_DIR}/results.py" read trader/state/results/INFY-*.xml --basis standalone
python3 "${CLAUDE_SKILL_DIR}/results.py" read INFY-*.xml --metrics trader/metrics.json
python3 "${CLAUDE_SKILL_DIR}/results.py" read quarters.csv --symbol INFY    # your own numbers
```

## What it reads and writes

- `fetch` reads NSE's financial results list for the symbol and downloads
  each filing's XBRL to `trader/state/results/`. If NSE refuses the script,
  download the XBRL from the company's results page on NSE or BSE (the
  "XBRL" link next to each quarter) and pass the files to `read`.
- `read` takes one or more results XBRL files. Each filing carries the
  quarter, the quarter before and the same quarter last year, so two or three
  filings give a two-year series. When a later filing restates a quarter, the
  later number wins and the quarter is flagged.
- A CSV also works (`period_end, revenue, other_income, finance_costs,
  depreciation, exceptional, pbt, tax, net_profit, owners_profit,
  eps_diluted`, with an optional `unit` column: crore or lakh).
- `--metrics trader/metrics.json` adds a row for `/india-screener`:
  revenue, profit and EPS for the last four quarters, YoY growth, EBITDA and
  net margin, other income share, and the flags.

## The rules

- **Filed numbers first.** The table is what the company filed. Use the
  results PDF and the investor presentation for context (segment colour,
  management commentary) and say which numbers came from where.
- **Consolidated by default**, standalone with `--basis standalone`. Never
  compare one with the other; the tool warns if they are mixed.
- **Like with like.** Growth is against the same quarter a year earlier
  first. Quarter-on-quarter is shown, but Indian businesses are seasonal
  (festive quarters, monsoon, March-quarter year ends).
- **Operating profit is not net profit.** EBITDA here leaves out other income
  and exceptional items. When other income is over 25% of profit before tax,
  or an exceptional item is in the quarter, the tool flags it: say so.
- **Profit for the owners** is used when the filing has it (after minority
  interest), so EPS and profit line up.
- **Banks and NBFCs** file a different format (interest earned, provisions):
  revenue falls back to interest earned and EBITDA is not meaningful. Read
  NIM, asset quality (GNPA, NNPA) and provisions from the filing itself.

## Steps

1. Get the filings: `fetch SYMBOL --quarters 5`, or the user's files.
2. `read` them and show the table in crores.
3. Write the summary: one line on the quarter (revenue, profit, EPS, YoY),
   one on margins, one on anything unusual (other income, exceptional items,
   tax, restatement, a loss), and the links. Plain words, real numbers, no
   adjectives the numbers do not support.
4. If the user is screening, add `--metrics trader/metrics.json` and hand
   over to `/india-screener`.

## Example (test fixtures: a made-up company)

```
ALPHAPIPE Alpha Pipes Limited, consolidated figures, rupees in crores
quarter        revenue      YoY      QoQ     EBITDA  margin     profit      YoY      EPS     YoY
2026-03-31     1150.00   +21.1%    +9.5%     202.00   17.6%     216.00   +96.4%     8.64  +96.4%
2026-06-30     1100.00             -4.3%     208.00   18.9%     132.00              5.28
2026-09-30     1200.00   +20.0%    +9.1%     230.00   19.2%     150.00   +27.1%     6.00  +27.1%
FLAG 2026-03-31: exceptional item of Rs 50.00 cr in profit
Last four quarters: revenue Rs 4500.00 cr, profit Rs 623.00 cr, diluted EPS Rs 24.92.
```

Summary: "Q2 FY27 revenue Rs 1,200 crore, +20% on a year ago; profit to
shareholders Rs 150 crore, +27%; EBITDA margin 19.2%, flat on a year. The
March quarter's +96% profit jump included a Rs 50 crore exceptional gain;
without it the growth is far smaller."

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell.
- Never invents a number the filings do not have, or annualises a quarter's EPS.
- Never mixes consolidated and standalone without saying so.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/india-broker`, after `/india-risk` passes and
the user types an explicit yes for that order.
