---
name: india-fno-scan
description: >-
  F&O Trade Screener (India). Screens NSE futures-and-options underlyings
  (NIFTY, BANKNIFTY, F&O stocks) for trade setups on closed candles (trend
  pullback, breakout, Supertrend flip, volatility squeeze) and gives every
  signal a complete plan: entry, stop-loss, TP1, TP2, a trailing stop, the
  size in whole lots for the user's rupee risk, the futures margin, the
  option strike with premium SL and targets, OI walls and IV from the option
  chain, and the setup's own backtested track record on that underlying with
  a confidence interval. Use when the user says "scan F&O", "find me trades",
  "give me SL and target", "nifty trade setup", "which F&O stocks to trade",
  "option buying setup", "intraday levels", or "trailing stop".
argument-hint: "[candle files] [--chain SYMBOL=file]"
---

# india-fno-scan

The screener finds where a written rule fires, then does the parts people
get wrong under pressure: where the stop really goes, how many lots that
allows, what the option premium will be at the stop and the targets, whether
the target sits behind a wall of open interest, and how this exact rule has
done on this underlying before.

It does not predict. **About 91% of individual F&O traders lost money in
FY2024-25 (SEBI's study; 87.7% in FY2025-26).** Say that once, early, every
time this skill is used, and never call a signal a sure thing or quote an
"accuracy" other than the measured track record with its confidence interval.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" --candles trader/fno/NIFTY-1d.json trader/fno/RELIANCE-1d.json \
  --lots RELIANCE=500 --chain NIFTY=trader/fno/nifty-chain.json --capital 500000 --risk-pct 1
python3 "${CLAUDE_SKILL_DIR}/scan.py" --candles trader/fno/*.json --proven-only
python3 "${CLAUDE_SKILL_DIR}/scan.py" --candles trader/fno/*.json --setups trend_pullback --json
```

## Getting the data (the INDmoney connector, if the user has it)

1. `lookup_ind_keys` for each underlying (NIFTY 50 is an index key; stocks
   with filter IN_STOCKS).
2. `get_indian_stocks_ohlc` with interval `1day`, lookback `1y` (or
   `15minute`/`60minute` over `7d`/`14d` for intraday). Save each result
   exactly as returned to `trader/fno/<SYMBOL>-1d.json`. The file name before
   the first `-` is the symbol.
3. For index or stock options: `get_indian_stocks_option_chain` on the
   underlying (nearest expiry, `strikes_around_atm` 10), saved to
   `trader/fno/<symbol>-chain.json`. It carries the lot size, bid/ask, OI and
   greeks; its IV field is 0, so the tool solves IV itself (Black-76 on the
   parity forward; it reproduces INDmoney's own contract IV to within 0.1
   point).
4. Stock lot sizes: from the option chain (`lot_size`) or NSE's contract
   list. Index lots in the tool (NIFTY 65, BANKNIFTY 30, FINNIFTY 60,
   MIDCPNIFTY 120, NIFTYNXT50 25) are from NSE's October 2025 revision; the
   chain's lot size wins.

No connector: any CSV with date, open, high, low, close, volume works.

## What every signal carries

| part | how it is set |
| --- | --- |
| entry | the last close (the plan is made after the close; the order goes in next session) |
| stop-loss | beyond the last swing low/high plus 0.2 ATR when that is 0.75 to 3 ATR away, else 1.5 ATR |
| TP1 | 1R: book half, move the stop to entry |
| TP2 | 2R, pulled in before an OI wall or support that sits closer; under 1.5R the trade is NOT WORTH IT |
| trail | after TP1, 2.5 ATR from the best price since entry; after TP2, 1.5 ATR; never loosens |
| time stop | exit at the close if TP1 has not come within 8 bars |
| size | lots = your rupee risk / (stop distance x lot size + round-trip charges); 0 means skip |
| option | strike near 0.55 delta with a spread under 2% and OI over 50,000; premium SL/TP by Black-76 at the underlying's levels after the expected days of decay |
| odds | with no edge, P(TP1 before SL) = risk / (risk + reward) exactly: 50% at 1R, 33% at 2R. That is the break-even win rate |
| track record | the same rule backtested on the same candles: trades, win rate and its 95% interval, expectancy in R |

Set the defaults once under `"fno"` in `trader/config.json` (capital,
risk_per_trade_pct, sl_atr_mult, trail_atr_mult, ...). The tool never
changes them.

## Steps

1. Say the SEBI loss figure once. Ask for capital and risk per trade (1% is
   a common ceiling; never more than 2%).
2. Fetch candles (and chains for the underlyings they will trade options on).
3. Run `scan.py`. Exit 1 means nothing fired: say so; do not lower the bar to
   find something.
4. For each signal, show: direction, setup, the levels, size and max loss,
   the option alternative, and the track record WITH its interval. Lead with
   any NOT WORTH IT flag and with a track record that is negative or thin.
5. If the user wants to act: `/india-fno-plan` writes the entry order and
   saves the plan; then `/india-risk`, then `/india-broker`, one yes per order.

## Example (real data, NIFTY 50, close of 2026-10-19)

```
NIFTY SHORT (NOT WORTH IT)  [trend_pullback, score 2.0]  close Rs 22,888.95
  entry 22,888.95 | SL 23,301.25 (1.5 x ATR, 412.3 points) | TP1 22,476.65 | TP2 22,064.36 (2.00R)
  note: support 22500.00 sits before TP1: the trade has no room
  futures: 1 lot x 65, max loss at SL Rs 27,688 (incl. Rs 889 charges), est. margin Rs 1,78,534  (capital Rs 30 lakh)
  option: 20261027 22950 PE (delta -0.55, IV 12.6%, 8 days), buy 198.65, SL 36.94, TP1 484.72, TP2 876.72
  chain: PCR 0.928, max pain 23000, OI resistance 23000, support 22500, ATM IV 12.62%, 1-SD move +/-428
  track record here: 3 trades ... NOT a proven edge here
```

The honest reading: the rule fired, but the biggest put OI (22,500) sits
before TP1, so the reward is not there; and three past trades prove nothing.

## What it never does

- Never says a signal will work, never quotes an accuracy it did not measure.
- Never sizes beyond the user's risk budget or rounds lots up.
- Never proposes selling options (unlimited or large risk); `allow_option_selling` is off.
- Never places, approves or schedules an order.

## Safety

- Education and tooling, not financial advice. Nothing here is a recommendation or a SEBI-registered adviser's or research analyst's view.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- F&O stays off (`"allow_fno": false`) until the user turns it on; no option selling, no expiry-day entries.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
