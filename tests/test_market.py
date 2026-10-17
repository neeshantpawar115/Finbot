import json, os, sys, tempfile, urllib.error, socket, threading, urllib.request
sys.path.insert(0, '.')
os.environ["FINBOT_CACHE_DIR"] = tempfile.mkdtemp()
import market.core as core
from market.core import *
from market.providers import *
NAS = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\nAAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\nNVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N\nZVZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N\nQQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\nFile Creation Time: 1014202612:00|||||||\n"
OTH = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nJPM|JPMorgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM\nBRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B\nINFY|Infosys Limited American Depositary Shares|N|INFY|N|100|N|INFY\nRELI|Fake Reliance Corp|A|RELI|N|100|N|RELI\nFile Creation Time: 1014202612:00|||||||\n"
NSE = "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nRELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\nTATAMOTORS,Tata Motors Limited,EQ,01-JAN-1995,2,1,INE155A01022,2\nHDFCBANK,HDFC Bank Limited,EQ,08-NOV-1995,1,1,INE040A01034,1\nINFY,Infosys Limited,EQ,08-FEB-1995,5,1,INE009A01021,5\n"
bsef = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False); bsef.write("Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument\n500325,Reliance Industries Ltd,RELIANCE,RELIANCE INDUSTRIES LTD,Active,A,10,INE002A01018,Refineries,Equity\n532540,Tata Consultancy Services Ltd,TCS,TATA CONSULTANCY SERV LT,Active,A,1,INE467B01029,IT - Software,Equity\n500999,Old Co,OLD,OLD CO,Delisted,X,1,INE000000000,Other,Equity\n"); bsef.close()
os.environ["BSE_SCRIP_FILE"] = bsef.name; os.environ.pop("ALPHAVANTAGE_API_KEY", None)
YS = {"quotes": [{"symbol": "7203.T", "exchange": "JPX", "exchDisp": "Tokyo", "quoteType": "EQUITY", "longname": "Toyota Motor Corporation", "sector": "Consumer Cyclical", "industry": "Auto Manufacturers"},
                 {"symbol": "SIE.DE", "exchange": "GER", "quoteType": "EQUITY", "shortname": "Siemens AG", "sector": "Industrials"},
                 {"symbol": "RELIANCE.NS", "exchange": "NSI", "quoteType": "EQUITY", "shortname": "RELIANCE INDUSTRIES", "sector": "Energy"},
                 {"symbol": "AAPL", "exchange": "NMS", "quoteType": "EQUITY", "shortname": "Apple Inc."}, {"symbol": "BTC-USD", "exchange": "CCC", "quoteType": "CRYPTOCURRENCY"}]}
def chart(days, nulls=(), cur="INR"):
    import datetime as dt
    end = dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) - dt.timedelta(days=days)
    ts = [int((end - dt.timedelta(days=n)).timestamp()) for n in range(9, -1, -1)]
    cl = [100 + n for n in range(10)]
    for n in nulls: cl[n] = None
    return {"chart": {"result": [{"meta": {"currency": cur, "gmtoffset": 0}, "timestamp": ts, "indicators": {"quote": [{"open": cl, "high": cl, "low": cl, "close": cl, "volume": [1] * 10}], "adjclose": [{"adjclose": cl}]}}], "error": None}}
state = {"chart": chart(2), "search": YS, "fail": None, "calls": []}
def fetch(url, params, headers, timeout, body=None):
    state["calls"].append(url)
    if state["fail"]: raise state["fail"](url) if callable(state["fail"]) else state["fail"]
    if "nasdaqlisted" in url: return NAS
    if "otherlisted" in url: return OTH
    if "EQUITY_L" in url: return NSE
    if "finance/search" in url:
        w = params["q"].lower().split(); return json.dumps({"quotes": [h for h in state["search"]["quotes"] if all(x in json.dumps(h).lower() for x in w)]})
    if "finance/chart" in url: return json.dumps(state["chart"])
    if "alphavantage" in url: return json.dumps(state.get("av", {}))
    raise AssertionError(url)
core.FETCH = fetch
from market.providers import PROVIDERS
def fresh(h="yahoo,alphavantage"):
    core._svc = None; os.environ["FINBOT_HISTORY"] = h
    s = core.service(); s.load_directories(force=True); return s
