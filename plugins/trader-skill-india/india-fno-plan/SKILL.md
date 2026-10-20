---
name: india-fno-plan
description: >-
  F&O Trade Planner (India). Turns a view on NIFTY, BANKNIFTY or an F&O stock
  into an exact, sized trade: entry, stop-loss, TP1, TP2 and a trailing stop
  on the underlying, the same levels translated to the futures price (basis)
  and to the option premium (Black-76 repricing with time decay), whole lots
  sized to a rupee risk with every charge counted, the Kite trading symbol,
  the entry order for /india-risk and /india-broker, and the stop-loss and
  target orders to place after the fill. Then it trails the stop each session
  (chandelier, breakeven at TP1, tighter after TP2), flags expiry and stock
  F&O physical delivery, and keeps an F&O journal of P&L, charges and
  turnover. Use when the user says "plan this trade", "where should my SL
  be", "how many lots", "trail my stop", "update my stop-loss", "target for
  my option", or "my F&O P&L".
argument-hint: "[symbol] [long|short]"
---

# india-fno-plan

The planner makes the decisions that should be made before an order, not
during one: where the trade is wrong (the stop), what it can reasonably give
(the targets), how big it can be (lots from a rupee risk), and how the stop
will follow the price. **About 91% of individual F&O traders lost money in
FY2024-25 (SEBI).** Say it once, early.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/plan.py" new NIFTY --candles trader/fno/NIFTY-1d.json --direction short \
  --chain trader/fno/nifty-chain.json --instrument both --fut-price 22894 --capital 3000000 --risk-pct 1
python3 "${CLAUDE_SKILL_DIR}/plan.py" new RELIANCE --candles trader/fno/RELIANCE-1d.json --direction long \
  --lot 500 --instrument future --fut-expiry 20261124 --write
python3 "${CLAUDE_SKILL_DIR}/plan.py" trail RELIANCE-20261019-FUT --candles trader/fno/RELIANCE-1d.json
python3 "${CLAUDE_SKILL_DIR}/plan.py" list
python3 "${CLAUDE_SKILL_DIR}/plan.py" close RELIANCE-20261019-FUT --price 1240 --reason "trailing stop"
python3 "${CLAUDE_SKILL_DIR}/plan.py" journal --fy 2026
```

`fnocore.py` in this folder is the shared engine (indicators, Black-76,
setups, levels, trailing, backtest, chain analytics, journal) that
/india-fno-scan, /india-fno-chain and /india-fno-backtest also load.

## The plan

- **Stop-loss**: beyond the last swing low (long) or high (short) plus 0.2
  ATR, when that is between 0.75 and 3 ATR from the entry; otherwise 1.5 ATR.
- **TP1** at 1R: book half (`tp1_exit_fraction`) and move the stop to entry.
  With 1 lot there is no half: the plan says so; choose all-out or stop-to-entry.
- **TP2** at 2R, pulled in ahead of the OI resistance (long) or support
  (short) when the chain shows one closer. Under 1.5R, or with a wall before
  TP1, the plan is NOT WORTH IT and `--write` refuses unless `--force`.
- **Trailing**: after TP1 the stop follows 2.5 ATR behind the best price
  since entry, 1.5 ATR after TP2, recomputed on each closed bar, never
  loosened. A time stop exits at the close if TP1 has not come in 8 bars.
- **Futures**: levels shift by the basis when `--fut-price` is given. Size =
  your risk / (stop points x lot + round-trip charges). Margin is an estimate
  (12% index, 20% stock by default); the broker's calculator is exact.
- **Options** (buying only): a liquid strike near 0.55 delta; premium SL and
  targets come from repricing the option with Black-76 at the underlying's
  stop/targets after the expected days of decay (stop distance / ATR). The
  premium paid is the most a bought option can lose. A plain delta estimate is
  shown next to it; gamma and theta make the two differ.
- **Odds**: with no edge, the chance of TP1 before the stop is exactly risk /
  (risk + reward). Any claim of better odds has to come from a backtest
  (/india-fno-backtest), not from the levels.

## Orders

`--write` puts the ENTRY order in `trader/orders.json` (exchange NFO,
product NRML, a LIMIT a little through the price, quantity in whole lots,
with `sl`, `tp1`, `tp2`, `lot_size`, `expiry` and `max_loss_inr` attached)
and saves the plan to `trader/fno/plans.json`. After /india-risk passes it
and the user says yes in /india-broker, the ticket prints the protective
orders to place as soon as it fills:

- an **SL** order: trigger at the stop, limit 1% beyond (NSE has no SL-M on
  options); for an option the trigger is the premium level;
- a **LIMIT** at TP1 for half (Zerodha's GTT OCO can hold stop and target
  together);
- the **trail**: no exchange trailing order exists for Indian F&O. After each
  close, `plan.py trail` and modify the SL order. Never from a background task.

## Expiry and settlement

NIFTY weekly options expire on Tuesdays (monthly: the last Tuesday), moved to
the previous trading day for a holiday (Nov 2026: the 9th and the 23rd).
BANKNIFTY, FINNIFTY and MIDCPNIFTY are monthly only. Index F&O settles in
cash. **Stock F&O settles by physical delivery**: an open stock future or an
in-the-money stock option at expiry becomes a purchase or delivery of shares.
`trail` stops at expiry and warns four days before for stock F&O.

## Steps

1. Say the SEBI figure. Agree capital and risk per trade (default 1%).
2. Get candles and, for options, the chain (see /india-fno-scan).
3. `plan.py new` and walk the user through the levels, the size, the max
   loss in rupees, and for options the premium levels and the worst case.
4. Only if the user wants it: `--write`, then /india-risk (with
   `"allow_fno": true` set by the user), then /india-broker.
5. After each session: `plan.py trail` and tell the user the new stop.
6. `plan.py journal` for the year's F&O P&L and turnover (business income;
   see /india-tax).

## What it never does

- Never widens a stop, never averages down, never adds lots to a loser.
- Never plans option selling or naked shorts of options.
- Never sizes past the risk budget; 0 lots means "skip", not "1 lot anyway".
- Never places or modifies an order itself.

## Safety

- Education and tooling, not financial advice. Nothing here is a recommendation or a SEBI-registered adviser's or research analyst's view.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- F&O stays off (`"allow_fno": false`) until the user turns it on; no option selling, no expiry-day entries.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
