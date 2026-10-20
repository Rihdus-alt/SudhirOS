---
name: india-broker
description: >-
  Order Executor (India). Turns an order that /india-risk passed into a plain
  ticket and, only after the user's explicit yes for that one order, either
  fills it on paper (the default), shows exactly what to enter in their own
  broker's app (manual mode, works with any Indian broker: Zerodha, Groww,
  Upstox, Angel One, Dhan, INDmoney), or hands the exact arguments to a
  broker connector's place_order (Zerodha Kite MCP) behind Claude's own
  Allow once card. Records the yes word for word, records fills from the
  contract note as tax lots (cost without STT), and never places anything by
  itself. Use when the user says "place the orders", "buy it", "execute",
  "send it to Zerodha", "connect Kite", "paper trade it", "record my fill",
  or after /india-risk passes.
argument-hint: "[order number]"
---

# india-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **india-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat ("yes"
   or "haan"; not "ok", "sure", "yes to all").
3. **The user acts again**: they submit it themselves in their broker's app
   (`"broker": "manual"`), or click **Allow once** on Claude's own permission
   card because the connector's order tool is set to "Needs approval".

With `"paper": true` (the default) gate 3 is a simulated fill: nothing leaves
the computer.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                       # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-fill --index 1       # paper: fill at the limit or last price
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id 260421000123456
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol HDFCBANK --side BUY --qty 35 --price 712.40 --charges 29.67 --stt 25
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, simulates paper fills, and keeps the log in
`trader/state/broker.json`.

## Mode A: paper (the default)

`"paper": true`. Indian brokers have no paper account, so the pack keeps its
own: `paper-fill` fills an approved order at its limit or the last price in
`trader/prices.json` (a buy limit below the last price does not fill), adds
the charges from `/india-costs`, and records the lot. Run the whole flow on
paper first.

## Mode B: manual (any broker)

`"paper": false, "broker": "manual"`. After the yes, Claude shows the fields
to enter: exchange, symbol, Buy or Sell, quantity, product CNC (Delivery),
order type, price, validity Day. The user enters and submits it in their own
app. Then record what happened: `result --index N --status placed --broker-id
<order id>`, and when it fills, `fill` with the numbers from the contract note
(price, total charges, STT) so the tax lot is exact.

## Mode C: Zerodha Kite MCP connector

Zerodha publishes an official MCP server for Kite (kite.trade, source at
github.com/zerodha/kite-mcp-server). The hosted endpoint is
`https://mcp.kite.trade/mcp`.

1. Claude (web or desktop): Customize > Connectors > Add custom connector,
   URL `https://mcp.kite.trade/mcp`. Claude Desktop with a config file
   instead: the block in `claude_desktop_config.example.json` here
   (`npx mcp-remote https://mcp.kite.trade/mcp`).
2. Log in when the connector asks: it opens Kite's own login page. The user
   types their password and TOTP there, never in the chat.
3. **Set the tool permissions**: `place_order`, `modify_order`,
   `cancel_order` and any GTT tool to **Needs approval**. Never Always allow.
   Read-only tools (holdings, positions, margins, quotes) can be Always allow.
4. Check which tools the connector actually lists. If it has no order tool,
   keep `"broker": "manual"`.
5. Set `"broker": "kite"` and, when they decide to go live, `"paper": false`
   in `trader/config.json`. That is their decision, never yours.

The ticket's JSON uses Kite's field names (variety, exchange, tradingsymbol,
transaction_type, quantity, product, order_type, price, validity). Another
broker's connector: map the same fields to its tool and say so on the ticket.

In Claude Code, keep the order tool on ask in `.claude/settings.json`:
`"permissions": {"ask": ["mcp__kite__place_order"]}`, never in `allow`.

The INDmoney connector is read-only (portfolio, prices, funds): use it for
prices and holdings, not orders.

## Steps, for each order

1. `ticket.py show`. Only READY tickets go to the user. NOT READY or BLOCKED
   goes back to `/india-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question. Never offer "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   "Not recorded" means it was not an explicit yes for this order (a
   condition, a batch, a different stock or quantity). Ask again. Never turn
   a reply into a yes yourself.
4. Then, by mode: `paper-fill`; or show the manual fields; or call
   `place_order` with exactly the JSON the ticket printed, and Claude's card
   asks Allow once / Deny.
5. Record the outcome: `result --status placed|denied|failed`. A failed order
   is not retried without a new check and a new yes.
6. Record the fill from the contract note with `fill`. Lots keep the exact
   cost: price x quantity plus charges except STT, the cost the Income-tax
   Act allows. A sell is matched first-in, first-out and logged for
   `/india-tax`. Then the next ticket.

## F&O orders

Orders from /india-fno-plan have exchange NFO, product NRML (carry forward)
and whole lots; the ticket shows the lots and the plan's stop and targets.
After the entry fills (on paper or for real), the ticket prints the
protective SL order to place at once. F&O fills go to
`trader/fno/journal.json` (positions, fills, realized P&L, charges), not to
the demat lots: F&O is business income, not capital gains. Record real fills
from the contract note with `ticket.py fill --symbol NIFTY26OCT22950PE
--side SELL --qty 130 --price 320 --charges 103.09`; an F&O symbol is
recognised and routed to the journal. Exiting a bought option: the SELL
order carries `"closing": true`.

## Example: the reel

```
ORDER 1 of 5, PAPER (simulated, no broker), your broker's app (you place it yourself)
  BUY 35 shares of HDFCBANK on NSE, delivery (CNC), limit Rs 715.55, valid for today
  about Rs 25,044.25 + Rs 29.67 charges (worst case)
  READY: india-risk PASS, waiting for your yes
```

The user replies "yes to all": not recorded, a yes covers one order. They
reply "haan, 35 HDFCBANK": recorded. `paper-fill` fills 35 at Rs 712 (the
last price is under the limit): cost Rs 24,924.64 for tax (Rs 24,920 + Rs
29.64 charges - Rs 25 STT), cash Rs 75,050.36. Then order 2.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats a password, PIN, TOTP, API key or access token.
- Never switches `"paper"` to false or the broker to live. The user does that.
- Never uses F&O, MTF, GTT or bracket orders on its own.

## Safety

- Education and tooling, not financial advice. No order here is a recommendation, and nothing promises returns.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- SEBI's framework for retail algorithmic trading (2025) covers automated strategies through broker APIs;
  this pack places a handful of orders, each approved by a person. Ask the broker before automating anything.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
