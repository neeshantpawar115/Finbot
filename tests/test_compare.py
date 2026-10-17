"""Tests comparison and watchlist quotes on instruments with different exchange calendars, short/stale/missing data and known answers."""
import bisect, datetime as dt, json, math, os, statistics, sys, tempfile, threading, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, '.')
os.environ["FINBOT_CACHE_DIR"] = tempfile.mkdtemp()
for k in ("FINBOT_HISTORY_INDIA", "FINBOT_HISTORY_US", "FINBOT_HISTORY_OTHER", "FINBOT_SCAN_PROVIDERS", "FINBOT_ADMIN_TOKEN", "FINBOT_QUOTE_PROVIDERS", "ANGELONE_API_KEY", "ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "BSE_SCRIP_FILE", "LLM_BASE_URL", "NSE_EQUITY_FILE"):
    os.environ.pop(k, None)
os.environ["TWELVEDATA_API_KEY"] = "TK"; os.environ["FINBOT_HISTORY"] = "yahoo"
import market.core as core
from market.core import MarketError
t = lambda n: print("ok  ", n)
D1 = dt.timedelta(days=1); last_day = dt.date.today() - D1
BASE = last_day - dt.timedelta(days=420)
# symbol -> (name, exchange, currency, base, mu, amp, period, history_days, volume_base, drop_last_days)
S = {"AAA": ("Alpha Ltd", "NSE", "INR", 100, .0006, .03, 7, 400, 100000, 0), "BBB": ("Beta Ltd", "NSE", "INR", 200, .0006, .03, 7, 400, 50000, 0),
     "CCC": ("Gamma Inc", "NASDAQ", "USD", 300, .0006, .03, 7, 400, 2000000, 0), "DDD": ("Delta Inc", "NASDAQ", "USD", 50, -.0004, .04, 11, 400, 900000, 0),
     "EEE": ("Epsilon Inc", "NASDAQ", "USD", 80, .001, .02, 5, 60, 300000, 0), "FFF": ("Stale Ltd", "NSE", "INR", 70, .0003, .02, 9, 400, 40000, 40),
     "GGG": ("Early Ltd", "NSE", "INR", 90, .0004, .02, 13, 400, 60000, 2), "HHH": ("NoVol Inc", "NASDAQ", "USD", 40, .0002, .01, 9, 400, None, 0)}
HOL = {"NSE": {BASE + dt.timedelta(days=k) for k in range(30, 420, 23)}, "NASDAQ": {BASE + dt.timedelta(days=k) for k in range(35, 420, 29)}}
def dates(sym):
    _, ex, _, _, _, _, _, hd, _, drop = S[sym]; out = [last_day - dt.timedelta(days=k) for k in range(hd - 1, -1, -1)]
    out = [d for d in out if d.weekday() < 5 and d not in HOL[ex]]; return out[:len(out) - drop] if drop else out
def price(sym, d): _, _, _, b, mu, amp, per, *_ = S[sym]; n = (d - BASE).days; return b * math.exp(mu * n + amp * math.sin(n / per))
def vol(sym, d): vb = S[sym][8]; return None if vb is None else vb * (1 + (d - BASE).days % 3)
PX = {s: {d: price(s, d) for d in dates(s)} for s in S}
def chart(sym):
    ds = dates(sym); ts = [int(dt.datetime.combine(d, dt.time(10), dt.timezone.utc).timestamp()) for d in ds]; c = [price(sym, d) for d in ds]
    return {"chart": {"result": [{"meta": {"currency": S[sym][2], "gmtoffset": 0}, "timestamp": ts, "indicators": {"quote": [{"open": c, "high": c, "low": c, "close": c, "volume": [vol(sym, d) for d in ds]}], "adjclose": [{"adjclose": c}]}}], "error": None}}
NAS = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n" + "".join(f"{s}|{S[s][0]} - Common Stock|Q|N|N|100|N|N\n" for s in S if S[s][1] == "NASDAQ") + "File Creation Time: x|||||||\n"
OTH = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nJPM|JPM Test|N|JPM|N|100|N|JPM\nFile Creation Time: x|||||||\n"
NSE = "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n" + "".join(f"{s},{S[s][0]},EQ,x,10,1,INE{n:03d}A01018,10\n" for n, s in enumerate(s for s in S if S[s][1] == "NSE"))
ST = {"calls": [], "fund": {}}
def fetch(url, params, headers, timeout, body=None):
    ST["calls"].append(url)
    if "nasdaqlisted" in url: return NAS
    if "otherlisted" in url: return OTH
    if "EQUITY_L" in url: return NSE
    if "finance/search" in url: return json.dumps({"quotes": []})
    if "finnhub" in url:
        f = ST["fund"].get(params["symbol"])
        return "{}" if f is None else json.dumps(f["profile"] if "profile2" in url else {"metric": f["metric"]})
    if "finance/chart" in url: return json.dumps(chart(urllib.parse.unquote(url.split("/chart/")[1].split("?")[0]).replace(".NS", "")))
    raise AssertionError(url)
core.FETCH = fetch; core._svc = None
svc = core.service(); svc.load_directories(force=True)
near = lambda a, b, tol=1e-6: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= tol * max(1, abs(b)))
# ---------- oracle (independent of the engine)
def locf(sym, t, tol=5):
    ds = dates(sym); i = bisect.bisect_right(ds, t) - 1
    return (i, ds[i]) if i >= 0 and (t - ds[i]).days <= tol else None
