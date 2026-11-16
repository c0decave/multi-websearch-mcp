"""Offline-Tests der Kernlogik (core.py) und der Orchestrierung (engine.py)."""
import asyncio
import time

import pytest

from websearch.core import (InputError, BlockedError, BreakerState, CircuitBreaker, RateLimited, RateLimiter, Result,
                            StateStore, merge_rrf, normalize_date, normalize_url)
from websearch.engine import SearchEngine, TierPolicy
from websearch.providers import Provider, ResultList, _lang_code, _unwrap_redirect


# =========================================================================== Normalisierung

def test_normalize_url():
    assert normalize_url("http://www.Example.com/a/?utm_source=x&b=2") == "https://example.com/a?b=2"
    assert normalize_url("https://example.com") == normalize_url("https://example.com/")
    assert normalize_url("https://de.wikipedia.org/wiki/Künstliche_Intelligenz") == \
        normalize_url("https://de.wikipedia.org/wiki/K%C3%BCnstliche_Intelligenz")
    assert normalize_url("https://m.example.com/x") == "https://example.com/x"
    assert normalize_url("https://m.de/x") == "https://m.de/x"            # Domain "m.de" bleibt
    assert normalize_url("https://host:8080/x") != normalize_url("https://host/x")   # Port zaehlt
    assert normalize_url("https://host:443/x") == normalize_url("https://host/x")
    assert normalize_url("http://[::1") == "http://[::1"                # kaputte URL: kein Absturz


def test_normalize_date_formats():
    assert normalize_date("Tue, 03 Nov 2026 10:00:00 GMT") == "2026-11-03T10:00:00+00:00"
    assert normalize_date("2026-11-03T12:00:00Z") == "2026-11-03T12:00:00+00:00"
    assert normalize_date("2026-11-03T13:00:00+01:00") == "2026-11-03T12:00:00+00:00"
    assert normalize_date("2026-11-03T12:00:00") == "2026-11-03T12:00:00+00:00"      # ohne TZ = UTC
    assert normalize_date("20261103T120000Z") == "2026-11-03T12:00:00+00:00"          # GDELT
    assert normalize_date(0) == "1970-01-01T00:00:00+00:00"
    assert normalize_date("vor 3 Stunden") is None
    assert normalize_date(None) is None and normalize_date("") is None


def test_unwrap_bing_redirect_and_lang():
    u = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&url=https%3a%2f%2fexample.com%2fx&c=1"
    assert _unwrap_redirect(u) == "https://example.com/x"
    assert _lang_code("de-DE") == "de" and _lang_code("en_US") == "en"
    assert _lang_code("auto") == "en" and _lang_code("evil.com/x#") == "en"   # schuetzt Hostnamen


def test_result_from_dict_ignores_unknown_fields():
    r = Result.from_dict({"title": "t", "url": "https://u.de", "future_field": 1})
    assert r.title == "t" and r.url == "https://u.de"
    assert Result("x", "javascript:alert(1)").url == ""                # nur http(s)


def test_rrf_consensus_dedupe_and_no_mutation():
    a = [Result("A", "https://a.com", source="s1", rank=1), Result("B", "https://b.com", source="s1", rank=2),
         Result("A2", "https://a.com/", source="s1", rank=3)]                    # Duplikat in derselben Quelle
    b = [Result("B", "https://www.b.com/", source="s2", rank=1), Result("C", "https://c.com", source="s2", rank=2)]
    merged = merge_rrf([a, b])
    assert merged[0].url == "https://b.com" and set(merged[0].engines) == {"s1", "s2"}
    assert len(merged) == 3
    assert merged[1].url == "https://a.com" and abs(merged[1].score - 1 / 61) < 1e-9
    assert a[1].score == 0.0 and a[1].engines == []                             # Eingaben unveraendert


def test_news_title_dedupe_prefers_direct_url():
    g = [Result("Bundestag beschliesst neues KI-Gesetz - tagesschau.de", "https://news.google.com/rss/articles/X",
                source="google_news", rank=1, redirect=True)]
    b = [Result("Bundestag beschliesst neues KI-Gesetz", "https://www.tagesschau.de/ki", source="bing_rss", rank=2)]
    merged = merge_rrf([g, b], dedupe_titles=True)
    assert len(merged) == 1 and merged[0].url == "https://www.tagesschau.de/ki" and not merged[0].redirect
    assert set(merged[0].engines) == {"google_news", "bing_rss"}
    assert len(merge_rrf([g, b])) == 2                                   # ohne Titel-Dedupe (Websuche)
    b2 = [Result("Bundestag beschließt neues KI-Gesetz", "https://www.tagesschau.de/ki", source="bing_rss")]
    assert len(merge_rrf([g, b2], dedupe_titles=True)) == 2              # bewusst kein Fuzzy-Matching


# =========================================================================== Tempo

def run(coro):
    return asyncio.run(coro)


def test_rate_limiter_spacing():
    async def go():
        lim = RateLimiter(min_interval=0.1, concurrency=5)
        stamps = []

        async def hit():
            async with lim.slot():
                stamps.append(time.monotonic())
        await asyncio.gather(*[hit() for _ in range(5)])
        return sorted(stamps)
    s = run(go())
    assert all(b - a >= 0.09 for a, b in zip(s, s[1:]))


def test_rate_limiter_budget_skips_without_claiming(tmp_path):
    async def go():
        lim = RateLimiter(min_interval=5, concurrency=1)
        lim.attach(StateStore(tmp_path / "s.db"), "gdelt")
        async with lim.slot():
            pass                                           # naechster Slot in 5 s
        with pytest.raises(RateLimited) as ei:
            async with lim.slot(budget=1):
                pass
        assert 4 < ei.value.wait <= 5
        assert 4 < lim._wait_estimate() <= 5               # nichts zusaetzlich reserviert
        assert not lim._sem.locked()
    run(go())


