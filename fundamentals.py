"""Fundamentals providers (market cap, P/E, EPS, dividend yield, sector, industry). Each returns only what the provider
supplies; anything missing stays missing. Add a provider by writing a class with configured() and fundamentals(inst)."""
import json, os
from market.core import MarketError, MissingCredentials, NoData, Unsupported, env_key, http_get


# FINNHUB: keyed provider (FINNHUB_API_KEY). The free plan mainly covers US stocks; other markets usually need a paid plan.
class Finnhub:
    name, note = "finnhub", "Needs FINNHUB_API_KEY. Free plan mainly covers US stocks; international fundamentals usually need a paid plan."
    BASE = "https://finnhub.io/api/v1"
    SUFFIX = {"NSE": ".NS", "BSE": ".BO", "LSE": ".L", "TSX": ".TO", "XETRA": ".DE", "HKEX": ".HK", "TSE": ".T", "ASX": ".AX"}

    def configured(self):
        return (True, "") if env_key("FINNHUB_API_KEY") else (False, "Set FINNHUB_API_KEY to enable fundamentals.")

    def _get(self, path, params):
        try:
            return json.loads(http_get(self.BASE + path, self.name, {**params, "token": env_key("FINNHUB_API_KEY")}))
        except MarketError as e:
            if "HTTP 401" in str(e):
                raise MissingCredentials("Finnhub rejected the API key; check FINNHUB_API_KEY.", self.name)
            if "HTTP 403" in str(e):
                raise Unsupported("Your Finnhub plan does not include fundamentals for this instrument.", self.name)
            raise

    def fundamentals(self, inst):
        if inst.market == "United States":
            sym = inst.symbol
        elif inst.exchange in self.SUFFIX:
            sym = inst.symbol + self.SUFFIX[inst.exchange]
        else:
            raise Unsupported(f"No Finnhub symbol mapping is known for {inst.exchange}.", self.name)
        prof = self._get("/stock/profile2", {"symbol": sym}) or {}
        met = (self._get("/stock/metric", {"symbol": sym, "metric": "all"}) or {}).get("metric") or {}
        if not prof and not met:
            raise NoData(f"Finnhub has no fundamentals for {sym} (the free plan mainly covers US stocks).", self.name)

        def first(*keys):
            for k in keys:
                v = met.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return None
        cap = prof.get("marketCapitalization") if isinstance(prof.get("marketCapitalization"), (int, float)) else first("marketCapitalization")
        cur = prof.get("currency")
        m = {"market_cap": {"value": cap * 1e6, "currency": cur} if cap else None,  # Finnhub reports market cap in millions
             "pe_ttm": {"value": first("peTTM", "peBasicExclExtraTTM")} if first("peTTM", "peBasicExclExtraTTM") is not None else None,
             "eps_ttm": {"value": first("epsTTM", "epsBasicExclExtraItemsTTM"), "currency": cur} if first("epsTTM", "epsBasicExclExtraItemsTTM") is not None else None,
             "dividend_yield_pct": {"value": first("dividendYieldIndicatedAnnual")} if first("dividendYieldIndicatedAnnual") is not None else None}
        return {"metrics": m, "sector": None, "industry": prof.get("finnhubIndustry") or None}


FUNDAMENTALS = {"finnhub": Finnhub}
