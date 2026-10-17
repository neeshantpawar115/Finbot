"""Tests the market-wide screener on a deterministic universe: scan engine, metrics vs an independent oracle, every filter,
sorting, pagination, missing data, rate limits, cancellation, persistence, export, plain-English queries, HTTP routes and the chat tool."""
import csv, datetime as dt, io, json, os, socket, statistics, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, '.')
os.environ["FINBOT_CACHE_DIR"] = tempfile.mkdtemp()
for k in ("FINBOT_HISTORY", "FINBOT_HISTORY_INDIA", "FINBOT_HISTORY_US", "FINBOT_HISTORY_OTHER", "FINBOT_SCAN_PROVIDERS", "FINBOT_ADMIN_TOKEN", "ANGELONE_API_KEY", "ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "BSE_SCRIP_FILE", "LLM_BASE_URL", "NSE_EQUITY_FILE"):
    os.environ.pop(k, None)
os.environ["TWELVEDATA_API_KEY"] = "TK"; os.environ["FINBOT_HISTORY"] = "twelvedata,yahoo"
import market.core as core
from market.core import MarketError
from market.screener import Screener, can_scan, spec_from_query, ScanBusy
t = lambda n: print("ok  ", n)
today = dt.date.today(); last_day = today - dt.timedelta(days=1)
# symbol -> (name, base, daily growth, wobble, n_days, volume base, exchange)
U = {"ALPHABK": ("Alpha Bank Limited", 100, .004, .01, 366, 100000, "NSE"), "BETABK": ("Beta Bank Limited", 200, -.002, .01, 366, 200000, "NSE"),
     "GAMSTL": ("Gamma Steel Limited", 50, .001, .05, 366, 300000, "NSE"), "DELCEM": ("Delta Cement Limited", 400, .0025, .005, 300, 50000, "NSE"),
     "EPSPHA": ("Epsilon Pharma Limited", 800, -.004, .01, 366, 20000, "NSE"), "ZETATX": ("Zeta Textiles Limited", 10, 0, 0, 366, 900000, "NSE"),
     "ETASHT": ("Eta Short Limited", 70, .01, .01, 45, 10000, "NSE"), "THENOV": ("Theta NoVol Limited", 120, .0005, .01, 366, None, "NSE"),
     "RLCO": ("Rate Limited Co Limited", 300, .002, .01, 366, 70000, "NSE"),
     "AAA": ("Alpha Corp", 150, .003, .06, 366, 5000000, "NASDAQ"), "BBB": ("Beta Inc", 90, -.001, .01, 366, 3000000, "NASDAQ"),
     "CCC": ("Gamma Corp", 600, .0005, .01, 366, 1000000, "NASDAQ"), "DDD": ("Delta Inc", 30, .002, .08, 366, 8000000, "NASDAQ"), "JPM": ("JPMorgan Test Bank", 200, .0007, .01, 366, 4000000, "NYSE")}
def closes(sym): _, b, g, w, n, *_ = U[sym]; return [b * (1 + g) ** i * (1 + w * (-1) ** i) for i in range(n)]
def volumes(sym): vb, n = U[sym][5], U[sym][4]; return [None] * n if vb is None else [vb * (1 + i % 5) for i in range(n)]
def chart(sym):
    c, v = closes(sym), volumes(sym); n = len(c)
    ts = [int(dt.datetime.combine(last_day - dt.timedelta(days=n - 1 - i), dt.time(10), dt.timezone.utc).timestamp()) for i in range(n)]
    return {"chart": {"result": [{"meta": {"currency": "INR" if U[sym][6] == "NSE" else "USD", "gmtoffset": 0}, "timestamp": ts, "indicators": {"quote": [{"open": c, "high": c, "low": c, "close": c, "volume": v}], "adjclose": [{"adjclose": c}]}}], "error": None}}
NAS = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n" + "".join(f"{s}|{U[s][0]} - Common Stock|Q|N|N|100|N|N\n" for s in ("AAA", "BBB", "CCC", "DDD")) + "ETFX|Test ETF|G|N|N|100|Y|N\nFile Creation Time: x|||||||\n"
OTH = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nJPM|JPMorgan Test Bank|N|JPM|N|100|N|JPM\nFile Creation Time: x|||||||\n"
NSE = "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n" + "".join(f"{s},{U[s][0]},EQ,x,10,1,INE{n:03d}A01018,10\n" for n, s in enumerate([k for k in U if U[k][6] == "NSE"] + ["BADCO"])) .replace("BADCO,", "BADCO,Bad Company Limited") if False else "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n" + "".join(f"{s},{(U[s][0] if s in U else 'Bad Company Limited')},EQ,x,10,1,INE{n:03d}A01018,10\n" for n, s in enumerate([k for k in U if U[k][6] == "NSE"] + ["BADCO"]))
ST = {"rl": {"RLCO.NS": 2}, "all429": False, "sleep": 0, "calls": [], "fund": {}}
def fetch(url, params, headers, timeout, body=None):
    ST["calls"].append(url)
    if "nasdaqlisted" in url: return NAS
    if "otherlisted" in url: return OTH
    if "EQUITY_L" in url: return NSE
    if "finance/search" in url: return json.dumps({"quotes": []})
    if "finnhub" in url:
        sym = params["symbol"]; f = ST["fund"].get(sym)
        if f is None: return "{}"
        return json.dumps(f["profile"] if "profile2" in url else {"metric": f["metric"]})
    if "finance/chart" in url:
        if ST["sleep"]: time.sleep(ST["sleep"])
        sym = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0]).replace(".NS", "")
        if ST["all429"] or ST["rl"].get(sym + ".NS", 0) > 0 and (ST["rl"].__setitem__(sym + ".NS", ST["rl"][sym + ".NS"] - 1) or True):
            raise urllib.error.HTTPError(url, 429, "x", {}, None)
        if sym == "BADCO": return json.dumps({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}})
        return json.dumps(chart(sym))
    raise AssertionError(url)
