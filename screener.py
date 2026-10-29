"""
Iron condor screener engine (used by app.py).
Reads data from TWS / IB Gateway. It never sends orders.
"""
import math
import asyncio
from datetime import datetime, date

import numpy as np
import pandas as pd
from ib_async import IB, Stock, Option, util

# ------------------------------------------------------------------
# Parameters (the interface changes them through configure())
# ------------------------------------------------------------------
CAPITAL = 50000
RISK_PCT = 0.02
DTE_MIN, DTE_MAX, DTE_TARGET = 30, 55, 45
RANGE_PCT = 0.15
MAX_STRIKES = 50
BATCH = 40
WAIT = 4
VRP_MIN = 1.2
IVR_MIN, IVR_MAX = 30, 70
DELTA_MIN, DELTA_MAX, DELTA_TARGET = 0.10, 0.16, 0.13
DELTA_WIDE_MIN, DELTA_WIDE_MAX = 0.08, 0.20
OI_MIN, SPREAD_MAX = 500, 0.10
ETF_TICKERS = []

ib = None   # active connection (created by connect())


def configure(**params):
    """Update parameters, e.g. configure(CAPITAL=30000, VRP_MIN=1.3)."""
    for k, v in params.items():
        if k not in globals():
            raise KeyError(f"unknown parameter: {k}")
        globals()[k] = v


def connect(host='127.0.0.1', port=7496, client_id=1, data_type=1, ib_obj=None):
    """Open the connection (read-only). ib_obj is only used by demo mode."""
    global ib
    # Streamlit runs in a thread without an event loop, so create a new one
    asyncio.set_event_loop(asyncio.new_event_loop())
    ib = ib_obj if ib_obj is not None else IB()
    ib.connect(host, port, clientId=client_id, readonly=True)
    ib.reqMarketDataType(data_type)
    return ib


def disconnect():
    global ib
    if ib is not None:
        try:
            ib.disconnect()
        except Exception:
            pass
        ib = None


# ------------------------------------------------------------------
# Functions
# ------------------------------------------------------------------
def clean(x):
    """IBKR sends nan or -1 when a value is missing. Turn those into None."""
    if x is None:
        return None
    try:
        if math.isnan(x) or x < 0:
            return None
    except TypeError:
        return None
    return float(x)


def get_spot(stock):
    t = ib.reqMktData(stock, '', False, False)
    ib.sleep(2)
    price = t.marketPrice()
    if clean(price) is None:
        price = t.close          # market closed: use the last close
    ib.cancelMktData(stock)
    return clean(price)


def get_hv30(stock):
    """30-day historical volatility, annualized."""
    bars = ib.reqHistoricalData(stock, endDateTime='', durationStr='60 D',
                                barSizeSetting='1 day', whatToShow='TRADES',
                                useRTH=True)
    df = util.df(bars)
    if df is None or len(df) < 31:
        return None
    r = np.log(df['close'] / df['close'].shift(1))
    return r.tail(30).std() * np.sqrt(252)


def iv_rank_percentile(iv_series):
    """IV Rank and IV Percentile on one year of implied volatility."""
    s = pd.Series(iv_series).dropna()
    if len(s) < 20:
        return None, None, None
    cur, lo, hi = s.iloc[-1], s.min(), s.max()
    rank = (cur - lo) / (hi - lo) * 100 if hi > lo else None
    pct = (s < cur).mean() * 100
    return cur, rank, pct


def get_iv_stats(stock):
    """IBKR implied volatility history of the underlying -> IV Rank / Percentile."""
    bars = ib.reqHistoricalData(stock, endDateTime='', durationStr='1 Y',
                                barSizeSetting='1 day',
                                whatToShow='OPTION_IMPLIED_VOLATILITY',
                                useRTH=True)
    df = util.df(bars)
    if df is None:
        return None, None, None
    return iv_rank_percentile(df['close'])