def test_rate_limiter_refunds_slot_and_semaphore_on_cancel(tmp_path):
    async def go():
        store = StateStore(tmp_path / "s.db")
        lim = RateLimiter(min_interval=2, concurrency=1)
        lim.attach(store, "x")
        async with lim.slot():
            pass

        async def waiter():
            async with lim.slot():
                pass
        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert not lim._sem.locked()
        assert lim._wait_estimate() <= 2                   # Slot zurueckgegeben (sonst ~4 s)
    run(go())


def test_rate_limit_slots_shared_between_processes(tmp_path):
    a, b = StateStore(tmp_path / "s.db"), StateStore(tmp_path / "s.db")
    t0 = time.time()
    assert a.claim_slot("gdelt", 5) - t0 < 0.5
    assert 4.5 < b.claim_slot("gdelt", 5) - t0 <= 5.5     # zweiter "Prozess" bekommt den naechsten Slot
    assert b.claim_slot("gdelt", 5, max_wait=1) is None   # zu weit weg -> keine Reservierung


# =========================================================================== Circuit-Breaker

def test_breaker_backoff_no_escalation_while_open():
    br = CircuitBreaker("x", base_cooldown=10)
    br.trip("x")
    first = br.open_until - time.time()
    br.trip("noch eine wartende Anfrage")
    assert br.strikes == 1 and br.last_error == "noch eine wartende Anfrage"
    br.state = br.state._replace(open_until=0)                       # Pause abgelaufen
    br.trip("wieder gesperrt")
    assert br.is_open() and br.strikes == 2 and br.open_until - time.time() > first * 1.8


def test_breaker_success_from_before_block_is_ignored():
    br = CircuitBreaker("x", base_cooldown=60)
    br.trip("captcha")
    br.success()                                                     # spaete Antwort einer alten Anfrage
    assert br.is_open() and br.strikes == 1
    br.state = br.state._replace(open_until=0)
    br.success()
    assert br.state == BreakerState(0, 0, None, 0)


def test_breaker_silent_block():
    br = CircuitBreaker("y")
    br.note_empty(True)
    br.note_empty(True)
    br.note_empty(False)                                             # andere auch leer -> zurueck auf 0
    assert br.empty_streak == 0
    for _ in range(3):
        br.note_empty(others_found=True)
    assert br.is_open() and "stille Sperre" in br.last_error


def test_breaker_state_is_shared_between_processes(tmp_path):
    db = tmp_path / "s.db"
    a = CircuitBreaker("google", base_cooldown=60, store=StateStore(db))
    a.trip("captcha")
    b = CircuitBreaker("google", base_cooldown=60, store=StateStore(db))   # "anderer Prozess"
    assert b.is_open() and b.last_error == "captcha" and b.strikes == 1
    StateStore(db).reset_state()
    assert not CircuitBreaker("google", store=StateStore(db)).is_open()


def test_cache_ttl_purge_and_version(tmp_path):
    st = StateStore(tmp_path / "c.db", ttl=100)
    st.set("k", [1])
    assert st.get("k") == [1] and st.get("k", ttl=0) is None
    st._db.execute("UPDATE cache SET t = 0")
    assert StateStore(tmp_path / "c.db").get("k", ttl=10**12) is None    # beim Oeffnen geloescht
    assert StateStore.key("a") != StateStore.key("b")


# =========================================================================== Orchestrierung

class Fake(Provider):
    min_interval = 0

    def __init__(self, name, results=(), block=False, delay=0.0, kinds=("web", "news", "ref"), meta=None,
                 exc=None, scraped=False):
        self.name, self._results, self._block, self._delay = name, list(results), block, delay
        self.kinds, self._meta, self._exc, self.scraped, self.calls = kinds, meta or {}, exc, scraped, 0
        super().__init__()

    async def search(self, client, query, n, lang, kind):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._block:
            raise BlockedError("captcha")
        if self._exc:
            raise self._exc
        return ResultList([Result(t, u, source=self.name, rank=i, published=d)
                           for i, (t, u, d) in enumerate(self._results, 1)], **self._meta)


class NoKey(Fake):
    available = property(lambda self: False)


def R(*urls, date=None):
    return [(u, u, date) for u in urls]


POLICY = TierPolicy(primary={"web": ["p1", "p2"], "news": ["p1"], "ref": ["p1"]},
                    fallback={"web": ["f1"]}, paid={"web": ["paid1", "paid2"]})


def make(tmp_path, *provs, policy=POLICY, **kw):
    names = {p.name for p in provs}
    pol = TierPolicy(**{t: {k: [n for n in v if n in names] for k, v in getattr(policy, t).items()}
                        for t in ("primary", "fallback", "paid")})
    return SearchEngine({p.name: p for p in provs}, policy=pol, cache_path=tmp_path / "c.db", **kw)


def states(res):
    return {k: v["state"] for k, v in res["sources"].items()}


def test_engine_requires_async_with(tmp_path):
    eng = make(tmp_path, Fake("p1"))
    with pytest.raises(RuntimeError, match="async with"):
        run(eng.search("q"))


def test_policy_with_unknown_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unbekannte Quellen"):
        SearchEngine({"p1": Fake("p1")}, policy=TierPolicy(primary={"web": ["p1", "tippfehler"]}),
                     cache_path=tmp_path / "c.db")


def test_block_pauses_source_and_cache_is_used(tmp_path):
    async def go():
        p1, p2 = Fake("p1", block=True), Fake("p2", R("https://x.com", "https://y.com", "https://z.com"))
        f1 = Fake("f1", R("https://f.com"))
        async with make(tmp_path, p1, p2, f1) as eng:
            res = await eng.search("q", n=4)
            assert states(res) == {"p1": "blocked", "p2": "ok"}      # 3 >= min(4, max(3, 2)) -> kein Fallback
            res2 = await eng.search("q", n=4)
            assert states(res2) == {"p1": "paused", "p2": "cached"} and p1.calls == 1 and p2.calls == 1
    run(go())