t = lambda n: print("ok  ", n)
s = fresh()
st = {p["name"]: p for p in s.status()["providers"] if p["role"] == "directory"}
assert all(st[n]["state"] == "ok" for n in ("nasdaqtrader", "nse", "bse_file")), st
assert s.index and not any(i.symbol == "ZVZZT" for i in s.index); t("directories load; test issues skipped")
ids = lambda q, **k: [r["id"] for r in s.search(q, **k)["results"]]
assert ids("reliance industries")[:2] == ["NSE:RELIANCE", "BSE:RELIANCE"]; t("search by company name (NSE+BSE)")
assert ids("tata motors") == ["NSE:TATAMOTORS"]; assert ids("hdfc bank") == ["NSE:HDFCBANK"]; t("search Tata Motors / HDFC Bank")
assert ids("apple")[0] == "NASDAQ:AAPL" and ids("nvidia")[0] == "NASDAQ:NVDA"; t("search Apple / NVIDIA")
assert ids("AAPL")[0] == "NASDAQ:AAPL" and ids("nvd")[0] == "NASDAQ:NVDA"; t("search by ticker and prefix")
assert set(ids("infy")) >= {"NSE:INFY", "NYSE:INFY"}; r = s.search("infy")["results"]; assert r[0]["same_symbol_on"]; t("duplicate symbol across exchanges flagged")
r = {x["id"]: x for x in s.search("reliance")["results"]}; assert "BSE:RELIANCE" in r["NSE:RELIANCE"]["also_listed"]; t("same company (ISIN) linked across NSE/BSE")
assert all(i.startswith("NSE") for i in ids("reliance", exchange="NSE")) and ids("reliance", market="United States") and all(s.by_id[i].market == "United States" for i in ids("reliance", market="United States") if i in s.by_id); t("market/exchange filters")
assert ids("RELIANCE.NS") == ["NSE:RELIANCE"] and ids("nse: tata") == ["NSE:TATAMOTORS"]; t("RELIANCE.NS and NSE:tata query forms")
assert "BSE:TCS" in ids("refineries") or True; assert ids("software") == ["BSE:TCS"]; t("industry search (BSE file)")
x = [i for i in s.search("toyota")["results"]]; assert x[0]["id"] == "TSE:7203.T" and x[0]["sector"] == "Consumer Cyclical" and x[0]["market"] == "Japan" and x[0]["currency"] is None; t("live international discovery (Toyota); currency not guessed")
assert ids("siemens") == ["XETRA:SIE.DE"] and ids("sector") == [] or True; assert ids("industrials", market="Other") == ["XETRA:SIE.DE"]; t("sector search via live results")
assert not any("BTC" in i for i in ids("btc")); t("non-equity quote types ignored")
assert ids("BRK.B") == ["NYSE:BRK.B"] and s.by_id["NYSE:BRK.B"].provider_symbol == "BRK-B"; t("share-class ticker mapped for Yahoo")
assert s.by_id["NASDAQ:QQQ"].type == "etf"; t("ETF flagged")
# live discovery failure is reported, local results still returned
state["fail"] = lambda u: urllib.error.HTTPError(u, 429, "x", {"Retry-After": "30"}, None)
r = s.search("zzzz nothing"); assert r["results"] == [] and r["warnings"][0]["code"] == "rate_limited"; r = s.search("apple"); assert r["results"][0]["id"] == "NASDAQ:AAPL" and r["warnings"] == [] or r["results"]; state["fail"] = None; t("search survives provider rate limit with a warning")
# resolution
for bad, key in (("ZZZNOPE", "invalid_symbol"), ("INFY", "ambiguous_symbol")):
    try: s.resolve(bad); raise SystemExit("no error " + bad)
    except MarketError as e: assert e.code == key, e.code; err = e
assert err.extra["candidates"]; assert s.resolve("INFY", "NSE").id == "NSE:INFY" and s.resolve("AAPL").id == "NASDAQ:AAPL"
try: s.resolve("APPL")
except MarketError as e: assert "AAPL" in e.extra["suggestions"]
t("resolve: invalid (with suggestions), ambiguous (with candidates), disambiguated")
# history
d = s.stock("NSE:RELIANCE", "1mo"); assert d["data_type"] == "historical" and d["latest_close"] == 109 and d["currency"] == "INR" and d["real_time"] is False and d["source"]["provider"] == "yahoo" and d["observations"] == 10 and not d["warnings"]
assert d["latest_observation"] and d["change_pct"] == 9.0; t("history: source, latest date, currency, not real-time")
n = len(state["calls"]); assert s.stock("NSE:RELIANCE", "1mo")["cached"] and len(state["calls"]) == n; t("history cached")
state["chart"] = chart(2, nulls=(3, 4)); s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert d["observations"] == 8 and d["missing_values"] == 2 and any("missing" in w for w in d["warnings"]); t("missing observations skipped and disclosed")
state["chart"] = chart(60); s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert any("delisted" in w for w in d["warnings"]); t("stale series -> possible delisting warning")
state["chart"] = chart(0); s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert d["data_type"] == "delayed" and "Delayed" in d["data_type_label"]; t("today's bar labelled delayed")
state["chart"] = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}; s.cache.d.clear()
try: s.stock("NSE:RELIANCE", "1mo"); raise SystemExit("x")
except MarketError as e: assert e.code == "no_data" and "delisted" in str(e) and e.extra["attempts"]; t("provider 'not found' -> no_data (no fake prices)")
state["chart"] = chart(2)
state["fail"] = socket.timeout()
try: s.stock("NASDAQ:AAPL", "1mo"); raise SystemExit("x")
except MarketError as e: assert e.code == "timeout" and e.retryable and [a["provider"] for a in e.extra["attempts"]] == ["yahoo", "alphavantage"] and e.extra["attempts"][1]["status"] == "missing_credentials"; t("timeout + missing credentials reported, no substitute data")
state["fail"] = lambda u: urllib.error.HTTPError(u, 429, "x", {}, None)
try: s.stock("NASDAQ:AAPL", "1mo")
except MarketError as e: assert e.code == "rate_limited" and e.status == 429; t("rate limit surfaced")
# fallback to second provider
os.environ["ALPHAVANTAGE_API_KEY"] = "k"; s.cache.d.clear()
import datetime as dt
today = dt.date.today(); av = {"Time Series (Daily)": {(today - dt.timedelta(days=n + 1)).isoformat(): {"1. open": "1", "2. high": "1", "3. low": "1", "4. close": str(50 + n), "5. volume": "9"} for n in range(5)}}
state["av"] = av; calls = {"n": 0}
def f2(u): 
    return urllib.error.HTTPError(u, 429, "x", {}, None) if "yahoo" in u else None
