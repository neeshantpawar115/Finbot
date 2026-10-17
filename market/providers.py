"""Data providers. Each class only fetches and parses raw data; to add or swap one, write a class with
configured() plus load() (directory), search() (live discovery) and/or history() (prices), register it in PROVIDERS,
and list its name in FINBOT_DIRECTORIES / FINBOT_SEARCH / FINBOT_HISTORY. The UI needs no changes."""
import base64, csv, hashlib, hmac, io, json, os, struct, threading, time, urllib.parse
from datetime import datetime, timedelta, timezone
from .core import (MissingCredentials, MarketError, NoData, RateLimited, Unsupported, disk_load, disk_save, env_key, http_get, make_instrument)

UA = {"User-Agent": "Mozilla/5.0 (compatible; FinBot/0.3)", "Accept": "*/*"}


# US DIRECTORY: Nasdaq Trader's official symbol directory (every Nasdaq-listed and NYSE/NYSE American/Arca/Cboe/IEX-listed security).
class NasdaqTrader:
    name, note = "nasdaqtrader", "Official US symbol directory (no key). Names and tickers only; no sector."
    URLS = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
    OTHER = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}

    def configured(self):
        return True, ""

    @staticmethod
    def _rows(text):
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("File Creation Time")]
        head = lines[0].split("|")
        return [dict(zip(head, l.split("|"))) for l in lines[1:]]

    def load(self):
        out = []
        for url, nasdaq in zip(self.URLS, (True, False)):
            for r in self._rows(http_get(url, self.name, headers=UA)):
                if r.get("Test Issue", "N").strip() == "Y":
                    continue
                sym = (r.get("Symbol") if nasdaq else r.get("ACT Symbol") or "").strip()
                ex = "NASDAQ" if nasdaq else self.OTHER.get(r.get("Exchange", "").strip())
                if not sym or not ex:
                    continue
                nm = r.get("Security Name", "").strip()
                typ = "etf" if r.get("ETF", "").strip() == "Y" else ("other_security" if any(w in nm.lower() for w in ("warrant", "rights", " unit")) else "equity")
                out.append(make_instrument(sym, nm.split(" - ")[0], ex, self.name, type=typ, provider_symbol=sym.replace(".", "-")))
        if not out:
            raise MarketError("The directory file had no usable rows (its format may have changed).", self.name)
        return out


