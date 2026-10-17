"""Tests for Angel One, Twelve Data, per-market routing, the OpenAI-compatible chat adapter and the hardened public server."""
import datetime as dt, json, os, socket, sys, tempfile, threading, urllib.error, urllib.request
sys.path.insert(0, '.')
os.environ["FINBOT_CACHE_DIR"] = tempfile.mkdtemp()
for k in ("FINBOT_HISTORY", "FINBOT_HISTORY_INDIA", "FINBOT_HISTORY_US", "FINBOT_HISTORY_OTHER", "LLM_BASE_URL", "ANTHROPIC_API_KEY", "ALPHAVANTAGE_API_KEY", "TWELVEDATA_API_KEY", "BSE_SCRIP_FILE"):
    os.environ.pop(k, None)
import market.core as core
from market.core import MarketError
from market.providers import totp
t = lambda n: print("ok  ", n)
assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", now=59) == "287082" and totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", now=1111111109) == "081804"; t("TOTP matches RFC 6238 test vectors")
today = dt.date.today()
def candles(n=8): return [[(today - dt.timedelta(days=n - i)).isoformat() + "T00:00:00+05:30", 100 + i, 101 + i, 99 + i, 100 + i, 1000] for i in range(n)]
S = {"calls": [], "login": 0, "candle": [], "td": None, "yahoo": "ok"}
MASTER = [{"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
          {"token": "2886", "symbol": "RELIANCE-BE", "name": "RELIANCE", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
          {"token": "77", "symbol": "RELIANCE26OCTFUT", "name": "RELIANCE", "expiry": "28OCT2026", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
          {"token": "500325", "symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES", "expiry": "", "instrumenttype": "", "exch_seg": "BSE"}]
def yahoo_chart():
    ts = [int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9 - n)).timestamp()) for n in range(8)]; cl = [200 + n for n in range(8)]
    return {"chart": {"result": [{"meta": {"currency": "INR", "gmtoffset": 0}, "timestamp": ts, "indicators": {"quote": [{"open": cl, "high": cl, "low": cl, "close": cl, "volume": [1] * 8}], "adjclose": [{"adjclose": cl}]}}], "error": None}}
def fetch(url, params, headers, timeout, body=None):
    S["calls"].append((url.split("?")[0].split("/")[-1], body))
    if "OpenAPIScripMaster" in url: return json.dumps(MASTER)
    if "loginByPassword" in url:
        S["login"] += 1; b = json.loads(body); assert b["clientcode"] == "C1" and len(b["totp"]) == 6 and headers["X-PrivateKey"] == "AK"
        return json.dumps({"status": True, "data": {"jwtToken": f"jwt{S['login']}"}})
    if "getCandleData" in url:
        assert headers["Authorization"] == f"Bearer jwt{S['login']}"; b = json.loads(body); S["last"] = b
        r = S["candle"].pop(0) if S["candle"] else {"status": True, "data": candles()}
        if isinstance(r, Exception): raise r
        return json.dumps(r)
    if "twelvedata" in url:
        r = S["td"] or {"meta": {"currency": "USD"}, "values": [{"datetime": (today - dt.timedelta(days=n + 1)).isoformat(), "open": "1", "high": "2", "low": "1", "close": str(50 + n), "volume": "9"} for n in range(6)], "status": "ok"}
        S["tdparams"] = params; return json.dumps(r)
    if "finance/chart" in url:
        if S["yahoo"] != "ok": raise S["yahoo"]
        return json.dumps(yahoo_chart())
    if "nasdaqlisted" in url: return "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\nAAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\nFile Creation Time: x|||||||\n"
    if "otherlisted" in url: return "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nJPM|JPMorgan|N|JPM|N|100|N|JPM\nFile Creation Time: x|||||||\n"
    if "EQUITY_L" in url: return "SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nRELIANCE,Reliance Industries Limited,EQ,x,10,1,INE002A01018,10\n"
    if "finance/search" in url: return json.dumps({"quotes": []})
    raise AssertionError(url)
core.FETCH = fetch
def fresh():
    core._svc = None; s = core.service(); s.load_directories(force=True); s.cache.d.clear(); S["calls"].clear(); return s
