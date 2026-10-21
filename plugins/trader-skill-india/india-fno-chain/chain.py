#!/usr/bin/env python3
"""Option-chain analytics for india-fno-chain. Standard library only, no network.

    python3 chain.py nifty-chain.json                       # INDmoney or NSE option chain JSON
    python3 chain.py nifty-chain.json --now 2026-10-19T15:30 --strikes 6
    python3 chain.py nifty-chain.json --iv 23000CE=122.1     # implied volatility of one price
    python3 chain.py nifty-chain.json --json

What it works out, from the chain alone:
  forward         the price the options imply (put-call parity at the ATM strike): the right
                  underlying for European index options, dividends and carry included
  IV per strike   solved from each option's mid price with Black-76 when the source gives none
                  (INDmoney's chain reports IV as 0; its single-contract IV is a VWAP)
  greeks          delta per strike from that IV
  ATM IV          and the 1-standard-deviation move to expiry (forward x IV x sqrt(time)); the
                  ATM straddle price is the market's own estimate of the move (about 0.8 x 1 SD)
  PCR             put OI / call OI and put volume / call volume, over the strikes in the file
  max pain        the settlement price that would pay option holders the least
  OI walls        the strike with the most call OI above the price (resistance) and the most put
                  OI below it (support), and where OI was added today
  liquidity       bid-ask spread as % of the mid; wide spreads cost you on the way in and out

OI walls and PCR are crowd positioning, not forecasts: walls move, and a level with big OI often
breaks on expiry day. Use them for room-to-target checks, not as signals on their own.
"""

import argparse
import importlib.util
import json
import os
import re
import sys


def _core():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "india-fno-plan", "fnocore.py")
    if not os.path.exists(path):
        sys.exit("chain.py: india-fno-plan/fnocore.py not found: install every india-* folder")
    spec = importlib.util.spec_from_file_location("fnocore", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f = _core()


def render(an, n):
    L = ["%s options expiring %s (%.2f days): spot %s, forward %s, ATM %d"
         % (an["underlying"], an["expiry"], an["days_to_expiry"], f.inr(an["spot"]), f.inr(an["forward"]),
            an["atm_strike"])]
    L.append("ATM IV %s%%: 1-SD move to expiry +/-%s points; ATM straddle %s"
             % (an["atm_iv_pct"], an["expected_move_1sd"], f.inr(an["atm_straddle"])))
    L.append("PCR (OI, %d strikes shown) %s, PCR (volume) %s%s; max pain %d"
             % (an["strikes_shown"], an["pcr_oi"], an["pcr_volume"],
                ("; the provider's whole-chain PCR %s" % an["provider_pcr"]) if an.get("provider_pcr") else "",
                an["max_pain"]))
    L.append("OI resistance %s (biggest call OI above), support %s (biggest put OI below); call wall %s, put wall %s"
             % (an["resistance"], an["support"], an["call_wall"], an["put_wall"]))
    L.append("Most call OI added: %s; most put OI added: %s"
             % (", ".join("%d (+%s)" % (k, f"{v:,}".replace(",", ",")) for k, v in an["top_call_oi_added"]),
                ", ".join("%d (+%s)" % (k, f"{v:,}") for k, v in an["top_put_oi_added"])))
    L.append("")
    L.append("%9s | %8s %7s %6s %6s %11s | %8s %7s %6s %6s %11s" % ("strike", "CE mid", "IV%", "delta", "sprd%",
                                                                  "OI", "PE mid", "IV%", "delta", "sprd%", "OI"))
    rows = sorted(an["rows"], key=lambda r: abs(r["strike"] - an["atm_strike"]))[:2 * n + 1]
    for r in sorted(rows, key=lambda r: r["strike"]):
        def side(k):
            x = r.get(k) or {}
            return "%8s %7s %6s %6s %11s" % (x.get("mid") or "", x.get("iv_pct") or "", x.get("delta") or "",
                                             x.get("spread_pct") if x.get("spread_pct") is not None else "",
                                             "{:,.0f}".format(x["oi"]) if x.get("oi") else "")
        L.append("%9d | %s | %s%s" % (r["strike"], side("CE"), side("PE"), "  <- ATM" if r["strike"] == an["atm_strike"] else ""))
    L.append("")
    L.append("Positioning, not prediction. Education and tooling, not financial advice.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Option-chain analytics (india-fno-chain).")
    ap.add_argument("chain", help="INDmoney get_indian_stocks_option_chain output or NSE option-chain JSON")
    ap.add_argument("--underlying", help="name, if the file does not say")
    ap.add_argument("--expiry", help="NSE files carry several expiries: DD-Mon-YYYY")
    ap.add_argument("--now", help="ISO time (no offset = IST); default: the chain's own price time")
    ap.add_argument("--rate", type=float, default=0.065, help="risk-free rate for discounting (default 6.5%%)")
    ap.add_argument("--strikes", type=int, default=5, help="strikes each side of ATM in the table")
    ap.add_argument("--iv", nargs="*", default=[], help="STRIKE{CE|PE}=PRICE to solve an implied volatility")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        ch = f.parse_chain(a.chain, a.underlying, a.expiry)
        now = f.parse_now(a.now, ch)
        an = f.chain_analytics(ch, now, a.rate)
        solved = []
        T = f.years_to_expiry(ch["expiry"], now)
        for item in a.iv:
            m = re.match(r"^(\d+(?:\.\d+)?)(CE|PE)=(\d+(?:\.\d+)?)$", item.upper())
            if not m:
                raise f.FnoError("--iv wants STRIKE{CE|PE}=PRICE, got %r" % item)
            k, kind, px = float(m.group(1)), m.group(2), float(m.group(3))
            iv = f.implied_vol(px, an["forward"], k, T, a.rate, kind)
            g = f.greeks(an["forward"], k, T, iv, a.rate, kind) if iv else {}
            solved.append({"strike": k, "kind": kind, "price": px, "iv_pct": round(100 * iv, 2) if iv else None,
                           "greeks": {x: round(v, 4) for x, v in g.items()}})
    except (f.FnoError, OSError, ValueError) as e:
        print("chain.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(dict(an, solved=solved), indent=2, ensure_ascii=False))
        return 0
    print(render(an, a.strikes))
    for s in solved:
        print("%d %s at %s: IV %s%%, delta %s, gamma %s, vega %s, theta %s a day"
              % (s["strike"], s["kind"], f.inr(s["price"]), s["iv_pct"], s["greeks"].get("delta"),
                 s["greeks"].get("gamma"), s["greeks"].get("vega"), s["greeks"].get("theta")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