core.FETCH = fetch; core._svc = None
svc = core.service(); svc.load_directories(force=True); sc = svc.screener
sc.interval, sc.backoff, sc.workers = 0, 0.01, 4
# ---------- independent oracle
def oracle(sym):
    c, v = closes(sym), volumes(sym); n = len(c); r = [c[i] / c[i - 1] - 1 for i in range(1, n)]
    def perf(days):
        base = n - 1 - days
        if base >= 0: return (c[-1] / c[base] - 1) * 100
        return (c[-1] / c[0] - 1) * 100 if (days - (n - 1)) <= 5 else None  # first bar within 5 days of the target date
    peak, mdd = c[0], 0
    for x in c: peak = max(peak, x); mdd = min(mdd, x / peak - 1)
    v20 = [x for x in v[-20:] if x]
    return {"price": c[-1], "chg": (c[-1] / c[-2] - 1) * 100, "perf_1m": perf(31), "perf_3m": perf(92), "perf_6m": perf(183), "perf_1y": perf(366),
            "vol": statistics.stdev(r) * 252 ** .5 * 100 if len(r) >= 20 else None, "mdd": mdd * 100, "avgvol": sum(v20) / 20 if len(v20) >= 20 else None,
            "sma20_pct": (c[-1] / (sum(c[-20:]) / 20) - 1) * 100 if n >= 20 else None, "sma50_pct": (c[-1] / (sum(c[-50:]) / 50) - 1) * 100 if n >= 50 else None,
            "sma200_pct": (c[-1] / (sum(c[-200:]) / 200) - 1) * 100 if n >= 200 else None}
