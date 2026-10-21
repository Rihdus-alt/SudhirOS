---
name: india-copycat
description: >-
  The Reverse Engineer (India). Rebuilds the portfolio of a fund or investor
  the user trusts from public Indian disclosures and turns it into weights to
  copy: a mutual fund scheme's full monthly portfolio (SEBI makes every AMC
  publish it: ISIN, quantity, value, % of net assets), the INDmoney
  connector's fund holdings, or a "superstar" investor's named stakes from
  quarterly shareholding patterns (holders over 1%). Checks every ISIN,
  maps it to its NSE symbol, drops debt, cash, foreign stocks and unlisted
  rows with the reason, shows what changed since last month or quarter, and
  writes trader/targets.json. Use when the user says "copy Parag Parikh
  Flexi Cap", "what does this fund hold", "copy a smallcase-like basket",
  "what is Kacholia buying", "superstar portfolio", "what did the fund buy
  last month", or names any Indian fund or investor.
argument-hint: "[fund file or investor] [--top N]"
---

# india-copycat

Copying starts with what a fund or investor is required to show:

- **Mutual funds** publish their complete portfolio every month (SEBI rule:
  within 10 days of the month end, on the AMC's website, usually as an Excel
  file with one sheet per scheme). This is the most complete and exact
  public portfolio in India: every holding, its ISIN, quantity and % of net
  assets.
- **Individual investors** do not file portfolios. Their stakes show up only
  where they own more than 1% of a company, by name, in that company's
  quarterly shareholding pattern (SEBI LODR Reg 31), and in bulk deals
  (`/india-insider`). Everything smaller is invisible.

Tool in this folder (standard library Python):

```bash
python3 "${CLAUDE_SKILL_DIR}/copycat.py" --help
python3 "${CLAUDE_SKILL_DIR}/copycat.py" symbols --download                 # NSE equity list: ISIN -> symbol
python3 "${CLAUDE_SKILL_DIR}/copycat.py" mf portfolio-sep-2026.xlsx --sheet "Flexi Cap"
python3 "${CLAUDE_SKILL_DIR}/copycat.py" diff portfolio-aug.xlsx portfolio-sep.xlsx --sheet "Flexi Cap"
python3 "${CLAUDE_SKILL_DIR}/copycat.py" copy portfolio-sep-2026.xlsx --sheet "Flexi Cap" --top 10
python3 "${CLAUDE_SKILL_DIR}/copycat.py" mf trader/state/fund.json --indmoney
python3 "${CLAUDE_SKILL_DIR}/copycat.py" investor trader/investor-rows.csv --name "Kacholia" --out trader/targets.json
python3 "${CLAUDE_SKILL_DIR}/copycat.py" shp SHP-XBRL.xml --min-pct 1
```

## What it writes

- `trader/targets.json`: `{ "source", "as_of", "weights": [ {"ticker",
  "name", "isin", "weight"} ] }`, weights in % summing to 100 over the stocks
  copied, plus what was dropped or not copied and why, and the caveats.
  `/india-budget` reads it. (`/india-budget/targets.example.json` is the
  reel's output.)
- `trader/symbols.json` (the user's, optional): ISIN or company name to NSE
  symbol fixes, used first: `{"INE002A01018": "RELIANCE"}`.

## Sources, most exact first

1. **The AMC's monthly portfolio file.** Ask the user for the fund and month,
   download the AMC's "monthly portfolio" Excel from its website (or ask the
   user to), and pass the scheme's sheet with `--sheet`. Old `.xls` files:
   save as `.xlsx` or `.csv`. The tool finds the header row (ISIN, name,
   quantity, market value in lakhs, % to net assets), reads the "as on"
   date, and handles Indian number formats and bracketed negatives.
2. **The INDmoney connector** (`get_mf_funds_details` with includes
   `["holdings"]`, saved as returned): quick, but it lists top holdings only,
   rounded to 0.1%, without ISINs. The tool says how much of the fund that
   covers. Fine for a top-5 copy, not for an exact one.
3. **An investor's stakes**: collect rows (investor, symbol, quarter end,
   shares, % of company) from each company's shareholding pattern on NSE or
   BSE (Shareholding Pattern > Public shareholders > holding more than 1%),
   or the filing's XBRL (`shp` reads it), into a CSV. `investor` values them at
   today's prices, weights them, and shows new, exited, added and trimmed
   since the previous quarter.

## The rules

- **Say how old the data is, every time.** A monthly portfolio is the last
  day of the month, out by the 10th; a shareholding pattern is the quarter
  end, filed within 21 days. Say "holdings as on 30 September 2026".
- **Say what it cannot show.** Funds: nothing traded inside the month, no
  prices paid. Investors: nothing under 1% of a company, no trades between
  quarter ends (bulk deals cover some), and family or company accounts may
  hold under other names.
- **Every ISIN is checked** (check digit, India, equity code). Foreign
  stocks (a flexi-cap fund's US holdings), debt, T-bills, TREPS, REIT/InvIT
  units and partly paid shares are never copied; they are listed with the
  reason.
- **No guessed symbols.** A holding without an exact ISIN or name match is
  listed, not copied. If it is as big as something being copied, `copy`
  stops and asks for the mapping (add it to `trader/symbols.json`, or run
  `symbols --download`). `--guess` marks name guesses with `*` and `copy`
  refuses them unless `--allow-guesses` after the user confirms.
- **The fund, not the manager.** "The fund held", never "the manager bought".

## Steps

1. Ask who to copy and how much of it (top N, or everything over a percent).
2. Get the source (above). Run `mf` (or `investor`) and show the holdings,
   the coverage and what is not copied.
3. Run `diff` against last month's file when the user wants to know what
   changed: share counts at two month ends, not trades.
4. `copy ... --top N --out trader/targets.json`, show the weights, then hand
   off to `/india-budget`.

## Example: the reel

"Copy Parag Parikh Flexi Cap, top 5." From the INDmoney connector on
2026-10-20: HDFC Bank 7.8%, ICICI Bank 5.2%, M&M 3.9%, Axis Bank 2.8%, TCS
2.5% (Amazon.com, 2.3%, is a US stock and is not copied). Renormalised: 35.14
/ 23.42 / 17.57 / 12.61 / 11.26. The tool notes that INDmoney lists 20 equity
rows covering about 30% of a 90.2% equity fund, rounded to 0.1%: for an exact
copy, read PPFAS's own monthly portfolio file. Then: "Next, /india-budget
sizes it to your Rs 1,00,000 in whole shares."

## What it never does

- Never claims the fund or investor still holds a stock today, or why.
- Never claims returns, or that copying makes money. A copy buys weeks after
  the fund did, at today's price.
- Never places an order. It writes target weights; sizing, risk checks and
  the user's yes come after.
- Never copies derivatives, debt, foreign or unlisted holdings.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
- Disclosures are monthly or quarterly and up to weeks old when published. Say so.