def expiry_candidates(expirations, today=None):
    """All expiries inside the DTE window: list of (expiry, dte)."""
    today = today or date.today()
    out = []
    for e in sorted(expirations):
        dte = (datetime.strptime(e, '%Y%m%d').date() - today).days
        d = datetime.strptime(e, '%Y%m%d').date()
        # SPY, QQQ etc. have daily expiries: keep only Fridays (weekly and monthly),
        # otherwise minutes are wasted downloading all the others
        if DTE_MIN <= dte <= DTE_MAX and d.weekday() == 4:
            out.append((e, dte))
    return out


def pick_strikes(all_strikes, spot):
    """Strikes inside the range, evenly spread if there are too many."""
    ks = sorted(k for k in all_strikes if abs(k / spot - 1) <= RANGE_PCT)
    if len(ks) > MAX_STRIKES:
        idx = np.linspace(0, len(ks) - 1, MAX_STRIKES).round().astype(int)
        ks = sorted(set(ks[i] for i in idx))
    # make sure the ATM strike is always included
    atm = min(all_strikes, key=lambda k: abs(k - spot))
    if atm not in ks:
        ks = sorted(ks + [atm])
    return ks


def get_contracts(symbol, chain, expiry):
    """All options that really exist for THIS expiry.
    (chain.strikes mixes the strikes of all expiries together,
    many do not exist on the chosen expiry)"""
    details = ib.reqContractDetails(Option(symbol, expiry, exchange='SMART',
                                           tradingClass=chain.tradingClass))
    return [d.contract for d in details]


def download_options(opts):
    """Download bid/ask/IV/delta/OI/volume for a list of options."""
    rows = []
    for i in range(0, len(opts), BATCH):
        batch = opts[i:i + BATCH]
        tks = [ib.reqMktData(c, '101,106', False, False) for c in batch]
        ib.sleep(WAIT)
        for c, t in zip(batch, tks):
            g = t.modelGreeks
            bid, ask = clean(t.bid), clean(t.ask)
            oi = t.putOpenInterest if c.right == 'P' else t.callOpenInterest
            rows.append({
                'strike': c.strike,
                'right': c.right,
                'bid': bid,
                'ask': ask,
                'mid': (bid + ask) / 2 if bid is not None and ask is not None else None,
                'spread': ask - bid if bid is not None and ask is not None else None,
                'iv': clean(g.impliedVol) if g else None,
                'delta': g.delta if g and g.delta is not None else None,
                'oi': clean(oi),
                'volume': clean(t.volume),
            })
            ib.cancelMktData(c)
    return rows


def strikes_around_target(df, right, valid_strikes):
    """After the first pass (sparse strikes) find where delta crosses the target
    and return ALL real strikes in that zone, so the 10-16 delta band is not skipped."""
    side = df[(df['right'] == right) & df['delta'].notna()].sort_values('strike')
    if len(side) < 2:
        return set()
    ks = side['strike'].tolist()
    ad = side['delta'].abs().tolist()
    for i in range(len(ks) - 1):
        if (ad[i] - DELTA_TARGET) * (ad[i + 1] - DELTA_TARGET) <= 0:
            lo = ks[max(i - 1, 0)]
            hi = ks[min(i + 2, len(ks) - 1)]
            return {k for k in valid_strikes if lo <= k <= hi}
    return set()


def get_chain(contracts, spot):
    """Download the smile in 2 passes.
    Pass 1: sparse strikes over the whole range (OTM puts below spot, OTM calls above).
    Pass 2: every strike in the zone where delta is about 0.13."""
    valid_strikes = sorted({c.strike for c in contracts})
    first = set(pick_strikes(valid_strikes, spot))

    def otm(strikes):
        return [c for c in contracts
                if c.strike in strikes and c.right == ('P' if c.strike < spot else 'C')]

    rows = download_options(otm(first))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    extra = set()
    for right in ['P', 'C']:
        extra |= strikes_around_target(df, right, valid_strikes)
    extra -= first
    if extra:
        rows += download_options(otm(extra))

    return pd.DataFrame(rows).sort_values('strike').reset_index(drop=True)


def atm_iv(df, spot):
    """ATM IV = mean of the 2 options with strike closest to spot."""
    d = df.dropna(subset=['iv']).copy()
    if d.empty:
        return None
    d['dist'] = (d['strike'] - spot).abs()
    return d.nsmallest(2, 'dist')['iv'].mean()