def test_small_n_does_not_trigger_fallback(tmp_path):
    async def go():
        f1 = Fake("f1", R("https://f.com"))
        async with make(tmp_path, Fake("p1", R("https://a.com", "https://b.com")), f1) as eng:
            await eng.search("q", n=2)
            assert f1.calls == 0
    run(go())


def test_page_size_shares_cache_between_n(tmp_path):
    async def go():
        p1 = Fake("p1", R(*[f"https://x.com/{i}" for i in range(12)]))
        async with make(tmp_path, p1) as eng:
            a = await eng.search("q", n=8)
            b = await eng.search("q", n=10)
            assert len(a["results"]) == 8 and len(b["results"]) == 10 and p1.calls == 1
    run(go())


def test_fallback_then_paid_walks_until_success(tmp_path):
    async def go():
        paid1 = Fake("paid1", exc=BlockedError("HTTP 401 - API-Key ungueltig"))
        paid2 = Fake("paid2", R("https://q.com"))
        async with make(tmp_path, Fake("p1", block=True), Fake("p2"), Fake("f1", R("https://f.com")),
                        paid1, paid2) as eng:
            res = await eng.search("q")
            assert states(res)["paid1"] == "blocked" and states(res)["paid2"] == "ok"
            res = await eng.search("q2")                            # paid1 jetzt pausiert -> nicht gefragt
            assert states(res)["paid1"] == "paused" and paid1.calls == 1 and paid2.calls == 2
            res = await eng.search("q3", paid=False)
            assert "paid2" not in res["sources"]
    run(go())


def test_paid_only_one_successful_call_and_not_for_rare_queries(tmp_path):
    async def go():
        paid1, paid2 = Fake("paid1", R("https://p.com")), Fake("paid2", R("https://q.com"))
        async with make(tmp_path, Fake("p1"), Fake("p2"), Fake("f1"), paid1, paid2) as eng:
            await eng.search("seltene anfrage")                     # alles ok, nur wenig -> nicht bezahlen
            assert paid1.calls == paid2.calls == 0
            eng.providers["p1"]._block = True
            await eng.search("q2")
            assert paid1.calls == 1 and paid2.calls == 0
    run(go())


def test_paid_skipped_when_key_missing(tmp_path):
    async def go():
        paid2 = Fake("paid2", R("https://q.com"))
        async with make(tmp_path, Fake("p1", block=True), Fake("p2"), Fake("f1"), NoKey("paid1"), paid2) as eng:
            res = await eng.search("q")
            assert "paid2" in res["sources"] and "paid1" not in res["sources"]
    run(go())


def test_deadline_returns_partial_results(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com", "https://b.com", "https://c.com")),
                        Fake("p2", R("https://slow.com"), delay=5)) as eng:
            t = time.monotonic()
            res = await eng.search("q", n=4, deadline=0.5)
            assert time.monotonic() - t < 2
            assert states(res)["p2"] == "deadline" and len(res["results"]) == 3
            assert "Zeitbudget" in res["sources"]["p2"]["message"] and not res["sources"]["p2"]["meta"]
    run(go())


def test_source_bug_is_isolated_and_logged(tmp_path, caplog):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com", "https://b.com", "https://c.com")),
                        Fake("p2", exc=ZeroDivisionError("Bug"))) as eng:
            res = await eng.search("q")
            assert states(res) == {"p1": "ok", "p2": "error"} and len(res["results"]) == 3
    with caplog.at_level("ERROR", logger="websearch"):
        run(go())
    assert any("Unerwarteter Fehler in Quelle p2" in r.message and r.exc_info for r in caplog.records)


def test_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com"))
        async with make(tmp_path, p1) as eng:
            await eng.search("q")
            eng.store._db.execute("UPDATE cache SET v='{kaputt'")
            res = await eng.search("q")
            assert states(res) == {"p1": "ok"} and p1.calls == 2
    run(go())


