---
name: india-tax
description: >-
  Tax Optimizer (India). Capital gains on listed shares under the Income-tax
  Act: FIFO lots (mandatory for demat shares), STCG vs LTCG at 12 months, the
  rates before and after 23 July 2024 (15%/10% then 20%/12.5%), the Section
  112A exemption (Rs 1.25 lakh a year from FY 2024-25), grandfathering for
  shares bought before 1 Feb 2018, set-off of losses and carry forward, bonus
  and split adjustments, bonus-stripping checks, and an estimated tax for the
  financial year. Finds tax-loss harvesting and LTCG exemption (gain)
  harvesting candidates and writes sells only as proposals. Not tax advice.
  Use when the user says "what are my gains", "how much tax if I sell",
  "STCG or LTCG", "harvest losses", "use my 1.25 lakh exemption",
  "grandfathering", "bonus shares cost", or before selling anything.
argument-hint: "[symbol]"
---

# india-tax

The Tax Optimizer does the arithmetic on the user's lots so nobody sells
blind. **This is not tax advice**: a chartered accountant decides what applies
to them. Say that once, early, in every answer that uses this skill.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/lots.py" report --lots                      # every lot, STCG or LTCG, LTCG date
python3 "${CLAUDE_SKILL_DIR}/lots.py" sell AXISBANK 50 --price 1236       # what a FIFO sale realizes, and its tax
python3 "${CLAUDE_SKILL_DIR}/lots.py" sell AXISBANK 50 --price 1236 --record   # it filled
python3 "${CLAUDE_SKILL_DIR}/lots.py" fy                                  # this FY: set-off, exemption, est. tax
python3 "${CLAUDE_SKILL_DIR}/lots.py" fy --fy 2025 --cess
python3 "${CLAUDE_SKILL_DIR}/lots.py" harvest                             # loss + exemption harvesting
python3 "${CLAUDE_SKILL_DIR}/lots.py" harvest --write                     # sells -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/lots.py" bonus HDFCBANK --ratio 1:1 --date 2025-08-27
python3 "${CLAUDE_SKILL_DIR}/lots.py" split TCS --ratio 1:5 --date 2026-01-15
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/portfolio.json` | positions with lots `{"id", "date", "shares", "cost"}`; cost = price x shares + buy charges except STT; `"fmv_2018_01_31"` per share on pre-2018 lots (shape: `portfolio.example.json`) |
| `trader/prices.json` | current prices (`/india-alerts`) |
| `trader/state/tax.json` | sales recorded with `sell --record` (or by `/india-broker` fills); `"carried_forward": {"stcl": x, "ltcl": y}` for losses brought forward |
| `trader/orders.json` | `harvest --write` replaces it with sell proposals |

Lots come from `/india-broker` fills, or from the broker's tradebook / Tax
P&L report. If a position has no lots, ask for each purchase date, quantity
and price. Never invent a cost. INDmoney's holdings give an average price,
not lots; that is not enough for FIFO.

## The rules it applies (listed equity, STT paid, resident individual)

- **FIFO**: for demat shares the first shares bought are the first sold
  (CBDT Circular 768, 1998). There is no picking lots; the tool does not offer it.
- **Term**: long term if held for more than 12 months. `report --lots` shows
  the first date each lot is long term.
- **Rates by sale date**: before 23 July 2024, STCG 15% (s.111A) and LTCG 10%
  (s.112A); on or after, 20% and 12.5%. Budget 2026 left these unchanged.
- **Exemption**: the first Rs 1,25,000 of LTCG in a financial year (April to
  March) is not taxed (Rs 1,00,000 up to FY 2023-24).
- **Grandfathering** (s.55(2)(ac)): for shares bought before 1 Feb 2018 the
  cost is the higher of the actual cost and the lower of the fair market
  value on 31 Jan 2018 and the sale price. The FMV is the HIGHEST price
  quoted on 31 January 2018, adjusted for later splits and bonuses. Ask the
  user for it (their broker's Tax P&L usually shows it); never estimate it.
- **Set-off**: short-term losses offset STCG first (the higher rate), then
  LTCG; long-term losses offset only LTCG. What is left carries forward 8
  years, only if the return is filed on time.
- **Bonus and split**: bonus shares cost zero and are held from the allotment
  date. A split keeps the original cost and dates. `bonus` and `split` update
  the lots; `--date` is the ex-date (the record date under T+1), so shares
  bought on or after it are not entitled; `bonus --allotment` dates the new
  lot. Then refresh prices.
- **Bonus stripping (s.94(8))**: shares bought up to 3 months before a bonus
  and sold at a loss within 9 months after it: the loss is ignored. `sell`
  warns.
- **No wash-sale rule**, but a sell and a buy of the same stock on the same
  day are netted by the broker into an intraday trade: that is speculative
  business income and the old shares are NOT sold. Re-buy on the next trading
  day at the earliest.
- **Estimated tax**: at the special rates, before surcharge and 4% cess
  (`--cess` adds the cess), ignoring the basic exemption limit and rebates.
  Buybacks from 1 April 2026 are taxed as capital gains again; enter them as
  sales.

## Futures and options (not capital gains)

F&O profits and losses are **non-speculative business income** (s.43(5)
proviso), taxed at the user's slab rate and reported in ITR-3, not under
s.111A/112A. STT and charges paid on F&O are deductible business expenses.
A net F&O loss can be set off against other income except salary in the same
year, and carried forward 8 years against business income (file on time).
Turnover for the tax-audit threshold is the sum of the absolute profit or
loss of each square-off (ICAI Guidance Note). `india-fno-plan/plan.py journal`
prints the year's F&O P&L, charges and that turnover. Intraday equity (a
same-day buy and sell) is speculative business income: separate again. Not
tax advice: whether an audit or presumptive taxation applies is for a
chartered accountant.

## Steps

1. Refresh prices, then `report --lots`. Show gains by stock, which lots are
   STCG or LTCG, and when the short-term ones turn long term (waiting a few
   days can drop the rate from 20% to 12.5%, and into the exemption).
2. Before any sell (rebalance, a stop, a change of mind) run `sell` with the
   shares and show the FIFO lots, the gain, and how much it changes this
   year's estimated tax.
3. Harvesting, near the end of the financial year (before 31 March):
   - **Loss**: sell lots under water to offset gains already booked this
     year. Show the FIFO lots it would sell and the tax change.
   - **LTCG exemption**: book long-term gains up to the unused Rs 1.25 lakh
     tax-free and buy back the next trading day to raise the cost. The new
     shares start a new 12-month clock.
   Book harvests as real sales; selling only to manufacture a loss can be
   questioned under the anti-avoidance rules.
4. Only if the user wants proposals: `harvest --write`, then `/india-risk`,
   then `/india-broker`, one yes per order. Re-buys are a separate decision on
   a later day.
5. After a sale fills, `sell ... --record` (or the broker fill) updates the
   lots and logs the gain for `fy`.

## Example: the reel's portfolio

The copy portfolio bought on 2026-10-21 is all short term until 2027-10-22.
The example file adds an older lot, 50 Axis Bank bought 2017-11-20 for Rs
26,500 with an FMV of Rs 625 on 31 Jan 2018 (illustrative numbers). Selling
those 50 at Rs 1,236 on 2026-10-21: cost Rs 31,250 (grandfathered, because
the FMV value beats the actual cost), proceeds Rs 61,781.83 after charges,
LTCG Rs 30,531.83, inside the Rs 1,25,000 exemption: estimated tax Rs 0, Rs
94,468.17 of exemption left. `harvest` lists exactly this as an exemption
harvest.

## What it never does

It never files anything, never tells the user what they owe as a final
figure, never picks lots out of FIFO order, never invents a cost or an FMV,
never places an order, and never proposes a same-day sell and re-buy.

## Safety

- Not tax advice, and education and tooling, not financial advice. Check with a chartered accountant.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