def o_window(syms, days):
    end = min(dates(s)[-1] for s in syms); first = max(dates(s)[0] for s in syms); return (first if days is None else max(first, end - dt.timedelta(days=days))), end
def o_metrics(sym, start, end):
    ds = dates(sym); b = bisect.bisect_right(ds, start) - 1; e = bisect.bisect_right(ds, end) - 1; p = [PX[sym][d] for d in ds[b:e + 1]]
    r = [p[i] / p[i - 1] - 1 for i in range(1, len(p))]; peak, mdd = p[0], 0
    for x in p: peak = max(peak, x); mdd = min(mdd, x / peak - 1)
    vs = [vol(sym, d) for d in ds[b + 1:e + 1] if vol(sym, d)]
    return {"ret": (p[-1] / p[0] - 1) * 100, "vol": statistics.stdev(r) * 252 ** .5 * 100 if len(r) >= 20 else None, "mdd": mdd * 100, "avgvol": sum(vs) / len(vs) if vs else None, "n": e - b + 1,
            "sma50": sum(price(sym, d) for d in ds[e - 49:e + 1]) / 50 if e >= 49 else None}
def o_corr(a, b, start, end):
    com = sorted(d for d in set(PX[a]) & set(PX[b]) if start <= d <= end); ra = [PX[a][com[k]] / PX[a][com[k - 1]] - 1 for k in range(1, len(com))]; rb = [PX[b][com[k]] / PX[b][com[k - 1]] - 1 for k in range(1, len(com))]
    return (statistics.correlation(ra, rb) if len(ra) >= 30 else None), len(ra)
ids3 = ["NSE:AAA", "NASDAQ:CCC", "NASDAQ:DDD"]; syms3 = ["AAA", "CCC", "DDD"]
c = svc.compare(ids3, "1y", False)
st, en = o_window(syms3, 366)
assert c["window"]["start"] == st.isoformat() and c["window"]["end"] == en.isoformat() and c["range"] == "1y" and c["real_time"] is False; t("common window: ends at the earliest last date, starts at the range start (or the latest first date)")
for o, s in zip(c["instruments"], syms3):
    m = o_metrics(s, st, en); assert near(o["return_pct"], m["ret"], 1e-3) and near(o["vol_ann_pct"], m["vol"], 1e-3) and near(o["max_drawdown"]["pct"], m["mdd"], 1e-3) and near(o["avg_volume"], m["avgvol"], 1e-3) and o["observations"] == m["n"] and near(o["sma50"]["value"], m["sma50"], 1e-3), (s, o["return_pct"], m)