O = {s: oracle(s) for s in U}
near = lambda a, b, tol=1e-3: (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= tol * max(1, abs(b)))
# ---------- scan
assert len(sc.eligible("NASDAQ")) == 4 and len(sc.eligible("NASDAQ", include_etf=True)) == 5 and len(sc.eligible("NSE")) == 10; t("universe: ETFs excluded unless requested; full NSE directory eligible")
nse_calls = len(ST["calls"]); sc.start("NSE", background=False)
s = sc.snapshot("NSE"); assert s["eligible"] == 10 and s["targeted"] == 10 and s["ok"] == 9 and s["failed"] == 1 and s["processed"] == 10 and s["complete"] and not s["limited"] and s["stopped"] is None, s
assert s["failures"][0]["id"] == "NSE:BADCO" and s["failures"][0]["code"] == "no_data" and "delisted" in s["failures"][0]["message"]; t("NSE scan: 9 processed, 1 failure recorded with reason (delisted/not found)")
assert not any("twelvedata" in u for u in ST["calls"]); t("scans use only FINBOT_SCAN_PROVIDERS (Yahoo); Twelve Data quota untouched")
assert sc.state["rate_limit_pauses"] >= 2 and s["ok"] == 9; t("transient rate limit: scan paused, retried and still processed that instrument")
for sym in [k for k in U if U[k][6] == "NSE"]:
    row = s["rows"].get("NSE:" + sym)
    if row is None: assert sym == "BADCO"; continue
    for k, v in O[sym].items(): assert near(row[k], v), (sym, k, row[k], v)
t("every metric for every scanned NSE stock matches an independent calculation (price, day %, 1M/3M/6M/1Y, volatility, drawdown, volume, SMAs)")
assert s["rows"]["NSE:ETASHT"]["sma200_pct"] is None and s["rows"]["NSE:ETASHT"]["perf_3m"] is None and s["rows"]["NSE:DELCEM"]["perf_1y"] is None and s["rows"]["NSE:THENOV"]["avgvol"] is None; t("short history / no volume -> metrics are None, not estimated")
sc.start("NASDAQ", background=False); sc.start("NYSE", background=False); assert sc.snapshot("NASDAQ")["ok"] == 4 and sc.snapshot("NYSE")["ok"] == 1; t("NASDAQ and NYSE scans")
# consistency with the single-stock analysis engine
from market.analysis import compute, snapshot
rows_ = [{"d": d, "o": c, "h": c, "l": c, "c": c, "v": v, "ac": c} for d, c, v in zip([(last_day - dt.timedelta(days=365 - i)).isoformat() for i in range(366)], closes("GAMSTL"), volumes("GAMSTL"))]
a = compute({"id": "NSE:GAMSTL", "currency": "INR"}, {"rows": rows_, "missing": 0, "adjusted": True}, "yahoo", [], "1y"); sn = snapshot(rows_)
assert near(sn["vol"], a["risk"]["annualised_vol_pct"]) and near(sn["mdd"], a["risk"]["max_drawdown"]["pct"]) and near(sn["sma50"], a["indicators"]["sma50"]["value"]) and near(sn["perf_1m"], a["performance"][0]["return_pct"]) and near(sn["perf_6m"], a["performance"][2]["return_pct"]); t("screener metrics equal the stock-analysis page metrics")
# ---------- screening vs oracle
def ids(spec): return [r["id"] for r in sc.screen({"page_size": 100, **spec})["rows"]]
def exp_ids(pred, ex="NSE", key=None, rev=True):
    syms = [k for k in U if U[k][6] == ex and (key is None or O[k][key] is not None)]
    out = sorted([k for k in syms if pred(O[k])], key=lambda k: (O[k][key] if key else k), reverse=rev); return [f"{ex}:{k}" for k in out]
sp = {"exchanges": ["NSE"]}
assert set(ids({**sp, "price_min": 100, "price_max": 500})) == set(exp_ids(lambda m: 100 <= m["price"] <= 500)); t("filter: price range")
assert set(ids({**sp, "change_min": 0.3})) == set(exp_ids(lambda m: m["chg"] >= 0.3)) and ids({**sp, "change_min": 0.3}); t("filter: daily % change")
for per, key in (("1M", "perf_1m"), ("3M", "perf_3m"), ("6M", "perf_6m"), ("1Y", "perf_1y")):
    assert set(ids({**sp, "perf_period": per, "perf_min": 5})) == set(exp_ids(lambda m: m[key] is not None and m[key] >= 5, key=key)), per
    assert set(ids({**sp, "perf_period": per, "perf_max": -5})) == set(exp_ids(lambda m: m[key] is not None and m[key] <= -5, key=key)), per
