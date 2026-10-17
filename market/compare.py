"""Stock comparison and watchlist quotes. A comparison never puts raw share prices side by side: every instrument is rebased to 100 at the
same start date inside a common date window. The last close is carried forward only to draw the chart across different exchange calendars;
correlations use only the dates on which both instruments actually traded. Instruments that cannot be aligned are reported, never forced in."""
import bisect, math, os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from . import analysis as an
from .core import MarketError, NoData

MAX_COMPARE, MAX_QUOTES = 6, 30
STALE_DAYS = 10   # an instrument whose latest data is this many days older than the others is left out
TOL = 5           # a carried-forward close is used for at most this many calendar days
MIN_CORR = 30     # minimum overlapping return observations for a correlation
MAX_POINTS = 800


class BadRequest(MarketError):
    code, status = "bad_request", 400


def _dedupe(ids, limit=None):
    out = []
    for i in ids:
        i = str(i).strip()
        if i and i not in out:
            out.append(i)
    return out


def _at(D, t):
    """Index of the last observation on or before date t (None if there is none)."""
    i = bisect.bisect_right(D, t) - 1
    return i if i >= 0 else None


def _prep(inst, bars, provider):
    rows = sorted({r["d"]: r for r in bars["rows"] if r.get("c") is not None}.values(), key=lambda r: r["d"])
    if len(rows) < 2:
        raise NoData(f"{inst.id} has fewer than two usable observations.", provider)
    adj = all(r.get("ac") is not None for r in rows)
    return {"inst": inst, "rows": rows, "D": [date.fromisoformat(r["d"]) for r in rows], "C": [r["c"] for r in rows], "V": [r.get("v") for r in rows],
            "PX": [r["ac"] for r in rows] if adj else [r["c"] for r in rows], "adj": adj, "provider": provider, "note": bars.get("note", ""),
            "currency": bars.get("currency") or inst.currency}


def pearson(a, b):
    n = len(a)
    if n < 2:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa, sb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    if sa == 0 or sb == 0:
        return None
    return max(-1.0, min(1.0, sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(sa * sb)))


