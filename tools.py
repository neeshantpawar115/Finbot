"""Finance functions the AI may call, so the model never does arithmetic or invents market data itself.
Keep these in sync with the TOOLS object in index.html (used by the demo and platform providers)."""

# Instructions given to the model on every request.
SYSTEM = (
    "You are FinBot, a finance assistant for users in India. Use rupees and Indian grouping; understand lakh "
    "(1,00,000), crore (1,00,00,000), EMI, SIP, PPF, EPF, NPS, FD/RD, ELSS, GST, CIBIL, Nifty/Sensex, SEBI, RBI, demat. "
    "Rules: (1) Use tools for every calculation (emi, compound, expenses); never do arithmetic yourself. "
    "(2) For any stock price, return or market fact, call market_data first; if it errors, say the data is unavailable "
    "and explain concepts only. Never invent numbers or market facts. (3) If inputs are missing, ask or state clearly "
    "labelled assumptions. (4) Separate Facts, Calculations, Assumptions and Estimates; explain uncertainty; never "
    "guarantee returns; this is educational, not personal advice. (5) Be concise; use markdown and tables when comparing."
)


from market.core import MarketError


class ToolError(Exception):
    pass


def _r(x):
    return round(x, 2)


def emi(principal, annual_rate_pct, years=None, months=None):
    n = int(months or round((years or 0) * 12))
    if not (principal > 0 and n > 0 and annual_rate_pct >= 0):
        raise ToolError("principal, rate and tenure (years or months) must be positive")
    r = annual_rate_pct / 1200
    e = principal * r * (1 + r) ** n / ((1 + r) ** n - 1) if r else principal / n
    return {"monthly_emi": _r(e), "months": n, "total_payment": _r(e * n), "total_interest": _r(e * n - principal),
            "method": "reducing balance, monthly compounding"}


def compound(annual_rate_pct, years, principal=0, monthly_contribution=0):
    n, r = round(years * 12), annual_rate_pct / 1200
    if not (n > 0 and principal >= 0 and monthly_contribution >= 0 and (principal or monthly_contribution)):
        raise ToolError("need years>0 and a principal and/or monthly_contribution")
    b = principal
    for _ in range(n):
        b = b * (1 + r) + monthly_contribution
    put = principal + monthly_contribution * n
    return {"future_value": _r(b), "total_invested": _r(put), "growth": _r(b - put),
            "assumption": "constant annual return, compounded monthly, contributions at month end"}


