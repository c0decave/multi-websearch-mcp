"""Offline-Tests fuer das Monitoring."""
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest


from websearch import monitor as M
from websearch.core import Result
from websearch.engine import SearchEngine
from websearch.providers import Provider


class Feed(Provider):
    name = "feed"
    kinds = ("web", "news", "ref")
    min_interval = 0

    def __init__(self):
        super().__init__()
        self.items, self.fail = [], False

    async def search(self, client, query, n, lang, kind):
        if self.fail:
            raise RuntimeError("kaputt")
        return [Result(t, u, source=self.name, rank=i, published=d) for i, (t, u, d) in enumerate(self.items, 1)]


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class Cache:
    """Pfad zur Engine-Cache-DB. Pro Lauf wird eine frische Engine gebaut (sie schliesst ihre DB beim Verlassen)."""
    def __init__(self, path):
        self.path = path


@pytest.fixture
def env(tmp_path):
    feed = Feed()
    eng = Cache(tmp_path / "c.db")
    store = M.SeenStore(tmp_path / "m.db")
    sent = []
    hook = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: sent.append(r) or httpx.Response(200)))
    return feed, eng, store, hook, sent


W = {"name": "w", "query": "q", "kind": "news", "sources": ["feed"], "every_min": 5}


def run_check(env, w=W, **kw):
    feed, cache, store, hook, _ = env
    eng = SearchEngine({"feed": feed}, cache_path=cache.path)

    async def go():
        async with eng:
            return await M.check_watch(eng, store, dict(w, **kw.pop("extra", {})), hook, **kw)
    return asyncio.run(go())


def test_baseline_then_only_new(env, monkeypatch):
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    feed.items = [("a", "https://a.com", iso(0)), ("b", "https://b.com", None)]
    assert run_check(env) is None                         # Basislinie, keine Meldung
    feed.items.append(("c", "https://www.c.com/", iso(0)))
    clear_cache(eng)
    hit = run_check(env)
    assert [r["title"] for r in hit["new"]] == ["c"]
    assert len(sent) == 1 and json.loads(sent[0].content)["watch"] == "w"
    clear_cache(eng)
    assert run_check(env) is None                         # nichts Neues


def test_empty_first_run_does_not_count_as_baseline(env):
    feed, eng, store, *_ = env
    feed.items = []
    assert run_check(env) is None and not store.has_baseline("w")
    feed.items = [("a", "https://a.com", None)]
    assert run_check(env) is None and store.has_baseline("w")   # jetzt erst Basislinie, keine Fehlmeldung


def test_old_items_are_not_reported(env):
    feed, eng, *_ = env
    feed.items = [("a", "https://a.com", None)]
    run_check(env)
    feed.items += [("alt", "https://old.com", iso(30)), ("neu", "https://new.com", iso(0.1))]
    clear_cache(eng)
    hit = run_check(env)
    assert [r["title"] for r in hit["new"]] == ["neu"]


def test_text_webhook_format(env, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")      # Meldungstexte werden auf Deutsch geprüft
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://ntfy.local/topic?auth=geheim")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "text")
    feed.items = [("a", "https://a.com", None)]
    run_check(env, alert_first=True)
    assert sent[0].content.decode() == "a\nhttps://a.com"
    assert sent[0].url.params["title"] == "1 neu: w" and sent[0].url.params["auth"] == "geheim"


def test_failing_watch_does_not_stop_others(tmp_path, monkeypatch):
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([dict(W, name="ok"), dict(W, name="bad", query="kaputt")]))
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    real_search = eng.search

    async def search(query, **kw):
        if query == "kaputt":
            raise RuntimeError("Absturz in diesem Watch")
        return await real_search(query, **kw)
    monkeypatch.setattr(eng, "search", search)
    hits = asyncio.run(M.run_monitor(str(wf), alert_first=True, db_path=str(tmp_path / "m.db"), engine=eng))
    assert [h["watch"] for h in hits] == ["ok"]


@pytest.mark.parametrize("bad", [
    {"x": 1},
    [{"name": "a"}],
    [{"name": "a", "query": "q"}, {"name": "a", "query": "r"}],
    [{"name": "a", "query": "q", "kind": "bilder"}],
    [{"name": "a", "query": "q", "every_min": 1}],
    [{"name": "a", "query": "q", "every_min": "abc"}],
    [{"name": "a", "query": "q", "n": 0}],
    [{"name": "a", "query": "q", "n": 2.5}],
    [{"name": "a", "query": "q", "n": True}],
    [{"name": "a", "query": "q", "deadline": 1000}],
    [{"name": "a", "query": "q", "max_age_days": 0}],
    [{"name": "a", "query": "q", "sources": "feed"}],
    [{"name": "a", "query": "q", "sources": []}],
    [{"name": "a", "query": "q", "lang": 5}],
    [{"name": "a", "query": "   "}],
    [{"name": "", "query": "q"}],
    ["kein objekt"],
    [{"name": "a", "query": "q", "every_mins": 60}],
])
def test_watch_validation(tmp_path, bad):
    f = tmp_path / "w.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        M.load_watches(f)


def test_example_watchfile_is_valid():
    M.load_watches(os.path.join(os.path.dirname(__file__), "..", "watches.example.json"),
                   known_sources=M.default_source_names())   # inkl. Quellennamen


def clear_cache(eng):
    if not isinstance(eng, Cache):                      # offene Engine innerhalb von "async with"
        eng.store._db.execute("DELETE FROM cache")
        return
    if not eng.path.exists():
        return
    db = sqlite3.connect(str(eng.path))
    db.execute("DELETE FROM cache")
    db.commit()
    db.close()