def compare(svc, ids, rng="1y", fundamentals=True):
    """Builds the aligned comparison of 2 to 6 instruments; instruments that fail or cannot be aligned are listed in errors."""
    rng = rng if rng in an.WINDOWS else "1y"
    refs = _dedupe(ids)
    if len(refs) < 2:
        raise BadRequest("Select at least two instruments to compare.")
    if len(refs) > MAX_COMPARE:
        raise BadRequest(f"Compare at most {MAX_COMPARE} instruments at a time.")
    errors, loaded = [], []

    def load(ref):
        inst = svc.resolve(ref)
        bars, prov, _att = svc._raw(inst, an.fetch_range(rng))
        return _prep(inst, bars, prov)
    with ThreadPoolExecutor(min(4, len(refs))) as ex:
        futs = [(r, ex.submit(load, r)) for r in refs]
    seen = set()
    for ref, f in futs:
        try:
            p = f.result()
        except MarketError as e:
            errors.append({"id": ref, **e.to_dict()})
            continue
        if p["inst"].id in seen:
            errors.append({"id": ref, "code": "duplicate", "message": f"{ref} is the same instrument as one already selected ({p['inst'].id}).", "retryable": False})
            continue
        seen.add(p["inst"].id)
        loaded.append(p)
    if loaded:
        newest = max(p["D"][-1] for p in loaded)
        keep = []
        for p in loaded:
            gap = (newest - p["D"][-1]).days
            if gap > STALE_DAYS:
                errors.append({"id": p["inst"].id, "code": "stale_data", "retryable": False, "provider": p["provider"],
                               "message": f"{p['inst'].name}'s latest observation ({p['D'][-1]}) is {gap} days older than the others, so it was left out; including it would misalign the dates."})
            else:
                keep.append(p)
        loaded = keep
    if len(loaded) < 2:
        raise NoData("At least two instruments with usable, current data are needed to compare. " + " ".join(e["message"] for e in errors), None, errors=errors)
    end = min(p["D"][-1] for p in loaded)
    days = an.WINDOWS[rng]
    first = max(p["D"][0] for p in loaded)
    start = first if days is None else max(first, end - timedelta(days=days))
    if (end - start).days < 5:
        raise NoData(f"The selected instruments have only {(end - start).days} days of overlapping history, which is too short to compare.", None, errors=errors)
    warnings = []
    limiter = max(loaded, key=lambda p: p["D"][0])
    if days is not None and first > end - timedelta(days=days):
        warnings.append(f"{limiter['inst'].name} has data only from {limiter['D'][0]}, so the comparison window starts there instead of {rng} before the end.")
    if any(p["D"][-1] > end for p in loaded):
        warnings.append(f"The comparison ends on {end}, the latest date every instrument has data for; newer prices for some instruments were not used.")
    curs = sorted({p["currency"] for p in loaded if p["currency"]})
    if len(curs) > 1:
        warnings.append(f"These instruments trade in different currencies ({', '.join(curs)}). Returns are in each stock's own currency and exclude currency movements; share prices are never compared directly.")
    if len({p["inst"].exchange for p in loaded}) > 1:
        warnings.append("Exchanges have different holidays and time zones. The performance chart carries the last close forward for up to 5 days on non-trading days; correlations use only dates when both instruments traded.")
    if len({p["adj"] for p in loaded}) > 1:
        warnings.append("Some instruments have dividend/split-adjusted prices and others do not, so returns are not perfectly like-for-like: " + ", ".join(p["inst"].id for p in loaded if not p["adj"]) + " use unadjusted closes.")
    for e in errors:
        warnings.append(f"{e['id']} was not compared: {e['message']}")
    # ---- per-instrument metrics over the common window
    out = []
    for p in loaded:
        D, PX, C, V, rows = p["D"], p["PX"], p["C"], p["V"], p["rows"]
        b, e = _at(D, start), _at(D, end)
        p["b"], p["e"] = b, e
        seg, dts = PX[b:e + 1], [r["d"] for r in rows[b:e + 1]]
        v = an.ann_vol(an.daily_returns(seg)[1:])
        dd, mdd = an.drawdowns(seg, dts) if len(seg) > 1 else ([0.0], None)
        p["dd"] = dd
        vols = [x for x in V[b + 1:e + 1] if x]
        sm = {}
        for n in (20, 50, 200):
            m = sum(C[e - n + 1:e + 1]) / n if e + 1 >= n else None
            sm[f"sma{n}"] = {"value": an.r4(m), "price_vs_pct": an.r4((C[e] / m - 1) * 100) if m else None, "reason": None if m else f"Needs {n} observations; {e + 1} available."}
        w = []
        if (start - D[b]).days > TOL:
            w.append(f"No observation within {TOL} days before the start date; its baseline is the close on {D[b]}.")
        if not vols:
            w.append("No volume data from the provider.")
        out.append({"id": p["inst"].id, "name": p["inst"].name, "symbol": p["inst"].symbol, "exchange": p["inst"].exchange, "market": p["inst"].market,
                    "currency": p["currency"], "source": p["provider"], "basis": "adjusted" if p["adj"] else "close", "baseline_date": D[b].isoformat(),
                    "last_date": D[e].isoformat(), "observations": e - b + 1, "price": C[e],
                    "return_pct": an.r4((PX[e] / PX[b] - 1) * 100) if PX[b] else None,
                    "vol_ann_pct": an.r4(v[1]) if v else None, "vol_returns": e - b, "vol_reason": None if v else f"Needs at least 20 daily returns; {e - b} available.",
                    "max_drawdown": mdd, "avg_volume": an.r4(sum(vols) / len(vols)) if vols else None, "latest_volume": V[e] or None, **sm, "warnings": w})
    # ---- aligned chart series: rebased to 100 at the start; last close carried forward up to TOL days
    ds = sorted({d for p in loaded for d in p["D"][p["b"]:p["e"] + 1] if start <= d <= end} | {start})
    perf, drawdown = {}, {}
    for p in loaded:
        pf, dr = [], []
        for t in ds:
            i = _at(p["D"], t)
            ok = i is not None and (t - p["D"][i]).days <= TOL and i >= p["b"]
            pf.append(an.r4((p["PX"][i] / p["PX"][p["b"]] - 1) * 100) if ok else None)
            dr.append(an.r4(p["dd"][i - p["b"]] * 100) if ok else None)
        perf[p["inst"].id], drawdown[p["inst"].id] = pf, dr
    idx = list(range(len(ds)))
    if len(ds) > MAX_POINTS:
        idx = sorted({int(k * (len(ds) - 1) / (MAX_POINTS - 1)) for k in range(MAX_POINTS)})
    series = {"dates": [ds[i].isoformat() for i in idx], "perf": {k: [v[i] for i in idx] for k, v in perf.items()}, "drawdown": {k: [v[i] for i in idx] for k, v in drawdown.items()}}
    # ---- correlation of returns on dates both instruments actually traded
    n = len(loaded)
    win = [{D: PX for D, PX in zip(p["D"][p["b"]:p["e"] + 1], p["PX"][p["b"]:p["e"] + 1]) if D >= start} for p in loaded]
    mat, ov, notes = [[None] * n for _ in range(n)], [[0] * n for _ in range(n)], []
    for i in range(n):
        mat[i][i], ov[i][i] = 1.0, max(0, len(win[i]) - 1)
        for j in range(i + 1, n):
            com = sorted(set(win[i]) & set(win[j]))
            ra = [win[i][com[k]] / win[i][com[k - 1]] - 1 for k in range(1, len(com))]
            rb = [win[j][com[k]] / win[j][com[k - 1]] - 1 for k in range(1, len(com))]
            ov[i][j] = ov[j][i] = len(ra)
            c = pearson(ra, rb) if len(ra) >= MIN_CORR else None
            mat[i][j] = mat[j][i] = None if c is None else round(c, 4)
            if len(ra) < MIN_CORR:
                notes.append(f"{loaded[i]['inst'].symbol} / {loaded[j]['inst'].symbol}: only {len(ra)} overlapping return observations (need {MIN_CORR}), so no correlation is shown.")
            elif c is None:
                notes.append(f"{loaded[i]['inst'].symbol} / {loaded[j]['inst'].symbol}: no variation in one series, so correlation is undefined.")
    # ---- fundamentals (as available from the configured provider)
    if fundamentals:
        def fund(p):
            try:
                f = svc.fundamentals(p["inst"].id)
                return {k: f.get(k) for k in ("available", "provider", "metrics", "sector", "industry", "reason")}
            except MarketError as e:
                return {"available": False, "provider": None, "metrics": {}, "sector": None, "industry": None, "reason": str(e)}
        with ThreadPoolExecutor(min(4, n)) as ex:
            for o, f in zip(out, [ex.submit(fund, p) for p in loaded]):
                o["fundamentals"] = f.result()
    today = datetime.now(timezone.utc).date()
    return {"range": rng, "window": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days}, "instruments": out, "series": series,
            "correlation": {"ids": [o["id"] for o in out], "matrix": mat, "overlap": ov, "min_returns": MIN_CORR, "notes": notes},
            "errors": errors, "warnings": warnings, "mixed_currency": len(curs) > 1, "cross_exchange": len({p["inst"].exchange for p in loaded}) > 1,
            "data_type_label": "Delayed (latest bar may be intraday)" if end >= today else "Historical end-of-day", "real_time": False,
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def quote_bars(svc, inst):
    """Recent daily bars for a watchlist quote; prefers FINBOT_QUOTE_PROVIDERS (default yahoo) so free quotas are not spent on lists."""
    key = ("quote", inst.id)
    hit = svc.cache.get(key)
    if hit:
        return hit
    names = [n.strip() for n in os.environ.get("FINBOT_QUOTE_PROVIDERS", "yahoo").split(",") if n.strip()]
    res = None
    for hp in svc.histories:
        if hp.name in names and hp.configured()[0]:
            try:
                res = (hp.history(inst, "1mo"), hp.name)
                break
            except MarketError:
                continue
    if res is None:
        bars, prov, _att = svc._raw(inst, "1mo")
        res = (bars, prov)
    svc.cache.set(key, res, 15 * 60)
    return res