def find_short(df, right):
    """Short strike with |delta| in [0.10, 0.16], closest to 0.13.
    If no strike is in that band (strikes too far apart), widen to
    [0.08, 0.20] and flag the row as 'outside_band'."""
    side = df[(df['right'] == right) & df['delta'].notna()].copy()
    side['absdelta'] = side['delta'].abs()
    for lo, hi, wide in [(DELTA_MIN, DELTA_MAX, False), (DELTA_WIDE_MIN, DELTA_WIDE_MAX, True)]:
        cand = side[(side['absdelta'] >= lo) & (side['absdelta'] <= hi)]
        if not cand.empty:
            row = cand.loc[(cand['absdelta'] - DELTA_TARGET).abs().idxmin()].copy()
            row['outside_band'] = wide
            return row
    return None


def quote_leg(contracts, strike, right):
    """Download a single option for the wings.
    If the exact strike does not exist, take the first one further out (slightly wider wing)."""
    side = [c for c in contracts if c.right == right]
    if right == 'P':
        side = sorted([c for c in side if c.strike <= strike], key=lambda c: -c.strike)
    else:
        side = sorted([c for c in side if c.strike >= strike], key=lambda c: c.strike)
    if not side:
        return None
    c = side[0]
    t = ib.reqMktData(c, '101,106', False, False)
    ib.sleep(WAIT)
    bid, ask = clean(t.bid), clean(t.ask)
    oi = t.putOpenInterest if right == 'P' else t.callOpenInterest
    ib.cancelMktData(c)
    return pd.Series({
        'strike': c.strike, 'right': right, 'bid': bid, 'ask': ask,
        'mid': (bid + ask) / 2 if bid is not None and ask is not None else None,
        'spread': ask - bid if bid is not None and ask is not None else None,
        'iv': clean(t.modelGreeks.impliedVol) if t.modelGreeks else None,
        'delta': t.modelGreeks.delta if t.modelGreeks else None,
        'oi': clean(oi), 'volume': clean(t.volume),
    })


def wing_for(spot):
    """Wing width in dollars, based on the underlying price."""
    if spot >= 100:
        return 5
    if spot >= 50:
        return 2.5
    return 1


def find_wing(df, right, short_strike, fetch=None, wing=5):
    """Long strike 'wing' dollars further out than the short.
    Try the exact strike first (fetch); if it does not exist use the closest one in the chain."""
    target = short_strike - wing if right == 'P' else short_strike + wing
    if fetch is not None:
        row = fetch(right, target)
        if row is not None:
            return row
    if right == 'P':
        side = df[(df['right'] == 'P') & (df['strike'] <= short_strike - wing)]
        return None if side.empty else side.iloc[-1]   # highest strike below the target
    side = df[(df['right'] == 'C') & (df['strike'] >= short_strike + wing)]
    return None if side.empty else side.iloc[0]        # lowest strike above the target


def is_liquid(row):
    """Spread must be at most 0.10. Open interest is checked only if IBKR sends it
    (it often arrives as 0 or empty: then check it by hand in TWS).
    Daily volume is not a filter: far wings trade little even on normal days."""
    if row is None:
        return False
    spread_ok = row['spread'] is not None and row['spread'] <= SPREAD_MAX
    oi = row['oi']
    oi_ok = (oi is None or oi == 0) or oi >= OI_MIN
    return spread_ok and oi_ok