def test_meta_cannot_clobber_status_fields(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com"), meta={"count": 99, "engines_failed": [["google", "CAPTCHA"]]})
        async with make(tmp_path, p1) as eng:
            st = (await eng.search("q", sources=["p1"]))["sources"]["p1"]
            assert st["count"] == 1 and st["meta"]["count"] == 99
            cached = (await eng.search("q", sources=["p1"]))["sources"]["p1"]
            assert cached["state"] == "cached" and cached["meta"]["engines_failed"]
    run(go())


def test_unsupported_and_not_configured(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://n.com"), kinds=("web",)), NoKey("p2")) as eng:
            res = await eng.search("q", kind="news", sources=["p1", "p2"])
            assert states(res) == {"p1": "unsupported", "p2": "not_configured"}
    run(go())


def test_dates_normalized_and_news_sorted(tmp_path):
    async def go():
        p1 = Fake("p1", [("old", "https://o.com", "Thu, 01 Jan 2026 00:00:00 GMT"), ("none", "https://x.com", None),
                         ("new", "https://n.com", "2026-11-04T00:00:00Z")])
        async with make(tmp_path, p1) as eng:
            res = await eng.search("q", kind="news")
            assert [r["title"] for r in res["results"]] == ["new", "old", "none"]
            assert res["results"][1]["published"] == "2026-01-01T00:00:00+00:00"
    run(go())


def test_silent_block_only_for_scraped_sources(tmp_path):
    async def go():
        scraped, official = Fake("p2", scraped=True), Fake("f1")
        pol = TierPolicy(primary={"web": ["p1", "p2", "f1"]})
        async with make(tmp_path, Fake("p1", R("https://a.com", "https://b.com", "https://c.com")),
                        scraped, official, policy=pol) as eng:
            for i in range(3):
                await eng.search(f"q{i}")
            assert scraped.breaker.is_open() and not official.breaker.is_open()
    run(go())


def test_queued_requests_do_not_escalate_or_hit_blocked_source(tmp_path):
    async def go():
        p1 = Fake("p1", block=True)
        p1.limiter = RateLimiter(0.05, 1)
        async with make(tmp_path, p1, Fake("p2", R("https://a.com", "https://b.com", "https://c.com"))) as eng:
            await eng.search_many([f"q{i}" for i in range(6)], concurrency=6)
            assert p1.calls == 1 and p1.breaker.strikes == 1
    run(go())


def test_rate_limited_source_is_skipped_not_awaited(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com", "https://b.com", "https://c.com"))
        slow = Fake("p2", R("https://s.com"))
        slow.limiter = RateLimiter(30, 1)
        async with make(tmp_path, p1, slow) as eng:
            await eng.search("q1")
            t = time.monotonic()
            res = await eng.search("q2")
            assert states(res)["p2"] == "rate_limited" and time.monotonic() - t < 1
    run(go())


def test_fallback_skips_engine_searxng_reports_blocked(tmp_path):
    async def go():
        sx = Fake("sx", R("https://a.com"), meta={"engines_failed": [["duckduckgo", "CAPTCHA"]]})
        ddg, other = Fake("ddg", R("https://d.com")), Fake("other", R("https://o.com"))
        ddg.same_engine = "duckduckgo"
        pol = TierPolicy(primary={"web": ["sx"]}, fallback={"web": ["ddg", "other"]})
        async with make(tmp_path, sx, ddg, other, policy=pol) as eng:
            res = await eng.search("q")
            assert "ddg" not in res["sources"] and other.calls == 1 and ddg.calls == 0
    run(go())


def test_default_news_cache_is_short(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com"))
        async with make(tmp_path, p1) as eng:
            await eng.search("q", kind="news")
            eng.store._db.execute("UPDATE cache SET t = t - 1500")      # 25 min alt
            await eng.search("q", kind="news")
            assert p1.calls == 2                                         # News-Cache (20 min) abgelaufen
            await eng.search("q", kind="web")
            eng.store._db.execute("UPDATE cache SET t = t - 1500")
            await eng.search("q", kind="web")
            assert p1.calls == 3                                         # Web-Cache (6 h) greift
    run(go())


def test_input_validation(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1")) as eng:
            for kw in ({"query": " "}, {"query": "q", "kind": "bilder"}, {"query": "q", "sources": ["nope"]},
                       {"query": "x" * 2001}):
                with pytest.raises(ValueError):
                    await eng.search(**kw)
            res = await eng.search_many(["a", "", "b"])
            assert [r["query"] for r in res] == ["a", "", "b"] and "error" in res[1]
    run(go())


def test_search_many_respects_concurrency(tmp_path):
    running = peak = 0

    class Count(Fake):
        async def search(self, client, query, n, lang, kind):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return [Result("x", f"https://x.com/{query}/{i}", source=self.name, rank=i) for i in (1, 2, 3)]

    async def go():
        p1 = Count("p1")
        p1.limiter = RateLimiter(0, 100)
        async with make(tmp_path, p1) as eng:
            res = await eng.search_many([f"q{i}" for i in range(10)], concurrency=3)
            assert len(res) == 10 and peak <= 3 and all(len(r["results"]) == 3 for r in res)
    run(go())


def test_status_report(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", block=True), NoKey("p2")) as eng:
            await eng.search("q", sources=["p1"])
            st = eng.status()
            assert st["p1"]["blocked"] and st["p1"]["retry_in_s"] > 0 and not st["p2"]["available"]
    run(go())



# =========================================================================== Runde-3-Fixes

def test_cancelling_a_search_cancels_its_source_requests(tmp_path):
    async def go():
        slow = Fake("p1", R("https://a.com"), delay=2)
        async with make(tmp_path, slow) as eng:
            t = asyncio.create_task(eng.search("q"))
            await asyncio.sleep(0.2)
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            await asyncio.sleep(0.05)
            alive = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
            assert alive == []                                    # keine verwaisten Quellen-Anfragen
            assert not slow.limiter._sem.locked()
    run(go())


def test_ms_is_measured_per_source(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com")), Fake("p2", R("https://b.com"), delay=0.3)) as eng:
            st = (await eng.search("q"))["sources"]
            assert st["p1"]["ms"] < 150 <= st["p2"]["ms"]
    run(go())


def test_engine_lifecycle_rules(tmp_path):
    async def go():
        eng = make(tmp_path, Fake("p1", R("https://a.com")))
        assert not eng.is_open
        async with eng:
            assert eng.is_open
            with pytest.raises(RuntimeError, match="einmal"):
                await eng.__aenter__()
        assert not eng.is_open
        with pytest.raises(RuntimeError):
            await eng.search("q")                                   # nach dem Schliessen verbraucht
        with pytest.raises(RuntimeError, match="einmal"):
            async with eng:
                pass
    run(go())


def test_provider_instance_cannot_be_shared_between_engines(tmp_path):
    p = Fake("p1")

    async def go():
        async with SearchEngine({"p1": p}, policy=TierPolicy(primary={"web": ["p1"]}), cache_path=tmp_path / "a.db"):
            pass
        with pytest.raises(ValueError, match="anderen Engine"):
            async with SearchEngine({"p1": p}, policy=TierPolicy(primary={"web": ["p1"]}),
                                    cache_path=tmp_path / "b.db"):
                pass
    run(go())


@pytest.mark.parametrize("kw,msg", [
    ({"primary": {"wbe": ["p1"]}}, "unbekannte Suchart"),
    ({"primary": {"web": "p1"}}, "Liste von Quellnamen"),
    ({"primary": ["p1"]}, "Dict"),
])
def test_tier_policy_validation(kw, msg):
    with pytest.raises(InputError, match=msg):
        TierPolicy(**kw)


def test_tier_policy_is_deeply_immutable():
    pol = TierPolicy(primary={"web": ["a"]})
    with pytest.raises((TypeError, AttributeError)):
        pol.primary["web"].append("b")
    with pytest.raises(TypeError):
        pol.primary["news"] = ("x",)


def test_no_usable_source_for_kind_is_an_error(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", kinds=("web",)), policy=TierPolicy(primary={"web": ["p1"]})) as eng:
            with pytest.raises(InputError, match="keine nutzbare Quelle"):
                await eng.search("q", kind="news")
    run(go())


def test_db_errors_in_breaker_do_not_crash_the_search(tmp_path, monkeypatch):
    import sqlite3

    async def go():
        async with make(tmp_path, Fake("p1", block=True), Fake("p2", R("https://a.com", "https://b.com", "https://c.com"))) as eng:
            def locked(*a, **k):
                raise sqlite3.OperationalError("database is locked")
            monkeypatch.setattr(eng.store, "update_breaker", locked)
            monkeypatch.setattr(eng.store, "load_breaker", locked)
            monkeypatch.setattr(eng.store, "claim_slot", locked)
            res = await eng.search("q")
            assert states(res) == {"p1": "blocked", "p2": "ok"} and len(res["results"]) == 3
            assert eng.providers["p1"].breaker.is_open()            # Sperre wenigstens prozessintern gemerkt
    run(go())


def test_wrong_shape_cache_entry_is_a_miss(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com"))
        async with make(tmp_path, p1) as eng:
            await eng.search("q")
            eng.store._db.execute("UPDATE cache SET v='{\"unerwartet\": 1}'")
            res = await eng.search("q")
            assert states(res) == {"p1": "ok"} and p1.calls == 2
    run(go())


def test_locked_database_does_not_block_the_event_loop(tmp_path):
    import sqlite3

    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com"))) as eng:
            other = sqlite3.connect(str(tmp_path / "c.db"), isolation_level=None)
            other.execute("BEGIN EXCLUSIVE")                        # anderer Prozess haelt die Datei
            t = time.monotonic()
            res = await eng.search("q", deadline=3)
            other.execute("ROLLBACK")
            assert time.monotonic() - t < 2.5                       # statt 10 s busy_timeout
            assert len(res["results"]) == 1
    run(go())


def test_search_many_total_deadline(tmp_path):
    async def go():
        slow = Fake("p1", R("https://a.com"), delay=0.4)
        slow.limiter = RateLimiter(0, 100)
        async with make(tmp_path, slow) as eng:
            t = time.monotonic()
            res = await eng.search_many([f"q{i}" for i in range(8)], concurrency=2, total_deadline=1.5)
            assert len(res) == 8 and time.monotonic() - t < 4
    run(go())


# =========================================================================== Runde-4-Fixes

class Unavail(Fake):
    def __init__(self, name, meta):
        super().__init__(name)
        self._m = meta

    async def search(self, client, query, n, lang, kind):
        self.calls += 1
        from websearch.core import SourceUnavailable
        raise SourceUnavailable("searxng: keine Treffer, Engines ausgefallen", meta=self._m)


def test_all_engines_failed_still_skips_those_engines_in_fallback(tmp_path):
    async def go():
        sx = Unavail("sx", {"engines_failed": [["duckduckgo", "CAPTCHA"], ["brave", "too many requests"]]})
        ddg, brave, other = Fake("ddg", R("https://d.com")), Fake("brave", R("https://b.com")), Fake("o", R("https://o.com"))
        ddg.same_engine, brave.same_engine = "duckduckgo", "brave"
        pol = TierPolicy(primary={"web": ["sx"]}, fallback={"web": ["ddg", "brave", "o"]})
        async with make(tmp_path, sx, ddg, brave, other, policy=pol) as eng:
            res = await eng.search("q")
            assert states(res)["sx"] == "unavailable" and res["sources"]["sx"]["meta"]["engines_failed"]
            assert ddg.calls == brave.calls == 0 and other.calls == 1
    run(go())


def test_paid_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PAID_DAILY_LIMIT", "1")

    class Paid(Fake):
        paid = True

    async def go():
        paid1 = Paid("paid1", R("https://p.com"))
        async with make(tmp_path, Fake("p1", block=True), Fake("p2"), Fake("f1"), paid1) as eng:
            await eng.search("q1")
            res = await eng.search("q2")
            assert paid1.calls == 1 and states(res)["paid1"] == "budget"
            assert eng.paid_usage()["day"] == 1
    run(go())


def test_rate_limited_does_not_trigger_paid(tmp_path):
    class Paid(Fake):
        paid = True

    async def go():
        p1 = Fake("p1", R("https://a.com"))
        p1.limiter = RateLimiter(60, 1)
        paid1 = Paid("paid1", R("https://p.com"))
        pol = TierPolicy(primary={"web": ["p1"]}, paid={"web": ["paid1"]})
        async with make(tmp_path, p1, paid1, policy=pol) as eng:
            await eng.search("q1")
            res = await eng.search("q2")
            assert states(res)["p1"] == "rate_limited" and paid1.calls == 0
    run(go())


def test_hung_primary_does_not_starve_fallback(tmp_path):
    async def go():
        slow, fb = Fake("p1", R("https://a.com"), delay=10), Fake("f1", R("https://f.com"))
        pol = TierPolicy(primary={"web": ["p1"]}, fallback={"web": ["f1"]})
        async with make(tmp_path, slow, fb, policy=pol) as eng:
            t = time.monotonic()
            res = await eng.search("q", deadline=2)
            assert time.monotonic() - t < 3 and fb.calls == 1 and res["results"][0]["url"] == "https://f.com"
    run(go())


def test_search_many_dedupes_identical_queries(tmp_path):
    async def go():
        p1 = Fake("p1", R("https://a.com"))
        async with make(tmp_path, p1) as eng:
            res = await eng.search_many(["x", "x", "y", "x"], concurrency=4)
            assert [r["query"] for r in res] == ["x", "x", "y", "x"] and p1.calls == 2
    run(go())


def test_explicit_paid_source_respects_paid_false(tmp_path):
    class Paid(Fake):
        paid = True

    async def go():
        paid1 = Paid("paid1", R("https://p.com"))
        async with make(tmp_path, paid1) as eng:
            res = await eng.search("q", sources=["paid1"], paid=False)
            assert states(res) == {"paid1": "not_configured"} and paid1.calls == 0
    run(go())


def test_unknown_explicit_source_gets_a_hint(tmp_path):
    async def go():
        async with make(tmp_path, Fake("searxng")) as eng:
            with pytest.raises(InputError, match="gemeint: 'searxng'"):
                await eng.search("q", sources=["searxgn"])
            with pytest.raises(InputError, match="deadline"):
                await eng.search("q", deadline=-5)
    run(go())


def test_double_cancel_still_cleans_up(tmp_path):
    class Stubborn(Fake):
        async def search(self, client, query, n, lang, kind):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)          # braucht etwas, um aufzuräumen
                raise
            return []

    async def go():
        async with make(tmp_path, Stubborn("p1")) as eng:
            t = asyncio.create_task(eng.search("q"))
            await asyncio.sleep(0.1)
            t.cancel()
            await asyncio.sleep(0.05)
            t.cancel()                            # zweiter Abbruch während des Aufräumens
            with pytest.raises(asyncio.CancelledError):
                await t
            await asyncio.sleep(0.05)
            assert [x for x in asyncio.all_tasks() if x is not asyncio.current_task()] == []
    run(go())


def test_bad_rows_are_coerced_or_dropped():
    r = Result(None, 123, snippet=5, engines=None, rank="x")          # type: ignore[arg-type]
    assert (r.title, r.url, r.snippet, r.engines, r.rank) == ("", "", "5", [], 0)
    merged = merge_rrf([[Result("ok", "https://a.com"), r, Result({"x": 1}, "https://b.com")]])  # type: ignore[arg-type]
    assert [m.url for m in merged] == ["https://a.com", "https://b.com"] and merged[1].title == ""
    assert normalize_date("0001-01-01T00:00:00+05:00") is None


def test_future_slot_is_clamped(tmp_path):
    st = StateStore(tmp_path / "s.db")
    st._db.execute("REPLACE INTO slots VALUES ('x', ?)", (time.time() + 86400,))
    assert st.claim_slot("x", 1) - time.time() < 1


def test_store_survives_startup_while_another_process_writes(tmp_path):
    import sqlite3
    import threading
    StateStore(tmp_path / "c.db").close()
    other = sqlite3.connect(str(tmp_path / "c.db"), isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(0.6, lambda: other.execute("ROLLBACK")).start()   # Sperre länger als 250 ms
    st = StateStore(tmp_path / "c.db")                                # darf nicht scheitern
    time.sleep(0.7)                                                   # fremde Sperre ist vorbei
    st.set("k", 1)
    assert st.get("k") == 1


def test_degraded_store_fails_fast(tmp_path):
    import sqlite3
    st = StateStore(tmp_path / "c.db")
    other = sqlite3.connect(str(tmp_path / "c.db"), isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    with pytest.raises(sqlite3.OperationalError):
        st.claim_slot("x", 1)                                  # ~250 ms, dann "degradiert"
    t = time.monotonic()
    for _ in range(20):
        with pytest.raises(sqlite3.OperationalError, match="degradiert"):
            st.claim_slot("x", 1)
    assert time.monotonic() - t < 0.1                          # keine 20 x 250 ms mehr
    other.execute("ROLLBACK")


def test_tier_policy_rejects_non_list_and_is_hashable():
    with pytest.raises(InputError):
        TierPolicy(primary={"web": 5})                          # type: ignore[dict-item]
    assert isinstance(hash(TierPolicy(primary={"web": ["a"]})), int)


def test_provider_with_raising_available_is_skipped(tmp_path):
    class Broken(Fake):
        @property
        def available(self):
            raise RuntimeError("kaputt")

    async def go():
        pol = TierPolicy(primary={"web": ["b", "p1"]})
        async with make(tmp_path, Broken("b"), Fake("p1", R("https://a.com")), policy=pol) as eng:
            res = await eng.search("q")
            assert res["results"] and "b" not in res["sources"]
    run(go())


def test_enough_results_do_not_wait_for_slow_stragglers(tmp_path):
    async def go():
        fast = Fake("p1", R(*[f"https://a.com/{i}" for i in range(10)]))
        slow = Fake("p2", R("https://slow.com"), delay=10)
        pol = TierPolicy(primary={"web": ["p1", "p2"]})
        async with make(tmp_path, fast, slow, policy=pol) as eng:
            t = time.monotonic()
            res = await eng.search("q", n=10, deadline=20)
            assert time.monotonic() - t < 4 and states(res)["p2"] == "deadline" and len(res["results"]) == 10
            assert res["sources"]["p2"]["meta"] == {"early": True}
    run(go())


# =========================================================================== Runde 5

@pytest.mark.parametrize("kw,msg", [({"deadline": float("nan")}, "endliche"), ({"deadline": float("inf")}, "endliche"),
                                    ({"max_age": "abc"}, "Zahlen"), ({"sources": "free"}, "Liste")])
def test_more_input_validation(tmp_path, kw, msg):
    async def go():
        async with make(tmp_path, Fake("p1")) as eng:
            with pytest.raises(InputError, match=msg):
                await eng.search("q", **kw)
    run(go())


def test_rate_limited_paid_source_does_not_consume_budget(tmp_path, monkeypatch):
    class Paid(Fake):
        paid = True

    async def go():
        p = Paid("paid1", R("https://p.com"))
        p.limiter = RateLimiter(60, 1)
        async with make(tmp_path, p, policy=TierPolicy(primary={"web": ["paid1"]})) as eng:
            await eng.search("q1")
            res = await eng.search("q2")
            assert states(res)["paid1"] == "rate_limited" and eng.paid_usage()["day"] == 1
    run(go())


def test_search_many_cancels_the_rest_on_unexpected_error(tmp_path, monkeypatch):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com"), delay=1)) as eng:
            orig = eng.search

            async def boom(q, **kw):
                if q == "bad":
                    raise RuntimeError("Bug")
                return await orig(q, **kw)
            monkeypatch.setattr(eng, "search", boom)
            with pytest.raises(RuntimeError):
                await eng.search_many(["a", "bad", "c"], concurrency=3)
            await asyncio.sleep(0.05)
            assert [x for x in asyncio.all_tasks() if x is not asyncio.current_task()] == []
    run(go())


def test_empty_env_path_counts_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_CACHE", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    eng = SearchEngine({"p1": Fake("p1")}, policy=TierPolicy(primary={"web": ["p1"]}))
    assert eng.cache_path == tmp_path / ".cache/websearch/cache.db"


def test_env_flag():
    import os
    from websearch.core import env_flag
    for v, exp in (("off", False), ("NO", False), ("0", False), ("yes", True), ("1", True), ("", None)):
        os.environ["X_FLAG"] = v
        assert env_flag("X_FLAG", default=None) is exp     # type: ignore[arg-type]
    del os.environ["X_FLAG"]


# =========================================================================== Runde 6

def test_proxy_is_not_used_for_own_searxng_and_paid_apis(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", "http://proxy.local:3128")
    seen = {}

    class Direct(Fake):
        via_proxy = False

        async def search(self, client, query, n, lang, kind):
            seen[self.name] = client
            return [Result("x", "https://a.com", source=self.name, rank=1)]

    class Proxied(Direct):
        via_proxy = True

    async def go():
        pol = TierPolicy(primary={"web": ["d", "p"]})
        async with make(tmp_path, Direct("d"), Proxied("p"), policy=pol) as eng:
            await eng.search("q")
            assert seen["d"] is eng._client and seen["p"] is eng._proxied and eng._client is not eng._proxied
    run(go())
    from websearch.providers import SearXNG, SerperAPI, BingRSS
    assert not SearXNG.via_proxy and not SerperAPI.via_proxy and BingRSS.via_proxy


@pytest.mark.parametrize("url,msg", [("kein proxy", "gültige Proxy-URL"), ("socks5://127.0.0.1:1080", None)])
def test_invalid_proxy_is_a_config_error(tmp_path, monkeypatch, url, msg):
    monkeypatch.setenv("WEBSEARCH_PROXY", url)

    async def go():
        async with make(tmp_path, Fake("p1")):
            pass
    try:
        import socksio  # noqa: F401
        has_socks = True
    except ImportError:
        has_socks = False
    if msg is None and has_socks:
        run(go())
        return
    with pytest.raises(InputError, match=msg or "socksio"):
        run(go())


def test_precheck_skip_costs_no_rate_limit_slot(tmp_path):
    from websearch.core import SourceSkipped

    class Picky(Fake):
        def precheck(self, query, kind):
            if query == "kurz":
                raise SourceSkipped("zu kurz")

    async def go():
        p = Picky("p1", R("https://a.com"))
        p.limiter = RateLimiter(30, 1)
        async with make(tmp_path, p) as eng:
            res = await eng.search("kurz")
            assert states(res) == {"p1": "skipped"} and p.calls == 0
            res = await eng.search("lang genug")                      # Slot noch frei
            assert states(res) == {"p1": "ok"}
    run(go())


def test_future_cache_entries_and_dates_are_ignored(tmp_path):
    st = StateStore(tmp_path / "c.db")
    st.set("k", 1)
    st._db.execute("UPDATE cache SET t = t + 1800")                    # Uhr zurückgestellt
    assert st.get("k") is None
    assert normalize_date("2099-01-01T00:00:00Z") is None


def test_corrupt_state_file_heals_itself(tmp_path, caplog):
    p = tmp_path / "c.db"
    p.write_bytes(b"das ist keine sqlite-datei" * 100)
    st = StateStore(p)
    st.set("k", 1)
    assert st.get("k") == 1 and any(x.name.startswith("c.db.corrupt-") for x in tmp_path.iterdir())


def test_block_found_while_degraded_is_not_forgotten(tmp_path):
    import sqlite3
    st = StateStore(tmp_path / "s.db")
    br = CircuitBreaker("g", base_cooldown=60, store=st)
    st._degraded_until = time.monotonic() + 10                        # Datei gilt als gesperrt
    br.trip("captcha")
    assert br.is_open()
    st._degraded_until = 0                                            # wieder da
    assert br.is_open() and st.load_breaker("g").open_until > time.time()   # nachgetragen



def test_unexpected_response_shape_is_unavailable_not_a_bug(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", R("https://a.com", "https://b.com", "https://c.com")),
                        Fake("p2", exc=KeyError("objectID"))) as eng:
            res = await eng.search("q")
            assert states(res)["p2"] == "unavailable" and "Antwortformat" in res["sources"]["p2"]["message"]
    run(go())


def test_waiting_behind_own_slow_request_is_rate_limited_not_failure(tmp_path):
    class Paid(Fake):
        paid = True

    async def go():
        slow = Fake("p1", R("https://a.com"), delay=3)
        slow.limiter = RateLimiter(0, 1)                  # Parallelität 1, kein Takt
        pd = Paid("pd", R("https://p.com"))
        pol = TierPolicy(primary={"news": ["p1"]}, paid={"news": ["pd"]})
        async with make(tmp_path, slow, pd, policy=pol) as eng:
            res = await eng.search_many(["a", "b"], kind="news", deadline=2, concurrency=2)
            st = {r["query"]: states(r) for r in res}
            # die eine wirklich langsame Anfrage darf zahlen (Ausfall), die nur wartende nicht
            assert sorted((st["a"]["p1"], st["b"]["p1"])) == ["deadline", "rate_limited"] and pd.calls <= 1
    run(go())


def test_empty_number_settings_use_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PAID_DAILY_LIMIT", "")
    eng = SearchEngine({"p1": Fake("p1")}, policy=TierPolicy(primary={"web": ["p1"]}), cache_path=tmp_path / "c.db")
    assert eng.paid_daily_limit == 100


def test_explicit_sources_that_cannot_do_the_kind_are_an_input_error(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1", kinds=("ref",))) as eng:
            with pytest.raises(InputError, match="kann die Suchart"):
                await eng.search("q", kind="web", sources=["p1"])
    run(go())


def test_more_type_validation(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1")) as eng:
            for coro in (eng.search("q", lang=5), eng.search_many(["a"], total_deadline="x"),
                         eng.search_many([1, 2])):
                with pytest.raises(InputError):
                    await coro
    run(go())


# =========================================================================== Runde 8

def test_paid_cascade_stops_at_first_normal_answer_and_caches_it(tmp_path):
    class Paid(Fake):
        paid = True

    async def go():
        p1, p2 = Paid("paid1"), Paid("paid2")          # beide antworten "ok, 0 Treffer"
        pol = TierPolicy(primary={"web": ["sx"]}, paid={"web": ["paid1", "paid2"]})
        async with make(tmp_path, Fake("sx", block=True), p1, p2, policy=pol) as eng:
            for _ in range(3):
                await eng.search("seltene marke")
            assert p1.calls == 1 and p2.calls == 0       # einmal gefragt, leeres Ergebnis gecacht
    run(go())


def test_capped_transport_stops_chunked_giant_responses(monkeypatch):
    import httpx
    from websearch import engine as E

    monkeypatch.setattr(E, "MAX_RESPONSE_BYTES", 1000)

    async def go():
        async def body():
            for _ in range(100):
                yield b"x" * 100
        inner = httpx.MockTransport(lambda r: httpx.Response(200, content=body()))

        class T(E._CappedTransport):
            async def handle_async_request(self, request):
                resp = await inner.handle_async_request(request)
                resp.stream = E._CappedStream(resp.stream, E.MAX_RESPONSE_BYTES)
                return resp
        async with httpx.AsyncClient(transport=T()) as c:
            from websearch.core import SourceUnavailable
            with pytest.raises(SourceUnavailable, match="größer"):
                await c.get("https://x.example/")
    run(go())


def test_invalid_utf8_query_is_input_error(tmp_path):
    async def go():
        async with make(tmp_path, Fake("p1")) as eng:
            with pytest.raises(InputError, match="ungültige Zeichen"):
                await eng.search("abc\udcff")
            with pytest.raises(InputError):
                await eng.search_many("abc")                       # Text statt Liste
    run(go())


def test_env_proxy_does_not_override_websearch_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", "http://wsproxy.local:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://envproxy.local:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://envproxy.local:3128")

    async def go():
        async with SearchEngine({"p1": Fake("p1")}, policy=TierPolicy(primary={"web": ["p1"]}),
                                cache_path=tmp_path / "c.db") as eng:
            for client in (eng._client, eng._proxied):
                assert not client._mounts or all("envproxy" not in repr(t) for t in client._mounts.values())
            assert not eng._client._trust_env and not eng._proxied._trust_env
    run(go())


def test_state_files_are_private(tmp_path):
    import stat
    from websearch.monitor import SeenStore
    d = tmp_path / "neu" / "websearch"
    st = StateStore(d / "cache.db")
    st.set("k", 1)
    SeenStore(d / "monitor.db")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in d.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, f.name
    old = tmp_path / "alt.db"
    old.write_text("")
    old.chmod(0o644)
    StateStore(old)
    assert stat.S_IMODE(old.stat().st_mode) == 0o600              # vorhandene Datei wird nachgezogen


def test_corrupt_state_file_keeps_paid_counters(tmp_path, monkeypatch):
    import sqlite3
    from websearch.core import BudgetExceeded
    st = StateStore(tmp_path / "s.db")
    for _ in range(3):
        st.reserve_paid_call(100, 1000)
    st.close()
    # Beschädigung beim Öffnen simulieren (z.B. kaputter Cache-Index), die Bezahl-Tabelle bleibt lesbar
    real_open, calls = StateStore._open, []

    def flaky_open(self):
        calls.append(1)
        if len(calls) == 1:
            real_open(self)
            raise sqlite3.DatabaseError("database disk image is malformed")
        real_open(self)
    monkeypatch.setattr(StateStore, "_open", flaky_open)
    st2 = StateStore(tmp_path / "s.db")                    # neu angelegt ...
    assert st2.usage() == {"day": 3, "month": 3}           # ... Zähler übernommen
    monkeypatch.setattr(StateStore, "_open", real_open)
    garbage = tmp_path / "g.db"
    garbage.write_bytes(b"kein sqlite" * 100)
    st3 = StateStore(garbage)                              # gar nicht lesbar: für heute gesperrt
    with pytest.raises(BudgetExceeded):
        st3.reserve_paid_call(100, 1000)
