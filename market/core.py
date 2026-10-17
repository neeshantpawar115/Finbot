"""Core of the market-data system: models, errors, caching, HTTP, search, ticker resolution and the MarketService facade.
Providers (market/providers.py) only fetch raw data; this module never assumes which provider is behind them."""
import difflib, heapq, json, os, pathlib, re, socket, threading, time, urllib.error, urllib.parse, urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

CACHE_DIR = pathlib.Path(os.environ.get("FINBOT_CACHE_DIR", pathlib.Path(__file__).resolve().parent.parent / ".cache"))
RANGES = ("1mo", "6mo", "1y", "5y", "max")
DIR_TTL = 24 * 3600      # instrument directories are refreshed daily
HIST_TTL = 15 * 60       # price history is cached for 15 minutes


# ERRORS: each failure type has a stable code, HTTP status and retry hint so the UI can explain it precisely.
class MarketError(Exception):
    code, status, retryable = "provider_error", 502, False

    def __init__(self, message, provider=None, **extra):
        super().__init__(message)
        self.provider, self.extra = provider, extra

    def to_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable, "provider": self.provider, **self.extra}


class InvalidSymbol(MarketError): code, status = "invalid_symbol", 404
class AmbiguousSymbol(MarketError): code, status = "ambiguous_symbol", 409
class NoData(MarketError): code, status = "no_data", 404
class RateLimited(MarketError): code, status, retryable = "rate_limited", 429, True
class ProviderTimeout(MarketError): code, status, retryable = "timeout", 504, True
class MissingCredentials(MarketError): code, status = "missing_credentials", 503
class Unsupported(MarketError): code, status = "unsupported", 422


# EXCHANGE METADATA: market and currency are filled in only where they are certain; otherwise they stay None (never guessed).
EXCHANGES = {
    "NSE": ("India", "INR"), "BSE": ("India", "INR"), "NASDAQ": ("United States", "USD"), "NYSE": ("United States", "USD"),
    "NYSE American": ("United States", "USD"), "NYSE Arca": ("United States", "USD"), "Cboe BZX": ("United States", "USD"),
    "IEX": ("United States", "USD"), "LSE": ("United Kingdom", None), "TSX": ("Canada", None), "XETRA": ("Germany", None),
    "Frankfurt": ("Germany", None), "HKEX": ("Hong Kong", None), "TSE": ("Japan", None), "ASX": ("Australia", None),
    "SGX": ("Singapore", None), "Euronext Paris": ("France", None), "Euronext Amsterdam": ("Netherlands", None),
    "Borsa Italiana": ("Italy", None), "SIX": ("Switzerland", None), "KRX": ("South Korea", None),
    "TWSE": ("Taiwan", None), "B3": ("Brazil", None)}
EXCH_UP = {k.upper(): k for k in EXCHANGES}
MAIN_MARKETS = ("India", "United States")


@dataclass
class Instrument:
    id: str; symbol: str; name: str; exchange: str; market: str; currency: str = None; sector: str = None
    industry: str = None; isin: str = None; type: str = "equity"; provider: str = ""; provider_symbol: str = None
    status: str = None


def make_instrument(symbol, name, exchange, provider, **kw):
    market, cur = EXCHANGES.get(exchange, ("Other", None))
    kw.setdefault("currency", cur)
    return Instrument(id=f"{exchange}:{symbol}", symbol=symbol, name=name.strip(), exchange=exchange, market=market,
                      provider=provider, **kw)


# CACHING: a small in-memory TTL cache for history plus JSON files for directories (stale copies are reused if a refresh fails).
class TTLCache:
    def __init__(self):
        self.d, self.lock = {}, threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            return v[1] if v and v[0] > time.time() else None

    def set(self, k, val, ttl):
        with self.lock:
            self.d[k] = (time.time() + ttl, val)


