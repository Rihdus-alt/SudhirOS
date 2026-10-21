---
name: india-fno-backtest
description: >-
  F&O Backtester (India). Measures how the F&O setups and their stop-loss,
  targets and trailing stop actually did on history, with no look-ahead:
  signal on a bar's close, fill at the next open, gaps through the stop
  filled at the open, the stop assumed first when a bar touches both, every
  charge (STT, exchange, stamp duty, SEBI, GST, brokerage) deducted. Reports
  trades, win rate with its 95% confidence interval, the break-even win rate,
  expectancy and t-statistic, profit factor and drawdown, per underlying and
  per setup, and an out-of-sample split to catch over-fitting. Use when the
  user asks "how accurate is this", "backtest", "win rate", "does this
  strategy work", "is the edge real", or before trusting any setup with money.
argument-hint: "[candle files] [--split 0.3]"
---

# india-fno-backtest

"Accuracy" in trading means one thing: what a rule did, counted honestly,
over enough trades to mean something. This skill replays exactly the rules
/india-fno-scan and /india-fno-plan use and reports the numbers, including
the ones that disappoint.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" trader/fno/NIFTY-1d.json trader/fno/RELIANCE-1d.json --lots RELIANCE=500
python3 "${CLAUDE_SKILL_DIR}/backtest.py" trader/fno/*.json --split 0.3          # hold out the last 30%
python3 "${CLAUDE_SKILL_DIR}/backtest.py" trader/fno/NIFTY-1d.json --setups trend_pullback --trades
```

## How to read the result

- **Win rate vs break-even**: a 40% win rate is fine if wins are twice the
  losses (break-even 33%); a 60% win rate loses money if losses are twice
  the wins. Always compare the two.
- **Confidence interval**: with 15 trades, a 47% win rate has a 95% interval
  of roughly 25-70%. Read the interval before the point estimate.
- **Expectancy and t**: average R per trade after costs. t >= 2 over 20+
  trades is the minimum before calling anything an edge; even then, markets
  change.
- **Out-of-sample**: `--split 0.3` fits nothing, but it shows whether a rule
  that looked good in the first 70% held up in the last 30%. If it did not,
  the first result was luck.
- **Verdict**: the tool prints one of "too few trades", "no edge",
  "positive but not meaningful", "positive and meaningful". Quote it.

## Example (real data: NIFTY, RELIANCE, HDFC Bank, 2025-10-21 to 2026-10-19)

```
NIFTY      15 trades  win 33% (95% CI 15-58%, needs 45%)  expectancy -0.17R (t -0.76)
RELIANCE   19 trades  win 47% (95% CI 27-68%, needs 57%)  expectancy -0.15R (t -0.73)
HDFCBANK   15 trades  win 67% (95% CI 42-85%, needs 55%)  expectancy +0.18R (t 0.72)
trend_pullback  27 trades  expectancy +0.09R (t 0.58)    breakout  13 trades  expectancy -0.43R (t -1.64)
POOLED     49 trades  win 49% (95% CI 36-62%, needs 53%)  expectancy -0.06R  -> no edge after costs
--split 0.3: trend_pullback in-sample +0.39R (t 2.38) but out-of-sample -0.39R
```

That last line is the lesson: a rule that looks significant on the data it
was chosen on can fail on the next stretch. Tell the user plainly when the
numbers say there is no edge, and do not tune the parameters until they look
good: that is fitting noise.

## Limits (say them)

- Tested on the underlying's candles. Options add theta, IV changes and
  spreads; futures add a basis and a roll (with its costs) at every expiry.
- A year of daily bars gives tens of trades, not hundreds. Intraday candles
  give more trades but INDmoney keeps only 7 days of minute bars.
- Past results are not future results.

## Safety

Education and tooling, not financial advice. This skill only replays
history. Nothing places an order except `/india-broker`, after `/india-risk`
passes and the user types an explicit yes for that order.
