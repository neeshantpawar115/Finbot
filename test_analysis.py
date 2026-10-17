"""Verifies the analysis maths against hand-computed values, then the full workflow on several instruments with mocked providers."""
import datetime as dt, json, math, os, socket, statistics, sys, tempfile, threading, urllib.error, urllib.request
sys.path.insert(0, '.')
os.environ["FINBOT_CACHE_DIR"] = tempfile.mkdtemp()
for k in ("FINBOT_HISTORY", "FINBOT_HISTORY_INDIA", "FINBOT_HISTORY_US", "FINBOT_HISTORY_OTHER", "TWELVEDATA_API_KEY", "ANGELONE_API_KEY", "ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "BSE_SCRIP_FILE", "LLM_BASE_URL"):
    os.environ.pop(k, None)
import market.core as core
from market.analysis import sma, rsi, daily_returns, ann_vol, drawdowns, compute, rolling_vol
t = lambda n: print("ok  ", n)
close = lambda a, b, tol=1e-9: abs(a - b) <= tol
# --- indicator maths
assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]; t("SMA: None until the window is full, then exact means")
W = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
R = rsi(W); assert R[:14] == [None] * 14 and close(R[14], 70.46, 0.1) and close(R[15], 66.25, 0.15), R[14:17]; t("RSI(14) Wilder: matches the textbook worked example (first values ~70.5, ~66.3)")
assert rsi([1, 2, 3, 4, 5] * 4)[14] is not None and rsi(list(range(20)))[-1] == 100.0 and rsi([5.0] * 20)[-1] == 50.0 and rsi([1, 2, 3]) == [None] * 3; t("RSI edge cases: all gains=100, flat=50, too short=None")
px = [100, 120, 90, 95, 130]; dates = [f"2026-01-0{i + 1}" for i in range(5)]
dd, m = drawdowns(px, dates); assert close(m["pct"], -25.0) and m["peak_date"] == "2026-01-02" and m["trough_date"] == "2026-01-03" and m["recovery_date"] == "2026-01-05" and m["recovered"]; assert close(dd[2], -0.25)
dd, m = drawdowns([100, 120, 90, 95], dates[:4]); assert m["recovery_date"] is None and not m["recovered"]; dd, m = drawdowns([1, 2, 3], dates[:3]); assert m["pct"] == 0 and m["recovered"]; t("max drawdown: peak, trough, recovery date, unrecovered, none")
alt = [100 * (1.01 if i % 2 == 0 else 0.99) for i in range(1, 41)]; rets = [0.01 if i % 2 == 0 else -0.01 for i in range(40)]
v = ann_vol(rets); assert close(v[0], statistics.stdev(rets) * 100) and close(v[1], statistics.stdev(rets) * math.sqrt(252) * 100) and ann_vol(rets[:19]) is None; t("volatility: sample stdev, annualised by sqrt(252), None under 20 returns")
r = daily_returns([100, 110, 99]); assert r[0] is None and close(r[1], .1) and close(r[2], -.1); t("daily returns")
# --- compute() on synthetic data
today = dt.date.today()
def rows(closes, start=None, vol=1000, adj=True, end=None):
    end = end or today - dt.timedelta(days=1); out = []
    d = end - dt.timedelta(days=len(closes) - 1)
    for c in closes:
        out.append({"d": d.isoformat(), "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": vol, "ac": c if adj else None}); d += dt.timedelta(days=1)
    return out
