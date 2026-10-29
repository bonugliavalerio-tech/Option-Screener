"""
Iron condor screener - interface (Streamlit).
Run:  python -m streamlit run app.py
Reads data from TWS / IB Gateway in read-only mode. It never sends orders.
"""
import pickle
import re
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import demo
import screener

st.set_page_config(page_title="Iron Condor Screener", layout="wide")
HISTORY = Path(__file__).parent / "history"
HISTORY.mkdir(exist_ok=True)

ETF_DEFAULT = "SPY, QQQ, IWM, DIA, GLD, SLV, TLT, HYG, EEM, XLE, XLF, XLK, XLV, XLU, XLI, SMH"
STOCK_DEFAULT = "AAPL, MSFT, META, GOOG, NVDA, AMD, AVGO, ORCL, INTC, JPM, BAC, XOM, CVX, LMT, IBM"


def parse_tickers(text):
    """'spy, qqq  aapl' -> ['SPY', 'QQQ', 'AAPL'] (no duplicates)"""
    out = []
    for t in re.split(r"[\s,;]+", text.upper()):
        if t and t not in out:
            out.append(t)
    return out


# ------------------------------------------------------------------
# Sidebar: all parameters
# ------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    use_demo = st.checkbox("Demo mode (made-up data, no TWS needed)", value=False,
                           help="To see how the interface works. The numbers are not real.")

    st.subheader("Connection")
    ports = {"TWS live (7496)": 7496, "TWS paper (7497)": 7497,
             "Gateway live (4001)": 4001, "Gateway paper (4002)": 4002}
    port = ports[st.selectbox("Port", list(ports))]
    types_ = {"Live": 1, "Last values (market closed)": 2, "Delayed 15 min (free)": 3}
    data_type = types_[st.selectbox("Data type", list(types_))]

    st.subheader("Capital and risk")
    capital = st.number_input("Account capital ($)", 1000, 10_000_000, 50_000, step=1000)
    risk = st.slider("Max loss per position (%)", 0.5, 5.0, 2.0, 0.5)

    st.subheader("Filters")
    vrp_min = st.number_input("Minimum VRP (IV / HV30)", 0.5, 3.0, 1.2, 0.05)
    ivr_lo, ivr_hi = st.slider("IV Rank", 0, 100, (30, 70))
    d_lo, d_hi = st.slider("Delta of the shorts", 0.05, 0.30, (0.10, 0.16), 0.01)
    d_target = st.slider("Target delta", d_lo, d_hi, min(max(0.13, d_lo), d_hi), 0.01)
    spread_max = st.number_input("Maximum spread per leg ($)", 0.01, 2.0, 0.10, 0.01)
    dte_lo, dte_hi = st.slider("Days to expiry (DTE)", 7, 90, (30, 55))
    dte_target = st.slider("Target DTE", dte_lo, dte_hi, min(max(45, dte_lo), dte_hi))

    st.subheader("Ticker")
    etf_txt = st.text_area("ETFs (no earnings check)", ETF_DEFAULT, height=90)
    stock_txt = st.text_area("Single stocks (earnings check)", STOCK_DEFAULT, height=90)
    wait = st.slider("Data wait per batch (seconds)", 2, 10, 4,
                     help="Raise it if you see many empty IVs.")

etfs, stocks = parse_tickers(etf_txt), parse_tickers(stock_txt)
tickers = etfs + [s for s in stocks if s not in etfs]


# ------------------------------------------------------------------
# Scan
# ------------------------------------------------------------------
def run_scan():
    screener.configure(
        CAPITAL=capital, RISK_PCT=risk / 100,
        DTE_MIN=dte_lo, DTE_MAX=dte_hi, DTE_TARGET=dte_target,
        VRP_MIN=vrp_min, IVR_MIN=ivr_lo, IVR_MAX=ivr_hi,
        DELTA_MIN=d_lo, DELTA_MAX=d_hi, DELTA_TARGET=d_target,
        SPREAD_MAX=spread_max, WAIT=wait, ETF_TICKERS=etfs)

    bar = st.progress(0.0)
    text = st.empty()

    def progress(i, n, sym):
        bar.progress(i / n)
        text.write(f"Analyzing {sym} ({i + 1}/{n})..." if sym else "Done.")

    try:
        if use_demo:
            screener.EARNINGS_LOOKUP = demo.fake_earnings
            screener.connect(ib_obj=demo.DemoIB())
        else:
            screener.EARNINGS_LOOKUP = screener.get_next_earnings
            screener.connect(port=port, data_type=data_type)
        summary, chains = screener.scan(tickers, progress)
    except Exception as e:
        st.error(f"Cannot connect to TWS: {e}")
        st.info("Check that: TWS is open and logged in, the port in the sidebar is the right one, "
                "and the API is enabled (Configure > API > Settings > "
                "Enable ActiveX and Socket Clients). If TWS shows a popup, press Close.")
        return
    finally:
        screener.disconnect()

    when = datetime.now()
    st.session_state.update(summary=summary, chains=chains, when=when, demo=use_demo)
    if not use_demo:   # demo scans are not saved
        stamp = when.strftime("%Y%m%d_%H%M")
        summary.to_csv(HISTORY / f"scan_{stamp}.csv", index=False)
        with open(HISTORY / f"chains_{stamp}.pkl", "wb") as f:
            pickle.dump(chains, f)


