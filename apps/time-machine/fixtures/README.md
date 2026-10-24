# Fixtures

Real data from INDmoney's connector, saved so the engine tests run offline.

| file | what |
| --- | --- |
| `NIFTY-1d.csv`, `RELIANCE-1d.csv` | daily bars (the same files the Python skill's tests use) |
| `indmoney-chain-NIFTY-20261027.json` | NIFTY option chain, 27-Oct expiry, as of the 19-Oct close |
| `NIFTY-5m.csv` | NIFTY 5-minute bars, 19 (afternoon), 21, 22 and 23 Oct 2026 (the last 250 the API returns for a 7-day window). The 15:15 and 15:20 bars are flat: the index feed repeats the 15:10 print, then the 15:25 bar carries the closing print |
| `indmoney-chain-NIFTY-20261027-eod23.json` | NIFTY chain, 27-Oct expiry, as of the 23-Oct close (13 strikes, the fields the engine reads) |
| `indmoney-chain-SENSEX-20261029-eod23.json` | SENSEX chain, 29-Oct expiry, as of the 23-Oct close (BSE: charges use the BFO rates) |