t("filter: performance over 1M / 3M / 6M / 1Y, positive and negative thresholds")
assert set(ids({**sp, "avgvol_min": 150000})) == set(exp_ids(lambda m: m["avgvol"] is not None and m["avgvol"] >= 150000, key="avgvol")); t("filter: trading volume (20-day average)")
assert set(ids({**sp, "vol_min": 15})) == set(exp_ids(lambda m: m["vol"] is not None and m["vol"] >= 15, key="vol")) and "NSE:GAMSTL" in ids({**sp, "vol_min": 15}) and "NSE:ZETATX" not in ids({**sp, "vol_min": 15}); t("filter: historical volatility")
assert set(ids({**sp, "vol_max": 5})) == set(exp_ids(lambda m: m["vol"] is not None and m["vol"] <= 5, key="vol")) and "NSE:ZETATX" in ids({**sp, "vol_max": 5}); t("filter: low volatility maximum")
assert set(ids({**sp, "mdd_min": -30})) == set(exp_ids(lambda m: m["mdd"] >= -30)) and "NSE:EPSPHA" not in ids({**sp, "mdd_min": -30}); t("filter: maximum drawdown not worse than a limit")
for n_ in (20, 50, 200):
    for side in ("above", "below"):
        e = exp_ids(lambda m: m[f"sma{n_}_pct"] is not None and ((m[f"sma{n_}_pct"] > 0) if side == "above" else (m[f"sma{n_}_pct"] < 0)), key=f"sma{n_}_pct")
        assert set(ids({**sp, "sma_period": n_, "sma_side": side})) == set(e), (n_, side)
r = sc.screen({**sp, "sma_period": 200, "sma_side": "above", "page_size": 100}); assert r["excluded_missing"]["200-day moving average"] == 1 and "NSE:ETASHT" not in [x["id"] for x in r["rows"]]; t("filter: price above/below 20/50/200-day average; short-history stocks excluded and counted")
assert ids({**sp, "name_contains": "bank"}) == ["NSE:ALPHABK", "NSE:BETABK"] or set(ids({**sp, "name_contains": "bank"})) == {"NSE:ALPHABK", "NSE:BETABK"}; assert set(ids({"name_contains": "bank"})) == {"NSE:ALPHABK", "NSE:BETABK", "NYSE:JPM"}; t("filter: company-name keyword (across exchanges)")
assert set(ids({"market": "United States"})) == {f"{U[k][6]}:{k}" for k in U if U[k][6] != "NSE"} and set(ids({"market": "India"})) == {f"NSE:{k}" for k in U if U[k][6] == "NSE"} and ids({"market": "Other"}) == []; assert set(ids({"exchanges": ["NASDAQ"]})) == {f"NASDAQ:{k}" for k in U if U[k][6] == "NASDAQ"}; t("filter: market and exchange selection")
assert len(ids({})) == 14 and len(ids({"exchanges": ["NSE", "NASDAQ"]})) == 13; t("no exchange selected -> all scanned exchanges")
# sorting
for key in ("price", "chg", "perf_1m", "perf_6m", "vol", "mdd", "avgvol", "sma200_pct"):
    for d in ("asc", "desc"):
        got = ids({**sp, "sort": key, "dir": d}); have = [x for x in got if O[x.split(":")[1]][key] is not None]; vals = [O[x.split(":")[1]][key] for x in have]
        assert vals == sorted(vals, reverse=d == "desc") and got[:len(have)] == have, (key, d)