def cfg_angel(): os.environ.update(ANGELONE_API_KEY="AK", ANGELONE_CLIENT_CODE="C1", ANGELONE_PIN="1234", ANGELONE_TOTP_SECRET="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
s = fresh()
st = s.status(); assert st["routes"]["India"] == ["angelone", "yahoo"] and st["routes"]["United States"] == ["twelvedata", "yahoo", "alphavantage"] and st["routes"]["*"][0] == "twelvedata"
assert [p for p in st["providers"] if p["name"] == "angelone"][0]["markets"] == ["India"]; t("default routing: India angelone>yahoo; US/global twelvedata>yahoo>alphavantage")
# India without Angel credentials -> Yahoo, and the skip is recorded
d = s.stock("NSE:RELIANCE", "1mo"); assert d["source"]["provider"] == "yahoo" and d["attempts"][0] == {"provider": "angelone", "status": "missing_credentials", "message": "Set ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, ANGELONE_PIN, ANGELONE_TOTP_SECRET."}; t("India falls back to Yahoo when Angel One is not configured")
# Angel One primary
cfg_angel(); s = fresh()
d = s.stock("NSE:RELIANCE", "1y"); assert d["source"]["provider"] == "angelone" and d["currency"] == "INR" and d["latest_close"] == 107 and d["real_time"] is False and d["attempts"] == [{"provider": "angelone", "status": "ok"}]
assert S["last"]["symboltoken"] == "2885" and S["last"]["exchange"] == "NSE" and S["last"]["interval"] == "ONE_DAY"; assert [c[0] for c in S["calls"]][:3] == ["OpenAPIScripMaster.json", "loginByPassword", "getCandleData"]; t("Angel One: token lookup (EQ series, not futures), login, candles")
n = len(S["calls"]); s.cache.d.clear(); s.stock("NSE:RELIANCE", "6mo"); assert [c[0] for c in S["calls"][n:]] == ["getCandleData"]; t("Angel One: session and token map reused")
S["candle"] = [{"status": False, "message": "Invalid Token", "errorcode": "AG8001"}]; s.cache.d.clear(); logins = S["login"]; d = s.stock("NSE:RELIANCE", "1mo"); assert d["source"]["provider"] == "angelone" and S["login"] == logins + 1; t("Angel One: expired session triggers one re-login")
S["candle"] = [{"status": False, "message": "Access denied because of exceeding access rate", "errorcode": "AB1004"}]; s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert d["source"]["provider"] == "yahoo" and d["attempts"][0]["status"] == "rate_limited"; t("Angel One rate limit -> Yahoo fallback, recorded")
S["candle"] = [{"status": True, "data": []}]; s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert d["source"]["provider"] == "yahoo" and d["attempts"][0]["status"] == "no_data"; t("Angel One empty result -> fallback")
S["candle"] = [urllib.error.HTTPError("u", 403, "x", {}, None)]; s.cache.d.clear(); d = s.stock("NSE:RELIANCE", "1mo"); assert d["attempts"][0]["status"] == "provider_error" and "403" in d["attempts"][0]["message"]; t("Angel One HTTP 403 explained")
os.environ["ANGELONE_PIN"] = "bad"; core._svc = None; s = fresh()
def badlogin(url, params, headers, timeout, body=None):
    if "loginByPassword" in url: return json.dumps({"status": False, "message": "Invalid totp", "errorcode": "AB1050"})
    return fetch(url, params, headers, timeout, body)
core.FETCH = badlogin; d = s.stock("NSE:RELIANCE", "1mo"); assert d["source"]["provider"] == "yahoo" and d["attempts"][0]["status"] == "missing_credentials" and "login failed" in d["attempts"][0]["message"]; core.FETCH = fetch; t("Angel One login failure explained, falls back")
# BSE routes through India chain too
os.environ["ANGELONE_PIN"] = "1234"; s = fresh(); s.extra["BSE:RELIANCE"] = core.make_instrument("RELIANCE", "Reliance", "BSE", "x"); d = s.stock("BSE:RELIANCE", "1mo"); assert S["last"]["symboltoken"] == "500325" and S["last"]["exchange"] == "BSE"; t("Angel One serves BSE tokens")
# Twelve Data
os.environ["TWELVEDATA_API_KEY"] = "TK"; s = fresh()
d = s.stock("NASDAQ:AAPL", "1mo"); assert d["source"]["provider"] == "twelvedata" and d["currency"] == "USD" and d["latest_close"] == 50 and S["tdparams"]["symbol"] == "AAPL" and "exchange" not in S["tdparams"] and S["tdparams"]["apikey"] == "TK"; t("Twelve Data primary for US stocks")
for resp, status in (({"code": 429, "message": "You have run out of API credits for the current minute", "status": "error"}, "rate_limited"), ({"code": 401, "message": "**apikey** parameter is incorrect", "status": "error"}, "missing_credentials"),
                     ({"code": 404, "message": "symbol not found", "status": "error"}, "no_data"), ({"code": 403, "message": "This is available on higher plans", "status": "error"}, "unsupported")):
    S["td"] = resp; s.cache.d.clear(); d = s.stock("NASDAQ:AAPL", "1mo"); assert d["source"]["provider"] == "yahoo" and d["attempts"][0]["status"] == status, (status, d["attempts"])
S["td"] = None; t("Twelve Data errors (limit, key, symbol, plan) map to codes and fall back to Yahoo")
def td401(url, params, headers, timeout, body=None):
    if "twelvedata" in url: raise urllib.error.HTTPError(url, 401, "x", {}, None)
    return fetch(url, params, headers, timeout, body)
core.FETCH = td401; s.cache.d.clear(); d = s.stock("NASDAQ:AAPL", "1mo"); a0 = d["attempts"][0]; assert d["source"]["provider"] == "yahoo" and a0["status"] == "missing_credentials" and "rejected the API key" in a0["message"] and "dashboard" in a0["message"]; core.FETCH = fetch; t("Twelve Data HTTP 401 -> clear 'key rejected' message, falls back to Yahoo")
os.environ["TWELVEDATA_API_KEY"] = ' "TK2" '; s.cache.d.clear(); S["td"] = None; s.stock("NASDAQ:AAPL", "1mo"); assert S["tdparams"]["apikey"] == "TK2"; os.environ["TWELVEDATA_API_KEY"] = "TK"; t("keys pasted with spaces or quotes are cleaned")
S["yahoo"] = socket.timeout(); S["td"] = {"code": 429, "message": "limit", "status": "error"}; s.cache.d.clear()
try: s.stock("NASDAQ:AAPL", "1mo"); raise SystemExit("x")
except MarketError as e: assert [a["provider"] for a in e.extra["attempts"]] == ["twelvedata", "yahoo", "alphavantage"] and e.code == "rate_limited"; t("all US providers fail -> typed error with full attempt trail, no substitute data")
S["yahoo"] = "ok"; S["td"] = None
s.extra["LSE:VOD"] = core.make_instrument("VOD", "Vodafone", "LSE", "yahoo", provider_symbol="VOD.L"); s.stock("LSE:VOD", "1mo"); assert S["tdparams"]["exchange"] == "LSE"; t("global stocks go to Twelve Data first with an exchange code")
zi = core.make_instrument("Z", "Zed", "Moon Exchange", "yahoo", provider_symbol="Z.MX"); s.extra[zi.id] = zi; d = s.stock("Moon Exchange:Z", "1mo"); assert d["attempts"][0]["status"] == "unsupported"; t("unmapped exchange skipped as unsupported, then next provider")
os.environ["FINBOT_HISTORY_INDIA"] = "yahoo"; core._svc = None; s = fresh(); assert s.status()["routes"]["India"] == ["yahoo"]; os.environ.pop("FINBOT_HISTORY_INDIA"); t("routes configurable by environment")
# NSE directory: slow first host -> second host; both down -> typed error
from market.providers import NseEquities
nse_hosts = []
def nse_fetch(url, params, headers, timeout, body=None):
    nse_hosts.append((url.split("/")[2], timeout, headers.get("Referer")))
    if "nsearchives" in url: raise socket.timeout()
    return fetch(url, params, headers, timeout, body)
core.FETCH = nse_fetch; items = NseEquities().load(); assert items and [h[0] for h in nse_hosts] == ["nsearchives.nseindia.com", "archives.nseindia.com"] and nse_hosts[0][1] >= 40 and nse_hosts[0][2]; t("NSE list: long timeout, falls back to the second host")
core.FETCH = lambda *a, **k: (_ for _ in ()).throw(socket.timeout())
try: NseEquities().load(); raise SystemExit("x")
except MarketError as e: assert e.code == "timeout"; t("NSE list: both hosts down -> typed timeout error")
import tempfile as _tf
nf = _tf.NamedTemporaryFile("w", suffix=".csv", delete=False); nf.write("SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nINFY,Infosys Limited,EQ,x,5,1,INE009A01021,5\n"); nf.close()
os.environ["NSE_EQUITY_FILE"] = nf.name; items = NseEquities().load(); assert [i.id for i in items] == ["NSE:INFY"]; t("NSE list: local NSE_EQUITY_FILE used when downloads fail")
os.environ.pop("NSE_EQUITY_FILE")
try: NseEquities().load(); raise SystemExit("x")
except MarketError as e: assert "NSE_EQUITY_FILE" in str(e); t("NSE list failure tells the user about the manual-file backup")
core.FETCH = fetch
# OpenAI-compatible chat
import llm, tools
os.environ.update(LLM_BASE_URL="http://x/v1/", LLM_MODEL="m1", LLM_API_KEY="k")
seq = []
def post(url, headers, body):
    seq.append((url, headers, body)); assert url == "http://x/v1/chat/completions" and headers["Authorization"] == "Bearer k" and body["model"] == "m1" and body["messages"][0]["role"] == "system"
    if len(seq) == 1: return {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "emi", "arguments": json.dumps({"principal": 500000, "annual_rate_pct": 10, "years": 5})}}]}}]}
    assert body["messages"][-1]["role"] == "tool" and json.loads(body["messages"][-1]["content"])["monthly_emi"] == 10623.52
    return {"choices": [{"message": {"content": "EMI is 10,623.52"}}]}