def test_drift_into_top_n_is_not_alerted(env):
    feed, eng, store, hook, sent = env
    w = dict(W, n=2)
    feed.items = [("a", "https://a.com", None), ("b", "https://b.com", None), ("c", "https://c.com", None)]
    assert run_check(env, w) is None                      # Basislinie: c liegt im Pool, ausserhalb Top-2
    clear_cache(eng)
    feed.items = [("c", "https://c.com", None), ("d", "https://d.com", None), ("a", "https://a.com", None)]
    hit = run_check(env, w)
    assert [r["title"] for r in hit["new"]] == ["d"]      # c ist nur hochgerutscht, d ist wirklich neu


class Feed2(Feed):
    name = "feed2"


def test_first_time_source_is_absorbed_silently(tmp_path):
    feed, feed2 = Feed(), Feed2()
    eng = SearchEngine({"feed": feed, "feed2": feed2}, cache_path=tmp_path / "c.db")
    store = M.SeenStore(tmp_path / "m.db")
    hook = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    w = dict(W, sources=["feed", "feed2"])

    async def check():
        clear_cache(eng)
        return await M.check_watch(eng, store, w, hook)

    async def go():
        async with eng:
            feed.items, feed2.fail = [("a", "https://a.com", None)], True
            assert await check() is None                  # Basislinie, feed2 (z.B. SearXNG) noch nicht bereit
            feed2.fail = False
            feed2.items = [("alt1", "https://old1.com", None), ("alt2", "https://old2.com", None)]
            assert await check() is None                  # feed2 liefert erstmals -> still uebernehmen
            feed.items.append(("neu", "https://new.com", None))
            feed2.items.append(("neu2", "https://new2.com", None))
            hit = await check()                           # beide Quellen jetzt bekannt -> melden
            assert sorted(r["title"] for r in hit["new"]) == ["neu", "neu2"]
    asyncio.run(go())


@pytest.mark.parametrize("url,handler_exc", [("::not a url", None), ("https://hook.local/x", ValueError)])
def test_webhook_errors_do_not_lose_payload(tmp_path, monkeypatch, capsys, caplog, url, handler_exc):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")      # Meldungstexte werden auf Deutsch geprüft
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", url)

    def handler(req):
        if handler_exc:
            raise handler_exc("boom")
        return httpx.Response(200)
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    store = M.SeenStore(tmp_path / "m.db")
    hook = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        async with eng:
            return await M.check_watch(eng, store, W, hook, alert_first=True)
    with caplog.at_level(logging.WARNING, logger="websearch.notify"):
        hit = asyncio.run(go())
    assert hit and hit["new"][0]["url"] == "https://a.com"
    assert '"watch": "w"' in capsys.readouterr().out
    assert "Webhook fehlgeschlagen" in caplog.text


# --------------------------------------------------------------------------- Runde 2

def test_watch_parsing_defaults_and_messages(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([{"name": " marke ", "query": " q ", "sources": ["feed"]}]))
    w = M.load_watches(f)[0]
    assert (w.name, w.query, w.kind, w.lang, w.every_min, w.n, w.deadline, w.max_age_days, w.sources) == \
        ("marke", "q", "news", "auto", 60.0, 20, 60.0, 3.0, ("feed",))
    for bad, msg in [({"name": "a", "query": "q", "every_mins": 5}, "gemeint: 'every_min'"),
                     ({"name": "a", "query": "q", "every_min": "abc"}, "muss eine Zahl sein"),
                     ({"name": "a", "query": "q", "n": 2.5}, "ganze Zahl"),
                     ({"name": "a", "query": "q", "kind": "bilder"}, "web, news, ref")]:
        f.write_text(json.dumps([bad]))
        with pytest.raises(ValueError, match=msg):
            M.load_watches(f)
    with pytest.raises(ValueError, match="nicht gefunden"):
        M.load_watches(tmp_path / "fehlt.json")
    f.write_text("[{kaputt")
    with pytest.raises(ValueError, match="kein gültiges JSON"):
        M.load_watches(f)


def test_blind_detection_alerts_once_and_recovers(env, monkeypatch):
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "2")
    feed.fail = True
    for _ in range(4):                                     # 4 blinde Laeufe -> genau 1 Health-Meldung
        clear_cache(eng)
        run_check(env)
    health = [json.loads(r.content) for r in sent]
    assert [h["status"] for h in health] == ["blind"] and health[0]["type"] == "health"
    assert health[0]["watch"] == "w" and "feed" in health[0]["errors"] and health[0]["since"]
    assert store.health("w")["blind_runs"] == 4
    feed.fail, feed.items = False, [("a", "https://a.com", None)]
    clear_cache(eng)
    run_check(env)
    assert json.loads(sent[-1].content)["status"] == "recovered" and len(sent) == 2
    h = store.health("w")
    assert h["blind_runs"] == 0 and not h["alerted"] and h["last_success"]


def test_legit_empty_is_not_blind(env, monkeypatch):
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    feed.items = []                                        # Quelle ok, nur nichts gefunden
    for _ in range(3):
        clear_cache(eng)
        run_check(env)
    assert sent == [] and store.health("w")["blind_runs"] == 0


def test_text_webhook_umlauts_reach_a_real_http_request(env, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")      # Meldungstexte werden auf Deutsch geprüft
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://ntfy.local/topic")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "text")
    feed.items = [("Grüne Energie: Förderung", "https://ä.example/ü", None)]
    hit = run_check(env, dict(W, name="grüne-energie"), alert_first=True)
    assert hit and len(sent) == 1
    req = sent[0]
    assert req.url.params["title"] == "1 neu: grüne-energie"
    assert req.content.decode("utf-8").startswith("Grüne Energie: Förderung")
    # Header muessen ASCII sein - sonst haette httpx schon beim Senden abgebrochen
    assert all(v.isascii() for v in req.headers.values())
    # Gegenprobe: die alte Variante (Titel als Header) scheitert tatsaechlich
    with pytest.raises(UnicodeEncodeError):
        httpx.Request("POST", "https://x", headers={"Title": "grüne"})


def test_slack_format(env, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")      # Meldungstexte werden auf Deutsch geprüft
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hooks.slack.local/x")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "slack")
    feed.items = [("A <b> & B", "https://a.com/?x=1", None)]
    run_check(env, alert_first=True)
    body = json.loads(sent[0].content)
    assert set(body) == {"text"}
    assert "<https://a.com/?x=1|A &lt;b&gt; &amp; B>" in body["text"] and "*1 neu: w*" in body["text"]


def test_invalid_webhook_config_fails_at_startup(tmp_path, monkeypatch):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    eng = SearchEngine({"feed": Feed()}, cache_path=tmp_path / "c.db")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "xml")
    with pytest.raises(ValueError, match="WEBSEARCH_WEBHOOK_FORMAT"):
        asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"), engine=eng))
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "json")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "ftp://x")
    with pytest.raises(ValueError, match="http"):
        asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"), engine=eng))