t("sorting by every numeric column, ascending and descending; missing values always last")
assert ids({**sp, "sort": "name", "dir": "asc"})[0] == "NSE:ALPHABK" and ids({**sp, "sort": "symbol", "dir": "desc"})[0] == "NSE:ZETATX"; t("sorting by name and ticker")
assert ids({**sp, "perf_period": "1M", "sort": "perf_1m", "dir": "desc"})[0] == "NSE:ETASHT" and ids({**sp, "sort": "perf_1m", "dir": "asc"})[0] == "NSE:EPSPHA"; t("strongest and weakest ranking correct")
# pagination
p1 = sc.screen({**sp, "page_size": 4, "sort": "symbol", "dir": "asc", "page": 1}); p3 = sc.screen({**sp, "page_size": 4, "sort": "symbol", "dir": "asc", "page": 3}); p9 = sc.screen({**sp, "page_size": 4, "sort": "symbol", "dir": "asc", "page": 9})
assert p1["total"] == 9 and p1["pages"] == 3 and len(p1["rows"]) == 4 and len(p3["rows"]) == 1 and p9["page"] == 3 and p9["rows"] == p3["rows"]; allp = [x["id"] for pg in (1, 2, 3) for x in sc.screen({**sp, "page_size": 4, "sort": "symbol", "dir": "asc", "page": pg})["rows"]]; assert allp == sorted(allp, key=lambda i: i.split(":")[1]) and len(set(allp)) == 9; t("pagination: pages, last partial page, out-of-range clamped, no duplicates/gaps")
assert sc.screen({**sp, "page_size": 1000})["page_size"] == 100; assert sc.screen({**sp, "price_min": 1e9})["total"] == 0 and sc.screen({**sp, "price_min": 1e9})["rows"] == []; t("page size capped at 100; empty result handled")
# coverage honesty
cov = sc.screen(sp)["scan"]; assert "Screened 9 of 10 NSE instruments" in cov["statement"] and "1 could not be processed" in cov["statement"] and "missing from the rankings" in cov["statement"] and not cov["any_partial"] and cov["exchanges"][0]["failures_sample"][0]["id"] == "NSE:BADCO" and cov["exchanges"][0]["obs_max"] == last_day.isoformat(); t("coverage statement: counts, failures, scan date, latest observation date")
r = sc.screen({"exchanges": ["NSE", "BSE"]}); assert r["missing_exchanges"] == ["BSE"]; t("requesting an unscanned exchange is reported, not silently ignored")
# limited / cancelled / rate-limited scans are labelled partial
sc.start("NSE", limit=3, background=False); s = sc.snapshot("NSE"); assert s["targeted"] == 3 and s["limited"] and not s["complete"] and s["ok"] == 2 and s["failed"] == 1 and sorted(r["symbol"] for r in s["rows"].values()) == sorted(x.symbol for x in sc.eligible("NSE")[:3] if x.symbol != "BADCO")  # the first three alphabetically include the failing BADCO
st = sc.screen({"exchanges": ["NSE"]})["scan"]; assert st["any_partial"] and "partial ranking, not a complete market ranking" in st["statement"] and "first 3 alphabetically" in st["statement"]; t("limited scan: partial, first N alphabetically, never called complete")
sc.start("NSE", background=False); assert sc.snapshot("NSE")["complete"]; ST["rl"] = {"RLCO.NS": 2}
ST["all429"] = True; ST["rl"] = {}; sc.start("NSE", background=False); s = sc.snapshot("NSE"); assert s["stopped"] == "rate_limited" and not s["complete"] and s["ok"] == 0 and s["processed"] < 10 and "partial" in sc.screen({"exchanges": ["NSE"]})["scan"]["statement"] and all(f["code"] == "rate_limited" for f in s["failures"]); ST["all429"] = False; t("persistent provider rate limit: scan stops early, flagged partial, failures say rate_limited")
sc.start("NSE", background=False); ST["sleep"] = 0.08; sc.workers = 2; sc.start("NSE"); time.sleep(0.25); sc.cancel(); sc.thread.join(10); s = sc.snapshot("NSE"); assert s["stopped"] == "cancelled" and s["processed"] < 10 and not s["complete"] and sc.state["running"] is False; ST["sleep"] = 0; sc.workers = 4; t("cancelling a running scan keeps partial results and labels them")
try:
    ST["sleep"] = 0.05; sc.start("NSE"); time.sleep(0.05)
    try: sc.start("NASDAQ"); raise SystemExit("x")
    except ScanBusy as e: assert e.status == 409
    sc.cancel(); sc.thread.join(10); ST["sleep"] = 0; t("second scan while one is running -> rejected (busy)")