def quotes(svc, ids):
    """Latest end-of-day price, previous close, daily change and 1-month change for up to 30 instruments; failures are listed per instrument."""
    refs = _dedupe(ids)
    if not refs:
        raise BadRequest("No instruments were given.")
    if len(refs) > MAX_QUOTES:
        raise BadRequest(f"At most {MAX_QUOTES} instruments per request.")
    today = datetime.now(timezone.utc).date().isoformat()

    def one(ref):
        inst = svc.resolve(ref)
        bars, prov = quote_bars(svc, inst)
        s = an.snapshot(bars["rows"])
        if not s:
            raise NoData(f"{inst.id} has fewer than two usable observations.", prov)
        return {"id": inst.id, "name": inst.name, "symbol": inst.symbol, "exchange": inst.exchange, "currency": bars.get("currency") or inst.currency,
                "price": s["price"], "date": s["date"], "prev_close": s["prev"], "change": an.r4(s["price"] - s["prev"]), "change_pct": s["chg"], "perf_1m": s["perf_1m"],
                "source": prov, "data_type_label": "Delayed (latest bar may be intraday)" if s["date"] >= today else "Historical end-of-day"}
    with ThreadPoolExecutor(min(4, len(refs))) as ex:
        futs = [(r, ex.submit(one, r)) for r in refs]
    out, errs = [], []
    for ref, f in futs:
        try:
            out.append(f.result())
        except MarketError as e:
            errs.append({"id": ref, **e.to_dict()})
    return {"quotes": out, "errors": errs, "real_time": False, "retrieved_at": datetime.now(timezone.utc).isoformat()}