# INDIA DIRECTORY (NSE): the exchange's equity master CSV (symbol, company name, ISIN, series).
class NseEquities:
    name, note = "nse", "NSE equity list (no key). Names, tickers, ISIN; no sector. Falls back to a local NSE_EQUITY_FILE if NSE blocks downloads."
    URLS = ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", "https://archives.nseindia.com/content/equities/EQUITY_L.csv")
    HEADERS = {**UA, "Accept": "text/csv,*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}

    def configured(self):
        return True, ""

    def load(self):
        text, err = None, None
        for url in self.URLS:  # NSE's server is slow with scripted requests: longer timeout, and a second host as a retry
            try:
                text = http_get(url, self.name, headers=self.HEADERS, timeout=40)
                break
            except MarketError as e:
                err = e
        if text is None:
            path = os.environ.get("NSE_EQUITY_FILE")  # backup: the same EQUITY_L.csv saved manually from NSE
            if path and os.path.exists(path):
                with open(path, newline="", encoding="utf-8-sig") as f:
                    text = f.read()
            else:
                raise type(err)(f"{err} Tip: download EQUITY_L.csv from nseindia.com in your browser and set NSE_EQUITY_FILE to its path.", self.name)  # keeps the error type (timeout, rate limit...)
        out = []
        for r in csv.DictReader(io.StringIO(text), skipinitialspace=True):
            r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
            if r.get("SYMBOL"):
                out.append(make_instrument(r["SYMBOL"], r.get("NAME OF COMPANY") or r["SYMBOL"], "NSE", self.name,
                                           isin=r.get("ISIN NUMBER") or None, status=r.get("SERIES") or None,
                                           provider_symbol=r["SYMBOL"] + ".NS"))
        if not out:
            raise MarketError("The NSE file had no usable rows (its format may have changed).", self.name)
        return out


# INDIA DIRECTORY (BSE): reads a BSE scrip-list CSV you download from bseindia.com and point to with BSE_SCRIP_FILE.
class BseFile:
    name, note = "bse_file", "BSE scrip list from a local CSV (BSE_SCRIP_FILE); includes Industry when present."

    def configured(self):
        p = os.environ.get("BSE_SCRIP_FILE")
        return (True, "") if p and os.path.exists(p) else (False, "Set BSE_SCRIP_FILE to a BSE scrip-list CSV downloaded from bseindia.com.")

    def load(self):
        out = []
        with open(os.environ["BSE_SCRIP_FILE"], newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                col = lambda *keys: next((r[k] for k in r if any(x in k for x in keys) and r[k]), None)
                sid, nm = col("security id"), col("security name", "issuer name")
                if sid and nm and (col("status") or "Active").lower() == "active":
                    out.append(make_instrument(sid, nm, "BSE", self.name, isin=col("isin"), industry=col("industry"),
                                               provider_symbol=sid + ".BO"))
        if not out:
            raise MarketError("The BSE file had no usable rows.", self.name)
        return out


# YAHOO FINANCE: unofficial public endpoints used for live search across many exchanges and for daily price history.
class Yahoo:
    name, note = "yahoo", "Unofficial Yahoo Finance endpoints: live international search + end-of-day history. No SLA; personal/educational use."
    SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
    CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
    EXCH = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE", "ASE": "NYSE American", "PCX": "NYSE Arca",
            "BTS": "Cboe BZX", "NSI": "NSE", "BSE": "BSE", "LSE": "LSE", "TOR": "TSX", "GER": "XETRA", "FRA": "Frankfurt",
            "HKG": "HKEX", "JPX": "TSE", "ASX": "ASX", "SES": "SGX", "PAR": "Euronext Paris", "AMS": "Euronext Amsterdam",
            "MIL": "Borsa Italiana", "EBS": "SIX", "KSC": "KRX", "TAI": "TWSE", "SAO": "B3"}
    SUFFIX = {"NSE": ".NS", "BSE": ".BO", "LSE": ".L", "TSX": ".TO", "XETRA": ".DE", "HKEX": ".HK", "TSE": ".T", "ASX": ".AX",
              "SGX": ".SI", "Euronext Paris": ".PA", "Euronext Amsterdam": ".AS", "Borsa Italiana": ".MI", "SIX": ".SW",
              "KRX": ".KS", "TWSE": ".TW", "B3": ".SA", "Frankfurt": ".F"}

    def configured(self):
        return True, ""

    def search(self, q, limit=10):
        j = json.loads(http_get(self.SEARCH, self.name, {"q": q, "quotesCount": min(limit, 20), "newsCount": 0}, UA))
        out = []
        for h in j.get("quotes", []):
            ex = self.EXCH.get(h.get("exchange"), h.get("exchDisp") or h.get("exchange"))
            if h.get("quoteType") not in ("EQUITY", "ETF") or not h.get("symbol") or not ex:
                continue
            sym = h["symbol"]
            for suf in (".NS", ".BO"):
                if ex in ("NSE", "BSE") and sym.endswith(suf):
                    sym = sym[:-len(suf)]
            out.append(make_instrument(sym, h.get("longname") or h.get("shortname") or sym, ex, self.name,
                                       sector=h.get("sector"), industry=h.get("industry"), provider_symbol=h["symbol"],
                                       type="etf" if h["quoteType"] == "ETF" else "equity"))
        return out

    def history(self, inst, rng):
        sym = inst.provider_symbol or (inst.symbol.replace(".", "-") + ("" if inst.market == "United States" else self.SUFFIX.get(inst.exchange, "\0")))
        if "\0" in sym:
            raise Unsupported(f"No Yahoo ticker mapping is known for exchange {inst.exchange}.", self.name)
        j = json.loads(http_get(self.CHART + urllib.parse.quote(sym), self.name, {"range": rng, "interval": "1d", "includeAdjustedClose": "true"}, UA))
        ch = j.get("chart", {})
        if ch.get("error"):
            raise NoData(f"Yahoo has no price data for {sym} ({ch['error'].get('description', 'not found')}). The symbol may be invalid or the security delisted.", self.name)
        res = (ch.get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            raise NoData(f"Yahoo returned no observations for {sym}.", self.name)
        q = res["indicators"]["quote"][0]
        adj = ((res["indicators"].get("adjclose") or [{}])[0]).get("adjclose")
        off, rows, missing = res.get("meta", {}).get("gmtoffset", 0), [], 0
        for n, ts in enumerate(res["timestamp"]):
            if q["close"][n] is None:
                missing += 1
                continue
            rows.append({"d": datetime.fromtimestamp(ts + off, timezone.utc).date().isoformat(), "o": q["open"][n], "h": q["high"][n],
                         "l": q["low"][n], "c": q["close"][n], "v": q["volume"][n], "ac": adj[n] if adj else None})
        return {"rows": rows, "missing": missing, "currency": res.get("meta", {}).get("currency"), "adjusted": bool(adj),
                "note": "Daily bars; the newest bar may be intraday/delayed."}


# ALPHA VANTAGE: keyed provider (ALPHAVANTAGE_API_KEY) for daily history; free keys are heavily rate-limited and cover fewer exchanges.
class AlphaVantage:
    name, note = "alphavantage", "Needs ALPHAVANTAGE_API_KEY. Daily history for US and some international listings; NSE is not assumed to be covered."
    URL = "https://www.alphavantage.co/query"
    SUFFIX = {"BSE": ".BSE", "LSE": ".LON", "TSX": ".TRT", "XETRA": ".DEX"}
    DAYS = {"1mo": 31, "6mo": 183, "1y": 366, "5y": 1830, "max": None}

    def configured(self):
        return (True, "") if env_key("ALPHAVANTAGE_API_KEY") else (False, "Set ALPHAVANTAGE_API_KEY to enable Alpha Vantage.")

    def history(self, inst, rng):
        if inst.market == "United States":
            sym = inst.symbol
        elif inst.exchange in self.SUFFIX:
            sym = inst.symbol + self.SUFFIX[inst.exchange]
        else:
            raise Unsupported(f"Alpha Vantage mapping for {inst.exchange} is not supported here.", self.name)
        j = json.loads(http_get(self.URL, self.name, {"function": "TIME_SERIES_DAILY", "symbol": sym, "outputsize": "compact" if rng in ("1mo", "6mo") else "full",
                                                       "apikey": env_key("ALPHAVANTAGE_API_KEY")}))
        if "Error Message" in j:
            raise NoData(f"Alpha Vantage has no data for {sym}.", self.name)
        msg = j.get("Note") or j.get("Information")
        if msg:
            raise RateLimited(f"Alpha Vantage limit or plan restriction: {msg[:140]}", self.name)
        series = j.get("Time Series (Daily)")
        if not series:
            raise NoData(f"Alpha Vantage returned no series for {sym}.", self.name)
        cut = (datetime.now(timezone.utc) - timedelta(days=self.DAYS[rng])).date().isoformat() if self.DAYS[rng] else ""
        rows = [{"d": d, "o": float(v["1. open"]), "h": float(v["2. high"]), "l": float(v["3. low"]), "c": float(v["4. close"]),
                 "v": float(v["5. volume"]), "ac": None} for d, v in series.items() if d >= cut]
        return {"rows": rows, "missing": 0, "currency": inst.currency, "adjusted": False, "note": "Daily bars, unadjusted closes."}


# TWELVE DATA: keyed provider (TWELVEDATA_API_KEY) for daily history; free plan has daily/per-minute call caps and slightly delayed prices.
class TwelveData:
    name, note = "twelvedata", "Needs TWELVEDATA_API_KEY (free plan: limited calls/day and per minute). Strongest for US stocks; other exchanges depend on your plan."
    URL = "https://api.twelvedata.com/time_series"
    SIZE = {"1mo": 31, "6mo": 135, "1y": 265, "5y": 1310, "max": 5000}
    DAYS = {"1mo": 31, "6mo": 183, "1y": 366, "5y": 1830, "max": None}
    EXCH = {"NSE": "NSE", "BSE": "BSE", "LSE": "LSE", "TSX": "TSX", "XETRA": "XETR", "HKEX": "HKEX", "ASX": "ASX", "SGX": "SGX", "SIX": "SIX", "KRX": "KRX", "B3": "BVMF"}

    def configured(self):
        return (True, "") if env_key("TWELVEDATA_API_KEY") else (False, "Set TWELVEDATA_API_KEY to enable Twelve Data.")

    def history(self, inst, rng):
        params = {"symbol": inst.symbol, "interval": "1day", "outputsize": self.SIZE[rng], "order": "DESC", "apikey": env_key("TWELVEDATA_API_KEY")}
        if inst.market != "United States":
            if inst.exchange not in self.EXCH:
                raise Unsupported(f"No Twelve Data exchange code is known for {inst.exchange}.", self.name)
            params["exchange"] = self.EXCH[inst.exchange]
        try:
            j = json.loads(http_get(self.URL, self.name, params))
        except MarketError as e:
            if "HTTP 401" in str(e):
                raise MissingCredentials("Twelve Data rejected the API key (HTTP 401). Copy the key again from your Twelve Data dashboard into TWELVEDATA_API_KEY, with no spaces or quotes, then restart.", self.name)
            raise
        if j.get("status") == "error" or "values" not in j:
            code, msg = j.get("code"), str(j.get("message", "unknown error"))
            low = msg.lower()
            if code == 429:
                raise RateLimited(f"Twelve Data call limit reached: {msg[:140]}", self.name)
            if code in (401, 403) and ("api key" in low or "apikey" in low):
                raise MissingCredentials("Twelve Data rejected the API key; check TWELVEDATA_API_KEY.", self.name)
            if code == 403 or "plan" in low or "upgrade" in low:
                raise Unsupported(f"Your Twelve Data plan does not include {inst.id}: {msg[:120]}", self.name)
            if code in (400, 404):
                raise NoData(f"Twelve Data has no data for {inst.id}: {msg[:120]}", self.name)
            raise MarketError(f"Twelve Data error {code}: {msg[:140]}", self.name)
        cut = (datetime.now(timezone.utc) - timedelta(days=self.DAYS[rng])).date().isoformat() if self.DAYS[rng] else ""
        rows, missing = [], 0
        for v in j["values"]:
            if not v.get("close") or v["datetime"][:10] < cut:
                missing += 1 if not v.get("close") else 0
                continue
            f = lambda k: float(v[k]) if v.get(k) not in (None, "") else None
            rows.append({"d": v["datetime"][:10], "o": f("open"), "h": f("high"), "l": f("low"), "c": f("close"), "v": f("volume"), "ac": None})
        return {"rows": rows, "missing": missing, "currency": (j.get("meta") or {}).get("currency") or inst.currency, "adjusted": False,
                "note": "Daily bars; free-plan prices may be delayed; closes unadjusted."}


def totp(secret, now=None, digits=6, step=30):
    """RFC 6238 time-based one-time password (standard library only), as Angel One's login requires."""
    s = secret.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int((now if now is not None else time.time()) // step)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


# ANGEL ONE SMARTAPI: free NSE/BSE daily history for Angel One account holders (login with client code, PIN and TOTP; tokens come from its instrument master).
class AngelOne:
    name, note = "angelone", "India NSE/BSE daily history (free for Angel One clients). Needs ANGELONE_API_KEY, _CLIENT_CODE, _PIN, _TOTP_SECRET."
    BASE = "https://apiconnect.angelone.in"
    MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    ENV = ("ANGELONE_API_KEY", "ANGELONE_CLIENT_CODE", "ANGELONE_PIN", "ANGELONE_TOTP_SECRET")
    DAYS = {"1mo": 31, "6mo": 183, "1y": 366, "5y": 1830, "max": 1999}

    def __init__(self):
        self.jwt, self.jwt_at, self.tokens, self.lock = None, 0, None, threading.Lock()

    def configured(self):
        missing = [k for k in self.ENV if not os.environ.get(k)]
        return (False, "Set " + ", ".join(missing) + ".") if missing else (True, "")

    def _call(self, path, payload, auth=True):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
             "X-PrivateKey": os.environ["ANGELONE_API_KEY"]}
        if auth:
            h["Authorization"] = "Bearer " + self.jwt
        try:
            return json.loads(http_get(self.BASE + path, self.name, headers=h, body=json.dumps(payload).encode()))
        except MarketError as e:
            if "HTTP 403" in str(e):
                raise MarketError("Angel One refused the request (HTTP 403): check the API key, or you may be over its request-rate limit.", self.name)
            raise

    def _ensure(self, force=False):
        with self.lock:
            if self.jwt and not force and time.time() - self.jwt_at < 6 * 3600:
                return
            j = self._call("/rest/auth/angelbroking/user/v1/loginByPassword", {
                "clientcode": os.environ["ANGELONE_CLIENT_CODE"], "password": os.environ["ANGELONE_PIN"],
                "totp": totp(os.environ["ANGELONE_TOTP_SECRET"])}, auth=False)
            if not j.get("status") or not j.get("data"):
                raise MissingCredentials(f"Angel One login failed: {j.get('message', 'unknown error')}. Check client code, PIN, TOTP secret and API key.", self.name)
            self.jwt, self.jwt_at = j["data"]["jwtToken"], time.time()

    def _token(self, inst):
        if self.tokens is None:
            hit = disk_load("angel_tokens", 7 * 86400)
            if hit:
                self.tokens = hit[0]
            else:
                tk = {"NSE": {}, "BSE": {}}
                for r in json.loads(http_get(self.MASTER, self.name, timeout=120)):  # large file: parsed once, then only equities are cached
                    if r.get("exch_seg") in tk and not r.get("expiry") and not r.get("instrumenttype"):
                        for key in {r.get("name", "").upper(), r.get("symbol", "").upper()}:
                            tk[r["exch_seg"]].setdefault(key, []).append([r["symbol"], r["token"]])
                self.tokens = tk
                disk_save("angel_tokens", tk)
        c = self.tokens.get(inst.exchange, {}).get(inst.symbol.upper(), [])
        if not c:
            raise Unsupported(f"Angel One has no instrument token for {inst.id}.", self.name)
        return next((x for x in c if x[0].endswith("-EQ")), c[0])[1]

    def history(self, inst, rng):
        if inst.exchange not in ("NSE", "BSE"):
            raise Unsupported("Angel One is used here for NSE and BSE only.", self.name)
        token = self._token(inst)
        end = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        payload = {"exchange": inst.exchange, "symboltoken": token, "interval": "ONE_DAY",
                   "fromdate": (end - timedelta(days=self.DAYS[rng])).strftime("%Y-%m-%d 09:15"), "todate": end.strftime("%Y-%m-%d %H:%M")}
        for attempt in (1, 2):
            self._ensure(force=attempt == 2)
            j = self._call("/rest/secure/angelbroking/historical/v1/getCandleData", payload)
            if j.get("status"):
                break
            msg, code = str(j.get("message", "")), str(j.get("errorcode", ""))
            if attempt == 1 and (code in ("AG8001", "AG8002", "AG8003") or "token" in msg.lower()):
                continue  # session expired: log in again once
            if "rate" in msg.lower() or code == "AB1004":
                raise RateLimited(f"Angel One rate limit: {msg[:120]}", self.name)
            raise MarketError(f"Angel One error {code}: {msg[:140]}", self.name)
        data = j.get("data") or []
        if not data:
            raise NoData(f"Angel One returned no candles for {inst.id}.", self.name)
        rows = [{"d": str(r[0])[:10], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "ac": None} for r in data if r[4] is not None]
        return {"rows": rows, "missing": len(data) - len(rows), "currency": "INR", "adjusted": False,
                "note": "Daily candles from Angel One SmartAPI; unadjusted; free historical data."}


PROVIDERS = {"nasdaqtrader": NasdaqTrader, "nse": NseEquities, "bse_file": BseFile, "yahoo": Yahoo, "alphavantage": AlphaVantage,
             "twelvedata": TwelveData, "angelone": AngelOne}