finally: ST["sleep"] = 0
sc.start("NSE", background=False)
# progress reporting
assert sc.status()["scan"]["running"] is False and sc.state["done"] == sc.state["total"] == 10; stt = sc.status(); assert {e["exchange"] for e in stt["exchanges"]} >= {"NSE", "NASDAQ", "NYSE"} and stt["exchanges"][0]["snapshot"]["scan_date"]; t("status exposes progress counters and per-exchange scan info")
# persistence
sc2 = Screener(svc); assert sc2.snapshot("NSE")["ok"] == 9 and sc2.screen({"exchanges": ["NSE"]})["total"] == 9; t("snapshots persist to disk and reload")
# fundamentals enrichment
os.environ["FINNHUB_API_KEY"] = "FK"; ST["fund"] = {"AAA": {"profile": {"marketCapitalization": 3000000.0, "currency": "USD", "finnhubIndustry": "Technology"}, "metric": {"peTTM": 30.0, "epsTTM": 5.0}}, "BBB": {"profile": {"marketCapitalization": 5000.0, "currency": "USD"}, "metric": {"peTTM": 10.0}}}
e = sc.enrich(["NASDAQ:AAA", "NASDAQ:BBB", "NASDAQ:CCC"]); assert e["loaded"] == 2 and e["unavailable"][0]["id"] == "NASDAQ:CCC" and e["stored_total"] == 2; t("enrichment: market cap and P/E loaded where the provider has them; others reported")
r = sc.screen({"exchanges": ["NASDAQ"], "mcap_min": 10000, "page_size": 100}); assert [x["id"] for x in r["rows"]] == ["NASDAQ:AAA"] and r["excluded_missing"]["Market cap (millions)"] == 2  # CCC and DDD have no fundamentals stored; t("filter: market cap (millions); instruments without data excluded and counted")
r = sc.screen({"exchanges": ["NASDAQ"], "pe_max": 15, "page_size": 100}); assert [x["id"] for x in r["rows"]] == ["NASDAQ:BBB"] and r["rows"][0]["mcap"] == 5e9 and r["rows"][0]["pe"] == 10.0; t("filter: P/E; columns carry the stored values")
assert sc.screen({"exchanges": ["NASDAQ"], "sort": "mcap", "dir": "desc"})["rows"][0]["id"] == "NASDAQ:AAA" and [x["mcap"] for x in sc.screen({"exchanges": ["NASDAQ"], "sort": "mcap", "dir": "desc"})["rows"]][2:] == [None, None]; assert len(sc.enrich([f"NASDAQ:X{i}" for i in range(50)])["unavailable"]) <= 20; t("sort by market cap with missing last; enrichment capped at 20 ids per request")
# sector / industry only where classification exists
sc.snaps["NASDAQ"]["rows"]["NASDAQ:AAA"]["sector"] = "Technology"; assert ids({"exchanges": ["NASDAQ"], "sector": "tech"}) == ["NASDAQ:AAA"] and sc.screen({"exchanges": ["NASDAQ"], "sector": "tech"})["excluded_missing"]["Sector"] == 3; t("sector filter works where a sector is known; others counted as missing")
# export
sc.snaps["NASDAQ"]["rows"]["NASDAQ:BBB"]["name"] = "=HYPERLINK(\"http://x\")"
text, res = sc.export_csv({"exchanges": ["NASDAQ", "NSE"], "sort": "symbol", "dir": "asc"}); rows = list(csv.reader(io.StringIO(text)))
assert rows[0][:4] == ["Company", "Ticker", "Exchange", "Currency"] and len(rows) - 1 == res["total"] == 13 and rows[1][1] == "AAA" 
assert len(rows) - 1 == 13 and all(len(r) == len(rows[0]) for r in rows) and any(r[0].startswith("'=") for r in rows) and not any(r[0].startswith("=") for r in rows); t("CSV export: all matches (not just one page), consistent columns, formula-injection guarded")
# plain-English queries
nl = lambda q: sc.parse_nl(q)["spec"]
a = nl("Show the strongest-performing available NSE stocks over the last month."); assert a["exchanges"] == ["NSE"] and a["perf_period"] == "1M" and a["sort"] == "perf_1m" and a["dir"] == "desc"
b = nl("Find highly volatile NASDAQ stocks."); assert b["exchanges"] == ["NASDAQ"] and b["vol_min"] == 40 and b["sort"] == "vol" and b["dir"] == "desc"
c = nl("Find Indian stocks trading above their 200-day moving average."); assert c["market"] == "India" and c["sma_period"] == 200 and c["sma_side"] == "above"
d = nl("Show stocks with positive one-year returns."); assert d["perf_period"] == "1Y" and d["perf_min"] > 0
e = sc.parse_nl("Compare banking stocks by historical performance."); assert e["spec"]["name_contains"] == "bank" and any("company name" in n.lower() or "sector classification" in n for n in e["notes"])
assert nl("top 5 weakest NSE stocks over 6 months")["page_size"] == 5 and nl("top 5 weakest NSE stocks over 6 months")["dir"] == "asc" and nl("NSE stocks under ₹500")["price_max"] == 500 and nl("stocks below their 50-day moving average")["sma_side"] == "below"; t("plain-English parser: the five example queries and variants (with stated assumptions)")
r = sc.search(a); assert [x["id"] for x in r["rows"]][:3] == ["NSE:ETASHT", "NSE:ALPHABK", "NSE:DELCEM"]
r = sc.search(c); assert all(x["market"] == "India" and x["sma200_pct"] > 0 for x in r["rows"]) and r["total"] == len(exp_ids(lambda m: m["sma200_pct"] is not None and m["sma200_pct"] > 0, key="sma200_pct"))
r = sc.search(sc.parse_nl("highly volatile NASDAQ stocks")["spec"]); assert [x["id"] for x in r["rows"]] == exp_ids(lambda m: m["vol"] >= 40, ex="NASDAQ", key="vol"); t("plain-English queries run through the real screening functions and match the oracle")
# chat tool
import tools
r = tools.run_tool("screen_stocks", {"exchanges": ["NSE"], "perf_period": "1M", "sort_by": "perf_1m", "sort_dir": "desc", "limit": 3}); assert r["results"][0]["id"] == "NSE:ETASHT" and len(r["results"]) == 3 and r["matches"] == 9 and "Screened 9 of 10" in r["coverage"] and r["partial_scan"] is False and r["results"][0]["perf_1m"] is not None; t("chat tool screen_stocks returns real ranked results with coverage statement")
sc.snaps.clear(); core.CACHE_DIR = __import__("pathlib").Path(tempfile.mkdtemp()); r = tools.run_tool("screen_stocks", {"exchanges": ["NSE"]}); assert "error" in r and "press Scan" in r["error"]; t("chat tool without a scan explains what to do")
core.CACHE_DIR = __import__("pathlib").Path(os.environ["FINBOT_CACHE_DIR"]); sc.snaps.clear()
# scan permission rules
assert can_scan({}, "127.0.0.1") and not can_scan({"X-Forwarded-For": "1.2.3.4"}, "127.0.0.1") and not can_scan({}, "10.0.0.5"); os.environ["FINBOT_ADMIN_TOKEN"] = "secret"; assert can_scan({"X-Admin-Token": "secret"}, "10.0.0.5") and not can_scan({"X-Admin-Token": "nope"}, "127.0.0.1") and not can_scan({}, "127.0.0.1"); os.environ.pop("FINBOT_ADMIN_TOKEN"); assert spec_from_query({"exchanges": "NSE,BSE", "perf_min": "5", "x": "1", "page": ""}) == {"exchanges": ["NSE", "BSE"], "perf_min": "5"}; t("scan permissions: local-only by default, admin token when set; query mapping ignores unknown/empty params")
# HTTP
import app
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start(); svc.started = True
base = "http://127.0.0.1:%d" % srv.server_address[1]
def call(path, data=None, headers=None):
    req = urllib.request.Request(base + path, json.dumps(data).encode() if data is not None else None, {"Content-Type": "application/json", **(headers or {})})
    try: r = urllib.request.urlopen(req); return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e: return e.code, e.headers, e.read()
