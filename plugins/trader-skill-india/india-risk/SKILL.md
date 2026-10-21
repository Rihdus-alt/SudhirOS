---
name: india-risk
description: >-
  Risk Manager (India). Checks every proposed order against the user's limits
  and the exchange's rules before it can reach a broker, and blocks anything
  that breaks them: whole-share quantity, NSE/BSE symbol, CNC delivery only
  (F&O, intraday MIS and MTF margin off unless allowed), limit price outside
  the day's circuit band, ASM/GSM surveillance stocks, trade-to-trade
  restrictions, order size, position cap after the fill, the cash buffer
  after charges, selling shares not held, duplicates and a daily order
  limit. Prints PASS or BLOCK with reasons and records each verdict so
  /india-broker shows only an order that passed. Use when the user says
  "check my orders", "is this safe", "risk check", "can I place this", or
  before /india-broker, always.
argument-hint: "[path to orders.json]"
---

# india-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by `/india-budget`, `/india-rebalance` or `/india-tax` passes through
it. A BLOCK is final for that order: it is not shown to the user for a yes
and it does not go to the broker.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"             # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write  # look without recording
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (Kite field names: exchange, tradingsymbol, transaction_type, quantity, product, order_type, price, validity, variety) |
| `trader/config.json` | the limits (`/india-budget` ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | prices plus circuit limits, series and surveillance (`/india-alerts`) |
| `trader/state/broker.json` | what was placed today, for duplicates and the daily limit |
| `trader/state/risk.json` | the verdicts, with a fingerprint of each exact order |

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a rule together is caught. Every
buy is valued at its limit (or the last price plus slippage for a MARKET
order) and its charges are added.

| check | blocks when |
| --- | --- |
| format | no symbol, exchange not NSE/BSE, side not BUY/SELL, a quantity that is not a positive whole number, unknown product, order type, validity or variety, LIMIT without a price, SL without a trigger, a price not in whole paise or off the stock's tick |
| product | MIS while `allow_intraday` is false, MTF while `allow_mtf` is false, NRML while `allow_fno` is false |
| asset | a futures or options contract (NIFTY26OCT25000CE) while `allow_fno` is false; anything that is not an NSE symbol or a BSE code |
| blocked | the symbol is on the `blocked` list |
| band | a buy limit above the upper circuit, or a sell limit below the lower circuit (the exchange would reject it) |
| surveillance | the stock is under ASM, GSM or ESM surveillance and `block_surveillance` is true (the default); MIS on a trade-to-trade (BE) stock |
| size | under `min_order_inr` or over `max_order_inr` |
| position | the stock would be over `max_position_pct` of the account after the fill |
| cash | the buy plus charges would leave less than the cash buffer, or need margin |
| short | a sell of more shares than held (retail cannot sell delivery shares it does not own) |
| duplicate | the same symbol and side twice in one batch, or already placed today |
| daily limit | more than `max_orders_per_day` placed today |

F&O orders (exchange NFO or BFO, from /india-fno-plan) get their own checks:

| check | blocks when |
| --- | --- |
| switch | `"allow_fno"` is false (the default) |
| symbol | not an F&O trading symbol (NIFTY26OCT23000CE, NIFTY26O2723000CE, RELIANCE26OCTFUT) |
| lots | the quantity is not a whole number of lots, is over `fno.max_lots_per_order`, or over the freeze quantity |
| stop | an entry without a stop-loss (`sl`), or a stop on the wrong side of the entry |
| risk | the loss at the stop is over `fno.max_risk_per_trade_pct` of `fno.capital` (default 2%) |
| option selling | selling an option you do not hold (`allow_option_selling` false); exits carry `"closing": true` |
| closing | an order marked `"closing"` that does not reduce a position in `trader/fno/journal.json` (india-broker records every F&O fill there), or that is bigger than it: it is then checked as the new position it would open |
| expiry | the contract has expired, or it is expiry day and the order opens a position |
| band | a limit outside the exchange's execution band (from INDmoney's contract quote) |
| cash | the estimated margin (futures) or premium plus charges (options) would dip into the cash buffer |

Stock F&O within four days of expiry gets a warning: it settles by delivery.

Warnings do not block, but say them: a LIVE account, a stock at its circuit
(the order may not fill), a 2% or 5% band with a MARKET order, a T2T stock,
a MARKET order (the fill price is unknown), prices more than 30 hours old, a
buy and a sell of the same stock on one day (the broker nets them into an
intraday trade, which is taxed as speculative income).

## Steps

1. Make sure `trader/prices.json` is fresh, with circuit limits if possible
   (`/india-alerts`, the INDmoney import or `--source nse`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order. For a BLOCK, give
   the reason and the fix (fewer shares, a different stock, or a rule the
   user may decide to change). Never change a limit yourself.
4. If the orders change in any way, check again. `/india-broker` matches each
   order to its verdict by fingerprint, so an edited order is not ready until
   it passes again, and a verdict from yesterday does not count today.

## Example: the reel

The five buys from `/india-budget` on a fresh Rs 1,00,000 paper account:

```
PASS  #1 BUY 35 x NSE:HDFCBANK, LIMIT at Rs 715.55, about Rs 25,044.25 + Rs 29.67 charges
PASS  #2 BUY 18 x NSE:ICICIBANK, LIMIT at Rs 1,387.90, about Rs 24,982.20 + Rs 29.65 charges
PASS  #3 BUY 6 x NSE:M&M, LIMIT at Rs 2,921.50, about Rs 17,529 + Rs 21.27 charges
PASS  #4 BUY 11 x NSE:AXISBANK, LIMIT at Rs 1,242.10, about Rs 13,663.10 + Rs 16.55 charges
PASS  #5 BUY 5 x NSE:TCS, LIMIT at Rs 2,473.20, about Rs 12,366 + Rs 14.30 charges
5 passed, 0 blocked. Cash after the passed orders, worst case: Rs 6,304.01 (paper account).
```

A sixth order for 3 more HDFC Bank at Rs 820 would be blocked three ways:
above today's Rs 801.70 upper circuit, HDFC Bank over its 25% cap, and a
second HDFCBANK buy in the batch.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, and never treats a PASS as the user's yes. A PASS only means
the order may be shown to the user.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
