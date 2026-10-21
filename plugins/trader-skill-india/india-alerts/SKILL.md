---
name: india-alerts
description: >-
  Price Alerts and the NSE clock. Rings when a price target, a stop, a percent
  move or a circuit limit is hit, each once so the user is not spammed; knows
  NSE hours in IST (pre-open 09:00, market 09:15 to 15:30, closing session)
  with the official 2026 trading holidays and Muhurat trading; fetches quotes
  from Yahoo (.NS) and NSE, or imports the INDmoney connector's live quotes
  with circuit limits, cross-checks two sources, flags stale quotes, and
  writes trader/prices.json for every other india-* skill. Use when the user
  says "alert me when Reliance hits", "set a stop", "is the market open",
  "is tomorrow a holiday", "get me prices", "refresh prices", "upper
  circuit", or before sizing, risk or tax.
argument-hint: "[symbol] [price]"
---

# india-alerts

Price Alerts watches the numbers so the user does not have to. An alert is
information, never an order: when a stop rings, nothing is sold. If the user
wants to act, the normal path runs (size, risk, their yes). It is also the
pack's price feed: every other skill reads the `trader/prices.json` it writes.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, in IST
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE TCS --write     # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --verify --write
python3 "${CLAUDE_SKILL_DIR}/alerts.py" import indmoney trader/state/indmoney.json --write
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol HDFCBANK --stop 680 --note "my stop"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol ICICIBANK --above 1450
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol AXISBANK --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --near-circuit 2
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang, 2 = a symbol had no quote
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset hdfcbank-stop-680
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when |
| `trader/prices.json` | prices in rupees, plus per-symbol detail: previous close, circuit limits, series, surveillance, quote time, source |
| `trader/calendar.json` | optional: holidays NSE adds or changes, Muhurat times, next year's list |

## Getting accurate prices (best first)

1. **The INDmoney connector**, if the user has it (this pack was built next to
   it). Resolve each stock with `lookup_ind_keys`, call
   `get_indian_stocks_details` for up to 10 keys, save the tool's JSON result
   to `trader/state/indmoney.json` exactly as returned, then
   `alerts.py import indmoney trader/state/indmoney.json --write`. It brings
   the live price, the timestamp and the day's upper and lower circuit, which
   `/india-risk` uses to block limit prices outside the band.
2. **NSE's own quote** (`--source nse`): price band, series (EQ, BE for
   trade-to-trade) and surveillance stage (ASM/GSM). NSE's site sometimes
   refuses scripts; then use 1 or 3.
3. **Yahoo** (`--source yahoo`, the default): `RELIANCE.NS`, delayed, no band.
4. `--verify` fetches a second source and prints any gap over 0.5%. When
   sources disagree, the broker's price decides; say so.
5. A quote older than the last close is marked STALE. Never size on a stale
   price without saying so.

## The NSE clock

All in IST (UTC+05:30, no daylight saving):

- Pre-open call auction 09:00 to 09:08 (orders collected), matching until 09:15.
- Normal market 09:15 to 15:30. Closing session 15:40 to 16:00 (closing price only).
- Outside those hours an order is an after-market order (AMO) for the next session.
- 2026 trading holidays (NSE circulars, December 2025): Jan 26 Republic Day,
  Mar 3 Holi, Mar 26 Shri Ram Navami, Mar 31 Shri Mahavir Jayanti, Apr 3 Good
  Friday, Apr 14 Ambedkar Jayanti, May 1 Maharashtra Day, May 28 Bakri Id,
  Jun 26 Muharram, Sep 14 Ganesh Chaturthi, Oct 2 Gandhi Jayanti, Oct 20
  Dussehra, Nov 10 Diwali Balipratipada, Nov 24 Guru Nanak Jayanti, Dec 25
  Christmas.
- Muhurat trading: Sunday 2026-11-08 (Diwali Laxmi Pujan). NSE notifies the
  times shortly before; until they are in `trader/calendar.json` the clock
  says "times unknown" rather than guessing.
- 2027 is not in the pack: NSE publishes it in December. Add it to
  `trader/calendar.json` from the circular; until then the clock warns.

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or by itself with `"rearm": true` once the price falls back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (`direction` up/down optional) | once a day |
| `near_circuit` | within `pct` percent of the upper or lower circuit (needs NSE or INDmoney quotes) | once a day |
| `opening_bell` | NSE's normal market is open | once a trading day |

## Steps

1. Ask what to watch in the user's words ("tell me if HDFC Bank goes under
   680") and add the rule. Confirm it back: symbol, type, level.
2. Run `check` now, and on a schedule if the user wants it (a scheduled task
   every 15 minutes between 09:15 and 15:30 IST on trading days is plenty).
3. Tell the user what rang, one line each. If they want to act, send them to
   sizing and `/india-risk`. Never act for them.

## Example: the reel's portfolio

After the copy portfolio is bought, the rules in `alerts.example.json`:
the opening bell, an HDFC Bank stop at Rs 680, ICICI Bank above Rs 1,450, a
3% move on Axis Bank, TCS within 2% of a circuit, M&M under Rs 2,800. With the
2026-10-19 closes (INDmoney): HDFC Bank Rs 712 (band Rs 656 to Rs 801.70),
TCS Rs 2,460.90 (band Rs 2,218.50 to Rs 2,711.50), and Axis Bank Rs 1,236,
down from Rs 1,290. `check` that evening rings one alert: "AXISBANK moved
-4.19% to Rs 1,236 (from Rs 1,290)." 2026-10-20 is Dussehra: `clock` says
"NSE holiday: Dussehra, next normal session 2026-10-21 09:15 IST", and the
opening bell rings once that morning.

## What it never does

It never places, cancels or changes an order, never calls a delayed quote
real time, never predicts where a price is going, and never guesses a
holiday or a Muhurat time.

## Safety

- Education and tooling, not financial advice. An alert is a fact about a price, not a signal to trade.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
