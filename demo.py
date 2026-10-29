"""
Demo mode: pretends to be TWS with made-up data (no real connection).
It lets you see the interface without TWS open. The numbers are NOT real.
"""
import math
import types
import hashlib
import zlib
import datetime as dt
import numpy as np
from ib_async import BarData


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# spot, 45-day IV, IV/HV ratio, position of IV in its yearly range (0-1)
KNOWN = {
    'SPY': (768, .14, 1.4, .15), 'QQQ': (713, .21, 1.1, .2), 'IWM': (283, .185, 1.3, .12),
    'DIA': (521, .137, 1.45, .17), 'XLE': (68.4, .265, 1.25, .55), 'XLI': (169, .19, 1.5, .4),
    'TLT': (81, .125, 1.05, .45), 'AAPL': (350, .27, 1.55, .5), 'MSFT': (528, .2, 1.25, .25),
}


def _params(sym):
    if sym in KNOWN:
        return KNOWN[sym]
    h = int(hashlib.md5(sym.encode()).hexdigest(), 16)
    spot = 40 + h % 400
    iv = .18 + (h >> 8) % 20 / 100
    return spot, iv, .9 + (h >> 16) % 70 / 100, ((h >> 24) % 90) / 100


class _Tick:
    def __init__(self):
        n = float('nan')
        self.bid = self.ask = self.volume = self.close = self.last = n
        self.callOpenInterest = self.putOpenInterest = n
        self.modelGreeks = None

    def marketPrice(self):
        return self.last


class DemoIB:
    """The same IB functions the screener uses, with simulated data."""
    def connect(self, *a, **k): pass
    def disconnect(self): pass
    def isConnected(self): return True
    def reqMarketDataType(self, x): pass
    def sleep(self, x): pass
    def cancelMktData(self, c): pass
    def qualifyContracts(self, *cs):
        for i, c in enumerate(cs):
            c.conId = i + 1
        return list(cs)

    def reqSecDefOptParams(self, sym, *a):
        today = dt.date.today()
        exps = []
        for d in (7, 14, 21, 28, 36, 43, 50, 57):
            x = today + dt.timedelta(d)
            x += dt.timedelta((4 - x.weekday()) % 7)
            exps.append(x.strftime('%Y%m%d'))
        return [types.SimpleNamespace(exchange='SMART', tradingClass=sym,
                                      expirations=set(exps), strikes=[])]

    def reqContractDetails(self, c):
        from ib_async import Option
        sp = _params(c.symbol)[0]
        step = 5 if sp >= 100 else 2.5 if sp >= 50 else 1
        out, k = [], round(sp * 0.7 / step) * step
        i = 1000
        while k <= sp * 1.3:
            for r in 'PC':
                o = Option(c.symbol, c.lastTradeDateOrContractMonth, float(k), r, 'SMART',
                           tradingClass=c.tradingClass)
                o.conId = i
                i += 1
                out.append(types.SimpleNamespace(contract=o))
            k += step
        return out

    def reqHistoricalData(self, c, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH):
        sp, iv, ratio, pos = _params(c.symbol)
        rng = np.random.default_rng(zlib.crc32(c.symbol.encode()) % 1000)
        n = 250
        if whatToShow == 'TRADES':
            hv = iv / ratio
            p = sp * np.exp(np.cumsum(rng.normal(0, hv / math.sqrt(252), n)))
            p = p * sp / p[-1]
        else:
            lo, hi = iv * .6, iv * 1.5
            cur = lo + pos * (hi - lo)
            p = np.append(rng.uniform(lo, hi, n - 1), cur)
            p[0], p[1] = lo, hi
        return [BarData(date=dt.date.today() - dt.timedelta(n - i), open=x, high=x, low=x,
                        close=x, volume=1) for i, x in enumerate(p)]

    def reqMktData(self, c, gen='', snap=False, reg=False):
        t = _Tick()
        sp, iv0, ratio, pos = _params(c.symbol)
        if c.secType == 'STK':
            t.last = float(sp)
            return t
        K = c.strike
        T = max((dt.datetime.strptime(c.lastTradeDateOrContractMonth, '%Y%m%d').date()
                 - dt.date.today()).days, 1) / 365
        iv = iv0 + .30 * max(0, (sp - K) / sp) + .05 * max(0, (K - sp) / sp)
        d1 = (math.log(sp / K) + .5 * iv * iv * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if c.right == 'C':
            px, de = sp * _cdf(d1) - K * _cdf(d2), _cdf(d1)
        else:
            px, de = K * _cdf(-d2) - sp * _cdf(-d1), _cdf(d1) - 1
        far = abs(K / sp - 1)
        half = .02 + 4 * far * .03
        t.bid, t.ask = round(max(px - half, .01), 2), round(px + half, 2)
        t.volume = float(int(max(0, 400 * math.exp(-far * 20))))
        t.callOpenInterest = t.putOpenInterest = float('nan')
        t.modelGreeks = types.SimpleNamespace(impliedVol=iv, delta=de)
        return t


def fake_earnings(symbol):
    """Made-up dates: some stocks have earnings before expiry."""
    if symbol in ('AAPL', 'CVX', 'XOM'):
        return dt.date.today() + dt.timedelta(5)
    if symbol in ('IBM', 'LMT', 'META'):
        return None
    return dt.date.today() + dt.timedelta(90)
