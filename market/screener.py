"""Market-wide screener. A background SCAN walks an exchange's instrument directory, fetches about a year of daily history per
instrument (batched, paced and rate-limit aware) and stores a snapshot of metrics on disk. SCREENING then filters, sorts and
pages that snapshot instantly. Every result reports the scan date, how many instruments were covered, which failed and why,
and whether the scan was partial; a partial or incomplete scan is never described as a complete market ranking."""
import csv, hmac, io, json, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from . import analysis as an
from .core import MarketError, MissingCredentials, NoData, ProviderTimeout, RateLimited, Unsupported, disk_load, disk_save


class ScanBusy(MarketError): code, status = "scan_busy", 409
class ScanForbidden(MarketError): code, status = "scan_forbidden", 403


SORTABLE = {"name", "symbol", "exchange", "price", "chg", "perf_1m", "perf_3m", "perf_6m", "perf_1y", "vol", "mdd", "avgvol", "sma200_pct", "mcap", "pe"}
PERIODS = {"1M": "perf_1m", "3M": "perf_3m", "6M": "perf_6m", "1Y": "perf_1y"}
HIGH_VOL, LOW_VOL = 40.0, 20.0  # the thresholds used for "highly volatile" and "low volatility" in plain-English queries
slug = lambda ex: "scan_" + re.sub(r"[^A-Za-z0-9]+", "_", ex)
num = lambda v: None if v in (None, "") else float(v)


def can_scan(headers, client_ip):
    """Scans spend provider quota, so they need FINBOT_ADMIN_TOKEN if one is set, otherwise a direct local connection."""
    token = os.environ.get("FINBOT_ADMIN_TOKEN")
    given = headers.get("X-Admin-Token", "")
    if token:
        return hmac.compare_digest(given, token)
    return client_ip in ("127.0.0.1", "::1") and not headers.get("X-Forwarded-For")


