---
name: india-rebalance
description: >-
  Rebalancer (India). Brings the portfolio back to its target weights when it
  drifts: values every holding at current prices, works out each stock's
  drift in percentage points, trades only the stocks outside a band (default
  5 points), in whole shares, keeps the cash buffer and the per-stock cap,
  counts STT and every other charge and skips trades whose charges are not
  worth it, lists sells before buys, and writes delivery limit orders to
  trader/orders.json as proposals. Use when the user says "rebalance", "my
  portfolio drifted", "bring it back to the targets", "I added money", "the
  fund's new portfolio changed the weights", "am I overweight", or on a
  schedule after /india-copycat updates the targets.
argument-hint: "[band in points]"
---

# india-rebalance

The Rebalancer keeps the portfolio looking like the plan. Prices move, new
money arrives and the copied fund's next monthly portfolio changes the
weights; this skill finds the stocks that drifted too far and proposes the
whole-share trades that bring them back. It writes proposals only:
`/india-risk` checks them and `/india-broker` asks the user yes or no for each.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/rebalance.py"                    # the drift table, no files written
python3 "${CLAUDE_SKILL_DIR}/rebalance.py" --band 5 --write   # proposals -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/rebalance.py" --sell-untracked   # also sell holdings not in the targets
python3 "${CLAUDE_SKILL_DIR}/rebalance.py" --json
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/portfolio.json` | cash and positions (shares) |
| `trader/prices.json` | a price for every holding and target (`/india-alerts`) |
| `trader/targets.json` | the target weights (`/india-copycat`) |
| `trader/config.json` | cash buffer, cap, min and max order, `rebalance_band_pct`, `max_cost_pct`, charges |
| `trader/orders.json` | the proposals, sells first, with `--write` |

## How it decides

- **Account value** = cash + every holding at its price.
- **Targets**: weights renormalised over the stock sleeve (account minus the
  buffer), none above `max_position_pct` of the account; the excess is shared
  out the way `/india-budget` does it.
- **Drift** = current percent of the account minus target percent.
- **Band**: only stocks drifting MORE than the band trade. A trade takes the
  stock to the whole number of shares nearest its target.
- **Charges**: a trade whose charges are over `max_cost_pct` of its value
  (default 1%: the DP fee alone is 0.8% of a Rs 2,000 sell) is skipped.
- **Sells first**, then buys, which only spend cash above the buffer after
  charges; if there is not enough, every buy is scaled down by the same share.
  Indian brokers usually let 80% of a delivery sale's proceeds be used the
  same day and the rest after T+1 settlement.
- Trades under `min_order_inr` are skipped; a trade over `max_order_inr` is
  cut to it, with a note to run again after it fills.
- **Untracked** holdings stay put and are listed, unless `--sell-untracked`.

## Steps

1. Refresh prices (`/india-alerts`).
2. Run `rebalance.py` and explain the drift table in plain words: which
   stocks are over or under, by how many points, and what it would trade.
3. Before any sell, run `/india-tax` (`lots.py sell SYMBOL SHARES`) and show
   what the sale would realize and its tax. Selling a short-term winner
   costs 20%; waiting until it is long term costs 12.5% and may fall inside
   the Rs 1.25 lakh exemption.
4. If the user wants the trades: `rebalance.py --write`, then `/india-risk`,
   then `/india-broker`, one yes per order.

## Example: the reel's portfolio

The copy portfolio bought on 2026-10-21 (35 HDFC Bank, 18 ICICI Bank, 6 M&M,
11 Axis Bank, 5 TCS, Rs 6,769 cash) valued at the 2026-10-19 closes sits on
its targets: every drift is under 2 points, so `rebalance.py` says "Nothing is
outside the band" and exits 1. Add Rs 50,000 of cash and the account is about
Rs 1.5 lakh with a Rs 37,472 cap: HDFC Bank is 8.4 points under target, ICICI
Bank 8.4 and M&M 7.4, so it proposes buying 17 HDFC Bank, 9 ICICI Bank and 4
M&M; Axis Bank (4.6) and TCS (4.0) are inside the band and wait.

## What it never does

It never places an order, never trades inside the band to chase small
differences, never sells holdings outside the targets unless asked, and never
says a rebalance will improve returns.

## Safety

- Education and tooling, not financial advice. A rebalance follows the user's own targets, not a forecast.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