# ------------------------------------------------------------------
# Main page
# ------------------------------------------------------------------
st.title("Iron Condor Screener")
st.caption("Reads data from TWS in read-only mode and never sends orders. "
           "Earnings, FOMC and news still have to be checked by hand before opening a trade.")

col_a, col_b = st.columns([1, 4])
with col_a:
    go = st.button(f"Scan {len(tickers)} tickers", type="primary", width='stretch')
with col_b:
    st.write(f"Estimated time: about {max(1, round(len(tickers) * 0.45))} minutes. "
             "Do not click anything else during the scan.")
if go:
    run_scan()

tab_res, tab_det, tab_hist, tab_guide = st.tabs(["Results", "Ticker detail", "History", "Guide"])

COLORS = {"PASS": "rgba(46,160,67,0.30)", "CLOSE": "rgba(227,179,65,0.30)",
          "NO": "rgba(128,128,128,0.08)", "ERROR": "rgba(248,81,73,0.30)"}


def style(df):
    return df.style.apply(
        lambda r: [f"background-color: {COLORS.get(r['Result'], '')}"] * len(r), axis=1
    ).format({"VRP": "{:.2f}", "Credit": "{:.2f}", "Max spread": "{:.2f}",
              "Spot": "{:.2f}", "Max loss $": "{:.0f}", "IV Rank": "{:.0f}",
              "Contracts": "{:.0f}"}, na_rep="-")


COL_VIEW = {"result": "Result", "ticker": "Ticker", "spot": "Spot", "dte": "DTE", "vrp": "VRP",
            "iv_rank": "IV Rank", "earnings": "Earnings", "credit": "Credit",
            "max_loss_$": "Max loss $", "contracts_2%": "Contracts",
            "spread_max": "Max spread", "reason": "Reason"}


def table(summary):
    cols = [c for c in COL_VIEW if c in summary.columns]
    return summary[cols].rename(columns=COL_VIEW)


with tab_res:
    if "summary" not in st.session_state:
        st.info("Press 'Scan' to start. If you only want to see the interface, "
                "turn on demo mode in the sidebar.")
    else:
        s = st.session_state["summary"]
        if st.session_state.get("demo"):
            st.warning("Demo mode: the numbers are made up.")
        st.caption(f"Scan of {st.session_state['when']:%Y-%m-%d at %H:%M}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PASS", int((s["result"] == "PASS").sum()))
        c2.metric("CLOSE (VRP and IV Rank ok)", int((s["result"] == "CLOSE").sum()))
        c3.metric("Rejected", int((s["result"] == "NO").sum()))
        c4.metric("Errors", int((s["result"] == "ERROR").sum()))

        view = st.radio("Show", ["Only PASS and CLOSE", "All"], horizontal=True)
        if view != "All":
            s = s[s["result"].isin(["PASS", "CLOSE"])]
        if s.empty:
            st.success("No ticker passes or comes close with these filters. "
                       "Choose 'All' to see the reasons.")
        else:
            st.dataframe(style(table(s)), width='stretch', hide_index=True)
        st.caption("Green: passes every filter. Yellow: VRP and IV Rank are ok but something else fails "
                   "(earnings, spread, legs). Grey: rejected.")

