"""Run `python -m market.selfcheck` (from the main folder) with internet to verify every configured provider live."""
try:
    import app
    app.load_env()  # read the same .env file the server uses
except Exception:
    pass
import os
from . import service
from .core import env_key

print("API keys (only length and format are shown, never the key itself):")
for k in ("TWELVEDATA_API_KEY", "ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "LLM_API_KEY", "ANTHROPIC_API_KEY"):
    raw = os.environ.get(k)
    if raw is None:
        print(f"  {k:22} not set")
    else:
        flags = [w for w, bad in (("has spaces/quotes around it (ignored)", raw != raw.strip().strip("\"'").strip()), ("looks like a placeholder", "your" in raw.lower() or "key" in raw.lower() and len(raw) < 20), ("is empty", not raw.strip())) if bad]
        print(f"  {k:22} set, {len(env_key(k))} characters" + (" (" + "; ".join(flags) + ")" if flags else ""))

svc = service()
svc.load_directories(force=True)
st = svc.status()
for p in st["providers"]:
    print(f"{p['role']:12} {p['name']:13} {p.get('state', 'configured' if p['configured'] else 'not configured'):15} count={p.get('count', '-')} {p.get('message', '')}")
print("price routing:", {m: r for m, r in st["routes"].items()})
for q in ("Reliance Industries", "HDFC Bank", "Tata Motors", "Apple", "NVIDIA", "Toyota", "Siemens"):
    try:
        r = svc.search(q, limit=3)
        print(f"search {q!r}:", [x["id"] for x in r["results"]], r["warnings"] or "")
    except Exception as e:
        print("search", q, "failed:", e)
for ref in ("NSE:RELIANCE", "NASDAQ:AAPL"):
    try:
        s = svc.stock(ref, "1mo")
        print(ref, s["latest_observation"], s["latest_close"], s["currency"], s["source"]["provider"], s["data_type_label"])
        for a in s["attempts"]:  # shows why a provider was skipped or failed before the one that worked
            print("    tried", a["provider"], "->", a["status"], a.get("message", ""))
    except Exception as e:
        print(ref, "history failed:", e)
        for a in getattr(e, "extra", {}).get("attempts", []):
            print("    tried", a["provider"], "->", a["status"], a.get("message", ""))
    try:
        f = svc.fundamentals(ref)
        print("   fundamentals:", "ok from " + f["provider"] if f["available"] else "unavailable: " + str(f["reason"]))
    except Exception as e:
        print("   fundamentals failed:", e)
