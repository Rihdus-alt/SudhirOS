# The Time Machine, as an app

The fifth tab (Time) of the Trading Desk page. It replays one trade through every way it can end and
counts: stop-loss, target, trailed out, out at 15:15, held to expiry. Two modes:

| mode | what it replays | on |
| --- | --- | --- |
| **Intraday** | the desk's own plan: SL -10%, T1 +15% (book half in whole lots; one lot exits all), T2 +20%, the trailing ladder (+10% to breakeven, then behind each step with breathing room), no entries after 15:00, out at 15:15 | 1-, 5- or 15-minute index candles (auto picks the one that suits the stop), the option repriced with Black-76 on every candle |
| **Swing** | a daily-bar trade (ATR/structure stop, TP1/TP2, chandelier trail, time stop) in futures, an option or shares | the chart's own history, a bootstrap of its real days, and the volatility the option chain prices |

It never predicts. Each result carries the model that made it, its interval, and where the models disagree.

## Every trade goes through it

One analysis function (`tmAnalyse` in `src/tmx-desk.js`) serves every entry point, so a plan gets the same engine, the
same learned settings and the same gate wherever it comes from:

* **The Trade tab** checks the plan as it is written (the picked contract, your entry, SL, T1/T2, lots) and shows
  PASS / WATCH / FAIL with the odds and the average result after charges. Logging a plan that fails needs a second tap
  ("Log anyway"); the journal records the check on the trade, and a failing one that was taken is marked as such. It
  warns, it does not stop you: the journal must still say what you did.
* **Scan** (Time tab) replays the contracts within delta 0.35-0.65 of both indices and ranks them by gate. When nothing
  passes it says so.
* **The coach** has a `time_machine` tool and is told to run every setup through it and to drop what fails.
* **The gate rule** is the Python tool's: a model is negative when the 95% band of its average result after charges is
  below zero; FAIL if any simulated model is, PASS if one is positive and none negative, otherwise WATCH.

## Candle size

The intraday models run on 1-, 5- or 15-minute candles, chosen in Options or left on Auto. The option's price is a
martingale along the paths at every size (tested), and the first-passage odds match theory for the desk's default
levels at every size. The size matters when the stop is close: a candle that touches both the stop and a target counts
as the stop, which over-states stops at 15 minutes (a 5%/8% plan reads 35% at 15 minutes against 38% at 1 and 5). Auto
uses 1-minute candles when the stop is within about three typical 5-minute moves, 5-minute otherwise. The 1-minute
bootstrap splits each real 5-minute bar into minutes that keep its own open, high, low and close. The data feed gives
5-minute bars at best, so the recent-sessions replay stays at 5 minutes.

## What it learns

`src/tmx-learn.js` is a pure state machine; the page keeps its state in the desk's database (`tmlearn/state`). It learns
only from data the app collects itself, and only these things:

1. **Pace.** The market's realised variance over the session against what the quote predicted, and the same for the
   overnight gap, shrunk toward 1 (six pseudo-observations), recency-weighted, bounded to 0.75-1.35, applied from 8
   observations, with a bootstrap interval. The implied model runs at that pace.
2. **Calibration.** A logistic correction of the odds (profit, reaches T1, stopped out) against what the plan did when
   played on the real tape the app saved, with a strong prior at "no change", applied from 30 settled predictions and
   judged on a held-out 30%. Your own closed trades (15+) get a separate view.
3. **Model weights.** A bounded Hedge update from each model's log loss, applied from 25 settled predictions, each model
   kept between 10% and 70%.
4. **Speed.** Milliseconds per path and candle on this device, to size path counts for the scan.

The saved sessions (`tmsess/`, full 5-minute sessions, the newest 120 of each index are kept) are data, and keep growing: the feed holds
only about three sessions, so the bootstrap's library, the replay and the volatility profile get a day richer each day
the app is opened. Observations (`tmobs/`) and predictions (`tmpred/`) wait there until the tape that settles them is saved.