t("return, volatility, maximum drawdown, average volume, observations and 50-day average match an independent calculation")
se = c["series"]; ds = se["dates"]; assert ds[0] == st.isoformat() and ds[-1] == en.isoformat() and all(se["perf"][i][0] == 0 for i in ids3) and all(len(v) == len(ds) for v in se["perf"].values()) and ds == sorted(ds); t("series: shared date axis; every instrument rebased to 0% (100) at the same start date")
for i, s in zip(ids3, syms3):
    base = PX[s][dates(s)[bisect.bisect_right(dates(s), st) - 1]]
    for k in range(0, len(ds), 17):
        L = locf(s, dt.date.fromisoformat(ds[k])); exp = None if L is None else (PX[s][L[1]] / base - 1) * 100
        assert near(se["perf"][i][k], exp, 1e-3), (i, ds[k], se["perf"][i][k], exp)
t("normalised chart values equal the independent last-close-carried-forward calculation on every sampled date")
all_nse = set(dates("AAA")); only_us = sorted(d for d in dates("CCC") if d not in all_nse and st <= d <= en); assert only_us
k = ds.index(only_us[0].isoformat()); prev = se["perf"]["NSE:AAA"][k - 1]; assert se["perf"]["NSE:AAA"][k] == prev and se["perf"]["NASDAQ:CCC"][k] != se["perf"]["NASDAQ:CCC"][k - 1]; t("cross-exchange: on a day only one exchange traded, the other's last close is carried forward")
assert c["mixed_currency"] and c["cross_exchange"] and any("different currencies (INR, USD)" in w and "never compared directly" in w for w in c["warnings"]) and any("different holidays" in w for w in c["warnings"]); t("warnings: mixed currencies (no conversion, prices never compared) and different exchange calendars")
assert [o["currency"] for o in c["instruments"]] == ["INR", "USD", "USD"] and all(abs(v) < 400 for i in ids3 for v in se["perf"][i] if v is not None); t("each instrument keeps its own currency; the chart carries only percentages")
cm = c["correlation"]; assert cm["ids"] == ids3 and cm["matrix"][0][0] == 1.0 and cm["matrix"][0][1] == cm["matrix"][1][0] and cm["matrix"][0][1] == 1.0
for (i, j) in ((0, 2), (1, 2)):
    exp, n = o_corr(syms3[i], syms3[j], st, en); assert near(cm["matrix"][i][j], exp, 1e-3) and cm["overlap"][i][j] == n, (i, j, cm["matrix"][i][j], exp)