def wait_idle():
    for _ in range(300):
        if not json.loads(call("/api/market/screener/status")[2])["scan"]["running"]: return
        time.sleep(0.05)
    raise SystemExit("scan did not finish")
c, h, b = call("/api/market/screener/scan", {"exchange": "NASDAQ"}, {"X-Forwarded-For": "8.8.8.8"}); assert c == 403 and json.loads(b)["error"]["code"] == "scan_forbidden"; t("HTTP: scan refused for non-local callers")
c, h, b = call("/api/market/screener/scan", {"exchange": "NASDAQ"}); assert c == 202 and json.loads(b)["running"]; wait_idle()
c, h, b = call("/api/market/screener/scan", {"exchange": "NSE"}); wait_idle(); c, h, b = call("/api/market/screener/scan", {"exchange": "Narnia"}); assert c == 422 and json.loads(b)["error"]["code"] == "unsupported"; t("HTTP: scan starts locally (202), unknown exchange rejected (422)")
c, h, b = call("/api/market/screener/status"); j = json.loads(b); assert c == 200 and j["exchanges"][0]["snapshot"]["scan_date"]
c, h, b = call("/api/market/screener/search?exchanges=NSE&sort=perf_1m&dir=desc&page_size=3"); j = json.loads(b); assert c == 200 and j["total"] == 9 and len(j["rows"]) == 3 and j["rows"][0]["id"] == "NSE:ETASHT" and "_all" not in j and j["scan"]["statement"]
c, h, b = call("/api/market/screener/search?exchanges=NSE&perf_period=1Y&perf_min=0&vol_max=100&sma_period=200&sma_side=above"); assert c == 200 and all(r["perf_1y"] >= 0 for r in json.loads(b)["rows"])
c, h, b = call("/api/market/screener/nl?q=" + urllib.parse.quote("strongest NSE stocks over the last month")); j = json.loads(b); assert c == 200 and j["spec"]["exchanges"] == ["NSE"] and j["understood"] and j["result"]["rows"][0]["id"] == "NSE:ETASHT"
c, h, b = call("/api/market/screener/export?exchanges=NSE"); assert c == 200 and "text/csv" in h["Content-Type"] and "attachment" in h["Content-Disposition"] and b.decode("utf-8-sig").startswith("Company,Ticker") and len(b.decode("utf-8-sig").strip().splitlines()) == 10; t("HTTP: status, search, plain-English, CSV export")
c, h, b = call("/api/market/screener/enrich", {"ids": ["NASDAQ:AAA"]}); assert c == 200 and json.loads(b)["stored_total"] >= 1; assert call("/api/market/screener/enrich", {"ids": []})[0] == 400 and call("/api/market/screener/enrich", {"ids": ["NASDAQ:AAA"]}, {"X-Forwarded-For": "1.1.1.1"})[0] == 403; assert call("/api/market/screener/zzz")[0] == 404; t("HTTP: enrich, validation and permissions")
os.environ["FINBOT_ADMIN_TOKEN"] = "secret"; assert call("/api/market/screener/scan", {"exchange": "NYSE"})[0] == 403; c, h, b = call("/api/market/screener/scan", {"exchange": "NYSE"}, {"X-Admin-Token": "secret"}); assert c == 202; wait_idle(); os.environ.pop("FINBOT_ADMIN_TOKEN"); c, h, b = call("/api/market/screener/cancel", {}); assert c == 200; t("HTTP: admin token honoured")
# fixtures for the page tests (real backend responses over the deterministic universe)
import pathlib
fx = pathlib.Path("tests/fixtures"); fx.mkdir(exist_ok=True)
for name, path in (("screen_status", "/api/market/screener/status"), ("screen_search", "/api/market/screener/search?exchanges=NSE&sort=perf_1m&dir=desc&page_size=4"),
                   ("screen_nl", "/api/market/screener/nl?q=" + urllib.parse.quote("Find Indian stocks trading above their 200-day moving average"))):
    (fx / f"{name}.json").write_text(call(path)[2].decode())
srv.shutdown()
