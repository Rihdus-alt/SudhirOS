---
name: india-fno-timemachine
description: >-
  The Time Machine (India F&O). Takes a chart and a trade (the setup that
  fired on the last closed bar, or the user's own direction, stop and
  targets) and replays every way it can end: stop-loss, gap through the stop,
  time stop, TP1 then break-even, TP1 then trailed, TP2 then trailed, still
  open, held to expiry. It replays three ways: every past occurrence of the
  setup in this chart's own history, tens of thousands of futures stitched
  from the chart's real days (block bootstrap), and the volatility the option
  chain prices. It gives each outcome's probability with a 95% interval, the
  result in R and in rupees per lot for futures, the option repriced along
  every path, and cash shares, after every charge. Use when the user says
  "time machine", "replay this trade", "what are the odds", "probability of
  hitting target", "every possible outcome", "how often does this hit SL",
  "Monte Carlo", "scenario analysis", or "is this trade worth it".
argument-hint: "--candles FILE [--chain FILE] [--direction long|short] [--sl --tp1 --tp2]"
---

# india-fno-timemachine

The scan finds a setup and the planner prices it. The Time Machine answers
the question in between: if I take this exact trade with these exact exits,
how does it end, and how often? It does not predict the next move. It plays
the trade forward through every path its models allow and counts.

**About 91% of individual F&O traders lost money in FY2024-25 (SEBI's
study; 87.7% in FY2025-26).** Say that once, early. Never present a
probability from this tool as a forecast or call a trade a sure thing:
quote the method, its interval and where the methods disagree.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" --candles trader/fno/NIFTY-1d.json --chain trader/fno/NIFTY-chain.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" --candles trader/fno/NIFTY-1d.json --direction short --lots 2 --grid
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" --candles trader/fno/RELIANCE-1d.json --lot 500 --direction long \
  --sl 1180 --tp1 1240 --tp2 1280 --paths 50000
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" --candles trader/fno/HDFCBANK-1d.json --instrument equity --qty 35 --direction long
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" --candles trader/fno/NIFTY-1d.json --chain trader/fno/NIFTY-chain.json --json
```

Get the candles and the chain exactly as india-fno-scan's SKILL.md says
(INDmoney `get_indian_stocks_ohlc` 1day over 1y; `get_indian_stocks_option_chain`
for an expiry that outlives the trade, usually the next month). The tools
leave out a session that has not closed.

## The three ways it replays the trade

| method | what it is | what it assumes |
| --- | --- | --- |
| replay | every bar in this chart's history where the same setup fired the same way, with that bar's own levels, entered at the next open | the past of this one chart repeats; few samples, and occurrences on consecutive bars overlap (the header shows how many are independent) |
| every bar | the same trade entered on every bar, no setup | the baseline: a setup has an edge here only if it beats this |
| bootstrap | N futures stitched from runs of this chart's real days (gap, high, low, close in ATR units), rescaled by the path's own ATR | the chart's own behaviour (fat tails, gaps, clustering) without its trend (`--keep-drift` keeps it) |
| implied | N risk-neutral paths at the chain's ATM IV, the overnight share of variance measured from the chart, each day's high and low from the Brownian bridge | what option prices assume: no drift, smooth volatility |

All of them use the pack's exact exit rules (the same engine as
india-fno-plan's `trail` and the backtest, held to it by the tests):

- the stop is checked first when a bar touches both levels;
- a gap through the stop fills at the open;
- TP1 books `tp1_exit_fraction`, but with `--lots 1` nothing can be booked, so the whole position trails;
- after TP1 the stop moves to entry, then a chandelier trail that only tightens;
- the time stop applies before TP1;
- each run has a horizon, and the option's expiry is a second horizon.

## Every way it can end

| outcome | meaning |
| --- | --- |
| no entry | the next open is already past the stop or TP1: the order is not taken |
| stop-loss before TP1 | the full loss; the line under it shows how often the open gapped through the stop (worse than planned) |
| time stop | no TP1 within `time_stop_bars`: out at that close |
| TP1, then stopped at entry | TP1 reached, the rest came back to entry |
| TP1, then trailed out | TP1 reached, the rest trailed out before TP2 |
| TP2, then trailed out | TP2 reached, the rest trailed out |
| still open at the horizon | still running when the replay ends |
| held to expiry | (option) the contract expired with the trade open: settled at intrinsic value |

For each outcome the tool reports:

- the probability, with its 95% interval: Wilson for the replay, Monte Carlo standard error for the simulations;
- the average R and the average time in the trade.

Then, overall:

- the expectancy after charges, with its interval, and the chance of a profit;
- the average win and loss, the median, and the 5% and 95% results;
- the adverse excursion (how far it went against you first);
- rupees per lot for the futures (or for the shares);
- for the option leg:
  - rupees per lot, after charges;
  - the chance of losing half the premium;
  - the chance it is still held at expiry.

`--grid` replays other stops (1 to 3 ATR) and TP2 targets (1.5R to 3R) on
the same bootstrap paths.

## The option leg

The strike comes from india-fno-plan's rule: near 0.55 delta, liquid. Along every path the option is repriced with Black-76:

- the chain's own IV for that strike (sticky strike);
- time to expiry on the real NSE calendar;
- the futures basis shrinking to zero at expiry;
- intrinsic value at expiry.

Prices are set so that the entry fills at the ask the chain shows. Exits are
valued at the session's close, a little more decay than an intraday exit. A
change in IV (vega) is not modelled: if IV is likely to fall, say so, and
replay with `--iv`.

## Steps

1. Say the SEBI loss figure once. Ask what they hold or plan: lots (1 lot
   cannot book a part at TP1), or shares for a cash position.
2. Fetch candles and, for options, a chain whose expiry outlives the trade
   (see the "still held at expiry" line).
3. Run it. If no setup fired and the user has no direction, say so: do not
   invent a trade.
4. Report in this order:
   - any NOT WORTH IT note;
   - the outcome table, method by method;
   - expectancy with its interval;
   - rupees per lot, the worst 5% of outcomes in rupees, and the worst path;
   - where the methods disagree, and what that means.
5. Never pick the best cell of `--grid` as "the right stop": it is this
   chart's past. Show it as a range.
6. To act: `/india-fno-plan` writes the order, then `/india-risk`, then
   `/india-broker`, one explicit yes per order. The Time Machine never writes
   or sends an order.

## Example (real data: NIFTY to the 2026-10-19 close, the 27-Oct chain, 2 lots)

```
TIME MACHINE  NIFTY SHORT [trend_pullback] on the 2026-10-19 close, entry Rs 22,888.95
  SL 23,301.25 (412.30 points, 1.50 ATR) | TP1 22,476.65 (1R) | TP2 22,064.36 (2R)
  option leg: 20261027 22950 PE at 198.65 (5 sessions to expiry), lot 65