def test_jsonl_export(env, monkeypatch, tmp_path):
    feed, *_ = env
    out = tmp_path / "sub" / "hits.jsonl"
    monkeypatch.setenv("WEBSEARCH_MONITOR_OUT", str(out))
    feed.items = [("a", "https://a.com", None)]
    run_check(env, alert_first=True)
    feed.items.append(("b", "https://b.com", None))
    clear_cache(env[1])
    run_check(env)
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert [l["type"] for l in lines] == ["hits", "hits"]
    assert [r["title"] for r in lines[1]["new"]] == ["b"]


def test_watch_file_hot_reload(tmp_path, caplog):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([W]))
    wf = M.WatchFile(f)
    assert [w.name for w in wf.current()] == ["w"]
    f.write_text(json.dumps([W, dict(W, name="neu")]))
    os.utime(f, ns=(1, 10**18))                            # mtime sicher aendern
    assert [w.name for w in wf.current()] == ["w", "neu"]
    f.write_text("[{kaputt")
    os.utime(f, ns=(1, 2 * 10**18))
    with caplog.at_level(logging.ERROR, logger="websearch.monitor"):
        assert [w.name for w in wf.current()] == ["w", "neu"]   # alte Watches bleiben aktiv
    assert "alte Watches bleiben aktiv" in caplog.text


def test_loop_picks_up_new_watch(tmp_path):
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    f = tmp_path / "w.json"
    f.write_text(json.dumps([W]))
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")

    async def go():
        task = asyncio.create_task(M.run_monitor(str(f), loop=True, alert_first=True, engine=eng,
                                                 db_path=str(tmp_path / "m.db"), interval=0.05, max_cycles=20))   # genug Zeit für den Startversatz
        await asyncio.sleep(0.08)
        f.write_text(json.dumps([W, dict(W, name="zweiter")]))
        os.utime(f, ns=(1, 10**18))
        return await task
    hits = asyncio.run(go())
    assert {h["watch"] for h in hits} == {"w", "zweiter"}


def test_seen_purge(tmp_path):
    st = M.SeenStore(tmp_path / "m.db")
    st.db.execute("INSERT INTO seen VALUES ('w', 'https://alt', 0)")
    st.db.execute("INSERT INTO seen VALUES ('w', 'https://neu', ?)", (__import__("time").time(),))
    st.db.commit()
    st2 = M.SeenStore(tmp_path / "m.db")
    assert [r[0] for r in st2.db.execute("SELECT url FROM seen")] == ["https://neu"]


def test_failed_helper_handles_new_engine_contract():
    res = {"results": [], "sources": {
        "a": {"state": "ok", "count": 3, "ms": 1, "message": None, "meta": {}},
        "b": {"state": "cached", "count": 1, "ms": 0, "message": None, "meta": {}},
        "c": {"state": "blocked", "count": 0, "ms": 5, "message": "HTTP 429", "meta": {}},
        "d": {"state": "deadline", "count": 0, "ms": 9, "message": None, "meta": {}}}}
    assert M._failed(res) == {"c": "HTTP 429", "d": "deadline"}
    assert M._delivering(res) == {"a", "b"}


# --------------------------------------------------------------------------- Runde 3: Zustellgarantie + Outbox

def _items(store, watch):
    """Gesehene URLs (gespeichert normalisiert, hier ohne Slash am Ende verglichen)."""
    return {r[0].rstrip("/") for r in store.db.execute("SELECT url FROM seen WHERE watch=?", (watch,))}


def test_hit_not_lost_when_emit_raises(env, monkeypatch):
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://a.com", None)]
    run_check(env)                                          # Basislinie
    feed.items.append(("neu", "https://neu.com", None))
    clear_cache(eng)
    real_emit = M._emit

    def broken(payload):
        raise BrokenPipeError("stdout weg")
    monkeypatch.setattr(M, "_emit", broken)
    with pytest.raises(BrokenPipeError):
        run_check(env)
    assert "https://neu.com" not in _items(store, "w")      # nichts als gesehen markiert
    monkeypatch.setattr(M, "_emit", real_emit)
    clear_cache(eng)
    hit = run_check(env)
    assert [r["title"] for r in hit["new"]] == ["neu"]      # beim naechsten Lauf gemeldet
    assert "https://neu.com" in _items(store, "w")


def test_webhook_failure_goes_to_outbox_and_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["watch"])
        return httpx.Response(500 if len(calls) == 1 else 200)
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    store = M.SeenStore(tmp_path / "m.db")

    async def go():
        async with eng, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as hook:
            hit = await M.check_watch(eng, store, W, hook, alert_first=True)
            assert hit and store.outbox_size() == 1 and calls == ["w"]
            assert "https://a.com" in _items(store, "w")      # stdout hat zugestellt -> gesehen
            assert await M.flush_outbox(store, hook) == 0      # Backoff noch nicht abgelaufen
            assert calls == ["w"]
            later = __import__("time").time() + 61
            assert await M.flush_outbox(store, hook, now=later) == 1
            assert store.outbox_size() == 0 and calls == ["w", "w"]
            assert await M.flush_outbox(store, hook, now=later + 3600) == 0   # genau einmal zugestellt
            assert calls == ["w", "w"]
    asyncio.run(go())