def disk_load(name, max_age=None):
    try:
        j = json.loads((CACHE_DIR / f"{name}.json").read_text())
        if max_age is None or time.time() - j["saved_at"] < max_age:
            return j["data"], j["saved_at"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def disk_save(name, data):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{name}.json").write_text(json.dumps({"saved_at": time.time(), "data": data}))
    except OSError:
        pass


def env_key(name):
    """Reads an API key from the environment, ignoring stray spaces or quotes pasted around it."""
    return os.environ.get(name, "").strip().strip("\"'").strip()


# HTTP: one place that turns network problems into typed errors; FETCH can be replaced in tests.
FETCH = None


def _real_fetch(url, params, headers, timeout, body=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers or {}), timeout=timeout) as r:
        return r.read().decode("utf-8-sig", "replace")


def http_get(url, provider, params=None, headers=None, timeout=15, body=None):
    """GET, or POST when a body is given."""
    f = FETCH or _real_fetch
    try:
        return f(url, params, headers, timeout) if body is None else f(url, params, headers, timeout, body)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"{provider} rate limit reached. Wait a minute and retry, or configure another provider.",
                              provider, retry_after=e.headers.get("Retry-After") if e.headers else None)
        if e.code in (401, 403):
            raise MarketError(f"{provider} refused the request (HTTP {e.code}). Credentials or its access policy may be the cause.", provider)
        raise MarketError(f"{provider} returned HTTP {e.code}.", provider)
    except (socket.timeout, TimeoutError):
        raise ProviderTimeout(f"{provider} did not respond within {timeout}s.", provider)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise ProviderTimeout(f"{provider} did not respond within {timeout}s.", provider)
        raise MarketError(f"Could not reach {provider}: {e.reason}", provider)