t("correlation: identical returns across different calendars = 1.0; other pairs match the oracle on dates both traded; overlap counts reported")
cb = svc.compare(["NSE:AAA", "NSE:BBB"], "1y", False); assert cb["correlation"]["matrix"][0][1] == 1.0 and not cb["mixed_currency"] and not cb["cross_exchange"] and not any("holidays" in w or "currencies" in w for w in cb["warnings"]); t("same-exchange, same-currency comparison carries no cross-market warnings")
c1 = svc.compare(ids3, "1mo", False); assert c1["correlation"]["matrix"][0][1] is None and c1["correlation"]["overlap"][0][1] < 30 and any("only" in n and "need 30" in n for n in c1["correlation"]["notes"]) and near(c1["instruments"][0]["vol_ann_pct"], o_metrics("AAA", *o_window(syms3, 31))["vol"], 1e-3); t("short window: correlation withheld with a reason instead of a misleading number")
rs = [svc.compare(ids3, r, False)["window"] for r in ("1mo", "3mo", "6mo", "1y", "5y", "max")]; starts = [w["start"] for w in rs]; assert starts == sorted(starts, reverse=True) and len(set(starts)) == 5 and all(w["end"] == rs[0]["end"] for w in rs) and rs[4]["start"] == rs[5]["start"]; t("all six ranges: same end date, start date moves back with the range (5Y and Max coincide for a 400-day history)")
ce = svc.compare(["NSE:AAA", "NASDAQ:EEE"], "1y", False); first = dates("EEE")[0]; assert ce["window"]["start"] == first.isoformat() and any("has data only from" in w for w in ce["warnings"]) and ce["instruments"][1]["observations"] == len([d for d in dates("EEE")]) ; t("short history: window starts at the later first date, with an explanatory warning")
cs = svc.compare(["NSE:AAA", "NSE:BBB", "NSE:FFF"], "1y", False); assert [o["id"] for o in cs["instruments"]] == ["NSE:AAA", "NSE:BBB"] and cs["errors"][0]["id"] == "NSE:FFF" and cs["errors"][0]["code"] == "stale_data" and f"{(dates('AAA')[-1] - dates('FFF')[-1]).days} days older" in cs["errors"][0]["message"]; assert any("FFF" in w and "not compared" in w for w in cs["warnings"]); t("stale instrument left out with a clear reason (delisted/suspended-style data), others still compared")
try: svc.compare(["NSE:AAA", "NSE:FFF"], "1y", False); raise SystemExit("x")
except MarketError as e: assert e.code == "no_data" and e.extra["errors"][0]["code"] == "stale_data" and "at least two" in str(e).lower(); t("fewer than two usable instruments -> clear error, never a one-stock 'comparison'")
cg = svc.compare(["NSE:AAA", "NSE:GGG"], "1y", False); assert cg["window"]["end"] == dates("GGG")[-1].isoformat() and any("The comparison ends on" in w for w in cg["warnings"]) and cg["series"]["dates"][-1] == dates("GGG")[-1].isoformat(); t("an instrument that ends a few days earlier sets the common end date; stated in a warning")
ci = svc.compare(["NSE:AAA", "NASDAQ:CCC", "NSE:ZZZ"], "1y", False); assert len(ci["instruments"]) == 2 and ci["errors"][0]["id"] == "NSE:ZZZ" and ci["errors"][0]["code"] == "invalid_symbol" and any("NSE:ZZZ was not compared" in w for w in ci["warnings"]); t("invalid symbol reported while the valid ones are compared")
cd = svc.compare(["AAA", "NSE:AAA", "NASDAQ:CCC", "NASDAQ:CCC"], "1y", False); assert [o["id"] for o in cd["instruments"]] == ["NSE:AAA", "NASDAQ:CCC"] and any(e["code"] == "duplicate" for e in cd["errors"]); t("duplicates (including bare ticker vs full id) removed")
for bad, msg in ((["NSE:AAA"], "at least two"), ([f"NSE:AAA{i}" for i in range(7)], "at most 6"), ([], "at least two")):
    try: svc.compare(bad, "1y", False); raise SystemExit("x")
    except MarketError as e: assert e.code == "bad_request" and e.status == 400 and msg in str(e).lower(); 
t("selection limits: needs 2, allows at most 6")
ch = svc.compare(["NASDAQ:CCC", "NASDAQ:HHH"], "1y", False); h = ch["instruments"][1]; assert h["avg_volume"] is None and h["latest_volume"] is None and any("No volume" in w for w in h["warnings"]) and ch["instruments"][0]["avg_volume"] is not None; t("missing volume reported per instrument, not treated as zero")
assert c["instruments"][2]["sma200"]["value"] is not None and ce["instruments"][1]["sma200"]["value"] is None and "Needs 200" in ce["instruments"][1]["sma200"]["reason"]; t("moving averages use full history; unavailable ones carry a reason")
cdd = c["series"]["drawdown"]; assert all(v[0] == 0 for v in cdd.values()) and all(x <= 0 for v in cdd.values() for x in v if x is not None) and near(min(x for x in cdd["NASDAQ:DDD"] if x is not None), c["instruments"][2]["max_drawdown"]["pct"], 1e-3); t("drawdown series: starts at 0, never positive, minimum equals the reported maximum drawdown")
# fundamentals
cf = svc.compare(ids3, "1y", True); assert all(o["fundamentals"]["available"] is False and "FINNHUB_API_KEY" in o["fundamentals"]["reason"] for o in cf["instruments"]); t("fundamentals without a key: unavailable with reason for every instrument")
os.environ["FINNHUB_API_KEY"] = "FK"; svc.cache.d.clear(); ST["fund"] = {"CCC": {"profile": {"marketCapitalization": 2.5e6, "currency": "USD", "finnhubIndustry": "Tech"}, "metric": {"peTTM": 21.5, "epsTTM": 4.2}}}
cf = svc.compare(ids3, "1y", True); f = {o["id"]: o["fundamentals"] for o in cf["instruments"]}; assert f["NASDAQ:CCC"]["available"] and f["NASDAQ:CCC"]["metrics"]["pe_ttm"]["value"] == 21.5 and f["NASDAQ:CCC"]["metrics"]["market_cap"]["currency"] == "USD" and not f["NSE:AAA"]["available"] and f["NASDAQ:DDD"]["available"] is False and f["NASDAQ:DDD"]["metrics"] == {}; t("fundamentals shown where the provider has them (with currency); missing elsewhere, never invented")
# quotes
q = svc.quotes(["NSE:AAA", "NASDAQ:CCC", "NSE:ZZZ"]); assert len(q["quotes"]) == 2 and q["errors"][0]["id"] == "NSE:ZZZ" and q["real_time"] is False
for r, s in zip(q["quotes"], ("AAA", "CCC")):
    ds_ = dates(s); assert near(r["price"], PX[s][ds_[-1]], 1e-6) and near(r["prev_close"], PX[s][ds_[-2]], 1e-6) and near(r["change"], PX[s][ds_[-1]] - PX[s][ds_[-2]], 1e-3) and near(r["change_pct"], (PX[s][ds_[-1]] / PX[s][ds_[-2]] - 1) * 100, 1e-3) and r["date"] == ds_[-1].isoformat() and r["source"] == "yahoo" and r["currency"] == S[s][2]