class Screener:
    def __init__(self, svc, workers=None, interval=None, backoff=None):
        self.svc, self.snaps, self.lock = svc, {}, threading.RLock()
        self.workers = workers or int(os.environ.get("FINBOT_SCAN_WORKERS", "4"))
        self.interval = float(os.environ.get("FINBOT_SCAN_INTERVAL", "0.15")) if interval is None else interval
        self.backoff = float(os.environ.get("FINBOT_SCAN_BACKOFF", "30")) if backoff is None else backoff
        self.state, self.cancel_ev, self.thread = {"running": False}, threading.Event(), None
        hit = disk_load("fundamentals_store")
        self.fund = hit[0] if hit else {}

    # ---------- scanning
    def providers(self):
        names = [n.strip() for n in os.environ.get("FINBOT_SCAN_PROVIDERS", "yahoo").split(",") if n.strip()]
        return [p for n in names for p in self.svc.histories if p.name == n]

    def eligible(self, exchange, include_etf=False):
        return sorted((i for i in self.svc.index if i.exchange == exchange and (include_etf or i.type == "equity")), key=lambda i: i.symbol)

    def start(self, exchange, limit=None, include_etf=False, background=True):
        with self.lock:
            if self.state.get("running"):
                raise ScanBusy(f"A scan of {self.state.get('exchange')} is already running ({self.state['done']}/{self.state['total']}).")
            full = self.eligible(exchange, include_etf)
            if not full:
                raise Unsupported(f"No instrument directory is loaded for '{exchange}', so it cannot be scanned. Check the coverage panel on the Market page.")
            uni = full[:int(limit)] if limit and int(limit) > 0 else full
            self.cancel_ev.clear()
            self.state = {"running": True, "exchange": exchange, "total": len(uni), "done": 0, "ok": 0, "failed": 0, "eligible": len(full),
                          "started": datetime.now(timezone.utc).isoformat(), "rate_limit_pauses": 0, "message": "Scanning", "paused": False}
        if background:
            self.thread = threading.Thread(target=self._run, args=(exchange, uni, len(full), include_etf), daemon=True)
            self.thread.start()
        else:
            self._run(exchange, uni, len(full), include_etf)
        return dict(self.state)

    def cancel(self):
        self.cancel_ev.set()

    def _wait(self):
        while True:
            now = time.monotonic()
            if now < self._pause_until:
                if self.cancel_ev.is_set():
                    return
                time.sleep(min(self._pause_until - now, 0.25))
                continue
            with self._gate:
                slot = max(now, self._next_slot)
                self._next_slot = slot + self.interval
            if slot > now:
                time.sleep(slot - now)
            return

    def _pause(self, err):
        try:
            delay = float(err.extra.get("retry_after"))
        except (TypeError, ValueError):
            delay = self.backoff * (2 ** min(self._rl_hits, 3))
        self._rl_hits += 1
        with self.lock:
            self._pause_until = max(self._pause_until, time.monotonic() + delay)
            self.state["rate_limit_pauses"] += 1
            self.state["message"] = f"Rate limited by the provider; pausing {delay:.0f}s"

    def _history(self, inst):
        last = None
        for hp in self.providers():
            ok, why = hp.configured()
            if not ok:
                last = MissingCredentials(why, hp.name)
                continue
            for attempt in range(3):
                if self.cancel_ev.is_set():
                    raise MarketError("Scan cancelled.")
                self._wait()
                try:
                    return hp.history(inst, "1y")
                except RateLimited as e:
                    last = e
                    self._pause(e)
                except ProviderTimeout as e:
                    last = e
                    if attempt == 1:
                        break
                except MarketError as e:
                    last = e
                    break
        raise last or MarketError("No scan provider is configured (FINBOT_SCAN_PROVIDERS).")

    def _run(self, exchange, uni, eligible, include_etf):
        rows, fails, st = {}, [], self.state
        self._pause_until, self._next_slot, self._rl_hits, self._abort = 0.0, 0.0, 0, None
        self._gate, consec = threading.Lock(), [0]

        def work(inst):
            if self.cancel_ev.is_set() or self._abort:
                return
            try:
                snap = an.snapshot(self._history(inst)["rows"])
                if not snap:
                    raise NoData("Fewer than two usable observations.", inst.provider)
                row = {"id": inst.id, "name": inst.name, "symbol": inst.symbol, "exchange": inst.exchange, "market": inst.market,
                       "currency": inst.currency, "sector": inst.sector, "industry": inst.industry, "type": inst.type, **snap}
                with self.lock:
                    rows[inst.id] = row
                    st["ok"] += 1
                    consec[0] = 0
            except MarketError as e:
                with self.lock:
                    if self.cancel_ev.is_set():
                        return
                    fails.append({"id": inst.id, "name": inst.name, "code": e.code, "message": str(e)[:160]})
                    st["failed"] += 1
                    consec[0] = consec[0] + 1 if isinstance(e, RateLimited) else 0
                    if consec[0] >= 5:
                        self._abort = "rate_limited"
            finally:
                with self.lock:
                    st["done"] = st["ok"] + st["failed"]
                    if not self._pause_until > time.monotonic():
                        st["message"] = "Scanning"
        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(work, uni))
        stopped = "cancelled" if self.cancel_ev.is_set() else self._abort
        processed = st["ok"] + st["failed"]
        last_dates = sorted(r["date"] for r in rows.values())
        snap = {"exchange": exchange, "scan_date": datetime.now(timezone.utc).date().isoformat(), "started": st["started"],
                "finished": datetime.now(timezone.utc).isoformat(), "eligible": eligible, "targeted": len(uni), "processed": processed,
                "ok": st["ok"], "failed": st["failed"], "failures": fails[:2000], "stopped": stopped, "include_etf": include_etf,
                "complete": stopped is None and len(uni) == eligible and processed == len(uni),
                "limited": len(uni) < eligible, "obs_min": last_dates[0] if last_dates else None, "obs_max": last_dates[-1] if last_dates else None,
                "providers": [p.name for p in self.providers()], "period": "up to 1 year of daily history per instrument (about 250 sessions)",
                "rows": rows}
        with self.lock:
            self.snaps[exchange] = snap
            st.update(running=False, message={"cancelled": "Cancelled; partial results kept", "rate_limited": "Stopped: the provider kept rate-limiting; partial results kept"}.get(stopped, "Finished"),
                      stopped=stopped, finished=snap["finished"])
        disk_save(slug(exchange), snap)

    def snapshot(self, exchange):
        with self.lock:
            if exchange not in self.snaps:
                hit = disk_load(slug(exchange))
                if hit:
                    self.snaps[exchange] = hit[0]
            return self.snaps.get(exchange)

    def status(self):
        exs = {}
        for i in self.svc.index:
            if i.type == "equity":
                exs[i.exchange] = exs.get(i.exchange, 0) + 1
        out = []
        for ex, n in sorted(exs.items(), key=lambda kv: -kv[1]):
            s = self.snapshot(ex)
            out.append({"exchange": ex, "eligible": n, "snapshot": None if not s else {k: s[k] for k in ("scan_date", "targeted", "processed", "ok", "failed", "complete", "limited", "stopped", "obs_min", "obs_max", "finished")}})
        return {"scan": dict(self.state), "exchanges": out, "workers": self.workers, "interval": self.interval, "providers": [p.name for p in self.providers()],
                "fundamentals_cached": len(self.fund), "directories_loading": self.svc.loading}

    # ---------- fundamentals enrichment (market cap, P/E) for instruments the user asks about
    def enrich(self, ids):
        loaded, bad = 0, []
        for n, id_ in enumerate(list(ids)[:20]):
            if n:
                time.sleep(self.interval)
            try:
                f = self.svc.fundamentals(id_)
            except MarketError as e:
                bad.append({"id": id_, "reason": str(e)})
                continue
            m = f.get("metrics") or {}
            if f.get("available") and (m.get("market_cap") or m.get("pe_ttm")):
                self.fund[f["instrument"]["id"]] = {"mcap": (m.get("market_cap") or {}).get("value"), "pe": (m.get("pe_ttm") or {}).get("value"),
                                                    "mcap_currency": (m.get("market_cap") or {}).get("currency"), "provider": f.get("provider"),
                                                    "asof": (f.get("retrieved_at") or "")[:10]}
                loaded += 1
            else:
                bad.append({"id": id_, "reason": f.get("reason") or "The provider returned no market cap or P/E."})
        disk_save("fundamentals_store", self.fund)
        return {"loaded": loaded, "unavailable": bad, "stored_total": len(self.fund), "limit_note": "At most 20 instruments per request."}

    # ---------- screening
    def screen(self, spec):
        """Filters, sorts and pages the stored snapshots. Instruments missing a metric that an active filter needs are excluded and counted."""
        wanted = spec.get("exchanges") or [e["exchange"] for e in self.status()["exchanges"] if e["snapshot"]]
        snaps, missing_ex = [], []
        for ex in wanted:
            s = self.snapshot(ex)
            (snaps if s else missing_ex).append(s or ex)
        rows = []
        for s in snaps:
            for r in s["rows"].values():
                f = self.fund.get(r["id"]) or {}
                rows.append({**r, "mcap": f.get("mcap"), "pe": f.get("pe"), "fund_asof": f.get("asof")})
        mk = spec.get("market")
        if mk and mk != "all":
            rows = [r for r in rows if ((r["market"] not in ("India", "United States")) if mk == "Other" else r["market"] == mk)]
        miss, applied = {}, []

        def rng(label, key, lo, hi, scale=1.0, unit=""):
            nonlocal rows
            lo, hi = num(lo), num(hi)
            if lo is None and hi is None:
                return
            applied.append(f"{label} " + (f"≥ {lo:g}{unit}" if lo is not None else "") + (" and " if lo is not None and hi is not None else "") + (f"≤ {hi:g}{unit}" if hi is not None else ""))
            keep = []
            for r in rows:
                v = r.get(key)
                if v is None:
                    miss[label] = miss.get(label, 0) + 1
                elif (lo is None or v / scale >= lo) and (hi is None or v / scale <= hi):
                    keep.append(r)
            rows = keep
        rng("Price", "price", spec.get("price_min"), spec.get("price_max"))
        rng("Daily change %", "chg", spec.get("change_min"), spec.get("change_max"), unit="%")
        pk = PERIODS.get(spec.get("perf_period", "1M"), "perf_1m")
        rng(f"{spec.get('perf_period', '1M')} performance %", pk, spec.get("perf_min"), spec.get("perf_max"), unit="%")
        rng("Avg volume (20d)", "avgvol", spec.get("avgvol_min"), spec.get("avgvol_max"))
        rng("Annualised volatility %", "vol", spec.get("vol_min"), spec.get("vol_max"), unit="%")
        rng("Max drawdown % (1y)", "mdd", spec.get("mdd_min"), None, unit="%")
        rng("Market cap (millions)", "mcap", spec.get("mcap_min"), spec.get("mcap_max"), scale=1e6)
        rng("P/E", "pe", spec.get("pe_min"), spec.get("pe_max"))
        if spec.get("sma_period") and spec.get("sma_side") in ("above", "below"):
            n, key = int(spec["sma_period"]), f"sma{int(spec['sma_period'])}_pct"
            applied.append(f"Price {spec['sma_side']} {n}-day moving average")
            keep = []
            for r in rows:
                v = r.get(key)
                if v is None:
                    miss[f"{n}-day moving average"] = miss.get(f"{n}-day moving average", 0) + 1
                elif (v > 0) if spec["sma_side"] == "above" else (v < 0):
                    keep.append(r)
            rows = keep
        for key, label in (("sector", "Sector"), ("industry", "Industry")):
            q = (spec.get(key) or "").strip().lower()
            if q:
                applied.append(f"{label} contains '{q}'")
                keep = []
                for r in rows:
                    if not r.get(key):
                        miss[label] = miss.get(label, 0) + 1
                    elif q in r[key].lower():
                        keep.append(r)
                rows = keep
        words = (spec.get("name_contains") or "").lower().split()
        if words:
            applied.append("Company name contains " + " ".join(f"'{w}'" for w in words))
            rows = [r for r in rows if all(w in r["name"].lower() for w in words)]
        sk, rev = spec.get("sort", "perf_1m"), spec.get("dir", "desc") != "asc"
        sk = sk if sk in SORTABLE else "perf_1m"
        txt = sk in ("name", "symbol", "exchange")
        have = [r for r in rows if r.get(sk) is not None]
        none = [r for r in rows if r.get(sk) is None]
        have.sort(key=lambda r: (str(r[sk]).lower() if txt else r[sk], r["symbol"]), reverse=rev)
        none.sort(key=lambda r: r["symbol"])
        rows = have + none  # missing values always last
        size = max(1, min(int(spec.get("page_size", 25) or 25), 100))
        pages = max(1, -(-len(rows) // size))
        page = max(1, min(int(spec.get("page", 1) or 1), pages))
        return {"total": len(rows), "page": page, "pages": pages, "page_size": size, "sort": sk, "dir": "desc" if rev else "asc",
                "rows": rows[(page - 1) * size:page * size], "excluded_missing": miss, "filters_applied": applied,
                "scan": self.coverage(snaps), "missing_exchanges": missing_ex, "fundamentals_cached": len(self.fund), "_all": rows}

    def coverage(self, snaps):
        info = []
        today = datetime.now(timezone.utc).date()
        for s in snaps:
            info.append({k: s[k] for k in ("exchange", "scan_date", "eligible", "targeted", "processed", "ok", "failed", "complete", "limited", "stopped", "obs_min", "obs_max", "period", "providers")}
                        | {"age_days": (today - date.fromisoformat(s["scan_date"])).days, "failures_sample": s["failures"][:50]})
        part = [i for i in info if not i["complete"]]
        bits = []
        for i in info:
            tail = []
            if i["failed"]:
                tail.append(f"{i['failed']:,} could not be processed")
            if i["limited"]:
                tail.append(f"limited to the first {i['targeted']:,} alphabetically")
            if i["stopped"]:
                tail.append("scan " + ("was cancelled" if i["stopped"] == "cancelled" else "stopped after repeated provider rate limits"))
            bits.append(f"{i['ok']:,} of {i['eligible']:,} {i['exchange']} instruments (scan {i['scan_date']}" + ("; " + "; ".join(tail) if tail else "") + ")")
        failed = sum(i["failed"] for i in info)
        tail = ("This is a partial ranking, not a complete market ranking." if part else
                ("Every listed equity in the directory was attempted; the instruments that could not be processed are missing from the rankings." if failed else
                 "Every listed equity in the directory was attempted and processed.") if info else "")
        text = ("Screened " + ", ".join(bits) + ". " if bits else "No exchange has been scanned yet. ") + tail
        return {"exchanges": info, "any_partial": bool(part), "statement": text.strip(), "included": sum(i["ok"] for i in info)}

    def search(self, spec):
        out = self.screen(spec)
        out.pop("_all")
        return out

    def export_csv(self, spec):
        spec = {**spec, "page": 1, "page_size": 100}
        res = self.screen(spec)
        rows = res["_all"][:10000]
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Company", "Ticker", "Exchange", "Currency", "Latest price", "Latest observation", "Day change %", "1M %", "3M %", "6M %", "1Y %",
                    "Annualised volatility %", "Max drawdown 1y %", "Avg volume 20d", "vs SMA20 %", "vs SMA50 %", "vs SMA200 %", "Market cap", "P/E", "Sector", "Industry", "Scan date"])
        sd = {i["exchange"]: i["scan_date"] for i in res["scan"]["exchanges"]}
        for r in rows:
            w.writerow([safe(r["name"]), safe(r["symbol"]), r["exchange"], r["currency"] or "", r["price"], r["date"], r["chg"], r["perf_1m"], r["perf_3m"], r["perf_6m"],
                        r["perf_1y"], r["vol"], r["mdd"], r["avgvol"], r["sma20_pct"], r["sma50_pct"], r["sma200_pct"], r["mcap"], r["pe"], r["sector"] or "",
                        r["industry"] or "", sd.get(r["exchange"], "")])
        return buf.getvalue(), res

    # ---------- plain-English queries
    def parse_nl(self, text):
        """Turns a question into screener filters with simple, visible rules; reports what it understood and assumed."""
        t, spec, got, notes = " " + (text or "").lower() + " ", {}, [], []
        ex = [e for e, pat in (("NSE", r"\bnse\b"), ("BSE", r"\bbse\b"), ("NASDAQ", r"\bnasdaq\b"), ("NYSE", r"\bnyse\b")) if re.search(pat, t)]
        if ex:
            spec["exchanges"] = ex
            got.append("Exchange: " + ", ".join(ex))
        elif re.search(r"\b(indian|india)\b", t):
            spec["market"] = "India"
            got.append("Market: India (all scanned Indian exchanges)")
        elif re.search(r"\b(us|american|united states|u\.s\.)\b", t):
            spec["market"] = "United States"
            got.append("Market: United States")
        period = None
        for pat, p in ((r"(last|past|1|one)[ -]month|30 days", "1M"), (r"(3|three) months?|quarter", "3M"), (r"(6|six) months?|half[- ]year", "6M"),
                       (r"(1|one|12|twelve)[ -](year|months?)|last year|past year|annual|yearly", "1Y")):
            if re.search(pat, t):
                period = p
                break
        strong = re.search(r"strong|best|top[- ]perform|top gainers|highest return|outperform|winners", t)
        weak = re.search(r"weak|worst|biggest losers|underperform|laggards", t)
        if period or strong or weak or re.search(r"returns?|perform", t):
            spec["perf_period"] = period or "1M"
            if not period:
                notes.append("No period given, so I used 1 month.")
            got.append("Performance period: " + spec["perf_period"])
        if strong or weak:
            spec["sort"], spec["dir"] = PERIODS[spec["perf_period"]], "desc" if strong else "asc"
            got.append(f"Sorted by {spec['perf_period']} performance, {'strongest' if strong else 'weakest'} first")
        if re.search(r"positive\s+(?:[\w-]+\s+){0,2}returns?|gaining|gained|up over|rising", t):
            spec["perf_min"] = 0.0001
            got.append(f"{spec.get('perf_period', '1M')} performance above 0%")
        elif re.search(r"negative\s+(?:[\w-]+\s+){0,2}returns?|losing|falling", t):
            spec["perf_max"] = -0.0001
            got.append(f"{spec.get('perf_period', '1M')} performance below 0%")
        if re.search(r"highly volatile|high volatility|very volatile|most volatile", t):
            spec["vol_min"], spec["sort"], spec["dir"] = HIGH_VOL, "vol", "desc"
            got.append(f"Annualised volatility ≥ {HIGH_VOL:g}% (my definition of 'highly volatile'), most volatile first")
        elif re.search(r"low volatility|less volatile|stable|calm", t):
            spec["vol_max"], spec["sort"], spec["dir"] = LOW_VOL, "vol", "asc"
            got.append(f"Annualised volatility ≤ {LOW_VOL:g}% (my definition of 'low volatility')")
        m = re.search(r"(above|over|below|under)\s+(?:their|its|the)?\s*(20|50|200)[- ]?(?:day)?\s*(?:moving average|ma|sma|dma)", t)
        if m:
            spec["sma_period"], spec["sma_side"] = int(m.group(2)), "above" if m.group(1) in ("above", "over") else "below"
            got.append(f"Price {spec['sma_side']} its {m.group(2)}-day moving average")
        else:
            m = re.search(r"(under|below|less than|cheaper than|above|over|more than)\s*(?:rs\.?|₹|\$)?\s*([\d,]+(?:\.\d+)?)(?!\s*(?:day|%|percent))", t)
            if m:
                v = float(m.group(2).replace(",", ""))
                key = "price_max" if m.group(1) in ("under", "below", "less than", "cheaper than") else "price_min"
                spec[key] = v
                got.append(f"Price {'≤' if key == 'price_max' else '≥'} {v:g}")
        for kw, name in (("bank", "bank"), ("banking", "bank"), ("banks", "bank"), ("pharma", "pharma"), ("pharmaceutical", "pharma"), ("insurance", "insur"), ("steel", "steel"), ("cement", "cement"), ("finance", "financ")):
            if re.search(rf"\b{kw}\b", t):
                spec["name_contains"] = name
                got.append(f"Company name contains '{name}'")
                notes.append("This universe has no sector classification, so industry words are matched against company names only, which can miss or include the wrong companies.")
                break
        m = re.search(r"\btop\s+(\d{1,3})\b|\b(\d{1,3})\s+(?:stocks|companies|results)", t)
        if m:
            spec["page_size"] = min(int(m.group(1) or m.group(2)), 100)
            got.append(f"Show {spec['page_size']} results per page")
        spec.setdefault("sort", PERIODS.get(spec.get("perf_period", "1M"), "perf_1m"))
        spec.setdefault("dir", "desc")
        return {"spec": spec, "understood": got, "notes": notes}

    def tool_screen(self, a):
        """Compact result for the chat model; all numbers come from the stored scan."""
        spec = {k: a.get(k) for k in ("exchanges", "market", "perf_period", "perf_min", "perf_max", "price_min", "price_max", "change_min", "change_max", "avgvol_min",
                                      "vol_min", "vol_max", "mdd_min", "mcap_min", "mcap_max", "pe_min", "pe_max", "sma_period", "sma_side", "name_contains", "sector", "industry") if a.get(k) is not None}
        spec["sort"], spec["dir"], spec["page_size"] = a.get("sort_by") or "perf_1m", a.get("sort_dir") or "desc", min(int(a.get("limit") or 10), 25)
        if isinstance(spec.get("exchanges"), str):
            spec["exchanges"] = [spec["exchanges"]]
        r = self.screen(spec)
        if not r["scan"]["exchanges"]:
            raise NoData("No exchange has been scanned yet" + (f" (requested: {', '.join(r['missing_exchanges'])})" if r["missing_exchanges"] else "") + ". Open the Stock Screener page and press Scan for the exchange first.")
        keep = ("id", "name", "price", "currency", "date", "chg", "perf_1m", "perf_3m", "perf_6m", "perf_1y", "vol", "mdd", "avgvol", "sma200_pct", "mcap", "pe")
        return {"matches": r["total"], "returned": len(r["rows"]), "sorted_by": r["sort"], "direction": r["dir"], "filters": r["filters_applied"],
                "excluded_for_missing_data": r["excluded_missing"], "coverage": r["scan"]["statement"], "partial_scan": r["scan"]["any_partial"],
                "results": [{k: row[k] for k in keep} for row in r["rows"]], "note": "End-of-day data from the last scan, not live. Percentages are price returns."}


def spec_from_query(q):
    """Maps URL query parameters (all strings) to a screener spec."""
    spec = {k: q[k] for k in ("market", "perf_period", "perf_min", "perf_max", "price_min", "price_max", "change_min", "change_max", "avgvol_min", "avgvol_max",
                              "vol_min", "vol_max", "mdd_min", "mcap_min", "mcap_max", "pe_min", "pe_max", "sma_period", "sma_side", "name_contains",
                              "sector", "industry", "sort", "dir", "page", "page_size") if q.get(k) not in (None, "")}
    if q.get("exchanges"):
        spec["exchanges"] = [e for e in q["exchanges"].split(",") if e]
    return spec