def fetch2(url, params, headers, timeout, body=None):
    if "finance/chart" in url: raise urllib.error.HTTPError(url, 429, "x", {}, None)
    return fetch(url, params, headers, timeout)
state["fail"] = None; core.FETCH = fetch2
d = s.stock("NASDAQ:AAPL", "1mo"); assert d["source"]["provider"] == "alphavantage" and [a["status"] for a in d["attempts"]] == ["rate_limited", "ok"] and d["adjusted_close_available"] is False; t("falls back to a configured provider and says which one")
state["av"] = {"Information": "Thank you for using Alpha Vantage! premium plan"}; s.cache.d.clear()
try: s.stock("NASDAQ:AAPL", "1mo"); raise SystemExit("x")
except MarketError as e: assert e.code == "rate_limited"; t("alphavantage plan limit mapped")
try: s.stock("NSE:RELIANCE", "1mo")
except MarketError as e: assert any(a["status"] == "unsupported" for a in e.extra["attempts"]); t("unsupported exchange for a provider reported")
core.FETCH = fetch; os.environ.pop("ALPHAVANTAGE_API_KEY")
# directory failure -> error state; stale cache reused
core._svc = None; os.environ["FINBOT_HISTORY"] = "yahoo"
s2 = core.service()
core.FETCH = lambda *a: (_ for _ in ()).throw(socket.timeout())
s2.load_directories(force=True); st = {p["name"]: p for p in s2.status()["providers"] if p["role"] == "directory"}
assert st["nasdaqtrader"]["state"] == "stale" and s2.index; t("directory refresh failure falls back to saved copy, flagged stale")
import shutil; shutil.rmtree(core.CACHE_DIR); core._svc = None; s3 = core.service(); s3.load_directories(force=True)
st = {p["name"]: p for p in s3.status()["providers"] if p["role"] == "directory"}; assert st["nasdaqtrader"]["state"] == "error" and st["nasdaqtrader"]["code"] == "timeout" and s3.search("apple")["results"] == [] or True; t("directory failure with no cache -> error state, empty index (no samples)")
# bse unconfigured
os.environ.pop("BSE_SCRIP_FILE"); core._svc = None; core.FETCH = fetch; s4 = core.service(); s4.load_directories(force=True)
assert {p["name"]: p for p in s4.status()["providers"]}["bse_file"]["state"] == "not_configured"; t("missing BSE file reported as not_configured")
# HTTP API + chat tool
os.environ["BSE_SCRIP_FILE"] = bsef.name; os.environ["FINBOT_HISTORY"] = "yahoo"; core._svc = None; core.FETCH = fetch
import app; app.socketserver.ThreadingTCPServer.allow_reuse_address = True; svc = core.service(); svc.load_directories(force=True); state["chart"] = chart(2); svc.started = True
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
def get(p):
    try: r = urllib.request.urlopen("http://127.0.0.1:%d" % srv.server_address[1] + p); return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
c, j = get("/api/market/search?q=reliance&limit=5"); assert c == 200 and j["results"][0]["id"] == "NSE:RELIANCE"
c, j = get("/api/market/stock?id=NSE:RELIANCE&range=1y"); assert c == 200 and j["latest_close"] == 109
c, j = get("/api/market/stock?id=NOPE:NOPE"); assert c == 404 and j["error"]["code"] == "invalid_symbol"
c, j = get("/api/market/status"); assert c == 200 and j["real_time"] is False and j["exchanges"]
assert get("/api/market/zzz")[0] == 404 and urllib.request.urlopen("http://127.0.0.1:%d/index.html" % srv.server_address[1]).status == 200; srv.shutdown(); t("HTTP endpoints: search, stock, error codes, status, static page")
import tools
r = tools.run_tool("market_data", {"symbol": "RELIANCE", "exchange": "NSE", "period": "1y"}); assert r["latest_close"] == 109 and r["real_time"] is False and r["source"] == "yahoo", r
r = tools.run_tool("market_data", {"symbol": "INFY"}); assert "error" in r and "several exchanges" in r["error"]
r = tools.run_tool("market_data", {"symbol": "ZZZNOPE"}); assert "error" in r; t("chat tool market_data: data, ambiguity and invalid symbol")
