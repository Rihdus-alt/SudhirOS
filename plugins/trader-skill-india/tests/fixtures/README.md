# Test fixtures

Two kinds, labelled so nobody mistakes one for the other:

- **Real**: `india-alerts/indmoney-details-2026-10-19.json` and
  `indmoney-raw-result.json` are the INDmoney connector's
  `get_indian_stocks_details` output for eight NSE stocks, read on
  2026-10-20 (prices of the 2026-10-19 close, circuit limits included).
  The holdings in `india-copycat/indmoney-ppfas-flexicap.json` are the same
  connector's `get_mf_funds_details` output for Parag Parikh Flexi Cap Fund,
  read the same day.
- **Synthetic, in the documented shape of the source**: everything else
  (Yahoo chart JSON, NSE quote/announcement/PIT JSON, NSE bulk and block deal
  CSVs, BSE announcement JSON, a shareholding-pattern XBRL, an Ind-AS results
  XBRL, an AMC monthly portfolio). The live endpoints could not be reached
  from the machine this pack was built on, so these were written by hand to
  the field names those sources publish. Company names like XYZSMALL and
  "Example Flexi Cap Fund" are made up. Numbers in the synthetic files are
  for testing the arithmetic, not facts about any company.
