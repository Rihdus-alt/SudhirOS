# The Time Machine, as an app

The fifth tab (Time) of the Trading Desk page. It replays one trade through every way it can end and
counts: stop-loss, target, trailed out, out at 15:15, held to expiry. Two modes:

| mode | what it replays | on |
| --- | --- | --- |
| **Intraday** | the desk's own plan: SL -10%, T1 +15% (book half in whole lots; one lot exits all), T2 +20%, the trailing ladder (+10% to breakeven, then behind each step with breathing room), no entries after 15:00, out at 15:15 | 5-minute index bars, the option repriced with Black-76 on every bar |
| **Swing** | a daily-bar trade (ATR/structure stop, TP1/TP2, chandelier trail, time stop) in futures, an option or shares | the chart's own history, a bootstrap of its real days, and the volatility the option chain prices |

It never predicts. Each result carries the model that made it, its interval, and where the models disagree.

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
src/tmx-intraday.js    the intraday engine (clock, models, the desk plan on premium bars)
src/tmx-charts.js      small SVG charts in the page's colour tokens
src/tmx-ui.js          the tab's code (runs inside the desk's script)
src/tmx.css, tmx.html  the tab's styles and view
build.py               patches the tab into the desk page (anchored: a changed base fails loudly)
fixtures/              real INDmoney data for the tests (see fixtures/README.md)
test/                  Node tests, the Python reference dump, and the Chromium harness
```

## Build and test

```bash
python3 apps/time-machine/build.py --base <the desk page as saved by Claude> --out-dir <folder>
node --test apps/time-machine/test/core.test.js apps/time-machine/test/intraday.test.js apps/time-machine/test/charts.test.js
node apps/time-machine/test/browser.test.js <folder>/desk-time.full.html [screenshot-folder]
python3 apps/time-machine/test/ref_dump.py     # regenerates test/ref.json from the Python tool
```

`core.test.js` holds the JavaScript swing engine to `plugins/trader-skill-india/india-fno-timemachine`: the
indicators, every setup that fires, the levels, the exit engine, the replay, the option leg and the charges to the
rupee, and 40,000-path simulations to within sampling error. `intraday.test.js` checks the clock, the martingale,
first-passage odds against the theory of a fair game (+15% before -10% is 40%), and the desk's rules.
`browser.test.js` drives the built page in Chromium against a stubbed `window.claude` serving the fixtures.

The base page itself (the user's Trading Desk) is not in this repository: it lives in Claude as an artifact.

Education and tooling, not financial advice.