def expenses(items):
    by = {}
    for i in items or []:
        if i.get("amount", 0) > 0:
            k = str(i.get("category") or "Other").strip()
            by[k] = by.get(k, 0) + float(i["amount"])
    if not by:
        raise ToolError("items needs [{category, amount}] with positive amounts")
    total = sum(by.values())
    rows = [{"category": k, "amount": _r(v), "share_pct": _r(v / total * 100)}
            for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    return {"total": _r(total), "by_category": rows, "largest": rows[0]["category"]}


def market_data(symbol, period="1y", exchange=None):
    """Looks the instrument up through the market-data service; errors are returned to the model so it can say data is unavailable."""
    from market import service
    return service().tool_summary(symbol, period, exchange)


def screen_stocks(**kw):
    """Runs the market-wide screener on the last stored scan; filters and sorting are deterministic code, not model guesses."""
    from market import service
    return service().screener.tool_screen(kw)


def compare_stocks(ids, range="1y"):
    """Compares 2-6 instruments on a common date window with deterministic code; the model must quote only these numbers."""
    from market import service
    c = service().compare(list(ids), range, True)
    keep = ("id", "name", "currency", "return_pct", "vol_ann_pct", "max_drawdown", "avg_volume", "observations", "source")
    return {"window": c["window"], "return_basis": "price return in each stock's own currency (adjusted where the provider supplies it)", "instruments": [{k: o[k] for k in keep} for o in c["instruments"]],
            "correlation": {"ids": c["correlation"]["ids"], "matrix": c["correlation"]["matrix"], "notes": c["correlation"]["notes"]}, "not_compared": [{"id": e["id"], "reason": e["message"]} for e in c["errors"]],
            "warnings": c["warnings"], "real_time": False, "note": "end-of-day data, not live; raw prices of different stocks or currencies must not be compared"}


_obj = lambda props, req: {"type": "object", "properties": props, "required": req}
_n = {"type": "number"}
# Registry: name -> (function, description, JSON schema) used by the API call and run_tool().
TOOLS = {
    "emi": (emi, "Compute a loan EMI. Convert lakh=100000, crore=10000000 to rupees first.",
            _obj({"principal": _n, "annual_rate_pct": _n, "years": _n, "months": _n}, ["principal", "annual_rate_pct"])),
    "compound": (compound, "Project future value of a lump sum and/or monthly SIP at a constant annual return.",
                 _obj({"principal": _n, "monthly_contribution": _n, "annual_rate_pct": _n, "years": _n},
                      ["annual_rate_pct", "years"])),
    "expenses": (expenses, "Summarise expenses by category with totals and shares.",
                 _obj({"items": {"type": "array", "items": _obj({"category": {"type": "string"}, "amount": _n},
                                                                  ["category", "amount"])}}, ["items"])),
    "screen_stocks": (screen_stocks, "Screen and rank stocks across exchanges that have been scanned (end-of-day data, not live). Convert the user's request into filters: "
                      "exchanges (NSE, BSE, NASDAQ, NYSE), perf_period (1M, 3M, 6M, 1Y) with perf_min/perf_max in percent, price_min/price_max, change_min/change_max (daily %), "
                      "vol_min/vol_max (annualised volatility %), mdd_min (max drawdown not worse than, e.g. -30), avgvol_min, mcap_min/mcap_max (millions), pe_min/pe_max, "
                      "sma_period (20/50/200) with sma_side (above/below), name_contains (no sector data for most stocks), sort_by "
                      "(perf_1m, perf_3m, perf_6m, perf_1y, vol, mdd, chg, price, avgvol, name) and sort_dir (asc/desc), limit (max 25). "
                      "Always tell the user the coverage statement and whether the scan is partial.",
                      _obj({"exchanges": {"type": "array", "items": {"type": "string"}}, "market": {"type": "string"}, "perf_period": {"type": "string"}, "perf_min": _n, "perf_max": _n,
                            "price_min": _n, "price_max": _n, "change_min": _n, "change_max": _n, "vol_min": _n, "vol_max": _n, "mdd_min": _n, "avgvol_min": _n,
                            "mcap_min": _n, "mcap_max": _n, "pe_min": _n, "pe_max": _n, "sma_period": _n, "sma_side": {"type": "string"}, "name_contains": {"type": "string"},
                            "sector": {"type": "string"}, "industry": {"type": "string"}, "sort_by": {"type": "string"}, "sort_dir": {"type": "string"}, "limit": _n}, [])),
    "compare_stocks": (compare_stocks, "Compare 2-6 stocks over a common date window: percentage return, volatility, maximum drawdown and return correlation. ids are instrument ids "
                       "such as NSE:RELIANCE or NASDAQ:AAPL (or plain tickers; if a ticker is ambiguous, ask which exchange). range: 1mo, 3mo, 6mo, 1y, 5y or max. "
                       "Never compare raw share prices across companies or currencies; report the normalised returns and the warnings.",
                       _obj({"ids": {"type": "array", "items": {"type": "string"}}, "range": {"type": "string"}}, ["ids"])),
    "market_data": (market_data, "Look up any listed stock and get its latest close, observation date, price change and data source. Call BEFORE stating any price or return. period: 1mo, 6mo, 1y, 5y or max. If a ticker is on several exchanges, pass exchange (NSE, BSE, NASDAQ, NYSE...).",
                    _obj({"symbol": {"type": "string"}, "period": {"type": "string"}, "exchange": {"type": "string"}}, ["symbol"])),
}


def schemas():
    return [{"name": n, "description": d, "input_schema": s} for n, (_, d, s) in TOOLS.items()]


def run_tool(name, args):
    """Runs one tool and returns its result or {'error': ...} so the model can explain failures honestly."""
    try:
        return TOOLS[name][0](**(args or {}))
    except (ToolError, MarketError, TypeError, KeyError, ValueError, ZeroDivisionError) as e:
        return {"error": str(e)}
