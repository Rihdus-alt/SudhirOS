---
name: india-budget
description: >-
  Budget Manager (India). Sizes every position to the user's rupee budget in
  WHOLE shares (NSE and BSE have no fractional shares): keeps a cash buffer,
  caps any one stock and shares the excess out, rounds each stock to the
  share count closest to its target instead of always rounding down, costs
  every buy at its worst-case price plus STT, stamp duty, exchange, SEBI and
  GST charges so the money really suffices, drops stocks whose one share
  costs more than their slice, reports the tracking error, and writes
  delivery (CNC) limit orders to trader/orders.json in broker field names.
  Nothing is placed. Use when the user says "I have 1 lakh", "size my
  trades", "how many shares of each", "split my money", "keep 5% in cash",
  "no stock over 25%", "set my rules", or after /india-copycat writes targets.
argument-hint: "[budget in rupees]"
---

# india-budget

The Budget Manager turns "copy this portfolio" into "buy exactly these many
shares of each", inside rules the user sets once. It writes proposals only.
`/india-risk` checks them and `/india-broker` asks the user yes or no for
each one.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader/targets.json + config + prices
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 100000 --write  # -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights 'HDFCBANK=7.8,ICICIBANK=5.2,M&M=3.9'
python3 "${CLAUDE_SKILL_DIR}/size.py" --fill                   # spend leftovers even past a target
python3 "${CLAUDE_SKILL_DIR}/size.py" --json
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/targets.json` | the weights to copy, from `/india-copycat` (`targets.example.json` here) |
| `trader/prices.json` | required: whole shares need real prices (`/india-alerts`) |
| `trader/orders.json` | the proposed buy orders, with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into it
and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much stays in cash (default 5).
- **max_position_pct**: the most any one stock may be, as a percent of the budget (default 25).
- **max_order_inr** and **min_order_inr**: the biggest and smallest single order (50,000 and 500).
- **order_type**: `LIMIT` (default; a limit 0.5% through the last price, on a valid tick, inside the
  circuit band, so the worst fill is known) or `MARKET`.
- **charges**: their broker's delivery brokerage and DP fee (see `/india-costs`).
- **blocked**: symbols they never want to own.
- **broker**: `manual` (they place each order in their own app) or `kite` (Zerodha's Kite MCP connector).
- Keep **paper** true until they have watched it work.

## Step 2: size it

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

The maths, in order: the cash buffer comes off the top; the weights are
renormalised over the stocks that are left; any stock over the cap is pinned
at the cap and its excess shared out by weight until nobody is over; each
stock gets the whole number of shares NEAREST its rupee target; then, if the
worst case (every buy at its limit plus all charges) does not fit, one share
at a time is taken back where it hurts the weights least; then shares are
added back where they make the portfolio more accurate and still fit. A stock
whose single share costs more than its slice or the cap (MRF at Rs 1.2 lakh a
share) is dropped with the reason and its weight shared out.

## Step 3: show the user

Show the table: symbol, the copied weight, the target after the cap, the
actual weight in whole shares, price, shares, value, charges and the limit
price. Then the cash buffer, what is left idle, the worst-case cash needed and
the tracking error. Say where the weights came from and how old they are.
Then hand over to `/india-risk`.

## Example: the reel

Parag Parikh Flexi Cap Fund's top five Indian holdings as INDmoney listed
them on 2026-10-20 (HDFC Bank 7.8%, ICICI Bank 5.2%, M&M 3.9%, Axis Bank
2.8%, TCS 2.5%; Amazon's 2.3% is left out because it is a US stock). Rs
1,00,000, a 5% buffer, a 25% cap, prices of the 2026-10-19 close:

| symbol | target | shares | value | limit |
| --- | --- | --- | --- | --- |
| HDFCBANK | 26.32% (capped) | 35 | Rs 24,920 | Rs 715.55 |
| ICICIBANK | 26.32% (capped) | 18 | Rs 24,858 | Rs 1,387.90 |
| M&M | 20.08% | 6 | Rs 17,442 | Rs 2,921.50 |
| AXISBANK | 14.42% | 11 | Rs 13,596 | Rs 1,242.10 |
| TCS | 12.87% | 5 | Rs 12,304.50 | Rs 2,473.20 |

Invested Rs 93,120.50, estimated charges Rs 111.44, buffer Rs 5,000, about
Rs 1,768 idle (a seventh M&M share would not fit the worst case), tracking
error 1.34%. The first order is
`{"variety": "regular", "exchange": "NSE", "tradingsymbol": "HDFCBANK", "transaction_type": "BUY", "quantity": 35, "product": "CNC", "order_type": "LIMIT", "validity": "DAY", "price": 715.55}`.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget.
- Every number comes from the config, the targets or the prices. Never invent a price.
- No projections: never say what the positions will be worth or earn.
- Whole shares only. Never propose a fractional quantity.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, and never sizes F&O, intraday, margin (MTF) or short positions.

## Safety

- Education and tooling, not financial advice. Nothing here is a recommendation or a SEBI-registered adviser's view.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