def test_outbox_backoff_grows_and_gives_up_after_24h(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        store.outbox_add(db, {"type": "hits", "watch": "w", "query": "q", "new": []}, "HTTP 500")
    t0 = store.db.execute("SELECT created FROM outbox").fetchone()[0]
    calls = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: calls.append(1) or httpx.Response(503))) as hook:
            await M.flush_outbox(store, hook, now=t0 + 61)
            nxt = store.db.execute("SELECT attempts, next_try FROM outbox").fetchone()
            assert nxt[0] == 2 and nxt[1] - (t0 + 61) == pytest.approx(120)   # verdoppelt
            with caplog.at_level(logging.ERROR, logger="websearch.monitor"):
                await M.flush_outbox(store, hook, now=t0 + 24 * 3600 + 1)
    asyncio.run(go())
    assert store.outbox_size() == 0 and len(calls) == 1
    assert "verworfen" in caplog.text


def test_parallel_watches_failure_does_not_leak_rows(tmp_path, monkeypatch):
    feed = Feed()
    feed.items = [("a", "https://a.com", None), ("b", "https://b.com", None)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([dict(W, name="ok"), dict(W, name="bad")]))
    real_emit = M._emit

    def emit(payload):
        if payload["watch"] == "bad":
            raise BrokenPipeError("weg")
        real_emit(payload)
    monkeypatch.setattr(M, "_emit", emit)
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    hits = asyncio.run(M.run_monitor(str(wf), alert_first=True, db_path=str(tmp_path / "m.db"), engine=eng))
    store = M.SeenStore(tmp_path / "m.db")
    assert [h["watch"] for h in hits] == ["ok"]
    assert _items(store, "ok") == {"https://a.com", "https://b.com"}
    assert _items(store, "bad") == set()                    # nichts halb Geschriebenes
    assert store.last_run("bad") > 0 and not store.has_baseline("bad")


def test_caller_engine_is_not_closed(tmp_path):
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            await M.run_monitor(str(wf), alert_first=True, db_path=str(tmp_path / "m.db"), engine=eng)
            res = await eng.search("noch offen", sources=["feed"])   # Engine weiter nutzbar
            assert res["results"]
    asyncio.run(go())


def test_notify_logs_under_own_logger(caplog, monkeypatch):
    from websearch import notify
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as hook:
            return await notify.send(hook, {"type": "hits", "watch": "w", "query": "q", "new": []})
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(go()) is False
    assert [r.name for r in caplog.records] == ["websearch.notify"]


def test_no_webhook_configured_means_no_outbox(env, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_WEBHOOK", raising=False)
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://a.com", None)]
    assert run_check(env, alert_first=True)
    assert store.outbox_size() == 0 and not sent


# --------------------------------------------------------------------------- Runde 3: UX-Nachträge

def test_unknown_source_fails_at_startup_with_hint(tmp_path, monkeypatch):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([dict(W, sources=["searxgn"])]))
    with pytest.raises(ValueError, match="unbekannte Quelle 'searxgn' \\(gemeint: 'searxng'\\?\\)"):
        M.load_watches(f, known_sources=M.default_source_names())
    eng = SearchEngine({"feed": Feed()}, cache_path=tmp_path / "c.db")
    with pytest.raises(ValueError, match="unbekannte Quelle"):            # run_monitor prüft vor dem ersten Lauf
        asyncio.run(M.run_monitor(str(f), db_path=str(tmp_path / "m.db"), engine=eng))
    monkeypatch.setenv("WEBSEARCH_DISABLE", "gdelt")
    f.write_text(json.dumps([dict(W, sources=["gdelt"])]))
    with pytest.raises(ValueError, match="per WEBSEARCH_DISABLE abgeschaltet"):
        M.load_watches(f, known_sources=M.default_source_names())


def test_cli_exit_code_2_for_source_typo(tmp_path, monkeypatch, capsys):
    from websearch import __main__ as cli
    f = tmp_path / "w.json"
    f.write_text(json.dumps([dict(W, sources=["searxgn"])]))
    monkeypatch.setenv("WEBSEARCH_CACHE", str(tmp_path / "c.db"))
    monkeypatch.setenv("WEBSEARCH_MONITOR_DB", str(tmp_path / "m.db"))
    assert cli.main(["monitor", str(f)]) == 2
    assert "gemeint: 'searxng'" in capsys.readouterr().err


def test_exceptions_count_as_blind_runs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "2")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("WEBSEARCH_MONITOR_OUT", str(out))
    sent = []
    store = M.SeenStore(tmp_path / "m.db")
    w = M.parse_watch(W)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: sent.append(json.loads(r.content)) or httpx.Response(200))) as hook:
            for _ in range(3):
                await M.record_failure(store, w, "RuntimeError: kaputt", hook)
    with caplog.at_level(logging.WARNING, logger="websearch.monitor"):
        asyncio.run(go())
    health = [p for p in sent if p["type"] == "health"]
    assert len(health) == 1 and health[0]["status"] == "blind"
    assert health[0]["errors"] == {"monitor": "RuntimeError: kaputt"}
    assert store.health("w")["blind_runs"] == 3 and store.last_run("w") > 0
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert [l["type"] for l in lines] == ["health"]                     # C) Health auch im JSONL-Export


def test_health_events_go_to_jsonl_export(env, monkeypatch, tmp_path):
    feed, eng, store, hook, sent = env
    out = tmp_path / "health.jsonl"
    monkeypatch.setenv("WEBSEARCH_MONITOR_OUT", str(out))
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    feed.fail = True
    run_check(env)                                                       # blind -> Health-Meldung
    feed.fail, feed.items = False, [("a", "https://a.com", None)]
    clear_cache(eng)
    run_check(env)                                                       # wieder ok -> recovered
    types = [(l["type"], l.get("status")) for l in map(json.loads, out.read_text(encoding="utf-8").splitlines())]
    assert types == [("health", "blind"), ("health", "recovered")]