inst = {"id": "NASDAQ:TEST", "symbol": "TEST", "currency": "USD"}
C = [100 * 1.01 ** i for i in range(300)]
a = compute(inst, {"rows": rows(C), "missing": 0, "currency": "USD", "adjusted": True}, "yahoo", [], "1y")
assert close(a["price"]["change_pct"], 1.0, 1e-3) and close(a["price"]["latest"], C[-1]) and close(a["price"]["previous_close"], C[-2]) and close(a["price"]["change"], C[-1] - C[-2], 1e-3); t("latest, previous close, daily change (abs and %)")
assert close(a["range_return_pct"], (C[-1] / C[0] - 1) * 100, 1e-3)  # 300 days of data all fall inside the 1y window
assert close(a["risk"]["daily_vol_pct"], 0, 1e-6) and a["risk"]["max_drawdown"]["pct"] == 0; t("constant-growth series: zero volatility, zero drawdown")
assert close(a["indicators"]["sma200"]["value"], sum(C[-200:]) / 200, 1e-3) and a["indicators"]["sma20"]["price_vs"] == "above" and a["indicators"]["rsi14"]["value"] == 100.0 and "overbought" in a["indicators"]["rsi14"]["zone"]; t("SMA20/50/200 and RSI from full history")
s = a["series"]; n = len(s["dates"]); assert all(len(s[k]) == n for k in s) and s["dates"][-1] == a["latest_observation"] and s["sma200"][-1] is not None and close(s["cum_return"][0], 0) and s["daily_return"][0] is None; t("chart series are aligned arrays; cumulative return starts at 0")
# windows: 1mo shows ~a month of dates but indicators still use full history
a1 = compute(inst, {"rows": rows(C), "missing": 0, "adjusted": True}, "yahoo", [], "1mo")
assert 22 <= a1["observations_window"] <= 33 and a1["window_start"] >= (today - dt.timedelta(days=33)).isoformat() and a1["indicators"]["sma200"]["value"] == a["indicators"]["sma200"]["value"] and a1["series"]["sma200"][0] is not None; t("1M window: short date range, indicators still computed from prior history")
assert [p["period"] for p in a["performance"]] == ["1M", "3M", "6M", "1Y", "5Y", "Max"]; pp = {p["period"]: p for p in a["performance"]}
assert pp["1M"]["return_pct"] is not None and pp["1Y"]["return_pct"] is not None and pp["5Y"]["return_pct"] is None and "History starts" in pp["5Y"]["reason"] and pp["Max"]["return_pct"] is not None and close(pp["Max"]["return_pct"], (C[-1] / C[0] - 1) * 100, 1e-3); t("period performance: available where covered, reason where not")
short = compute(inst, {"rows": rows(C[:40]), "missing": 0, "adjusted": True}, "yahoo", [], "5y")
assert short["indicators"]["sma200"]["value"] is None and "Needs 200" in short["indicators"]["sma200"]["reason"] and short["indicators"]["sma20"]["value"] is not None and any("shorter than" in w for w in short["warnings"]); t("insufficient history: SMA200 unavailable with reason, warning shown")
tiny = compute(inst, {"rows": rows(C[:10]), "missing": 0, "adjusted": True}, "yahoo", [], "1y"); assert tiny["risk"]["annualised_vol_pct"] is None and "at least 20" in tiny["risk"]["reason"] and tiny["indicators"]["rsi14"]["value"] is None; t("too few points: volatility and RSI unavailable, not estimated")
one = compute(inst, {"rows": rows(C[:1]), "missing": 0}, "yahoo", [], "1mo"); assert one["price"]["previous_close"] is None and one["range_return_pct"] is None and one["risk"]["max_drawdown"] is None and any("Fewer than two" in w for w in one["warnings"]); t("single observation handled")
nov = compute(inst, {"rows": rows(C[:60], vol=None), "missing": 0, "adjusted": True}, "yahoo", [], "1y"); assert nov["volume"]["available"] is False and nov["volume"]["reason"] and all(x is None for x in nov["series"]["volume"]); t("no volume data reported as unavailable")
spl = [100] * 30 + [50] * 30; sp = compute(inst, {"rows": rows(spl, adj=False), "missing": 0, "adjusted": False}, "yahoo", [], "1y"); assert any("unadjusted split" in w for w in sp["warnings"]) and "unadjusted" in sp["return_basis"]; t("suspected unadjusted split flagged when no adjusted close")
sp2 = compute(inst, {"rows": rows(spl, adj=True), "missing": 0, "adjusted": True}, "yahoo", [], "1y"); assert not any("split" in w for w in sp2["warnings"]) and "adjusted close" in sp2["return_basis"]; t("adjusted-close basis used when available")
old = compute(inst, {"rows": rows(C[:60], end=today - dt.timedelta(days=90)), "missing": 3, "adjusted": True}, "yahoo", [], "1y"); assert any("delisted" in w for w in old["warnings"]) and any("missing" in w for w in old["warnings"]) and old["data_type"] == "historical"; t("stale series and missing observations disclosed")
td = compute(inst, {"rows": rows(C[:60], end=today), "missing": 0, "adjusted": True}, "yahoo", [], "1y"); assert td["data_type"] == "delayed" and td["real_time"] is False; t("today's bar labelled delayed; never real-time")
big = compute(inst, {"rows": rows([100 + (i % 7) for i in range(4000)], end=today - dt.timedelta(days=1)), "missing": 0, "adjusted": True}, "yahoo", [], "max"); assert len(big["series"]["dates"]) <= 1500 and big["series"]["dates"][-1] == big["latest_observation"] and big["observations_total"] == 4000; t("long history downsampled for charting, latest point kept")
j = compute(inst, {"rows": rows([100, 105, 103, 108]), "missing": 0, "adjusted": True}, "yahoo", [], "max"); assert j["range_high_low"]["high"] == 108 * 1.01 and j["history_high_low"]["low"] == 100 * 0.99; t("highs and lows use intraday high/low when provided")
# --- full workflow through the service on several instruments
NAS = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\nAAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\nNVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N\nFile Creation Time: x|||||||\n"
OTH = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nJPM|JPMorgan Chase|N|JPM|N|100|N|JPM\nFile Creation Time: x|||||||\n"
NSE = "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nRELIANCE,Reliance Industries Limited,EQ,x,10,1,INE002A01018,10\nTATAMOTORS,Tata Motors Limited,EQ,x,2,1,INE155A01022,2\nHDFCBANK,HDFC Bank Limited,EQ,x,1,1,INE040A01034,1\n"
def chart(base, n, cur, slope, vol=True):
    ts = [int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n - i + 1)).replace(hour=10).timestamp()) for i in range(n)]
    cl = [base * (1 + slope) ** i * (1 + (0.02 if i % 5 == 0 else -0.01)) for i in range(n)]
    return {"chart": {"result": [{"meta": {"currency": cur, "gmtoffset": 0}, "timestamp": ts, "indicators": {"quote": [{"open": cl, "high": cl, "low": cl, "close": cl, "volume": [5000 + i for i in range(n)] if vol else [None] * n}], "adjclose": [{"adjclose": cl}]}}], "error": None}}
