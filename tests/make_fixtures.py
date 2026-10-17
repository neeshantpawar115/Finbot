"""Generates analysis JSON fixtures (from the real analysis engine, synthetic prices) for the page tests."""
import datetime as dt, json, math, pathlib, sys
sys.path.insert(0, '.')
from market.analysis import compute
today = dt.date.today()
def rows(closes, vol=True, adj=True, end=None):
    end = end or today - dt.timedelta(days=1); d = end - dt.timedelta(days=len(closes) - 1); out = []
    for k, c in enumerate(closes):
        out.append({"d": d.isoformat(), "o": c, "h": c * 1.01, "l": c * .99, "c": c, "v": (1_000_000 + 1000 * k) if vol else None, "ac": c if adj else None}); d += dt.timedelta(days=1)
    return out
def path(base, n, slope, wob): return [base * (1 + slope) ** i * (1 + wob * math.sin(i / 3)) for i in range(n)]
SPECS = {"aapl": ("NASDAQ:AAPL", "Apple Inc.", "AAPL", "NASDAQ", "United States", "USD", path(150, 800, .001, .03), True, "1y"),
         "reliance": ("NSE:RELIANCE", "Reliance Industries Limited", "RELIANCE", "NSE", "India", "INR", path(2500, 400, -.0006, .02), True, "6mo"),
         "tata": ("NSE:TATAMOTORS", "Tata Motors Limited", "TATAMOTORS", "NSE", "India", "INR", path(700, 45, .003, .02), False, "1y")}
out = pathlib.Path("tests/fixtures"); out.mkdir(exist_ok=True)
for k, (id_, name, sym, ex, mk, cur, closes, vol, rng) in SPECS.items():
    inst = {"id": id_, "name": name, "symbol": sym, "exchange": ex, "market": mk, "currency": cur, "type": "equity", "sector": None, "industry": None}
    a = compute(inst, {"rows": rows(closes, vol), "missing": 0, "currency": cur, "adjusted": True, "note": "Daily bars; fixture"}, "yahoo", [{"provider": "yahoo", "status": "ok"}], rng)
    (out / f"analysis_{k}.json").write_text(json.dumps(a))
print("fixtures written")