# --------------------------------------------------------------------------- Runde 4: Robustheit der Schleife

def test_corrupt_outbox_row_is_quarantined_not_fatal(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        db.execute("INSERT INTO outbox (payload, created, attempts, next_try, last_error) VALUES (?,?,?,?,?)",
                   ("{kaputt", 0, 1, 0, ""))
        store.outbox_add(db, {"id": "x", "type": "hits", "watch": "w", "query": "q", "new": []}, "HTTP 500")
    store.db.execute("UPDATE outbox SET next_try=0, created=?", (__import__("time").time(),))
    sent = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: sent.append(json.loads(r.content)) or httpx.Response(200))) as hook:
            with caplog.at_level(logging.ERROR, logger="websearch.monitor"):
                assert await M.flush_outbox(store, hook) == 1        # gueltiger Eintrag trotzdem zugestellt
                assert await M.flush_outbox(store, hook) == 0        # kaputte Zeile kommt nicht wieder
    asyncio.run(go())
    assert [p["id"] for p in sent] == ["x"]
    assert store.outbox_size() == 0
    assert store.db.execute("SELECT payload FROM outbox_dead").fetchall() == [("{kaputt",)]
    assert caplog.text.count("unlesbar") == 1                      # genau einmal geloggt


def test_loop_survives_store_errors(tmp_path, monkeypatch, caplog):
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")

    async def broken_flush(store, hook, now=None):
        raise sqlite3.OperationalError("database is locked")

    async def broken_check(*a, **k):
        raise RuntimeError("Watch kaputt")

    async def broken_record(*a, **k):
        raise sqlite3.OperationalError("commit fehlgeschlagen")
    monkeypatch.setattr(M, "flush_outbox", broken_flush)
    monkeypatch.setattr(M, "check_watch", broken_check)
    monkeypatch.setattr(M, "record_failure", broken_record)
    with caplog.at_level(logging.ERROR, logger="websearch.monitor"):
        hits = asyncio.run(M.run_monitor(str(wf), loop=True, interval=0, max_cycles=3,
                                         db_path=str(tmp_path / "m.db"), engine=eng))
    assert hits == []                                               # 3 Durchlaeufe, kein Absturz
    assert caplog.text.count("Outbox nachliefern fehlgeschlagen") == 3
    assert "Fehler festhalten fehlgeschlagen" in caplog.text


def test_watch_file_read_error_keeps_old_watches(tmp_path, monkeypatch, caplog):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([W]))
    wf = M.WatchFile(f)

    def boom():
        raise OSError("Datenträger weg")
    monkeypatch.setattr(wf, "current", boom)
    with caplog.at_level(logging.ERROR, logger="websearch.monitor"):
        assert [w.name for w in M._guard_sync("Watch-Datei prüfen", wf.current, default=wf.watches)] == ["w"]
    assert "Datenträger weg" in caplog.text


def test_seen_store_is_closed_after_run(tmp_path, monkeypatch):
    feed = Feed()
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    opened = []
    real_init = M.SeenStore.__init__

    def track(self, *a, **k):
        real_init(self, *a, **k)
        opened.append(self)
    monkeypatch.setattr(M.SeenStore, "__init__", track)
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"), engine=eng))
    with pytest.raises(sqlite3.ProgrammingError):                   # geschlossen
        opened[0].db.execute("SELECT 1")


def test_payload_ids_are_stable_and_distinct():
    a = {"type": "hits", "watch": "w", "new": [{"url": "https://www.a.com/"}, {"url": "https://b.com"}]}
    b = {"type": "hits", "watch": "w", "new": [{"url": "https://b.com/"}, {"url": "http://a.com"}]}
    assert M.payload_id(a) == M.payload_id(b)                       # Reihenfolge/Schreibweise egal
    assert M.payload_id(a) != M.payload_id(dict(a, watch="v"))
    h1 = {"type": "health", "watch": "w", "status": "blind", "since": "2026-11-05T10:00:00+00:00"}
    assert M.payload_id(h1) != M.payload_id(dict(h1, status="recovered"))
    assert len(M.payload_id(h1)) == 64


def test_webhook_payload_has_id_and_retry_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    got = []

    def handler(req):
        got.append(json.loads(req.content))
        return httpx.Response(500 if len(got) == 1 else 200)
    feed = Feed()
    feed.items = [("a", "https://a.com", None)]
    eng = SearchEngine({"feed": feed}, cache_path=tmp_path / "c.db")
    store = M.SeenStore(tmp_path / "m.db")

    async def go():
        async with eng, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as hook:
            hit = await M.check_watch(eng, store, W, hook, alert_first=True)
            await M.flush_outbox(store, hook, now=__import__("time").time() + 61)
            return hit
    hit = asyncio.run(go())
    assert len(got) == 2 and got[0]["id"] == got[1]["id"] == hit["id"] == M.payload_id(hit)