def build_condor(df, fetch=None, wing=5):
    """Legs, credit, max loss and number of contracts with the 2% rule."""
    sp, sc = find_short(df, 'P'), find_short(df, 'C')
    if sp is None or sc is None:
        return None
    lp = find_wing(df, 'P', sp['strike'], fetch, wing)
    lc = find_wing(df, 'C', sc['strike'], fetch, wing)
    if lp is None or lc is None:
        return None
    mids = [sp['mid'], lp['mid'], sc['mid'], lc['mid']]
    if any(m is None or pd.isna(m) for m in mids):
        credit = None
    else:
        credit = (sp['mid'] - lp['mid']) + (sc['mid'] - lc['mid'])
    width = max(sp['strike'] - lp['strike'], lc['strike'] - sc['strike'])
    max_loss = (width - credit) * 100 if credit is not None else None
    n = int(CAPITAL * RISK_PCT // max_loss) if max_loss and max_loss > 0 else 0
    return {
        'long_put': lp['strike'], 'short_put': sp['strike'], 'delta_sp': round(sp['delta'], 3),
        'short_call': sc['strike'], 'long_call': lc['strike'], 'delta_sc': round(sc['delta'], 3),
        'mid_long_put': lp['mid'], 'mid_short_put': sp['mid'],
        'mid_short_call': sc['mid'], 'mid_long_call': lc['mid'],
        'credit': round(credit, 2) if credit is not None else None,
        'max_loss_$': round(max_loss) if max_loss is not None else None,
        'contracts_2%': n,
        # prices for the closing GTC orders
        'GTC_profit_50%': round(credit * 0.5, 2) if credit is not None else None,
        'GTC_stop_200%': round(credit * 3, 2) if credit is not None else None,  # loss = 2x credit
        'liquid_ok': all(is_liquid(x) for x in [sp, lp, sc, lc]),
        'outside_band': bool(sp['outside_band'] or sc['outside_band']),
        # detail: the worst leg (lowest volume, widest spread)
        'vol_min': min((x['volume'] or 0) for x in [sp, lp, sc, lc]),
        'spread_max': max((x['spread'] if x['spread'] is not None else 9.99) for x in [sp, lp, sc, lc]),
    }


def get_next_earnings(symbol):
    """Next earnings date from Yahoo Finance (free, via yfinance).
    Returns a date, or None if not found or the site does not answer."""
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        dates = cal.get('Earnings Date') if isinstance(cal, dict) else None
        if not dates:
            return None
        dates = [pd.to_datetime(d).date() for d in dates]
        future = [d for d in dates if d >= date.today()]
        return min(future) if future else None
    except Exception:
        return None


def earnings_check(symbol, expiry):
    """Returns (text to show, ok, note).
    ETF: always ok. Single stock: ok only if earnings are NOT before expiry."""
    if symbol in ETF_TICKERS:
        return 'ETF', True, ''
    d = EARNINGS_LOOKUP(symbol)
    if d is None:
        return 'ND', False, 'earnings date not found: check by hand'
    exp_date = datetime.strptime(expiry, '%Y%m%d').date()
    if date.today() <= d <= exp_date:
        return str(d), False, f'EARNINGS on {d} before expiry'
    return str(d), True, ''


def analyze(symbol):
    """Full analysis of one ticker. Returns (summary row, chain)."""
    stock = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    spot = get_spot(stock)
    hv30 = get_hv30(stock)
    iv30, ivr, ivp = get_iv_stats(stock)

    chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
    smart = [c for c in chains if c.exchange == 'SMART']
    chain = next((c for c in smart if c.tradingClass == symbol), smart[0])

    # New expiries have few listed strikes (for example only +-6% around spot).
    # Among those in the DTE window pick the one with the MOST strikes in range,
    # and on a tie the one closest to 45 days (usually the monthly).
    best = None
    for e, d in expiry_candidates(chain.expirations):
        print(f'  checking expiry {e} ({d} DTE)...')
        cons = get_contracts(symbol, chain, e)
        n = len({c.strike for c in cons if abs(c.strike / spot - 1) <= RANGE_PCT})
        key = (n, -abs(d - DTE_TARGET))
        if best is None or key > best[0]:
            best = (key, e, d, cons)
    if best is None:
        return {'ticker': symbol, 'note': f'no expiry in {DTE_MIN}-{DTE_MAX} DTE'}, None
    _, expiry, dte, contracts = best
    df = get_chain(contracts, spot)
    if df.empty:
        return {'ticker': symbol, 'note': 'no option data (market data subscription?)'}, None
    iv_atm = atm_iv(df, spot)
    vrp = iv_atm / hv30 if iv_atm and hv30 else None
    fetch = lambda right, k: quote_leg(contracts, k, right)
    condor = build_condor(df, fetch, wing_for(spot)) or {}
    earn_txt, earn_ok, earn_note = earnings_check(symbol, expiry)
    if not condor:
        note = 'no short at delta 10-16 (few listed strikes?)'
    elif condor.get('credit') is None:
        note = 'one leg has no bid/ask quote: credit cannot be computed'
    elif condor.get('outside_band'):
        note = 'short delta outside 10-16 (8-20 used): strikes too far apart'
    else:
        note = ''
    note = ' | '.join(x for x in [note, earn_note] if x)

    vrp_ok = vrp is not None and vrp >= VRP_MIN
    ivr_ok = ivr is not None and IVR_MIN <= ivr <= IVR_MAX
    row = {
        'ticker': symbol, 'spot': round(spot, 2), 'expiry': expiry, 'dte': dte,
        'iv_atm': round(iv_atm, 3) if iv_atm else None,
        'hv30': round(hv30, 3) if hv30 else None,
        'vrp': round(vrp, 2) if vrp else None,
        'iv_rank': round(ivr) if ivr is not None else None,
        'iv_pct': round(ivp) if ivp is not None else None,
        **condor,
        'vrp_ok': vrp_ok, 'ivr_ok': ivr_ok,
        'earnings': earn_txt,
        'earn_ok': earn_ok,
        'PASS': vrp_ok and ivr_ok and earn_ok and condor.get('liquid_ok', False),
        'note': note,
    }
    return row, df



EARNINGS_LOOKUP = get_next_earnings   # replaced in demo mode


# ------------------------------------------------------------------
# Scan and classification
# ------------------------------------------------------------------
def scan(tickers, on_progress=None):
    """Analyze all tickers. on_progress(i, n, ticker) drives the progress bar."""
    results, chains = [], {}
    for i, sym in enumerate(tickers):
        if on_progress:
            on_progress(i, len(tickers), sym)
        try:
            row, df = analyze(sym)
            results.append(row)
            if df is not None:
                chains[sym] = df
        except Exception as e:
            results.append({'ticker': sym, 'note': f'error: {e}'})
    if on_progress:
        on_progress(len(tickers), len(tickers), '')
    summary = pd.DataFrame(results)
    if 'vrp' in summary.columns:
        summary = summary.sort_values('vrp', ascending=False, na_position='last')
    summary = classify(summary.reset_index(drop=True))
    return summary, chains


def _isna(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def _is_false(x):
    return (not _isna(x)) and (not bool(x))


def _is_true(x):
    return (not _isna(x)) and bool(x)


def classify(summary):
    """Add 'result' (PASS / CLOSE / NO / ERROR) and 'reason' (plain-language reasons)."""
    results, reasons = [], []
    for _, r in summary.iterrows():
        why = []
        if _isna(r.get('vrp')):
            results.append('ERROR')
            reasons.append(str(r.get('note', 'no data')))
            continue
        vrp_ok, ivr_ok = _is_true(r.get('vrp_ok')), _is_true(r.get('ivr_ok'))
        if not vrp_ok:
            why.append(f"VRP {r['vrp']:.2f} below {VRP_MIN}")
        if not ivr_ok:
            why.append(f"IV Rank {r.get('iv_rank')} outside {IVR_MIN}-{IVR_MAX}")
        if _is_false(r.get('earn_ok')):
            why.append(str(r.get('note', 'earnings')).split(' | ')[-1])
        if _isna(r.get('credit')):
            why.append('condor cannot be built or a leg has no quote')
        elif _is_false(r.get('liquid_ok')):
            why.append(f"spread too wide (max {r.get('spread_max')})")
        if _is_true(r.get('outside_band')):
            why.append('short delta outside 10-16')
        if _is_true(r.get('PASS')):
            results.append('PASS')
        elif vrp_ok and ivr_ok:
            results.append('CLOSE')
        else:
            results.append('NO')
        reasons.append(' | '.join(dict.fromkeys(why)))
    out = summary.copy()
    out.insert(0, 'result', results)
    out['reason'] = reasons
    return out