CH = {"AAPL": chart(150, 320, "USD", .001), "NVDA": chart(400, 320, "USD", .004), "RELIANCE.NS": chart(2500, 220, "INR", -.0008), "TATAMOTORS.NS": chart(700, 90, "INR", .002, vol=False), "HDFCBANK.NS": chart(1500, 320, "INR", .0005), "JPM": chart(200, 320, "USD", .0007)}
calls = []; FUND = {}
def fetch(url, params, headers, timeout, body=None):
    calls.append(url)
    if "nasdaqlisted" in url: return NAS
    if "otherlisted" in url: return OTH
    if "EQUITY_L" in url: return NSE
    if "finance/search" in url: return json.dumps({"quotes": []})
    if "finance/chart" in url:
        sym = urllib.parse.unquote(url.split("/chart/")[1]); 
        if sym not in CH: raise AssertionError(sym)
        return json.dumps(CH[sym])
    if "profile2" in url: return json.dumps(FUND.get("profile", {}))
    if "stock/metric" in url: return json.dumps(FUND.get("metric", {"metric": {}}))
    raise AssertionError(url)
import urllib.parse
core.FETCH = fetch; os.environ["FINBOT_HISTORY"] = "yahoo"; core._svc = None
svc = core.service(); svc.load_directories(force=True)
res = {}
for ref in ("NASDAQ:AAPL", "NASDAQ:NVDA", "NSE:RELIANCE", "NSE:TATAMOTORS", "NSE:HDFCBANK", "NYSE:JPM"):
    res[ref] = svc.analysis(ref, "1y")