def test_seen_store_uses_short_busy_timeout_after_open(tmp_path):
    store = M.SeenStore(tmp_path / "m.db")
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == M.BUSY_TIMEOUT_MS
    other = sqlite3.connect(str(tmp_path / "m.db"), isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    t0 = __import__("time").monotonic()
    with pytest.raises(sqlite3.OperationalError):
        store.mark_run("w")
    assert __import__("time").monotonic() - t0 < 2                  # statt 10 s
    other.execute("ROLLBACK")


def test_startup_purge_failure_is_not_fatal(tmp_path, caplog):
    M.SeenStore(tmp_path / "m.db").close()
    real_tx = M.SeenStore.transaction
    calls = {"n": 0}

    @__import__("contextlib").contextmanager
    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:                     # bestehende DB: kein Schema-Schritt, 1 = Aufräumen
            raise sqlite3.OperationalError("database is locked")
        with real_tx(self) as db:
            yield db
    M.SeenStore.transaction = flaky
    try:
        with caplog.at_level(logging.WARNING, logger="websearch.monitor"):
            st = M.SeenStore(tmp_path / "m.db")
    finally:
        M.SeenStore.transaction = real_tx
    assert "Aufräumen übersprungen" in caplog.text
    st.mark_run("w")                                                 # weiter benutzbar


def test_export_error_keeps_hits_unseen(env, monkeypatch, tmp_path):
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://a.com", None)]
    target = tmp_path / "out"
    target.mkdir()                                                    # Verzeichnis -> open("a") scheitert
    monkeypatch.setenv("WEBSEARCH_MONITOR_OUT", str(target))
    with pytest.raises(OSError):
        run_check(env, alert_first=True)
    assert _items(store, "w") == set()
    monkeypatch.setenv("WEBSEARCH_MONITOR_OUT", str(tmp_path / "ok.jsonl"))
    clear_cache(eng)
    assert run_check(env, alert_first=True)["new"][0]["url"] == "https://a.com"


def test_kind_source_mismatch_fails_at_startup(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([dict(W, kind="news", sources=["wikipedia"])]))
    with pytest.raises(ValueError, match="Quelle 'wikipedia' kann die Suchart 'news' nicht \\(nur: ref\\)"):
        M.load_watches(f, known_sources=M.default_source_kinds())
    M.load_watches(f, known_sources=M.default_source_names())        # nur Namen: keine Sucharten-Pruefung
    from websearch.core import InputError
    eng = SearchEngine({"feed": Feed()}, cache_path=tmp_path / "c.db")
    f.write_text(json.dumps([dict(W, kind="news", sources=["feed"])]))
    asyncio.run(M.run_monitor(str(f), db_path=str(tmp_path / "m.db"), engine=eng))   # Feed kann news

    class WebOnly(Feed):
        kinds = ("web",)
    eng2 = SearchEngine({"feed": WebOnly()}, cache_path=tmp_path / "c2.db")
    with pytest.raises(InputError, match="kann die Suchart 'news' nicht"):
        asyncio.run(M.run_monitor(str(f), db_path=str(tmp_path / "m2.db"), engine=eng2))


def test_log_messages_are_readable(env, monkeypatch, caplog):
    feed, eng, store, hook, sent = env
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    feed.fail = True
    with caplog.at_level(logging.INFO, logger="websearch.monitor"):
        run_check(env)
    assert "seit 1 Lauf blind" in caplog.text and "Läufen" not in caplog.text
    assert "{" not in caplog.text                                    # keine Python-Dict-Darstellung
    assert M.fmt_failed({"a": "x", "b": "y"}) == "a: x; b: y" and M.fmt_failed({}) == "keine"
    assert M._runs(2) == "2 Läufen"


# =========================================================================== Runde 4

class Skipper(Feed):
    name = "skip"

    async def search(self, client, query, n, lang, kind):
        from websearch.core import SourceSkipped
        raise SourceSkipped("kurze Wörter")


def test_skipped_or_rate_limited_sources_do_not_cause_blind_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    sent = []
    hook = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    store = M.SeenStore(tmp_path / "m.db")
    w = dict(W, sources=["feed", "skip"])

    async def go():
        for _ in range(3):
            eng = SearchEngine({"feed": Feed(), "skip": Skipper()}, cache_path=tmp_path / "c.db")
            async with eng:
                await M.check_watch(eng, store, w, hook)
    asyncio.run(go())
    assert not [r for r in sent if json.loads(r.content).get("type") == "health"]


def test_each_hit_has_a_stable_id():
    a = M.hit_id("w", "https://www.example.com/x/?utm_source=y")
    assert a == M.hit_id("w", "https://example.com/x") and a != M.hit_id("other", "https://example.com/x")


def test_hits_payload_items_carry_ids(env):
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://a.com", None)]
    hit = run_check(env, alert_first=True)
    assert hit["new"][0]["id"] == M.hit_id("w", "https://a.com")


def test_flush_outbox_stops_after_first_transport_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(500)
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        for i in range(5):
            db.execute("INSERT INTO outbox (payload, created, attempts, next_try, last_error) VALUES (?,?,?,?,?)",
                       (json.dumps({"type": "hits", "watch": f"w{i}", "new": []}), __import__("time").time(), 0, 0, None))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as hook:
            return await M.flush_outbox(store, hook)
    assert asyncio.run(go()) == 0 and len(calls) == 1


def test_monitor_limits_parallel_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_MONITOR_CONCURRENCY", "2")
    running = peak = 0

    class Slow(Feed):
        async def search(self, client, query, n, lang, kind):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return [Result("t", f"https://x.com/{query}", source=self.name, rank=1)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([dict(W, name=f"w{i}", query=f"q{i}") for i in range(6)]))
    eng = SearchEngine({"feed": Slow()}, cache_path=tmp_path / "c.db")
    asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"), engine=eng))
    assert peak <= 2


def test_seenstore_opens_existing_db_while_another_process_writes(tmp_path):
    import time as _t
    M.SeenStore(tmp_path / "m.db").close()
    other = sqlite3.connect(str(tmp_path / "m.db"), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    t = _t.monotonic()
    st = M.SeenStore(tmp_path / "m.db")                 # kein Schreib-Lock nötig -> kein Warten
    assert _t.monotonic() - t < 1.5
    other.execute("ROLLBACK")
    st.close()


def test_bad_concurrency_setting_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_MONITOR_CONCURRENCY", "viele")
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    from websearch.core import InputError
    with pytest.raises(InputError, match="CONCURRENCY"):
        asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"),
                                  engine=SearchEngine({"feed": Feed()}, cache_path=tmp_path / "c.db")))


# =========================================================================== Runde 6