t("watchlist quotes: latest price, previous close, daily change (absolute and %), as-of date, source, currency")
assert q["quotes"][0]["perf_1m"] is not None; assert not any("twelvedata" in u for u in ST["calls"]); t("quotes use Yahoo only by default (free Twelve Data quota untouched); 1-month change included")
for bad in ([], [f"NSE:A{i}" for i in range(31)]):
    try: svc.quotes(bad); raise SystemExit("x")
    except MarketError as e: assert e.code == "bad_request"
t("quote request limits")
# chat tool
import tools
r = tools.run_tool("compare_stocks", {"ids": ids3, "range": "1y"}); assert [i["id"] for i in r["instruments"]] == ids3 and r["correlation"]["matrix"][0][1] == 1.0 and r["window"]["start"] == st.isoformat() and r["real_time"] is False and "raw prices" in r["note"] and r["warnings"]; t("chat tool compare_stocks returns the same deterministic figures plus warnings")
r = tools.run_tool("compare_stocks", {"ids": ["NSE:AAA"]}); assert "error" in r and "at least two" in r["error"].lower(); t("chat tool rejects a single instrument")
# HTTP
import app
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start(); svc.started = True
def get(p):
    try: r = urllib.request.urlopen("http://127.0.0.1:%d" % srv.server_address[1] + p); return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
ids_q = urllib.parse.quote(",".join(["NSE:AAA", "NASDAQ:CCC", "NASDAQ:DDD", "NSE:FFF", "NSE:ZZZ"]), safe=",")
code, j = get(f"/api/market/compare?ids={ids_q}&range=1y&fundamentals=1"); assert code == 200 and len(j["instruments"]) == 3 and {e["code"] for e in j["errors"]} == {"stale_data", "invalid_symbol"} and j["series"]["dates"]
code, j2 = get(f"/api/market/compare?ids={ids_q}&range=1y&fundamentals=1"); assert j2.get("cached") is True
code, j3 = get("/api/market/compare?ids=NSE:AAA&range=1y"); assert code == 400 and j3["error"]["code"] == "bad_request"
code, j4 = get("/api/market/compare?ids=NSE:AAA,NSE:FFF"); assert code == 404 and j4["error"]["code"] == "no_data" and j4["error"]["errors"][0]["code"] == "stale_data"
code, jq = get("/api/market/quotes?ids=" + urllib.parse.quote("NSE:AAA,NASDAQ:CCC,NSE:ZZZ", safe=",")); assert code == 200 and len(jq["quotes"]) == 2 and jq["errors"][0]["code"] == "invalid_symbol"
assert get("/api/market/quotes")[0] == 400; t("HTTP: /compare and /quotes incl. partial results, caching and error codes")
fx = __import__("pathlib").Path("tests/fixtures"); fx.mkdir(exist_ok=True); (fx / "compare.json").write_text(json.dumps(j)); (fx / "quotes.json").write_text(json.dumps(jq))
srv.shutdown()
