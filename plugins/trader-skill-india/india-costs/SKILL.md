---
name: india-costs
description: >-
  Trade Costs (India). Works out every rupee a buy or sell on NSE or BSE
  really costs, line by line like a contract note: brokerage, STT (0.1% both
  sides on delivery), exchange transaction charge, SEBI fee, stamp duty (buy
  side), 18% GST and the DP charge on a delivery sell, plus the breakeven move
  for a round trip. Every other india-* skill sizes, checks and taxes with
  these numbers. Use when the user asks "what are the charges", "how much
  will this trade cost", "STT on this", "breakeven", "is it worth selling for
  this small gain", "brokerage calculator", or to check a contract note.
argument-hint: "[buy|sell|roundtrip] [rupees]"
---

# india-costs

In India the statutory charges on a trade are fixed by law and the
exchanges, not by the broker, and on delivery trades they add up to about
0.12% to buy and 0.1% plus a flat DP fee to sell. Small, but they decide
whether a small rebalance is worth doing and they are part of the tax cost.
This skill makes them visible.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/costs.py" buy 25000                     # delivery buy on NSE
python3 "${CLAUDE_SKILL_DIR}/costs.py" sell 25000 --exchange BSE
python3 "${CLAUDE_SKILL_DIR}/costs.py" roundtrip 24920 --exit 26000  # net P&L and breakeven
python3 "${CLAUDE_SKILL_DIR}/costs.py" buy 25000 --product MIS       # intraday, if they ask
python3 "${CLAUDE_SKILL_DIR}/costs.py" buy 25000 --json
```

## The lines

| line | delivery (CNC) | intraday (MIS) |
| --- | --- | --- |
| brokerage | the user's plan (default 0, set it in `"charges"`) | min(Rs 20, 0.03%) by default |
| STT | 0.1% on buy and on sell, rounded to the rupee | 0.025% on the sell only |
| exchange charge | NSE 0.00297%, BSE 0.00375% of turnover | same |
| SEBI fee | Rs 10 per crore | same |
| stamp duty | 0.015% on the buy | 0.003% on the buy |
| GST | 18% of brokerage + exchange charge + SEBI fee | same |
| DP charge | a flat fee per stock per day on a delivery sell (default Rs 15.93) | none |

These are the defaults as of 2026-10-20. Exchanges revise their transaction
charge from time to time and every broker has its own brokerage and DP fee.
Put the user's real numbers in `trader/config.json` under `"charges"`, for
example `{"charges": {"brokerage_delivery": {"flat": 20, "pct": 0.1},
"dp_charge_per_scrip": 18.5}}`, and check one real contract note against
`costs.py` output once. The contract note is always the final word.

## Futures and options

`costs.py buy 1488110 --fno FUT` (one NIFTY lot) or `costs.py roundtrip 25902 --exit 41600 --fno OPT`
(premium values). From 2026-04-01 (Finance Act 2026):

| line | futures | options |
| --- | --- | --- |
| STT | 0.05% on the sell side | 0.15% of the premium on the sell side; 0.15% of the intrinsic value if exercised (buyer) |
| exchange (NSE) | 0.00183% | 0.03553% of the premium |
| stamp duty | 0.002% on the buy | 0.003% on the buy |
| SEBI fee, GST | Rs 10 a crore; 18% of brokerage + exchange + SEBI | same |
| brokerage | Rs 20 an order by default (`"fno": {"brokerage_per_order": ...}` under `"charges"`) | same |

## Steps

1. Ask what trade: buy or sell, the value (price x quantity), NSE or BSE,
   delivery or intraday.
2. Run `costs.py`, show the lines and the total, and the percent of the order.
3. For a planned exit, run `roundtrip` with the entry and exit values and say
   the breakeven: how far the price must move before selling covers the
   charges.
4. If a trade's charges are a big share of it (a Rs 2,000 sell pays a Rs 15.93
   DP fee, 0.8%), say so plainly.

## Example

`costs.py roundtrip 24920 --exit 26000` (35 HDFC Bank shares bought at Rs 712,
sold at about Rs 742.86):

```
BUY  charges Rs 29.64 (0.119%)   STT Rs 25, stamp Rs 3.74, exchange Rs 0.74, GST Rs 0.14
SELL charges Rs 42.87 (0.165%)   STT Rs 26, DP Rs 15.93, exchange Rs 0.77, GST Rs 0.14
Gross Rs 1,080, charges Rs 72.51, net Rs 1,007.49.
Breakeven: the position must be worth Rs 24,991.47 (+0.287%) before you sell.
```

## What it never does

- Never says the charges are exact for the user's broker until the user has
  set their own plan in the config.
- Never folds income tax into these numbers: capital gains tax is
  `/india-tax`.
- Never places, sizes or recommends a trade.

## Safety

Education and tooling, not financial advice. This skill only does arithmetic.
Nothing places an order except `/india-broker`, after `/india-risk` passes and
the user types an explicit yes for that order.