def test_rare_query_with_one_failed_fallback_is_not_blind(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    sent = []
    hook = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    store = M.SeenStore(tmp_path / "m.db")

    class Down(Feed):
        name = "down"

        async def search(self, client, query, n, lang, kind):
            from websearch.core import SourceUnavailable
            raise SourceUnavailable("down")

    async def go():
        for _ in range(3):
            eng = SearchEngine({"feed": Feed(), "down": Down()}, cache_path=tmp_path / "c.db")
            async with eng:
                await M.check_watch(eng, store, dict(W, sources=["feed", "down"]), hook)
    asyncio.run(go())
    assert not [r for r in sent if json.loads(r.content).get("type") == "health"]


def test_webhook_redirect_is_not_delivery(tmp_path, monkeypatch):
    from websearch import notify
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "http://hook.local/topic")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(301, headers={"location": "https://hook.local/topic/"}))) as c:
            return await notify.send(c, {"type": "hits", "watch": "w", "new": []})
    assert asyncio.run(go()) is False


def test_seen_timestamps_are_refreshed_for_evergreen_hits(env):
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://ever.green/", None)]
    run_check(env)
    store.db.execute("UPDATE seen SET t = t - 200 * 86400")
    clear_cache(eng)
    run_check(env)
    t = store.db.execute("SELECT t FROM seen").fetchone()[0]
    assert __import__("time").time() - t < 60                       # aufgefrischt -> überlebt den Purge


def test_changed_query_starts_a_new_baseline(env):
    feed, eng, store, hook, sent = env
    feed.items = [("a", "https://a.com", None)]
    run_check(env)
    feed.items = [("b", "https://b.com", None), ("c", "https://c.com", None)]
    clear_cache(eng)
    assert run_check(env, extra={"query": "ganz anders"}) is None    # neue Basislinie statt Meldungsflut
    feed.items.append(("d", "https://d.com", None))
    clear_cache(eng)
    hit = run_check(env, extra={"query": "ganz anders"})
    assert [r["url"] for r in hit["new"]] == ["https://d.com"]


@pytest.mark.parametrize("content,msg", [
    ('﻿[{"name": "a", "query": "q"}]', None),
    ('[{"name": "a", "query": "q", "every_min": NaN}]', "kein gültiger Zahlenwert"),
    ('[{"name": "a", "query": "q", "deadline": Infinity}]', "kein gültiger Zahlenwert"),
])
def test_bom_and_nan_in_watch_files(tmp_path, content, msg):
    f = tmp_path / "w.json"
    f.write_text(content, encoding="utf-8")
    from websearch.core import InputError
    if msg is None:
        assert M.load_watches(f)[0].name == "a"
    else:
        with pytest.raises(InputError, match=msg):
            M.load_watches(f)


def test_non_utf8_watch_file_is_a_clear_error(tmp_path):
    f = tmp_path / "w.json"
    f.write_bytes('[{"name": "ä", "query": "q"}]'.encode("utf-16"))
    from websearch.core import InputError
    with pytest.raises(InputError, match="UTF-8"):
        M.load_watches(f)



# =========================================================================== Runde 7

def test_ok_zero_from_never_delivering_source_does_not_mask_blindness(tmp_path, monkeypatch):
    """HN/GDELT antworten bei Markennamen fast immer 'ok, 0' - das darf echte Ausfälle nicht überdecken."""
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "2")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    sent = []
    hook = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    store = M.SeenStore(tmp_path / "m.db")

    class Good(Feed):
        name = "good"

    class Down(Feed):
        name = "down"

        async def search(self, client, query, n, lang, kind):
            from websearch.core import BlockedError
            raise BlockedError("HTTP 429")

    class Empty(Feed):
        name = "empty"           # wie HN: antwortet immer, aber nie mit Treffern

    good = Good()
    good.items = [("a", "https://a.com", None)]

    async def go(first_ok: bool, i: int):
        eng = SearchEngine({"good": good if first_ok else Down(), "empty": Empty()},
                           cache_path=tmp_path / f"c{i}.db")      # frischer Cache je Lauf
        async with eng:
            await M.check_watch(eng, store, dict(W, sources=["good", "empty"]), hook)
    asyncio.run(go(True, 0))           # Basislinie: "good" liefert -> bekannt; "empty" nie
    for i in (1, 2):
        asyncio.run(go(False, i))      # "good" gesperrt, nur "empty" antwortet mit 0
    health = [json.loads(r.content) for r in sent if json.loads(r.content).get("type") == "health"]
    assert health and health[0]["status"] == "blind" and "good" in health[0]["errors"]


def test_only_skipped_sources_are_not_blind_and_such_watches_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "1")
    store = M.SeenStore(tmp_path / "m.db")
    res = {"results": [], "sources": {"gdelt": {"state": "skipped", "count": 0, "ms": 0, "message": "zu kurz", "meta": {}}}}
    payload, _ = M._health_transition(store, M.parse_watch(W), res, {})
    assert payload is None                                            # übersprungen ist kein Ausfall
    f = tmp_path / "w.json"
    f.write_text(json.dumps([{"name": "g", "query": "EU AI", "sources": ["gdelt"]}]))
    from websearch.core import InputError
    with pytest.raises(InputError, match="keine der Quellen kann diese Anfrage"):
        M.load_watches(f, known_sources=M.default_source_kinds())

def test_slow_watch_does_not_block_others_in_loop(tmp_path):
    import time as _t

    class Slow(Feed):
        async def search(self, client, query, n, lang, kind):
            if query == "langsam":
                await asyncio.sleep(1.5)
            return [Result("t", f"https://x.com/{query}", source=self.name, rank=1)]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([dict(W, name="slow", query="langsam"), dict(W, name="fast", query="schnell")]))
    eng = SearchEngine({"feed": Slow()}, cache_path=tmp_path / "c.db")
    t = _t.monotonic()
    hits = asyncio.run(M.run_monitor(str(wf), loop=True, interval=0.1, max_cycles=10, alert_first=True,
                                     db_path=str(tmp_path / "m.db"), engine=eng))
    assert {h["watch"] for h in hits} == {"slow", "fast"}


