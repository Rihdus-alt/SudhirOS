---
name: india-insider
description: >-
  The Insider Tracker (India's answer to the Pelosi tracker). Watches what
  insiders and big investors in Indian companies disclose: promoter,
  director and KMP trades under SEBI's insider-trading rules (PIT Reg 7(2),
  published by NSE), promoter pledge creation and invocation, and NSE bulk
  and block deals the same evening. Flags promoter market buying, clusters
  of insiders buying the same stock, late disclosures, pledges, and named
  investors in bulk deals, separates real market trades from gifts, ESOPs
  and inter-se transfers, and alerts once per new item. Use when the user
  says "are promoters buying", "insider trading in X", "what did Kacholia
  buy", "bulk deals today", "block deals", "promoter pledge", "smart money",
  or "alert me when insiders buy my stocks".
argument-hint: "[symbol or investor] [--days N]"
---

# india-insider

India has no congressional trade disclosures, but it has something more
useful: the people who run a company must disclose their own trades in it
within days, and any client who trades more than 0.5% of a company in a day
shows up in the bulk deals that evening. This skill reads those public
feeds, notices what is new, and tells the user what it means and does not.

It is fast. It is not early. Every row is published after the trade. There is
no inside information here, only public disclosures read quickly. Say that.

Tool in this folder (standard library Python):

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" --help
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --days 30                      # every insider disclosure
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --symbol INFY --days 180
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --promoters --buys --min-value 10000000
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --clusters --window 30         # 2+ insiders buying one stock
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --pledges
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals --kind bulk --client "kacholia"
python3 "${CLAUDE_SKILL_DIR}/insider.py" new --watch-holdings --investors trader/investors.json
```

## What it reads and writes

- `www.nseindia.com/api/corporates-pit` (insider disclosures, with the
  person, category, shares, value, holding before and after, trade dates,
  mode: market purchase, market sale, off market, ESOP, pledge).
- `nsearchives.nseindia.com/content/equities/bulk.csv` and `block.csv`
  (today's deals) and `www.nseindia.com/api/historical/bulk-deals` (history).
- `trader/investors.json`: names to watch in deals (`investors.example.json`
  here). Every word must appear in the client name the exchange prints.
- `trader/portfolio.json`: to flag what the user holds.
- Writes `trader/state/insider.json` (what was already shown).

NSE's site sometimes refuses scripts. If it does, open the same pages in a
browser (NSE > Companies > Insider Trading; Market data > Bulk and block
deals), or ask the user to download the CSV, and read that.

## How to read it

- **Promoter market buys** with their own money are the signal people watch.
  Gifts, inter-se transfers between promoters, ESOP allotments and
  off-market moves are not market views: the tool labels them `off-market`
  or `esop`.
- **Clusters**: two or more insiders buying in the market within 30 days.
- **Pledges**: promoters pledging shares to borrow is a risk flag, and an
  invocation (the lender selling pledged shares) more so.
- **Late**: insiders have 2 trading days to tell the company and the company
  2 more to tell the exchange; a row posted more than 6 days after the trade
  is marked.
- **Bulk deals**: a client name can be a fund, a foreign investor, a broker's
  client or a person. The same client buying and selling the same stock the
  same day is trading, not a position, and is marked.
- **Amounts** are what was disclosed. A promoter's Rs 6 crore buy in a Rs
  40,000 crore company is small; say the size against the company.

## Steps

1. Ask what to watch: the user's holdings, a stock, an investor's name.
2. Run the matching command and show the rows that matter: promoter market
   buys and sells, clusters, pledges, and deals by named investors, newest
   first, with the link to the disclosure when there is one.
3. For a schedule, `new --watch-holdings --investors ...` once each evening
   after 19:00 IST (bulk deal files post after the close); exit code 1 means
   nothing new.
4. If the user wants to act on something, it goes the long way: shares only,
   sized by `/india-budget`, checked by `/india-risk`, placed by
   `/india-broker` after their yes.

## Example (test fixtures: made-up companies)

```
CLUSTER ALPHAPIPE    2 insiders bought in the market 2026-10-07 to 2026-10-13, Rs 8.76 cr
CHARLIEINF   Example Promoter Three (Promoters): pledge created 30,00,000 shares, valued at Rs 63.00 cr
BULK  2026-10-16 ALPHAPIPE    EXAMPLE INVESTOR A BUY 7,00,000 shares at Rs 1248.35 (Rs 87.38 cr)
BULK  2026-10-16 ALPHAPIPE    EXAMPLE TRADING LLP BUY 9,00,000 shares ... (bought and sold the same day: trading, not a position)
```

Then: "Two promoters bought Alpha Pipes in the market this month and a known
investor bought 7 lakh shares in a bulk deal. These are public disclosures
made after the trades; they say what was bought, not what happens next."

## What it never does

- Never says or implies anyone has inside information, or that copying
  insiders beats the market. No return claims.
- Never treats a gift, ESOP or inter-se transfer as a market view.
- Never places an order, and never copies a derivatives trade.
- Never logs in anywhere or scrapes past what a browser user would see.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user either places it themselves in their broker's app or clicks Allow once on Claude's own card because
  the order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Delivery (CNC) only by default: no F&O, intraday, margin (MTF) or short selling unless the config allows it and the user confirms.
- Broker logins stay on the broker's own login page; never ask for a password, PIN or TOTP in the chat.
- Insider disclosures and deals are PUBLIC information published after the trade. This skill reads them
  fast; it has no inside information, and it says so. Trading on unpublished price-sensitive information
  is illegal under SEBI's PIT Regulations.
