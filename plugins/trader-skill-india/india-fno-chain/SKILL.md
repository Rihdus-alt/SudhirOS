---
name: india-fno-chain
description: >-
  Option Chain Analyst (India). Reads an NSE option chain (from the INDmoney
  connector or NSE's own JSON) and works out what the prices and open
  interest say: the forward implied by put-call parity, implied volatility
  and delta for every strike (solved with Black-76 when the source shows
  none), ATM IV and the 1-standard-deviation move to expiry, the ATM
  straddle, PCR by OI and volume, max pain, the call and put OI walls, where
  OI was added today, and bid-ask spreads for liquidity. Use when the user
  asks "nifty option chain", "PCR", "max pain", "support and resistance from
  OI", "IV of this option", "expected move", "which strike is liquid", or
  before choosing an option strike.
argument-hint: "[chain file] [--iv STRIKE{CE|PE}=PRICE]"
---

# india-fno-chain

An option chain is the market's own pricing of how far the underlying may
move and where the crowd is positioned. This skill reads it carefully, with
the maths done properly, and keeps the reading in proportion: positioning is
not prediction.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/chain.py" trader/fno/nifty-chain.json
python3 "${CLAUDE_SKILL_DIR}/chain.py" trader/fno/nifty-chain.json --now 2026-10-19T15:30 --strikes 6
python3 "${CLAUDE_SKILL_DIR}/chain.py" trader/fno/nifty-chain.json --iv 23000CE=122.1 23000PE=224.65
python3 "${CLAUDE_SKILL_DIR}/chain.py" nse-option-chain.json --underlying NIFTY --expiry 27-Oct-2026 --json
```

Getting the chain: the INDmoney connector's `get_indian_stocks_option_chain`
on the underlying's key (nearest expiry, or `use_expiry_date` with a date
from its `expiry_list`), saved exactly as returned. NSE's option-chain page
JSON also works (`records.data`, several expiries: pick one with `--expiry`).

## How it computes

- **Time** to 15:30 IST on the expiry day, in calendar years. `--now` sets
  the clock (the market's close when you read a closing chain).
- **Forward**: F = K + (C - P) e^{rT} at the ATM strike (put-call parity).
  NSE index options are European and cash-settled, so Black-76 on F is the
  right model; no dividend guess is needed.
- **IV**: solved by bisection from each mid price (bid-ask midpoint, else the
  last price). On the NIFTY chain of 2026-10-19 it gives 12.44% for the
  23000 CE at Rs 122.10; INDmoney's VWAP IV for that contract was 12.49%,
  and the greeks match INDmoney's to two decimals.
- **Expected move**: F x ATM IV x sqrt(T) is one standard deviation (about a
  68% range under the model); the ATM straddle price is roughly 0.8 of it.
- **PCR**: over the strikes in the file (say how many); the provider's
  whole-chain PCR is shown when it gives one. A high PCR is read as bullish
  by some and contrarian-bearish by others: report the number, not a verdict.
- **Max pain**: the expiry price that minimises what option holders collect.
- **OI walls**: biggest call OI above the price (resistance), biggest put OI
  below (support); INDmoney's `oi_change` is a percent and is converted to
  contracts added.

## Steps

1. Get the chain; note its time (the underlying's `price_updated_at_ist`).
2. Run `chain.py` and report: forward, ATM IV, expected move, PCR, max pain,
   the walls, the biggest OI additions, and which strikes are liquid
   (spread under about 2%).
3. Use the walls for room-to-target checks (/india-fno-plan does this
   automatically), not as entry signals. Walls move, and big-OI strikes often
   break near expiry.

## Example (real data, NIFTY 27-Oct-2026 expiry, 2026-10-19 close)

```
spot 22,888.95, forward 22,897.50, ATM 22900, 8 days
ATM IV 12.62%: 1-SD move +/-427.9 points; ATM straddle 340.95
PCR (OI, 21 strikes) 0.928, PCR (volume) 1.119; max pain 23000
OI resistance 23000, support 22500; most call OI added at 23000 (+40.6 lakh)
```

## What it never does

- Never turns PCR, max pain or an OI wall into a buy or sell call.
- Never recommends selling options.
- Never invents an IV: no mid price, no IV.

## Safety

Education and tooling, not financial advice. This skill only reads prices.
Nothing places an order except `/india-broker`, after `/india-risk` passes
and the user types an explicit yes for that order.
