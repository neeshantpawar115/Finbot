"""FinBot server: the website (index.html only), POST /api/chat (AI + finance tools), GET /api/market/* (market data),
GET /api/config and /healthz. Safe to expose publicly: only index.html is served, secrets stay in environment variables,
responses carry security headers, and per-IP rate limits protect your free API quotas.
Local:   python app.py  ->  http://localhost:8000        Hosted: set PORT (done by the host); the host adds HTTPS."""
import collections, http.server, json, os, pathlib, socketserver, threading, time, urllib.error, urllib.parse, urllib.request
import llm
import market
from llm import ApiError
from market import MarketError
from market.screener import ScanForbidden, can_scan, spec_from_query
from tools import SYSTEM, run_tool, schemas

ROOT = pathlib.Path(__file__).parent
STATIC = {"/": "/index.html", "/index.html": "/index.html"}  # the only files ever served
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"}


def load_env():
    """Reads KEY=VALUE lines from a local .env file (never overriding real environment variables); .env is never served."""
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError:
        pass


# ANTHROPIC CHAT: used when ANTHROPIC_API_KEY is set and no LLM_BASE_URL is configured.
def call_api(messages):
    """Sends one request to the Anthropic API using the server-side key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ApiError(503, "No AI key is configured on the server. Set ANTHROPIC_API_KEY, or LLM_BASE_URL + LLM_MODEL (+ LLM_API_KEY) for another provider.")
    body = json.dumps({"model": os.environ.get("FINBOT_MODEL", "claude-sonnet-5-5"), "max_tokens": 1500, "system": SYSTEM,
                       "tools": schemas(), "messages": messages}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ApiError(429 if e.code == 429 else 502, "The AI provider's rate limit was reached. Wait and retry." if e.code == 429 else f"The AI provider returned HTTP {e.code}.")
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(502, "Could not reach the AI provider.")


def chat(history):
    """Runs the tool-calling loop with whichever chat backend is configured."""
    if os.environ.get("LLM_BASE_URL"):
        return llm.chat_openai(history)
    messages, used = list(history), []
    for _ in range(6):
        resp = call_api(messages)
        if resp.get("stop_reason") != "tool_use":
            return {"text": "".join(b.get("text", "") for b in resp["content"] if b["type"] == "text"), "tools_used": used}
        messages.append({"role": "assistant", "content": resp["content"]})
        results = []
        for b in resp["content"]:
            if b["type"] == "tool_use":
                used.append(b["name"])
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(run_tool(b["name"], b["input"]))})
        messages.append({"role": "user", "content": results})
    raise ApiError(502, "The assistant used too many tool steps. Please rephrase.")


# RATE LIMITING: per-IP request limits plus a site-wide daily chat cap, so a public site cannot burn your free API quotas.
class Limiter:
    def __init__(self):
        self.hits, self.lock, self.day = collections.defaultdict(collections.deque), threading.Lock(), (None, 0)

    def allow(self, key, limit, window=60):
        now = time.time()
        with self.lock:
            if len(self.hits) > 5000:
                self.hits = collections.defaultdict(collections.deque, {k: q for k, q in self.hits.items() if q and q[-1] > now - window})
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def daily(self, cap):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            d, n = self.day
            n = n + 1 if d == today else 1
            self.day = (today, n)
            return n <= cap


LIMITER = Limiter()
env_int = lambda k, d: int(os.environ.get(k, d))


class Handler(http.server.SimpleHTTPRequestHandler):
    """Routes API calls, serves only index.html, and adds security headers to every response."""
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the visitor closed the connection mid-response; nothing to report

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _ip(self):
        xff = self.headers.get("X-Forwarded-For")  # the last entry is the one added by the host's trusted proxy
        return xff.split(",")[-1].strip() if xff else self.client_address[0]

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _csv(self, text, filename):
        data = text.encode("utf-8-sig")
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", f'attachment; filename="{filename}"')
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _screener_get(self, route, q, svc):
        sc = svc.screener
        if route == "status":
            return self._json(200, sc.status())
        if route == "search":
            return self._json(200, sc.search(spec_from_query(q)))
        if route == "nl":
            parsed = sc.parse_nl(q.get("q", ""))
            spec = {**parsed["spec"], **{k: v for k, v in spec_from_query(q).items() if k in ("page", "page_size", "sort", "dir")}}
            return self._json(200, {**parsed, "result": sc.search(spec)})
        if route == "export":
            text, res = sc.export_csv(spec_from_query(q))
            return self._csv(text, f"finbot-screen-{time.strftime('%Y%m%d', time.gmtime())}.csv")
        return self._json(404, {"error": {"code": "not_found", "message": "Unknown screener route."}})

    def _screener_post(self, route):
        if not can_scan(self.headers, self.client_address[0]):
            raise ScanForbidden("Scans spend data-provider quota, so they can only be started from the machine running FinBot, or with the FINBOT_ADMIN_TOKEN set on the server (send it as the admin token).")
        size = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(size) or b"{}") if size else {}
        sc = market.service().screener
        if route == "scan":
            market.service().ensure_loaded()
            return self._json(202, sc.start(str(body.get("exchange", "")), body.get("limit"), bool(body.get("include_etf"))))
        if route == "cancel":
            sc.cancel()
            return self._json(200, {"cancelling": True})
        if route == "enrich":
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids:
                return self._json(400, {"error": {"code": "bad_request", "message": "ids must be a non-empty list."}})
            return self._json(200, sc.enrich([str(i) for i in ids]))
        return self._json(404, {"error": {"code": "not_found", "message": "Unknown screener route."}})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if u.path == "/api/config":
            return self._json(200, llm.status())
        if not u.path.startswith("/api/market/"):
            if u.path in STATIC:
                self.path = STATIC[u.path]
                return super().do_GET()
            return self._json(404, {"error": "Not found"})
        if not LIMITER.allow(("market", self._ip()), env_int("FINBOT_RATE_MARKET", 120)):
            return self._json(429, {"error": {"code": "rate_limited", "message": "Too many requests from your address. Wait a minute and retry.", "retryable": True}})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        route, svc = u.path[len("/api/market/"):], market.service()
        svc.ensure_loaded()
        try:
            if route == "search":
                out = svc.search(q.get("q", ""), q.get("market", "all"), q.get("exchange", "all"), q.get("limit", 10))
            elif route == "stock":
                out = svc.stock(q.get("id", ""), q.get("range", "1y"))
            elif route == "compare":
                out = svc.compare([i for i in q.get("ids", "").split(",") if i.strip()], q.get("range", "1y"), q.get("fundamentals", "1") != "0")
            elif route == "quotes":
                out = svc.quotes([i for i in q.get("ids", "").split(",") if i.strip()])
            elif route == "analysis":
                out = svc.analysis(q.get("id", ""), q.get("range", "1y"))
            elif route == "fundamentals":
                out = svc.fundamentals(q.get("id", ""))
            elif route == "status":
                out = svc.status()
            elif route.startswith("screener/"):
                return self._screener_get(route[9:], q, svc)
            else:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown market route."}})
            self._json(200, out)
        except MarketError as e:
            self._json(e.status, {"error": e.to_dict()})
        except ValueError:
            self._json(400, {"error": {"code": "bad_request", "message": "Invalid parameters."}})

    def do_POST(self):
        if self.path.startswith("/api/market/screener/"):
            try:
                return self._screener_post(self.path[len("/api/market/screener/"):])
            except MarketError as e:
                return self._json(e.status, {"error": e.to_dict()})
            except (ValueError, AttributeError):
                return self._json(400, {"error": {"code": "bad_request", "message": "Invalid request."}})
        if self.path != "/api/chat":
            return self._json(404, {"error": "Not found"})
        if not LIMITER.allow(("chat", self._ip()), env_int("FINBOT_RATE_CHAT", 20)):
            return self._json(429, {"error": "Too many chat requests. Wait a minute and retry."})
        if not LIMITER.daily(env_int("FINBOT_CHAT_DAILY_CAP", 500)):
            return self._json(429, {"error": "This site's daily chat limit has been reached. Try again tomorrow."})
        try:
            size = int(self.headers.get("content-length", 0))
            if size > 100_000:
                return self._json(413, {"error": "Request too large."})
            msgs = json.loads(self.rfile.read(size))["messages"]
            ok = isinstance(msgs, list) and 0 < len(msgs) <= 30 and msgs[-1].get("role") == "user" and all(
                m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and len(m["content"]) < 8000 for m in msgs)
            if not ok:
                return self._json(400, {"error": "Invalid messages."})
            self._json(200, chat(msgs))
        except (ValueError, KeyError, AttributeError):
            self._json(400, {"error": "Invalid request body."})
        except ApiError as e:
            self._json(e.status, {"error": str(e)})


if __name__ == "__main__":
    load_env()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    market.service().ensure_loaded()  # start loading instrument directories in the background
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((host, port), Handler) as srv:
        print(f"FinBot running on http://{host}:{port}  chat backend: {llm.status()['backend']}")
        srv.serve_forever()