r = llm.chat_openai([{"role": "user", "content": "emi?"}], post); assert r == {"text": "EMI is 10,623.52", "tools_used": ["emi"]} and seq[0][2]["tools"][0]["type"] == "function"; t("OpenAI-compatible adapter: tool call executed, answer returned")
os.environ.pop("LLM_MODEL")
try: llm.chat_openai([{"role": "user", "content": "x"}], post); raise SystemExit("x")
except llm.ApiError as e: assert e.status == 503 and "LLM_MODEL" in str(e); t("missing LLM_MODEL explained")
assert llm.status() == {"chat": False, "backend": "openai-compatible", "model": ""}; os.environ["LLM_MODEL"] = "m1"; assert llm.status()["chat"] is True and "k" not in llm.status().values() and "api_key" not in llm.status(); os.environ.pop("LLM_BASE_URL"); assert llm.status()["chat"] is False; os.environ["ANTHROPIC_API_KEY"] = "a"; assert llm.status()["backend"] == "anthropic"; os.environ.pop("ANTHROPIC_API_KEY"); t("config status reports backend without leaking keys")
# public server hardening
import app
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start(); base = "http://127.0.0.1:%d" % srv.server_address[1]
def get(p, h=None, data=None):
    try: r = urllib.request.urlopen(urllib.request.Request(base + p, data, h or {})); return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e: return e.code, e.headers, e.read()
