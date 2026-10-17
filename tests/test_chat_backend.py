import json, threading, urllib.request, urllib.error, os, sys
sys.path.insert(0, '.')
import tools, app
os.environ.pop("ANTHROPIC_API_KEY", None)
e = tools.run_tool("emi", {"principal": 500000, "annual_rate_pct": 10, "years": 5}); print("emi", e)
assert e["monthly_emi"] == 10623.52
c = tools.run_tool("compound", {"principal": 100000, "annual_rate_pct": 8, "years": 10}); print("compound", c)
x = tools.run_tool("expenses", {"items": [{"category": "Rent", "amount": 15000}, {"category": "Food", "amount": 5000}]}); print("exp", x)
assert x["largest"] == "Rent" and x["by_category"][0]["share_pct"] == 75
assert "error" in tools.run_tool("market_data", {"symbol": "TCS"})
assert "error" in tools.run_tool("emi", {"principal": -1, "annual_rate_pct": 5, "years": 1})
# tool loop with a mocked model: tool_use then final text
calls = []
def fake(msgs):
    calls.append(msgs)
    if len(calls) == 1:
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "emi",
                "input": {"principal": 500000, "annual_rate_pct": 10, "years": 5}}]}
    assert json.loads(msgs[-1]["content"][0]["content"])["monthly_emi"] == 10623.52
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "EMI is 10,623.52"}]}
app.call_api = fake
r = app.chat([{"role": "user", "content": "emi?"}]); print("chat", r); assert r["tools_used"] == ["emi"]
app.call_api = lambda m: (_ for _ in ()).throw(app.ApiError(502, "down"))
# HTTP endpoint
import importlib; importlib.reload(app)  # restore real call_api (no key set)
app.socketserver.ThreadingTCPServer.allow_reuse_address = True
srv = app.socketserver.ThreadingTCPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
def post(body):
    req = urllib.request.Request("http://127.0.0.1:%d/api/chat" % srv.server_address[1], body, {"content-type": "application/json"})
    try: return urllib.request.urlopen(req).status, None
    except urllib.error.HTTPError as e: return e.code, json.load(e)
print("no key", post(json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()))
print("bad", post(b"{bad"), post(json.dumps({"messages": []}).encode()))
print("index", urllib.request.urlopen("http://127.0.0.1:%d/index.html" % srv.server_address[1]).status)
srv.shutdown()
