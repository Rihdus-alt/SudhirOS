---
name: india-screener
description: >-
  The Stock Screener (India). Checks a list of Indian companies against the
  user's own rules (ROCE, sales and profit growth, debt to equity, promoter
  holding, promoter pledge, P/E, EBITDA margin, anything in the metrics
  file), says exactly which rule each company passes or fails and by how
  much, and ranks the ones that pass. Reads the metrics /india-analyst writes
  from exchange filings, or any screener CSV export, understanding Indian
  number formats (Rs 1,02,350, 4,512 Cr, 35 lakh, bracketed negatives). Use
  when the user says "screen for stocks", "find me companies with ROCE over
  20", "low debt high growth", "promoter holding above 50", "no pledged
  shares", "filter my watchlist", "rank these stocks", or "shortlist".
argument-hint: "[metrics file] [--rules rules.json]"
---

# india-screener

A screen turns "I want profitable companies that are still growing, with
promoters who have skin in the game and no pledges" into rules a computer
can check, and shows its work: every pass and every fail with the number and
the threshold.

Tool in this folder (standard library Python, no network):

```bash
python3 "${CLAUDE_SKILL_DIR}/screen.py" --help
python3 "${CLAUDE_SKILL_DIR}/screen.py" trader/metrics.json --rules trader/rules.json --prices trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/screen.py" export.csv --rules "${CLAUDE_SKILL_DIR}/rules.example.json" --out trader/shortlist.json
```

## What it reads and writes

- Metrics: `trader/metrics.json` from `/india-analyst` (`results.py read ...
  --metrics trader/metrics.json`), or a CSV export from a screener site with
  an `NSE Code` / `Symbol` / `ticker` column. Column names are matched
  loosely ("ROCE %" = `roce`, "Pledged percentage" = `pledged_pct`, "Sales
  growth 3Years %" = `sales_growth_3y`). A column whose header says it is in
  crores ("Market Cap Rs.Cr.") stays in crores even when a cell says "Cr".
- Prices (optional): `trader/prices.json`. With prices it adds `price`, `pe`
  (only when EPS is positive), `earnings_yield` and `market_cap` (when the
  metrics carry `shares_outstanding`).
- Rules: JSON, start from `rules.example.json`.
- Writes `trader/shortlist.json` with `--out`.

## The rules file

```json
{"name": "Quality compounders at a fair price",
 "rules": [{"metric": "roce", "op": ">=", "value": 15},
           {"metric": "sales_growth_3y", "op": ">=", "value": 10},
           {"metric": "debt_to_equity", "op": "<=", "value": 0.5},
           {"metric": "promoter_holding", "op": ">=", "value": 40},
           {"metric": "pledged_pct", "op": "<=", "value": 5},
           {"metric": "pe", "op": "<=", "value": 40}],
 "rank": [{"metric": "roce", "order": "desc", "weight": 2},
          {"metric": "pe", "order": "asc", "weight": 1}],
 "missing": "fail"}
```

Ops: `>`, `>=`, `<`, `<=`, `==`, `!=`, `between` ([low, high]), `in` and
`not in` (a list of symbols or sectors). `"missing": "fail"` fails a company
missing a metric a rule needs (banks have no EBITDA margin); `"skip"` ignores
that rule for it and says so.

India-specific checks worth suggesting: promoter pledge (a high or rising
pledge is a classic Indian red flag), promoter holding, ROCE rather than ROE
for leveraged businesses, and other income as a share of profit (from
`/india-analyst`: profit that leans on treasury income is not operating
strength).

## Steps

1. Turn the user's words into rules. Ask when a threshold is vague ("cheap"
   means what P/E? "low debt" means what debt to equity?). Write them to
   `trader/rules.json`.
2. Get metrics: run `/india-analyst` on the candidates, or use the user's own
   export. Add prices from `/india-alerts` when P/E matters.
3. Run the screen. Exit code 1 means nothing passed: show the closest misses.
4. Report the shortlist with the reasons, the misses with the numbers that
   failed, and the dates the numbers are from. Suggest `/india-analyst` and
   `/india-news` on the top names before anything else.

## Example

`screen.py` on a five-company export with `rules.example.json` and prices:

```
Quality compounders at a fair price: 2 of 5 companies pass.
 1. PASS ALPHAPIPE    Alpha Pipes Ltd  score 0.50  (ROCE 24.5, growth 18.2, P/E 29.7)
 2. PASS BRAVOFOOD    Bravo Foods Ltd  score 0.50  (ROCE 31.0, growth 11.5, P/E 32.4)
    FAIL ECHOSOFT     pe 48.1, needs <= 40
    FAIL DELTAPHAR    sales_growth_3y 9.1, needs >= 10; pe 40.6, needs <= 40
    FAIL CHARLIEINF   roce 11, needs >= 15; debt_to_equity 1.4, needs <= 0.5; pledged_pct 12.5, needs <= 5
```

(The five are made-up test companies.)

## What it never does

- Never calls a passing company a buy. A screen is a filter on reported numbers.
- Never fills in a missing number or guesses a metric: missing means missing.
- Never sends anything to `/india-budget` or an order path by itself.

## Safety

Education and tooling, not financial advice. This skill only filters numbers.
Nothing places an order except `/india-broker`, after `/india-risk` passes and
the user types an explicit yes for that order.
