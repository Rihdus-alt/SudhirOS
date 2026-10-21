---
name: india-news
description: >-
  The News Monitor (India). Watches the stocks the user holds for what
  matters first: the company's own exchange filings on NSE and BSE (SEBI LODR
  Regulation 30 disclosures: results, dividends, bonuses, auditor or CFO
  resignations, credit rating downgrades, defaults, pledges, orders,
  acquisitions, regulator actions), then Indian business headlines (Google
  News India, plus ET, Moneycontrol, Mint or any RSS feed). Shows only what
  is new, newest first, merges the NSE and BSE copies of one filing, tags
  each item and marks red flags. Use when the user says "any news on my
  stocks", "what's happening with Reliance", "check the announcements",
  "did my company file anything", "morning briefing", or "watch my
  portfolio".
argument-hint: "[symbols]"
---

# india-news

A holding can change in a minute: a results miss, an auditor walking out, a
rating downgrade, a loan default. In India the company must tell the
exchanges first (Regulation 30), so the filing is the source; headlines are
leads. This skill reads both for each stock and shows only what is new.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py" --help
python3 "${CLAUDE_SKILL_DIR}/news.py"                                  # your holdings
python3 "${CLAUDE_SKILL_DIR}/news.py" --symbols RELIANCE TCS --hours 48
python3 "${CLAUDE_SKILL_DIR}/news.py" --symbols RELIANCE --bse 500325   # BSE filings too
python3 "${CLAUDE_SKILL_DIR}/news.py" --only-filings --important       # red flags only
python3 "${CLAUDE_SKILL_DIR}/news.py" --feeds https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms
```

## What it reads and writes

- The stocks: `--symbols`, else `trader/portfolio.json`, else
  `trader/targets.json` (so it works right after `/india-copycat`).
- Filings: NSE corporate announcements per symbol, and BSE announcements per
  scrip code (`--bse` codes in the same order as `--symbols`), each with its
  PDF link. The same filing on both exchanges is shown once ("BSE + NSE").
- Headlines: Google News India, one search per company name (names from
  `trader/prices.json` or the filings), and any `--feeds`. A headline is kept
  only if it names one of the user's companies.
- Writes `trader/state/news.json`: what was shown (30 days), so each item
  appears once.

## Tags and red flags

Each item gets tags: results, payout (dividend, bonus, split, buyback,
record date), people, capital (allotment, QIP, preferential issue), deal
(acquisition, merger, demerger, order win), legal, rating, meeting. `!` marks
a red flag: a **default** or insolvency (NCLT, CIRP), an **auditor**
resignation or qualified opinion, **fraud** or a raid, a **regulator** order
or penalty or surveillance, a rating **downgrade**, a **pledge**, a **cyber**
incident. An auditor or CFO resigning mid-term is one of the strongest
warning signs in Indian markets: always read that filing in full.

## The rules

- **Filings beat headlines.** If they disagree, the filing is what the
  company told the exchange. Open the PDF and read it before telling the
  user something happened. Quote the source and link it.
- **Never summarise a story you could not read** (paywalls): say it is paywalled.
- **Say what it touches**: which holding, and its weight from
  `trader/portfolio.json` or `trader/targets.json`.
- **No predictions.** Report what was published, never what the stock will do.

## Steps

1. Run the check (exit code 1 means nothing new).
2. Read every `!` filing and every results filing in full first, then the
   rest for the biggest positions.
3. Write a short brief: per stock, what happened, the source and link, and one
   line on why it matters to this portfolio. When a stock has no news, say so.
4. For a red flag, suggest `/india-analyst` for the numbers and `/india-risk`
   to recheck pending orders. Never act on a headline alone.

To run it on a schedule: a scheduled task each morning at 08:30 IST and each
evening at 20:00 IST (most board meetings and results land after 15:30).

## Example (test fixtures: a made-up company)

```
  FILING   ALPHAPIPE  18 Oct 19:06 IST  Outcome of Board Meeting  [results, payout]   BSE + NSE
  HEADLINE ALPHAPIPE  18 Oct 09:42 IST  Alpha Pipes shares slide after auditor quits
! FILING   ALPHAPIPE  17 Oct 21:00 IST  Resignation of Statutory Auditors  [auditor, people]
! FILING   ALPHAPIPE  16 Oct 12:00 IST  Credit Rating  [rating, downgrade]
```

Brief: "Alpha Pipes (your largest holding): its statutory auditor resigned on
17 October (filing linked); a rating agency downgraded it to A- with a
negative outlook on the 16th; Q2 results and a Rs 4 interim dividend on the
18th. Two red flags in three days: read the auditor's resignation letter."

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a headline.
- Never hammers a server: one request per feed per stock.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/india-broker`, after `/india-risk`
passes and the user types an explicit yes for that order.