How it ends                       replay 10 (3 ind.)   every bar 183   bootstrap (5000)   implied IV (5000)
  stop-loss before TP1              0.0%                22.4%           37.3%              39.1%
  TP2, then trailed out            40.0%                27.3%           20.8%              17.5%
  expectancy per trade            +0.838R             +0.131R          -0.021R +/-0.027   -0.052R +/-0.026
  option: still held at expiry                                          57.7%              64.6%
```

Read it as: ten past occurrences, only three of them independent, which is
too few to mean anything. The bootstrap from this chart's own days finds no
edge, and the option-implied paths find a small loss after costs. With five
sessions left, the October option would usually still be held at expiry:
use the next month's chain.

## Limits (say them)

- Models, not forecasts. The bootstrap assumes the chart's past days are a
  fair sample of its future days. The implied paths assume what option
  prices assume.
- The replay of one chart's past is small and overlapping. Wilson intervals
  on overlapping trades are optimistic, so the header shows the number of
  independent episodes.
- What daily bars cannot show:
  - Intraday order within a bar is unknown, and the stop is assumed first.
  - Liquidity, slippage beyond the modelled charges, and rollover spreads for futures held past the month are not counted.
- Volatility: the option leg holds IV constant. The implied method uses one
  volatility for every path.
- Education and tooling, not financial advice.