It does not learn where the market goes next. It never changes its own rules, code or any order. Every figure is on the
Learning card with its sample size; learning can be frozen (pace, calibration and weights stop moving) or reset. One
honest finding from building it: with the desk's default 10%/15% levels the odds barely depend on volatility (first-passage
theory: a fair game's chance of +15% before -10% is 40% at any pace), so the learned pace moves the wide-stop odds and the
spread of results more than the headline probability. The calibration, fed by real tapes and your own trades, is where
the app can correct itself most, and it needs weeks of use to have anything to say.

## How the intraday mode stays honest

* **One variance clock.** IV is quoted on calendar time, but variance arrives in the session (a U shape) and,
  overnight and over weekends, as the opening gap. `Teff = (1 - S[bar]) * (1 - phi) / 365 + calendar days to expiry / 365`.
  The volatility is solved from the quote's mid on that clock and the index paths spend variance on it, so the
  option's price is a martingale along them. The tests check that, from the open over a weekend too.
* **Three models, side by side.** Implied (Gaussian bars at the option's pace), Bootstrap (real 5-minute bars,
  reshuffled in 30-minute blocks, drift removed, at the recent realised pace or the implied one), and Recent
  sessions (the actual tapes of the last full sessions, replayed against today's contract). The API gives only
  about three sessions of 5-minute bars, so the third is an illustration, and the page says so.
* **Fills.** Bought at the ask, sold at the bid; stops and targets are checked on the bid; a bar that touches both
  the stop and a target counts as the stop; a stop the bar opens through fills at the open; every order pays its own
  brokerage and STT (NFO or BFO rates).
* **Open positions.** "From my open trade" replays what happens next to a trade you hold, against selling it now.

## Layout

```
src/tmx-core.js        swing engine + shared maths (Black-76, indicators, setups, exit engine, chain, option leg, costs)
src/tmx-intraday.js    the intraday engine (minute-grid clock, three models, the desk plan on premium candles)
src/tmx-learn.js       the learning layer (pace, calibration, model weights, speed): pure state in, state out
src/tmx-charts.js      small SVG charts in the page's colour tokens
src/tmx-ui.js          the tab's code (runs inside the desk's script)
src/tmx-desk.js        the desk's links: the shared analysis, the store, the Trade-tab gate, scan, coach tool, Learning card
src/tmx.css, tmx.html  the tab's styles and view
build.py               patches the tab into the desk page (anchored: a changed base fails loudly)
fixtures/              real INDmoney data for the tests (see fixtures/README.md)
test/                  Node tests, the Python reference dump, and the Chromium harness
```

## Build and test

```bash
python3 apps/time-machine/build.py --base <the desk page as saved by Claude> --out-dir <folder>
node --test apps/time-machine/test/core.test.js apps/time-machine/test/intraday.test.js apps/time-machine/test/charts.test.js apps/time-machine/test/learn.test.js
node apps/time-machine/test/browser.test.js <folder>/desk-time.full.html [screenshot-folder]
python3 apps/time-machine/test/ref_dump.py     # regenerates test/ref.json from the Python tool
```

`core.test.js` holds the JavaScript swing engine to `plugins/trader-skill-india/india-fno-timemachine`: the
indicators, every setup that fires, the levels, the exit engine, the replay, the option leg and the charges to the
rupee, and 40,000-path simulations to within sampling error. `intraday.test.js` checks the clock, the martingale,
first-passage odds against the theory of a fair game (+15% before -10% is 40%), and the desk's rules.
`learn.test.js` builds worlds whose answer is known (a market running 30% hotter than the quote, an over-confident model, a
model that is simply better) and checks the learner finds them, stays put when there is nothing to find, is bounded, dedupes
and can be frozen; it fails if the shrinkage, the bounds or the freeze are removed. `browser.test.js` drives the built page in
Chromium against a stubbed `window.claude` serving the fixtures: the closed and live runs, candle sizes, the scan, the Trade
tab's gate and its second tap, the journal chip, settling stored predictions on a saved tape, and the coach's tool.

The base page itself (the user's Trading Desk) is not in this repository: it lives in Claude as an artifact.

Education and tooling, not financial advice.