with tab_det:
    if "summary" not in st.session_state:
        st.info("Run a scan first.")
    else:
        s = st.session_state["summary"]
        chains = st.session_state["chains"]
        ok = s[s["credit"].notna()] if "credit" in s.columns else s.iloc[0:0]
        if ok.empty:
            st.info("No condor was built in this scan.")
        else:
            order = list(ok[ok["result"].isin(["PASS", "CLOSE"])]["ticker"]) + \
                    list(ok[~ok["result"].isin(["PASS", "CLOSE"])]["ticker"])
            sym = st.selectbox("Ticker", order)
            r = ok[ok["ticker"] == sym].iloc[0]
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Result", r["result"])
            m2.metric("VRP", f"{r['vrp']:.2f}")
            m3.metric("IV Rank", f"{r['iv_rank']:.0f}")
            m4.metric("Credit", f"{r['credit']:.2f}")
            m5.metric("Max loss", f"${r['max_loss_$']:.0f}")
            if r["reason"]:
                st.warning(r["reason"])

            exp = datetime.strptime(str(r["expiry"]), "%Y%m%d")
            legs = pd.DataFrame([
                ["Sell", "Put", r["short_put"], r["mid_short_put"], r["delta_sp"]],
                ["Buy", "Put", r["long_put"], r["mid_long_put"], None],
                ["Sell", "Call", r["short_call"], r["mid_short_call"], r["delta_sc"]],
                ["Buy", "Call", r["long_call"], r["mid_long_call"], None],
            ], columns=["Action", "Type", "Strike", "Mid", "Delta"])
            st.subheader(f"{sym}: expiry {exp:%Y-%m-%d} ({r['dte']} DTE)")
            st.dataframe(legs.style.format({"Strike": "{:.2f}", "Mid": "{:.2f}",
                                            "Delta": "{:.3f}"}, na_rep="-"),
                         width='stretch', hide_index=True)

            n = int(r["contracts_2%"])
            if n == 0:
                st.error("With your risk rules not even 1 contract fits "
                         "(the maximum loss is above the allowed risk).")
            order_txt = (
                f"OPEN: iron condor {sym} {exp:%d%b%y}\n"
                f"  +{r['long_put']:g}P / -{r['short_put']:g}P / -{r['short_call']:g}C / +{r['long_call']:g}C\n"
                f"  limit credit {r['credit']:.2f} (mid), contracts: {n}\n"
                f"CLOSE (GTC orders right after opening):\n"
                f"  50% profit: buy the condor back at {r['GTC_profit_50%']:.2f}\n"
                f"  200% stop: buy the condor back at {r['GTC_stop_200%']:.2f}\n"
                f"  time exit: close anyway on {exp - timedelta(days=21):%Y-%m-%d} (21 DTE)"
            )
            st.code(order_txt, language=None)
            st.caption("Indicative prices at the mid. Check bid/ask in TWS before sending, "
                       "and check open interest and earnings by hand.")

            ch = chains.get(sym)
            if ch is not None and not ch.empty:
                d = ch.dropna(subset=["iv"])
                fig, ax = plt.subplots(figsize=(9, 3.8))
                for side, name in [("P", "OTM puts"), ("C", "OTM calls")]:
                    x = d[d["right"] == side]
                    ax.plot(x["strike"], x["iv"] * 100, "o-", ms=3, label=name)
                ax.axvline(r["spot"], color="gray", ls="--", label=f"Spot {r['spot']:.2f}")
                for k in (r["short_put"], r["short_call"]):
                    ax.axvline(k, color="red", ls=":", alpha=0.7)
                ax.set_xlabel("Strike")
                ax.set_ylabel("IV %")
                ax.set_title(f"{sym} smile (red lines: short strikes)")
                ax.grid(alpha=0.3)
                ax.legend()
                st.pyplot(fig)

with tab_hist:
    files = sorted(HISTORY.glob("scan_*.csv"), reverse=True)
    if not files:
        st.info("No saved scan yet. Every real scan is saved here.")
    else:
        choice = st.selectbox("Scan", files, format_func=lambda p: p.stem.replace("scan_", ""))
        old = pd.read_csv(choice)
        st.dataframe(style(table(old)), width='stretch', hide_index=True)
        st.download_button("Download CSV", old.to_csv(index=False), file_name=choice.name)
        if len(files) > 1:
            st.subheader("How many tickers pass over time")
            rows = []
            for f in reversed(files):
                x = pd.read_csv(f)
                rows.append({"scan": f.stem.replace("scan_", ""),
                             "PASS": int((x["result"] == "PASS").sum()),
                             "CLOSE": int((x["result"] == "CLOSE").sum())})
            st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

with tab_guide:
    st.markdown(f"""
**How to read it**
- **VRP** = ATM IV / 30-day historical volatility. Above {vrp_min} the market pays more than the real movement.
- **IV Rank** = where IV sits today compared with the last year (0 = lowest, 100 = highest). Good zone: {ivr_lo}-{ivr_hi}.
- **Shorts** at delta {d_lo:.2f}-{d_hi:.2f}, wings $5 (spot >= 100), $2.5 (50-100) or $1 (below 50).
- **Contracts**: how many are needed so the loss stays under {risk:g}% of capital if the condor reaches its maximum loss.
- For single stocks the program rejects those with **earnings before expiry**. The dates come from Yahoo and are sometimes estimates, so always confirm them.

**What it does not do**
- It does not send orders (read-only connection).
- It does not check FOMC, news or open interest (IBKR does not always send it): check them by hand in TWS.

**If something goes wrong**
- "Cannot connect": TWS is closed, the port is wrong or the API is not enabled.
- Many empty IVs: raise "Data wait" in the sidebar or choose "Delayed" as data type.
- A ticker shows "ND" for earnings: Yahoo has no date, check it by hand.
""")
