"""Deterministic analysis of a retrieved price series: returns, period performance, highs/lows, volatility, drawdown,
moving averages, RSI and volume. Pure functions over provider data; no language model is involved and a metric that
cannot be computed from the available observations is reported as unavailable with the reason, never estimated."""
import math, statistics
from datetime import date, datetime, timedelta, timezone
from .core import NoData

TRADING_DAYS = 252  # assumption used to annualise daily volatility
PERIODS = (("1M", 31), ("3M", 92), ("6M", 183), ("1Y", 366), ("5Y", 1827))
WINDOWS = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "5y": 1827, "max": None}
MAX_POINTS = 1500


def fetch_range(rng):
    """Indicators need history before the displayed window, so everything except 'max' is computed from 5 years of data."""
    return "max" if rng == "max" else "5y"


r4 = lambda x: None if x is None else round(x, 4)


def sma(v, n):
    """Simple moving average; None until n observations exist."""
    return [sum(v[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(v))]


def _rsi(ag, al):
    return (100.0 if ag > 0 else 50.0) if al == 0 else 100 - 100 / (1 + ag / al)


def rsi(c, n=14):
    """Wilder's RSI: first average is a simple mean of n changes, then Wilder smoothing; None until n+1 observations exist."""
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = _rsi(ag, al)
    for i in range(n, len(g)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
        out[i + 1] = _rsi(ag, al)
    return out


def daily_returns(px):
    return [None] + [(px[i] / px[i - 1] - 1) if px[i - 1] else None for i in range(1, len(px))]


def ann_vol(rets):
    """Sample standard deviation of daily returns, daily and annualised (x sqrt(252)), in percent; None if under 20 returns."""
    x = [r for r in rets if r is not None]
    if len(x) < 20:
        return None
    sd = statistics.stdev(x)
    return sd * 100, sd * math.sqrt(TRADING_DAYS) * 100, len(x)


def rolling_vol(rets, n=20):
    return [ann_vol(rets[i - n + 1:i + 1])[1] if i >= n and None not in rets[i - n + 1:i + 1] else None for i in range(len(rets))]


def drawdowns(px, dates):
    """Running drawdown series plus the worst peak-to-trough fall and when (if ever) it recovered."""
    peak, pk_i, dd, worst = px[0], 0, [], (0.0, 0, 0)
    for i, p in enumerate(px):
        if p > peak:
            peak, pk_i = p, i
        d = p / peak - 1 if peak else 0
        dd.append(d)
        if d < worst[0]:
            worst = (d, pk_i, i)
    d, a, b = worst
    rec = next((dates[i] for i in range(b + 1, len(px)) if px[i] >= px[a]), None) if d < 0 else None
    return dd, {"pct": r4(d * 100), "peak_date": dates[a], "trough_date": dates[b], "recovery_date": rec, "recovered": d == 0 or rec is not None}


def compute(inst, bars, provider, attempts, rng, today=None):
    """Builds the full analysis response for one instrument and one selected date range."""
    today = today or datetime.now(timezone.utc).date()
    rows = sorted({r["d"]: r for r in bars["rows"] if r.get("c") is not None}.values(), key=lambda r: r["d"])
    if not rows:
        raise NoData(f"{provider} returned no usable prices for {inst['id']}.", provider, attempts=attempts)
    D = [date.fromisoformat(r["d"]) for r in rows]
    iso = [r["d"] for r in rows]
    C = [r["c"] for r in rows]
    V = [r.get("v") for r in rows]
    adj = all(r.get("ac") is not None for r in rows)
    PX = [r["ac"] for r in rows] if adj else C
    R = daily_returns(PX)
    last, warns = len(rows) - 1, []
    days = WINDOWS[rng]
    w0 = 0 if days is None else next((i for i, d in enumerate(D) if d >= D[last] - timedelta(days=days)), last)
    # --- data quality
    age = (today - D[last]).days
    if age > 10:
        warns.append(f"The latest observation is {age} days old; the security may be delisted, suspended or very illiquid.")
    if bars.get("missing"):
        warns.append(f"{bars['missing']} observation(s) were missing at the provider and were skipped, not filled in.")
    gaps = [(a, b) for a, b in zip(D, D[1:]) if (b - a).days > 5]
    if gaps:
        warns.append(f"{len(gaps)} gap(s) longer than 5 days in the series (largest {max((b - a).days for a, b in gaps)} days); returns across a gap span several sessions.")
    if not adj:
        jumps = [iso[i] for i, r in enumerate(R) if r is not None and abs(r) > 0.4]
        if jumps:
            warns.append(f"Daily moves above 40% on {', '.join(jumps[:3])}: possibly an unadjusted split or corporate action, which distorts returns and volatility.")
    if last - w0 < 1:
        warns.append("Fewer than two observations in the selected window; window metrics are unavailable.")
    elif days and D[0] > D[last] - timedelta(days=days) + timedelta(days=5):
        warns.append(f"Only {(D[last] - D[0]).days} days of history are available, which is shorter than the selected {rng} range.")
    # --- price block
    price = {"latest": C[last], "latest_date": iso[last], "previous_close": C[last - 1] if last else None,
             "previous_date": iso[last - 1] if last else None}
    price["change"] = r4(C[last] - C[last - 1]) if last else None
    price["change_pct"] = r4((C[last] / C[last - 1] - 1) * 100) if last and C[last - 1] else None
    # --- performance over fixed periods (always from the full retrieved history)
    perf = []
    for name, d in PERIODS + (("Max", None),):
        if d is None:
            base = 0 if last else None
        else:
            tgt = D[last] - timedelta(days=d)
            base = next((i for i in range(last, -1, -1) if D[i] <= tgt), None)
            if base is None and (D[0] - tgt).days <= 5:
                base = 0
        if base is None or base == last:
            perf.append({"period": name, "return_pct": None, "from_date": None,
                         "reason": f"History starts {iso[0]}, after the start of this period." if base is None else "Not enough observations."})
        else:
            perf.append({"period": name, "return_pct": r4((PX[last] / PX[base] - 1) * 100), "from_date": iso[base], "reason": None})

    def hl(i0):
        hi = [r["h"] if r.get("h") is not None else r["c"] for r in rows[i0:]]
        lo = [r["l"] if r.get("l") is not None else r["c"] for r in rows[i0:]]
        a, b = max(range(len(hi)), key=hi.__getitem__), min(range(len(lo)), key=lo.__getitem__)
        return {"high": hi[a], "high_date": iso[i0 + a], "low": lo[b], "low_date": iso[i0 + b]}
    w52 = next((i for i, d in enumerate(D) if d >= D[last] - timedelta(days=365)), last)
    wret = r4((PX[last] / PX[w0] - 1) * 100) if last > w0 else None
    # --- risk (selected window)
    rets = R[w0 + 1:last + 1]
    v = ann_vol(rets)
    if v:
        risk = {"daily_vol_pct": r4(v[0]), "annualised_vol_pct": r4(v[1]), "return_observations": v[2], "reason": None}
    else:
        risk = {"daily_vol_pct": None, "annualised_vol_pct": None, "return_observations": len(rets), "reason": f"Needs at least 20 daily returns in the window; {len(rets)} available."}
    wpx = PX[w0:]
    wdd, mdd = drawdowns(wpx, iso[w0:]) if len(wpx) > 1 else ([0.0] * len(wpx), None)
    risk.update(max_drawdown=mdd, current_drawdown_pct=r4((PX[last] / max(wpx) - 1) * 100) if len(wpx) > 1 else None,
                annualisation="daily standard deviation x sqrt(252)")
    # --- indicators (full history, then sliced)
    S = {n: sma(C, n) for n in (20, 50, 200)}
    RS = rsi(C)
    ind = {}
    for n in (20, 50, 200):
        lv = S[n][last]
        ind[f"sma{n}"] = {"value": r4(lv), "price_vs": None if lv is None else ("above" if C[last] > lv else "below" if C[last] < lv else "at"),
                          "reason": None if lv is not None else f"Needs {n} observations; {len(C)} available."}
    rv = RS[last]
    ind["rsi14"] = {"value": r4(rv), "zone": None if rv is None else ("above 70 (conventionally overbought)" if rv >= 70 else "below 30 (conventionally oversold)" if rv <= 30 else "between 30 and 70"),
                    "reason": None if rv is not None else f"Needs 15 observations; {len(C)} available."}
    # --- volume
    vv = [x for x in V[w0:] if x]
    last20 = [x for x in V[-20:] if x]
    vol = {"available": bool(vv), "latest": V[last] if V[last] else None, "avg20": r4(sum(last20) / len(last20)) if len(last20) >= 20 else None,
           "reason": None if vv else "The provider returned no volume for this instrument."}
    # --- chart series for the selected window
    n = last - w0 + 1
    idx = list(range(w0, last + 1))
    if n > MAX_POINTS:
        idx = sorted({w0 + int(k * (n - 1) / (MAX_POINTS - 1)) for k in range(MAX_POINTS)})
    RV = rolling_vol(R)
    series = {"dates": [iso[i] for i in idx], "close": [C[i] for i in idx], "volume": [V[i] for i in idx],
              "cum_return": [r4((PX[i] / PX[w0] - 1) * 100) for i in idx], "daily_return": [r4(R[i] * 100) if R[i] is not None and i > w0 else None for i in idx],
              "rolling_vol": [r4(RV[i]) for i in idx], "drawdown": [r4(wdd[i - w0] * 100) for i in idx],
              "sma20": [r4(S[20][i]) for i in idx], "sma50": [r4(S[50][i]) for i in idx], "sma200": [r4(S[200][i]) for i in idx], "rsi": [r4(RS[i]) for i in idx]}
    delayed = D[last] >= today
    return {"instrument": inst, "range": rng, "source": {"provider": provider, "note": bars.get("note", "")},
            "data_type": "delayed" if delayed else "historical", "data_type_label": "Delayed (latest bar may be intraday)" if delayed else "Historical end-of-day",
            "real_time": False, "currency": bars.get("currency") or inst.get("currency"), "latest_observation": iso[last], "history_start": iso[0],
            "observations_total": len(rows), "observations_window": n, "window_start": iso[w0],
            "return_basis": "adjusted close (includes dividends and splits)" if adj else "close (unadjusted: dividends and splits are not reflected)",
            "price": price, "range_return_pct": wret, "performance": perf, "range_high_low": hl(w0), "w52_high_low": hl(w52), "history_high_low": hl(0),
            "risk": risk, "indicators": ind, "volume": vol, "series": series, "warnings": warns,
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts, "cached": False}


def snapshot(rows):
    """Compact screener metrics from one instrument's ~1 year of daily history. Uses the same maths as compute(); None where
    the history is too short, so the screener never filters on an invented value."""
    rows = sorted({r["d"]: r for r in rows if r.get("c") is not None}.values(), key=lambda r: r["d"])
    if len(rows) < 2:
        return None
    D = [date.fromisoformat(r["d"]) for r in rows]
    iso = [r["d"] for r in rows]
    C = [r["c"] for r in rows]
    V = [r.get("v") for r in rows]
    adj = all(r.get("ac") is not None for r in rows)
    PX = [r["ac"] for r in rows] if adj else C
    R = daily_returns(PX)
    last = len(rows) - 1
    w0 = next((i for i, d in enumerate(D) if d >= D[last] - timedelta(days=366)), last)
    out = {"price": C[last], "date": iso[last], "prev": C[last - 1], "obs": len(rows), "basis": "adjusted" if adj else "close",
           "chg": r4((C[last] / C[last - 1] - 1) * 100) if C[last - 1] else None}
    for key, days in (("1m", 31), ("3m", 92), ("6m", 183), ("1y", 366)):
        tgt = D[last] - timedelta(days=days)
        base = next((i for i in range(last, -1, -1) if D[i] <= tgt), None)
        if base is None and (D[0] - tgt).days <= 5:
            base = 0
        out["perf_" + key] = r4((PX[last] / PX[base] - 1) * 100) if base is not None and base != last else None
    v = ann_vol(R[w0 + 1:last + 1])
    out["vol"] = r4(v[1]) if v else None
    out["mdd"] = drawdowns(PX[w0:], iso[w0:])[1]["pct"] if last - w0 >= 1 else None
    for n in (20, 50, 200):
        m = sum(C[-n:]) / n if len(C) >= n else None
        out[f"sma{n}"] = r4(m)
        out[f"sma{n}_pct"] = r4((C[last] / m - 1) * 100) if m else None
    v20 = [x for x in V[-20:] if x]
    out["avgvol"] = r4(sum(v20) / len(v20)) if len(v20) >= 20 else None
    out["volume"] = V[last] or None
    return out