def test_heartbeat_is_written(tmp_path, monkeypatch):
    hb = tmp_path / "hb"
    monkeypatch.setenv("WEBSEARCH_HEARTBEAT", str(hb))
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps([W]))
    asyncio.run(M.run_monitor(str(wf), db_path=str(tmp_path / "m.db"),
                              engine=SearchEngine({"feed": Feed()}, cache_path=tmp_path / "c.db")))
    assert hb.exists() and float(hb.read_text()) > 0


@pytest.fixture(autouse=True)
def _fresh_webhook_backoff():
    from websearch import notify
    notify.reset_backoff()
    yield
    notify.reset_backoff()


def test_slow_drip_webhook_is_cut_and_later_messages_skip_waiting(monkeypatch):
    from websearch import notify
    monkeypatch.setattr(notify, "SEND_TIMEOUT", 0.3)
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    calls = []

    async def drip():
        for _ in range(50):
            await asyncio.sleep(0.1)
            yield b"."

    def h(req):
        calls.append(req)
        return httpx.Response(200, content=drip())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            first = await notify.send(c, {"type": "hits", "watch": "w", "new": []})
            second = await notify.send(c, {"type": "hits", "watch": "w2", "new": []})
            return first, second
    import time as _t
    t = _t.monotonic()
    assert asyncio.run(go()) == (False, False) and len(calls) == 1 and _t.monotonic() - t < 2


def test_mcp_paid_typo_fails_closed(monkeypatch):
    from websearch import mcp_server
    monkeypatch.setenv("WEBSEARCH_MCP_PAID", "jein")
    assert mcp_server._paid() is False


def test_deeply_nested_watch_file_is_a_clear_error(tmp_path):
    from websearch.core import InputError
    f = tmp_path / "w.json"
    f.write_text("[" * 100000 + "]" * 100000)
    with pytest.raises(InputError):
        M.load_watches(f)


def test_new_watch_is_not_masked_by_sparse_sources(tmp_path, monkeypatch):
    """Neuer Watch: Hauptquellen gesperrt, nur HN/GDELT-artige Quellen antworten 0 -> muss blind werden."""
    monkeypatch.setenv("WEBSEARCH_BLIND_AFTER", "2")
    store = M.SeenStore(tmp_path / "m.db")
    res = {"results": [], "sources": {
        "searxng": {"state": "unavailable", "count": 0, "ms": 0, "message": "alle Engines aus", "meta": {}},
        "hackernews": {"state": "ok", "count": 0, "ms": 0, "message": None, "meta": {}}}}
    w = M.parse_watch(W)
    p1, st1 = M._health_transition(store, w, res, {"searxng": "alle Engines aus"}, set(), {"hackernews"})
    assert p1 is None and st1[0] == 1                                  # erster blinder Lauf gezählt


def test_webhook_backoff_expires_on_its_own(monkeypatch):
    from websearch import notify
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://hook.local/x")
    monkeypatch.setattr(notify, "BACKOFF_S", 0.2)
    calls = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(500 if len(calls) == 1 else 200))) as c:
            a = await notify.send(c, {"type": "hits", "watch": "w", "new": []})
            b = await notify.send(c, {"type": "hits", "watch": "w", "new": []})     # in der Pause: nicht gesendet
            await asyncio.sleep(0.3)
            d = await notify.send(c, {"type": "hits", "watch": "w", "new": []})     # Pause vorbei
            return a, b, d
    assert asyncio.run(go()) == (False, False, True) and len(calls) == 2


def test_no_false_recovered_when_nothing_answered(tmp_path):
    """Nach einer Blind-Warnung: Lauf, in dem alle Quellen nur gedrosselt/übersprungen waren -> kein 'recovered'."""
    store = M.SeenStore(tmp_path / "m.db")
    w = M.parse_watch(W)
    with store.transaction() as db:
        M.SeenStore.set_health(db, w.name, 3, 1000.0, True, None)
    res = {"results": [], "sources": {
        "searxng": {"state": "rate_limited", "count": 0, "ms": 0, "message": "Tempo", "meta": {}},
        "gdelt": {"state": "skipped", "count": 0, "ms": 0, "message": "zu kurz", "meta": {}}}}
    payload, state = M._health_transition(store, w, res, {})
    assert payload is None and state[2] is True                         # bleibt "alarmiert"
    res_ok = {"results": [{"url": "https://a.example"}], "sources": {
        "searxng": {"state": "ok", "count": 1, "ms": 0, "message": None, "meta": {}}}}
    payload, state = M._health_transition(store, w, res_ok, {})
    assert payload and payload["status"] == "recovered" and state[2] is False


def test_one_shot_monitor_exits_1_when_every_watch_is_blind(tmp_path, monkeypatch):
    from websearch import __main__ as cli
    monkeypatch.setenv("WEBSEARCH_MONITOR_DB", str(tmp_path / "m.db"))
    f = tmp_path / "w.json"
    f.write_text(json.dumps([{"name": "a", "query": "q"}, {"name": "b", "query": "r"}]))
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        M.SeenStore.set_health(db, "a", 1, 1.0, False, None)
    store.close()
    assert M.blind_watches(str(f)) == (1, 2)
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        M.SeenStore.set_health(db, "b", 2, 1.0, False, None)
    store.close()
    assert M.blind_watches(str(f)) == (2, 2)

    async def nothing(*a, **k):
        return []
    monkeypatch.setattr(M, "run_monitor", nothing)
    assert cli.main(["monitor", str(f)]) == 1


def test_run_time_is_the_start_of_the_run(tmp_path):
    store = M.SeenStore(tmp_path / "m.db")
    with store.transaction() as db:
        M.SeenStore.set_run(db, "w", 1234.5)
    assert store.last_run("w") == 1234.5