c, h, b = get("/"); assert c == 200 and b"FinBot" in b and h["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"; t("index served with security headers")
open(".env", "a").close() if not os.path.exists(".env") else None
for p in ("/app.py", "/tools.py", "/llm.py", "/market/core.py", "/.env", "/tests/test_market.py", "/.cache/dir_nse.json", "/Dockerfile", "/../app.py", "/%2e%2e/app.py", "/render.yaml"): assert get(p)[0] == 404, p
if os.path.getsize(".env") == 0: os.remove(".env")
t("source, .env, cache, tests and config files are NOT served (404)")
assert get("/healthz")[0] == 200 and json.loads(get("/api/config")[2])["chat"] is False; c, h, b = get("/", {"X-Forwarded-Proto": "https"}); assert "max-age" in h["Strict-Transport-Security"]; t("healthz, config, HSTS behind an HTTPS proxy")
os.environ["FINBOT_RATE_CHAT"] = "2"; body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(); hd = {"Content-Type": "application/json", "X-Forwarded-For": "9.9.9.9"}
codes = [get("/api/chat", hd, body)[0] for _ in range(4)]; assert codes[:2] == [503, 503] and codes[2:] == [429, 429]; assert get("/api/chat", {**hd, "X-Forwarded-For": "8.8.8.8"}, body)[0] == 503; t("per-IP chat rate limit (503 no key, then 429; other IP unaffected)")
os.environ["FINBOT_RATE_CHAT"] = "100"; os.environ["FINBOT_CHAT_DAILY_CAP"] = "1"; app.LIMITER.day = (None, 0); hd2 = {**hd, "X-Forwarded-For": "7.7.7.7"}; c1 = get("/api/chat", hd2, body)[0]; c2, _, b2 = get("/api/chat", hd2, body); assert c1 == 503 and c2 == 429 and b"daily" in b2; os.environ.pop("FINBOT_CHAT_DAILY_CAP"); t("site-wide daily chat cap")
os.environ["FINBOT_RATE_MARKET"] = "1"; c1 = get("/api/market/status", {"X-Forwarded-For": "6.6.6.6"})[0]; c2, _, b2 = get("/api/market/status", {"X-Forwarded-For": "6.6.6.6"}); assert c1 == 200 and c2 == 429 and json.loads(b2)["error"]["code"] == "rate_limited"; t("per-IP market rate limit")
srv.shutdown()