# SEARCH HELPERS: query parsing (NSE:RELIANCE, RELIANCE.NS) and relevance scoring across symbol, name, sector and industry.
def tokens(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()


def parse_query(q):
    q, ex = (q or "").strip(), None
    m = re.match(r"^([A-Za-z ]+?)\s*:\s*(.+)$", q)
    if m and m.group(1).strip().upper() in EXCH_UP:
        ex, q = EXCH_UP[m.group(1).strip().upper()], m.group(2)
    for suf, e in ((".NS", "NSE"), (".BO", "BSE")):
        if q.upper().endswith(suf):
            q, ex = q[:-len(suf)], e
    return q.strip(), ex


def score(i, q, toks):
    sym, qn, name = i.symbol.lower(), " ".join(toks), " ".join(tokens(i.name))
    nt = name.split()
    if sym == q.lower():
        s = 100
    elif name == qn:
        s = 95
    elif sym.startswith(q.lower()) and len(q) >= 2:
        s = 85 - min(len(sym) - len(q), 10)
    elif name.startswith(qn):
        s = 80
    elif all(any(w.startswith(t) for w in nt) for t in toks):
        s = 60
    elif qn in name:
        s = 45
    elif all(t in " ".join(tokens(f"{i.sector} {i.industry}")) for t in toks):
        s = 20
    else:
        return 0
    return s - (8 if i.type != "equity" else 0)


def _public(i, **extra):
    return {**asdict(i), **extra}


# SERVICE: loads instrument directories, searches them, resolves tickers and returns quality-checked price history.
class MarketService:
    def __init__(self, directories, searchers, histories, fundamentals=()):
        """histories is a list (used for every market) or {market: [providers], '*': [providers]} for per-market routing."""
        self.dirs, self.searchers, self.fprov = directories, searchers, list(fundamentals)
        self.routes = histories if isinstance(histories, dict) else {"*": list(histories)}
        self.histories = list({id(p): p for r in self.routes.values() for p in r}.values())
        self.cache, self.lock = TTLCache(), threading.Lock()
        self.parts, self.dstate, self.extra = {}, {}, {}
        self.index, self.by_id, self.by_symbol, self.by_isin = [], {}, {}, {}
        self.loading, self.started = False, False
        from .screener import Screener
        self.screener = Screener(self)

    # --- instrument discovery
    def ensure_loaded(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.load_directories, daemon=True).start()

    def load_directories(self, force=False):
        self.loading = True
        for p in self.dirs:
            st = self.dstate[p.name] = {"state": "loading", "count": 0, "message": ""}
            ok, why = p.configured()
            if not ok:
                st.update(state="not_configured", message=why)
                continue
            try:
                hit = None if force else disk_load(f"dir_{p.name}", DIR_TTL)
                if hit:
                    items, saved = [Instrument(**d) for d in hit[0]], hit[1]
                    st.update(state="ok", source="cache")
                else:
                    items, saved = p.load(), time.time()
                    disk_save(f"dir_{p.name}", [asdict(i) for i in items])
                    st.update(state="ok", source="live")
            except MarketError as e:
                old = disk_load(f"dir_{p.name}")
                if old:
                    items, saved = [Instrument(**d) for d in old[0]], old[1]
                    st.update(state="stale", message=f"Refresh failed ({e}); using a saved copy.", source="cache")
                else:
                    st.update(state="error", message=str(e), code=e.code)
                    continue
            st.update(count=len(items), loaded_at=datetime.fromtimestamp(saved, timezone.utc).isoformat())
            self.parts[p.name] = items
            self._rebuild()
        self.loading = False

    def _rebuild(self):
        idx = [i for part in list(self.parts.values()) for i in part]
        by_id, by_sym, by_isin = {}, {}, {}
        for i in idx:
            by_id.setdefault(i.id, i)
            by_sym.setdefault(i.symbol.upper(), []).append(i)
            if i.isin:
                by_isin.setdefault(i.isin, []).append(i)
        self.index, self.by_id, self.by_symbol, self.by_isin = idx, by_id, by_sym, by_isin

    def _ok(self, i, market, exchange):
        if exchange not in (None, "all") and i.exchange != exchange:
            return False
        if market in (None, "all"):
            return True
        return (i.market not in MAIN_MARKETS) if market == "Other" else i.market == market

    def search(self, q, market="all", exchange="all", limit=10):
        text, hint = parse_query(q)
        exchange = hint or exchange
        limit, warnings = max(1, min(int(limit), 50)), []
        base = {"query": text, "warnings": warnings, "loading": self.loading, "indexed": len(self.index)}
        if not text:
            return {**base, "results": []}
        toks = tokens(text)
        scored = ((score(i, text, toks), i) for i in self.index if self._ok(i, market, exchange))
        pri = {"NSE": 3, "NASDAQ": 3, "NYSE": 3, "BSE": 2}  # ties prefer the main listing venue
        top = heapq.nlargest(limit, ((s, pri.get(i.exchange, 1), -len(i.name), n, i) for n, (s, i) in enumerate(scored) if s > 0))
        found = [t[4] for t in top]
        if len(found) < limit and len(text) >= 2:
            seen = {(i.exchange, i.symbol.upper()) for i in found}
            for sp in self.searchers:
                try:
                    key = ("live", sp.name, text.lower(), limit)
                    live = self.cache.get(key)
                    if live is None:
                        live = sp.search(text, limit)
                        self.cache.set(key, live, 300)  # repeated keystrokes/queries reuse results to avoid rate limits
                    for i in live:
                        if (i.exchange, i.symbol.upper()) not in seen and self._ok(i, market, exchange):
                            seen.add((i.exchange, i.symbol.upper()))
                            self.extra[i.id] = i
                            found.append(i)
                except MarketError as e:
                    warnings.append({"provider": sp.name, "code": e.code, "message": str(e)})
        out = []
        for i in found[:limit]:
            also = [o.id for o in self.by_isin.get(i.isin, []) if o.id != i.id] if i.isin else []
            same = sorted({o.exchange for o in self.by_symbol.get(i.symbol.upper(), []) if o.id != i.id})
            out.append(_public(i, also_listed=also, same_symbol_on=same))
        return {**base, "results": out}

    # --- ticker resolution
    def resolve(self, ref, exchange=None):
        text, hint = parse_query(ref)
        exchange = hint or (exchange if exchange not in (None, "all") else None)
        if not text:
            raise InvalidSymbol("No symbol was provided.")
        for key in (ref, f"{exchange}:{text}" if exchange else None):
            if key and (key in self.by_id or key in self.extra):
                return self.by_id.get(key) or self.extra[key]
        cands = [i for i in self.by_symbol.get(text.upper(), []) if exchange in (None, i.exchange)]
        if not cands:
            for sp in self.searchers:
                try:
                    for i in sp.search(text, 10):
                        if i.symbol.upper() == text.upper() and exchange in (None, i.exchange):
                            self.extra[i.id] = i
                            cands.append(i)
                except MarketError:
                    pass
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise AmbiguousSymbol(f"'{text}' is listed on several exchanges ({', '.join(c.exchange for c in cands)}). Specify one, e.g. {cands[0].id}.",
                                  candidates=[{"id": c.id, "name": c.name, "exchange": c.exchange} for c in cands])
        elsewhere = self.by_symbol.get(text.upper(), [])
        if elsewhere and exchange:
            raise InvalidSymbol(f"'{text}' is not listed on {exchange} in the indexed data; it is listed on {', '.join(o.exchange for o in elsewhere)}. Try {elsewhere[0].id}.",
                                suggestions=[o.id for o in elsewhere])
        syms = list(self.by_symbol)
        close = difflib.get_close_matches(text.upper(), syms, n=3, cutoff=0.7)
        raise InvalidSymbol(f"No instrument found for '{text}'." + (f" Did you mean {', '.join(close)}?" if close else ""),
                            suggestions=close)

    # --- history
    def _route(self, inst):
        return self.routes.get(inst.market) or self.routes["*"]

    def _fetch_history(self, inst, rng):
        attempts, errors = [], []
        for hp in self._route(inst):
            ok, why = hp.configured()
            if not ok:
                e = MissingCredentials(why, hp.name)
            else:
                try:
                    bars = hp.history(inst, rng)
                    attempts.append({"provider": hp.name, "status": "ok"})
                    return bars, hp.name, attempts
                except MarketError as err:
                    e = err
            errors.append(e)
            attempts.append({"provider": hp.name, "status": e.code, "message": str(e)})
        rank = [NoData, RateLimited, ProviderTimeout, Unsupported, MissingCredentials]
        best = min(errors, key=lambda e: next((n for n, c in enumerate(rank) if isinstance(e, c)), 9)) if errors else \
            MarketError("No history provider is configured.")
        raise type(best)(str(best), best.provider, attempts=attempts)

    def _raw(self, inst, rng):
        """Fetches (or reuses a cached copy of) the raw price history so several analyses share one provider call."""
        key = ("raw", inst.id, rng)
        hit = self.cache.get(key)
        if hit:
            return hit
        res = self._fetch_history(inst, rng)
        self.cache.set(key, res, HIST_TTL)
        return res

    def analysis(self, ref, rng="1y"):
        """Returns the full deterministic analysis (performance, risk, indicators, chart series) for any resolvable instrument."""
        from . import analysis as an
        rng = rng if rng in an.WINDOWS else "1y"
        inst = self.resolve(ref)
        hit = self.cache.get(("analysis", inst.id, rng))
        if hit:
            return {**hit, "cached": True}
        bars, provider, attempts = self._raw(inst, an.fetch_range(rng))
        out = an.compute(asdict(inst), bars, provider, attempts, rng)
        self.cache.set(("analysis", inst.id, rng), out, HIST_TTL)
        return out

    def fundamentals(self, ref):
        """Returns provider-supplied fundamentals, or available=False with the reason; sector/industry fall back to the directory data."""
        inst = self.resolve(ref)
        hit = self.cache.get(("fund", inst.id))
        if hit:
            return {**hit, "cached": True}
        out = {"instrument": asdict(inst), "available": False, "provider": None, "metrics": {}, "sector": inst.sector, "industry": inst.industry,
               "sector_source": inst.provider if (inst.sector or inst.industry) else None, "reason": None, "attempts": [], "cached": False}
        for fp in self.fprov:
            ok, why = fp.configured()
            if not ok:
                out["attempts"].append({"provider": fp.name, "status": "missing_credentials", "message": why})
                continue
            try:
                f = fp.fundamentals(inst)
                out.update(available=True, provider=fp.name, metrics=f["metrics"], retrieved_at=datetime.now(timezone.utc).isoformat())
                for k in ("sector", "industry"):
                    if f.get(k):
                        out[k], out["sector_source"] = f[k], fp.name
                out["attempts"].append({"provider": fp.name, "status": "ok"})
                break
            except MarketError as e:
                out["attempts"].append({"provider": fp.name, "status": e.code, "message": str(e)})
        if not out["available"]:
            out["reason"] = out["attempts"][-1]["message"] if out["attempts"] else "No fundamentals provider is configured (set FINNHUB_API_KEY)."
        self.cache.set(("fund", inst.id), out, 6 * 3600 if out["available"] else 300)
        return out

    def compare(self, ids, rng="1y", fundamentals=True):
        """Aligned comparison of 2-6 instruments (see compare.py); cached briefly because it can involve several provider calls."""
        from . import compare as cp
        key = ("cmp", tuple(str(i).strip() for i in ids), rng, bool(fundamentals))
        hit = self.cache.get(key)
        if hit:
            return {**hit, "cached": True}
        out = cp.compare(self, ids, rng, fundamentals)
        self.cache.set(key, out, HIST_TTL)
        return out

    def quotes(self, ids):
        """Latest end-of-day quotes for a watchlist."""
        from . import compare as cp
        return cp.quotes(self, ids)

    def stock(self, ref, rng="1y"):
        rng = rng if rng in RANGES else "1y"
        inst = self.resolve(ref)
        hit = self.cache.get((inst.id, rng))
        if hit:
            return {**hit, "cached": True}
        bars, provider, attempts = self._raw(inst, rng)
        out = package(inst, bars, provider, attempts, rng)
        self.cache.set((inst.id, rng), out, HIST_TTL)
        return out

    def tool_summary(self, symbol, period="1y", exchange=None):
        """Compact, deterministic facts for the chat model; every number comes from the analysis engine."""
        inst = self.resolve(symbol, exchange)
        a = self.analysis(inst.id, period if period in ("1mo", "3mo", "6mo", "1y", "5y", "max") else "1y")
        ind, risk = a["indicators"], a["risk"]
        return {"instrument": f"{inst.name} ({inst.id})", "range": a["range"], "source": a["source"]["provider"], "real_time": False,
                "note": "end-of-day data, not live; returns are " + a["return_basis"], "currency": a["currency"], "data_type_label": a["data_type_label"],
                "latest_close": a["price"]["latest"], "latest_observation": a["latest_observation"], "previous_close": a["price"]["previous_close"],
                "day_change_pct": a["price"]["change_pct"], "change_pct": a["range_return_pct"], "first_observation": a["window_start"],
                "observations": a["observations_window"], "performance_pct": {p["period"]: p["return_pct"] for p in a["performance"]},
                "annualised_volatility_pct": risk["annualised_vol_pct"], "max_drawdown": risk["max_drawdown"],
                "sma20": ind["sma20"]["value"], "sma50": ind["sma50"]["value"], "sma200": ind["sma200"]["value"], "rsi14": ind["rsi14"]["value"],
                "history_high_low": a["history_high_low"], "warnings": a["warnings"]}

    def status(self):
        counts = {}
        for i in self.index:
            c = counts.setdefault(i.exchange, {"code": i.exchange, "market": i.market, "currency": i.currency, "count": 0})
            c["count"] += 1
        roles = [(p, "directory") for p in self.dirs] + [(p, "search") for p in self.searchers] + [(p, "history") for p in self.histories] + [(p, "fundamentals") for p in self.fprov]
        provs = []
        for p, role in roles:
            mk = [("Other markets" if m == "*" else m) for m, r in self.routes.items() if p in r] if role == "history" else None
            ok, why = p.configured()
            provs.append({"name": p.name, "role": role, "configured": ok, "message": "" if ok else why,
                          **({k: v for k, v in self.dstate.get(p.name, {}).items()} if role == "directory" else {}),
                          "note": getattr(p, "note", ""), **({"markets": mk} if mk is not None else {})})
        return {"providers": provs, "exchanges": sorted(counts.values(), key=lambda c: -c["count"]), "indexed": len(self.index),
                "loading": self.loading, "real_time": False,
                "routes": {m: [p.name for p in r] for m, r in self.routes.items()},
                "limits": ["No provider here supplies real-time prices; history is end-of-day and the newest bar can be delayed.",
                           "Sector/industry is only known for instruments found through live search or a BSE file with an Industry column.",
                           "International coverage comes from live search (Yahoo, unofficial) and is not a complete exchange list.",
                           "Fundamentals need a provider key (FINNHUB_API_KEY); the free plan mainly covers US stocks.",
                           "Set ALPHAVANTAGE_API_KEY or add a licensed provider class for broader, supported coverage."]}


# PACKAGING: validates a raw price series (missing values, gaps, stale or delisted securities) and builds the API response.
def package(inst, bars, provider, attempts, rng):
    rows = sorted({r["d"]: r for r in bars["rows"] if r.get("c") is not None}.values(), key=lambda r: r["d"])
    if not rows:
        raise NoData(f"{provider} returned no usable prices for {inst.id}.", provider, attempts=attempts)
    ds = [date.fromisoformat(r["d"]) for r in rows]
    gaps = [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days} for a, b in zip(ds, ds[1:]) if (b - a).days > 5]
    today, warns = datetime.now(timezone.utc).date(), []
    age = (today - ds[-1]).days
    if age > 10:
        warns.append(f"The latest observation is {age} days old; the security may be delisted, suspended or very illiquid.")
    if bars.get("missing"):
        warns.append(f"{bars['missing']} observation(s) were missing at the provider and were skipped, not filled in.")
    if gaps:
        warns.append(f"{len(gaps)} gap(s) longer than 5 days in the series (largest {max(g['days'] for g in gaps)} days).")
    if len(rows) < 2:
        warns.append("Fewer than two observations; changes cannot be computed.")
    delayed = ds[-1] >= today
    pts = [[r["d"], r["c"]] for r in rows]
    if len(pts) > 400:
        step = len(pts) / 400
        pts = [pts[int(k * step)] for k in range(400)] + [pts[-1]]
    first, last = rows[0]["c"], rows[-1]["c"]
    return {"instrument": asdict(inst), "range": rng, "source": {"provider": provider, "note": bars.get("note", "")},
            "data_type": "delayed" if delayed else "historical",
            "data_type_label": "Delayed (latest bar may be intraday)" if delayed else "Historical end-of-day",
            "real_time": False, "currency": bars.get("currency") or inst.currency,
            "latest_observation": rows[-1]["d"], "first_observation": rows[0]["d"], "observations": len(rows),
            "latest_close": last, "change_pct": round((last / first - 1) * 100, 2) if len(rows) > 1 and first else None,
            "missing_values": bars.get("missing", 0), "gaps": gaps[:5], "gap_count": len(gaps), "warnings": warns,
            "adjusted_close_available": bool(bars.get("adjusted")), "bars": pts,
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts, "cached": False}


