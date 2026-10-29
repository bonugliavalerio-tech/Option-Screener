# VRP Options Screener

A screener for iron condor trades on liquid ETFs and large stocks. It reads option data from Interactive Brokers (TWS or IB Gateway), computes the volatility risk premium and the other filters, and builds a condor for each ticker that passes. The connection is read-only: the program never sends orders.

A short technical note with the mathematics behind each step is available separately (PDF).

## What it computes

| Item | Definition |
|---|---|
| VRP | ATM implied volatility divided by 30-day historical volatility. Pass if at least 1.2. |
| IV Rank / Percentile | Position of today's IV in its one-year range (rank) or in its one-year distribution (percentile). Pass if the rank is between 30 and 70. |
| Short strikes | Put and call with absolute delta in [0.10, 0.16], closest to 0.13. If none exists, the band widens to [0.08, 0.20] and the trade is flagged. |
| Wings | $5 if spot is at least 100, $2.5 if between 50 and 100, $1 below 50. |
| Size | Number of condors so that the maximum loss stays under 2% of capital. |
| Exit prices | Buy back at 50% of the credit (profit), at 3 times the credit (stop, a loss of 2 times the credit), and close at 21 days to expiry. |
| Liquidity | Bid-ask spread of at most $0.10 on all four legs. Open interest is checked only if the broker sends it. |
| Earnings | Single stocks are rejected if earnings fall before expiry (dates from Yahoo Finance, sometimes estimates). |

The exit rules and the thresholds are practitioner rules of thumb. They have not been backtested here.

## Setup

1. Install Python 3.10 or newer.
2. Install the libraries: `python -m pip install -r requirements.txt` (on Windows, double click `install.bat`).
3. Open TWS or IB Gateway and log in. In `Configure > API > Settings` enable `Enable ActiveX and Socket Clients` and keep `Read-Only API` checked.

## Run

```
python -m streamlit run app.py
```

On Windows, double click `run_screener.bat`. A page opens in the browser. Press `Scan` and wait: a scan of 30 tickers takes about 15 minutes, mostly because of the data limits of the broker.

To try the interface without TWS, tick `Demo mode` in the sidebar. The demo numbers are made up.

## Files

- `screener.py`: the engine (data download, filters, condor construction).
- `app.py`: the Streamlit interface.
- `demo.py`: a fake broker with made-up data, used by the demo mode.
- Real scans are saved in the `history` folder (not included in the repository).

## Limits

- No backtest of the strategy.
- Implied volatility and delta come from the broker's model.
- A high VRP can come from a known event (earnings) or from a very low 30-day volatility.
- Open interest often arrives as zero from the broker, so check it by hand before trading.
- This is a research and learning tool. It is not financial advice.