assert len({r["price"]["latest"] for r in res.values()}) == 6 and len({r["instrument"]["id"] for r in res.values()}) == 6; t("six different instruments (US + India) analysed dynamically; results differ per instrument")
assert res["NSE:RELIANCE"]["currency"] == "INR" and res["NASDAQ:AAPL"]["currency"] == "USD" and all(r["real_time"] is False and r["source"]["provider"] == "yahoo" for r in res.values()); t("currency, source and not-real-time carried through for each")
assert res["NSE:RELIANCE"]["range_return_pct"] < 0 < res["NASDAQ:NVDA"]["range_return_pct"]; t("negative and positive performance computed per instrument")
assert res["NSE:TATAMOTORS"]["indicators"]["sma200"]["value"] is None and res["NSE:TATAMOTORS"]["volume"]["available"] is False and res["NASDAQ:AAPL"]["indicators"]["sma200"]["value"] is not None; t("short-history/no-volume instrument degrades gracefully; others unaffected")
n0 = len(calls); r2 = {rng: svc.analysis("NASDAQ:AAPL", rng) for rng in ("1mo", "3mo", "6mo", "1y", "5y", "max")}
assert len(calls) == n0 + 1 or len(calls) == n0 + 2, len(calls) - n0  # 5y and max are the only extra provider calls
ws = [r2[k]["observations_window"] for k in ("1mo", "3mo", "6mo", "1y", "5y", "max")]; assert ws == sorted(ws) and ws[0] < ws[1] < ws[2] < ws[3]; assert r2["1mo"]["window_start"] > r2["1y"]["window_start"] >= r2["5y"]["window_start"]; t("all six ranges: window grows with range, dates update, provider calls shared")
assert svc.analysis("NASDAQ:AAPL", "1y")["cached"] and svc.analysis("NASDAQ:AAPL", "bogus")["range"] == "1y"; t("analysis cached; unknown range falls back to 1y")
try: svc.analysis("ZZZNOPE", "1y"); raise SystemExit("x")
except core.MarketError as e: assert e.code == "invalid_symbol"; t("invalid symbol -> typed error")
# fundamentals
f = svc.fundamentals("NASDAQ:AAPL"); assert f["available"] is False and "FINNHUB_API_KEY" in f["reason"] and f["metrics"] == {} and f["attempts"][0]["status"] == "missing_credentials"; t("no key: fundamentals unavailable with reason; nothing invented")
os.environ["FINNHUB_API_KEY"] = "FK"; svc.cache.d.clear()
FUND.update(profile={"marketCapitalization": 3000000.0, "currency": "USD", "finnhubIndustry": "Technology", "name": "Apple"}, metric={"metric": {"peTTM": 31.2, "epsTTM": 6.5, "dividendYieldIndicatedAnnual": 0.45}})
f = svc.fundamentals("NASDAQ:AAPL"); m = f["metrics"]; assert f["available"] and f["provider"] == "finnhub" and m["market_cap"]["value"] == 3e12 and m["market_cap"]["currency"] == "USD" and m["pe_ttm"]["value"] == 31.2 and m["eps_ttm"]["value"] == 6.5 and m["dividend_yield_pct"]["value"] == 0.45 and f["industry"] == "Technology"; t("fundamentals from provider: market cap (millions converted), P/E, EPS, yield, industry")
FUND.update(profile={"marketCapitalization": 100.0}, metric={"metric": {"peTTM": 12}}); svc.cache.d.clear(); f = svc.fundamentals("NASDAQ:NVDA"); assert f["metrics"]["eps_ttm"] is None and f["metrics"]["dividend_yield_pct"] is None and f["metrics"]["pe_ttm"]["value"] == 12; t("partially missing metrics stay missing (None), never filled")
FUND.update(profile={}, metric={"metric": {}}); svc.cache.d.clear(); f = svc.fundamentals("NSE:RELIANCE"); assert f["available"] is False and "no fundamentals" in f["reason"].lower(); t("provider has nothing -> unavailable with reason")
def f403(url, params, headers, timeout, body=None):
    if "finnhub" in url: raise urllib.error.HTTPError(url, 403, "x", {}, None)
    return fetch(url, params, headers, timeout, body)
core.FETCH = f403; svc.cache.d.clear(); f = svc.fundamentals("NASDAQ:JPM"); assert f["available"] is False and "plan" in f["reason"]; core.FETCH = fetch; t("plan restriction (HTTP 403) explained")
i = core.make_instrument("X", "Xco", "NASDAQ", "yahoo", sector="Tech", industry="Chips"); svc.extra[i.id] = i; FUND.clear(); core.FETCH = f403; svc.cache.d.clear(); f = svc.fundamentals("NASDAQ:X"); assert f["sector"] == "Tech" and f["sector_source"] == "yahoo" and f["available"] is False; core.FETCH = fetch; t("sector/industry from live search shown with its source even without fundamentals")
# HTTP + chat tool
import app
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start(); svc.started = True
def get(p):
    try: r = urllib.request.urlopen("http://127.0.0.1:%d" % srv.server_address[1] + p); return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
c, j = get("/api/market/analysis?id=NSE:HDFCBANK&range=6mo"); assert c == 200 and j["instrument"]["id"] == "NSE:HDFCBANK" and j["range"] == "6mo" and j["series"]["dates"]
c, j = get("/api/market/fundamentals?id=NASDAQ:AAPL"); assert c == 200 and "available" in j
c, j = get("/api/market/analysis?id=NOPE:NOPE"); assert c == 404 and j["error"]["code"] == "invalid_symbol"; srv.shutdown(); t("HTTP: /analysis and /fundamentals incl. error codes")
import tools
r = tools.run_tool("market_data", {"symbol": "TATAMOTORS", "exchange": "NSE", "period": "1y"}); assert r["latest_close"] == res["NSE:TATAMOTORS"]["price"]["latest"] and r["real_time"] is False and "performance_pct" in r and r["source"] == "yahoo"; t("chat tool uses the same deterministic analysis")