# CONFIGURATION: builds the service from environment variables, so providers can be swapped without touching the UI.
_svc = None


def service():
    global _svc
    if _svc is None:
        from .providers import PROVIDERS
        from .fundamentals import FUNDAMENTALS
        made = {}
        make = lambda n: made.setdefault(n, PROVIDERS[n]())  # one shared instance per provider (keeps logins/tokens)
        pick = lambda var, dflt: [make(n.strip()) for n in os.environ.get(var, dflt).split(",") if n.strip()]
        # Price routing: FINBOT_HISTORY_INDIA / _US / _OTHER, else FINBOT_HISTORY for all markets, else the defaults below.
        route = lambda var, dflt: [make(n.strip()) for n in (os.environ.get(var) or os.environ.get("FINBOT_HISTORY") or dflt).split(",") if n.strip()]
        _svc = MarketService(pick("FINBOT_DIRECTORIES", "nasdaqtrader,nse,bse_file"), pick("FINBOT_SEARCH", "yahoo"), {
            "India": route("FINBOT_HISTORY_INDIA", "angelone,yahoo"),
            "United States": route("FINBOT_HISTORY_US", "twelvedata,yahoo,alphavantage"),
            "*": route("FINBOT_HISTORY_OTHER", "twelvedata,yahoo,alphavantage")}, fundamentals=[FUNDAMENTALS[n.strip()]() for n in os.environ.get("FINBOT_FUNDAMENTALS", "finnhub").split(",") if n.strip()])
    return _svc
