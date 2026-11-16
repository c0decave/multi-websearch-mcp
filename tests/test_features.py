"""Offline-Tests der Erweiterungen: Zeitfilter (days), Bild-/Videosuche, Metriken, Sprache (i18n),
Vorlagen (init/doctor: test_setup_cmds.py)."""
import ast
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from websearch import __main__ as cli
from websearch import i18n
from websearch import providers as P
from websearch.core import InputError, Result, StateStore
from websearch.engine import SearchEngine, TierPolicy
from websearch.metrics import render
from websearch.providers import Provider, ResultList

DAY = 86400


def iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * DAY))


class Dated(Provider):
    """Fake-Quelle mit datierten und undatierten Treffern; merkt sich das übergebene days."""
    min_interval = 0
    kinds = ("web", "news", "ref", "images", "videos")
    time_windows = ()                 # Zeitfilter landet genau in Tagen in der Anfrage

    def __init__(self, name, native=0, rows=None):
        self.name, self.max_native_days, self.seen_days = name, native, []
        self._rows = rows if rows is not None else [("neu", iso(1)), ("alt", iso(40)), ("ohne", None)]
        super().__init__()

    async def search(self, client, query, n, lang, kind, days=None):
        self.seen_days.append(days)
        return ResultList([Result(t, f"https://{self.name}.example/{t}", source=self.name, rank=i, published=d,
                                  image=f"https://img.example/{t}.jpg" if kind == "images" else "")
                           for i, (t, d) in enumerate(self._rows, 1)])


class OldStyle(Provider):
    """Eigene Quelle ohne days-Parameter (Altbestand): muss ohne Zeitfilter weiter funktionieren."""
    name, min_interval = "old", 0

    async def search(self, client, query, n, lang, kind):
        return [Result("x", "https://old.example/x", source=self.name, rank=1)]


def engine(tmp_path, *provs, kinds=("web", "news", "images", "videos")):
    pol = TierPolicy(primary={k: [p.name for p in provs] for k in kinds})
    return SearchEngine({p.name: p for p in provs}, policy=pol, cache_path=tmp_path / "c.db")


def search(eng, *a, **k):
    async def go():
        async with eng:
            return await eng.search(*a, **k)
    return asyncio.run(go())


# =========================================================================== Zeitfilter

def test_days_filters_post_hoc_and_drops_undated(tmp_path):
    src = Dated("a")
    res = search(engine(tmp_path, src), "x", days=7)
    assert [r["title"] for r in res["results"]] == ["neu"]
    assert src.seen_days == [7]
    assert res["sources"]["a"]["meta"].get("filtered") is True     # 0 nach Filter != stille Sperre


def test_days_native_source_keeps_undated(tmp_path):
    res = search(engine(tmp_path, Dated("a", native=366)), "x", days=7)
    assert [r["title"] for r in res["results"]] == ["neu", "ohne"]


def test_days_beyond_native_window_is_filtered_strictly(tmp_path):
    res = search(engine(tmp_path, Dated("a", native=31)), "x", days=100)
    assert [r["title"] for r in res["results"]] == ["neu", "alt"]   # undatiert fällt weg


def test_days_is_part_of_the_cache_key(tmp_path):
    src = Dated("a")
    eng = engine(tmp_path, src)

    async def go():
        async with eng:
            first = await eng.search("x", days=7)
            second = await eng.search("x")
            third = await eng.search("x", days=7)
        return first, second, third
    first, second, third = asyncio.run(go())
    assert len(first["results"]) == 1 and len(second["results"]) == 3
    assert third["sources"]["a"]["state"] == "cached" and src.seen_days == [7, None]


def test_days_not_passed_to_providers_without_parameter(tmp_path):
    assert search(engine(tmp_path, OldStyle()), "x")["results"]


@pytest.mark.parametrize("bad", [0, -1, 3651, 1.5, "abc", True, float("nan"), [7]])
def test_days_validation(tmp_path, bad):
    with pytest.raises(InputError):
        search(engine(tmp_path, Dated("a")), "x", days=bad)


def test_days_accepts_integral_numbers_and_rejects_ref(tmp_path):
    assert search(engine(tmp_path, Dated("a")), "x", days=7.0)["results"]
    assert search(engine(tmp_path, Dated("b")), "x", days="7")["results"]
    with pytest.raises(InputError, match="ref"):
        search(engine(tmp_path, Dated("c"), kinds=("ref",)), "x", kind="ref", days=7)


def test_time_window_picks_smallest_covering_window():
    assert P.time_window(1, P._SX_RANGES) == "day"
    assert P.time_window(2, P._SX_RANGES) == "week"
    assert P.time_window(31, P._SX_RANGES) == "month"
    assert P.time_window(366, P._SX_RANGES) == "year"
    assert P.time_window(400, P._SX_RANGES) is None and P.time_window(None, P._SX_RANGES) is None
    assert P.time_window(7, P._DMWY) == "w"


def run(provider, handler, kind="web", days=None, query="test"):
    seen = []

    def h(req):
        seen.append(req)
        return handler(req)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await provider.search(c, query, 10, "de", kind, days=days)
    return asyncio.run(go()), seen


def test_searxng_passes_time_range_and_category():
    res, seen = run(P.SearXNG("http://sx.local"), lambda r: httpx.Response(200, json={"results": []}),
                    kind="news", days=5)
    assert seen[0].url.params["time_range"] == "week" and seen[0].url.params["categories"] == "news"
    _, seen = run(P.SearXNG("http://sx.local"), lambda r: httpx.Response(200, json={"results": []}), days=None)
    assert "time_range" not in seen[0].url.params


def test_news_feeds_and_paid_apis_get_native_time_filters(monkeypatch):
    for var in ("BRAVE_API_KEY", "SERPER_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY", "LINKUP_API_KEY"):
        monkeypatch.setenv(var, "k")
    empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
    _, seen = run(P.GoogleNewsRSS(), lambda r: httpx.Response(200, text=empty_rss), kind="news", days=3)
    assert seen[0].url.params["q"] == "test when:3d"
    _, seen = run(P.HackerNews(), lambda r: httpx.Response(200, json={"hits": []}), kind="news", days=2)
    since = int(seen[0].url.params["numericFilters"].split(">")[1])
    assert abs(since - (time.time() - 2 * DAY)) < 60
    _, seen = run(P.GDELT(), lambda r: httpx.Response(200, json={"articles": []}), kind="news", days=200)
    assert seen[0].url.params["timespan"] == "90d"
    _, seen = run(P.BraveAPI(), lambda r: httpx.Response(200, json={"web": {"results": []}}), days=10)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dto\d{4}-\d\d-\d\d", seen[0].url.params["freshness"])
    _, seen = run(P.SerperAPI(), lambda r: httpx.Response(200, json={"organic": []}), days=10)
    assert json.loads(seen[0].content)["tbs"] == "qdr:m"
    _, seen = run(P.TavilyAPI(), lambda r: httpx.Response(200, json={"results": []}), days=1)
    assert json.loads(seen[0].content)["time_range"] == "day"
    _, seen = run(P.ExaAPI(), lambda r: httpx.Response(200, json={"results": []}), days=1)
    assert json.loads(seen[0].content)["startPublishedDate"].endswith("T00:00:00.000Z")
    _, seen = run(P.LinkupAPI(), lambda r: httpx.Response(200, json={"results": []}), days=1)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", json.loads(seen[0].content)["fromDate"])


# =========================================================================== Bilder und Videos

def test_searxng_maps_images_and_videos():
    def h(req):
        cat = req.url.params["categories"]
        row = {"title": "Nordlicht", "url": "https://page.example/a", "engines": ["bing images"],
               "img_src": "https://img.example/a.jpg", "thumbnail_src": "https://img.example/a_t.jpg",
               "length": 754, "thumbnail": "https://img.example/v.jpg"}
        return httpx.Response(200, json={"results": [row]}) if cat in ("images", "videos") else httpx.Response(500)
    (img,), seen = run(P.SearXNG("http://sx.local"), h, kind="images")
    assert seen[0].url.params["categories"] == "images"
    assert img.image == "https://img.example/a.jpg" and img.thumbnail == "https://img.example/a_t.jpg"
    assert img.duration == ""
    (vid,), _ = run(P.SearXNG("http://sx.local"), h, kind="videos")
    assert vid.duration == "12:34" and vid.image == "" and vid.thumbnail == "https://img.example/a_t.jpg"


def test_web_results_do_not_get_media_fields():
    row = {"title": "Nordlicht Foto", "url": "https://page.example/a", "engines": ["google"],
           "thumbnail": "https://img.example/t.jpg", "img_src": "https://img.example/a.jpg"}
    (res,), _ = run(P.SearXNG("http://sx.local"), lambda r: httpx.Response(200, json={"results": [row]}),
                    query="Nordlicht")
    assert res.image == "" and res.thumbnail == ""


def test_duration_and_media_url_sanitizing():
    assert P._duration(59) == "0:59" and P._duration(3661) == "1:01:01" and P._duration("4:05") == "4:05"
    assert P._duration(None) == "" and P._duration(True) == "" and P._duration(-5) == ""
    r = Result("t", "https://a.example", image="javascript:alert(1)", thumbnail=" https://t.example/x.jpg ")
    assert r.image == "" and r.thumbnail == "https://t.example/x.jpg"


def test_ddgs_duckduckgo_offers_media_kinds_others_not():
    assert "images" in P.DDGS("duckduckgo").kinds and "videos" in P.DDGS("duckduckgo").kinds
    assert "images" not in P.DDGS("yahoo").kinds and "images" not in P.DDGS.kinds


def test_ddgs_media_mapping(monkeypatch):
    import ddgs

    class FakeDDGS:
        def __init__(self, **k):
            pass

        def _get_engines(self, cat, backend):
            return []

        def images(self, query, **k):
            FakeDDGS.kwargs = k
            return [{"title": "Bild", "url": "https://page.example", "image": "https://img.example/b.jpg",
                     "thumbnail": "https://img.example/b_t.jpg", "source": "Bing"}]

        def videos(self, query, **k):
            return [{"title": "Video", "content": "https://video.example/v", "duration": "3:21",
                     "images": {"medium": "https://img.example/v.jpg"}, "published": "2026-11-01T00:00:00Z"}]
    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    (img,), _ = run(P.DDGS("duckduckgo"), lambda r: httpx.Response(500), kind="images", days=3)
    assert img.url == "https://page.example" and img.image == "https://img.example/b.jpg"
    assert FakeDDGS.kwargs["timelimit"] == "w"
    (vid,), _ = run(P.DDGS("duckduckgo"), lambda r: httpx.Response(500), kind="videos")
    assert vid.url == "https://video.example/v" and vid.duration == "3:21" and vid.thumbnail.endswith("v.jpg")


def test_engine_media_search_and_no_paid_tier(tmp_path):
    res = search(engine(tmp_path, Dated("a")), "x", kind="images")
    assert res["kind"] == "images" and res["results"][0]["image"].startswith("https://img.example/")
    from websearch.engine import DEFAULT_POLICY
    assert not DEFAULT_POLICY.paid.get("images") and not DEFAULT_POLICY.paid.get("videos")
    assert DEFAULT_POLICY.primary["images"] == ("searxng",)


# =========================================================================== Metriken

def test_stats_are_counted_and_rendered(tmp_path):
    eng = engine(tmp_path, Dated("a"), Dated("b", rows=[]))

    async def go():
        async with eng:
            await eng.search("x")
            await eng.search("x")              # zweites Mal aus dem Cache
            return render(eng)
    text = asyncio.run(go())
    assert 'websearch_source_requests_total{source="a",state="ok"} 1' in text
    assert 'websearch_source_requests_total{source="a",state="cached"} 1' in text
    assert 'websearch_searches_total{kind="web",result="ok"} 2' in text
    assert 'websearch_source_blocked{source="a"} 0' in text
    assert "# TYPE websearch_source_requests_total counter" in text and text.endswith("\n")
    assert 'websearch_paid_limit{period="day"} 100' in text


def test_stats_are_host_wide(tmp_path):
    a, b = StateStore(tmp_path / "s.db"), StateStore(tmp_path / "s.db")
    a.record_stat("x", "ok", 10)
    b.record_stat("x", "ok", 5)
    b.record_stat("x", "blocked", 1)
    assert a.stats() == [("x", "blocked", 1, 1), ("x", "ok", 2, 15)]


def test_metric_labels_are_escaped():
    from websearch.metrics import _family
    line = _family("m", "gauge", "h", [({"source": 'a"b\\c\nd'}, 1)])[-1]
    assert line == 'm{source="a\\"b\\\\c\\nd"} 1'


def test_metrics_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SearchEngine", lambda *a, **k: engine(tmp_path, Dated("a")))
    assert cli.main(["metrics"]) == 0
    assert "websearch_info" in capsys.readouterr().out


# =========================================================================== Sprache

def test_language_selection(monkeypatch):
    for var in ("WEBSEARCH_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    assert i18n.lang() == "en"
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert i18n.lang() == "de" and i18n.t("ja", "yes") == "ja"
    monkeypatch.setenv("LC_ALL", "C.UTF-8")             # LC_ALL hat Vorrang
    assert i18n.lang() == "en"
    monkeypatch.setenv("WEBSEARCH_LANG", "de")          # explizit gewinnt immer
    assert i18n.lang() == "de"
    monkeypatch.setenv("WEBSEARCH_LANG", "fr")
    with pytest.raises(InputError):
        i18n.check_lang()


def _t_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t":
            yield node


def _shape(node: ast.expr):
    """Platzhalter eines Literals: %-Platzhalter bzw. Ausdrücke der f-String-Felder (Texte darin ausgeblendet)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ("const", re.findall(r"%[-+ 0#]*\d*(?:\.\d+)?[sdrfi]", node.value))
    if isinstance(node, ast.JoinedStr):
        class Blank(ast.NodeTransformer):
            def visit_Constant(self, c):
                return ast.Constant("") if isinstance(c.value, str) else c
        return ("f", [ast.dump(Blank().visit(v.value)) + str(v.conversion) for v in node.values
                      if isinstance(v, ast.FormattedValue)])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dedent":
        return _shape(node.args[0])
    return ("other", ast.dump(node))


def test_every_message_exists_in_both_languages_with_same_placeholders():
    files = sorted(Path(__file__).parent.parent.joinpath("websearch").glob("*.py"))
    calls = 0
    for f in files:
        for call in _t_calls(f):
            calls += 1
            assert len(call.args) == 2 and not call.keywords, f"{f.name}:{call.lineno}: t() braucht genau 2 Texte"
            de, en = (_shape(a) for a in call.args)
            assert de[0] in ("const", "f") and en[0] in ("const", "f"), f"{f.name}:{call.lineno}: keine Literale"
            assert de == en, f"{f.name}:{call.lineno}: Platzhalter unterschiedlich: {de} vs {en}"
            assert ast.get_source_segment(f.read_text(encoding="utf-8"), call.args[0]) != \
                ast.get_source_segment(f.read_text(encoding="utf-8"), call.args[1]) or de[1] == [], \
                f"{f.name}:{call.lineno}: identische Texte"
    assert calls > 200


def test_cli_speaks_english(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    monkeypatch.setattr(cli, "SearchEngine", lambda *a, **k: engine(tmp_path, Dated("a")))
    assert cli.main(["search", "x", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "(web, 1 results)" in out and "-- sources:" in out
    assert cli.main(["search", "", "--no-paid"]) == 2
    assert "Error: empty query" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "Getting started" in capsys.readouterr().out


def test_cli_rejects_bad_language(monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_LANG", "klingon")
    assert cli.main(["sources"]) == 2
    assert "WEBSEARCH_LANG" in capsys.readouterr().err


def test_cli_image_output_and_days_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SearchEngine", lambda *a, **k: engine(tmp_path, Dated("a")))
    assert cli.main(["search", "x", "-k", "images"]) == 0
    assert "Bild: https://img.example/neu.jpg" in capsys.readouterr().out
    assert cli.main(["search", "x", "--days", "0"]) == 2


# =========================================================================== Vorlagen

def test_shipped_env_templates_list_the_same_variables():
    root = Path(__file__).parent.parent
    def keys(name):
        return sorted(re.findall(r"^#?\s?([A-Z][A-Z0-9_]+)=", (root / name).read_text(encoding="utf-8"), re.M))
    assert keys(".env.example") == keys(".env.example.de")
    assert {"WEBSEARCH_LANG", "MCP_AUTH_TOKEN", "REDDIT_CLIENT_ID", "WEBSEARCH_SMTP_HOST"} <= set(keys(".env.example"))


# =========================================================================== Review-Korrekturen (Runde 2)

def test_images_from_one_page_stay_separate_and_media_fields_are_merged(tmp_path):
    from websearch.core import merge_rrf
    page = "https://gallery.example/album"
    a = [Result(f"Bild {i}", page, image=f"https://img.example/{i}.jpg", source="a", rank=i) for i in range(1, 6)]
    b = [Result("Bild 1", page, source="b", rank=1, image="https://img.example/1.jpg",
                thumbnail="https://img.example/1_t.jpg")]
    merged = merge_rrf([a, b])
    assert len(merged) == 5
    first = next(r for r in merged if r.image.endswith("/1.jpg"))
    assert first.thumbnail == "https://img.example/1_t.jpg" and set(first.engines) == {"a", "b"}
    v1 = [Result("Video", "https://v.example/x", source="a", rank=1)]
    v2 = [Result("Video", "https://v.example/x", source="b", rank=1, duration="3:21", thumbnail="https://t.example/v")]
    (video,) = merge_rrf([v1, v2])
    assert video.duration == "3:21" and video.thumbnail == "https://t.example/v"


class Slow(Provider):
    min_interval, kinds = 0, ("web", "news")

    def __init__(self, name, delay):
        self.name, self._delay = name, delay
        super().__init__()

    async def search(self, client, query, n, lang, kind, days=None):
        await asyncio.sleep(self._delay)
        return [Result("x", f"https://{self.name}.example/x", source=self.name, rank=1)]


def test_sources_cut_by_the_deadline_are_counted_in_metrics(tmp_path):
    eng = engine(tmp_path, Slow("slow", 5), Slow("fast", 0), kinds=("web",))

    async def go():
        async with eng:
            await eng.search("x", deadline=0.6)
            return render(eng)
    text = asyncio.run(go())
    assert 'websearch_source_requests_total{source="slow",state="deadline"} 1' in text
    assert 'websearch_source_requests_total{source="fast",state="ok"} 1' in text


def test_aborted_searches_count_sources_as_cancelled(tmp_path):
    eng = engine(tmp_path, Slow("slow", 5), kinds=("web",))

    async def go():
        async with eng:
            task = asyncio.create_task(eng.search("x", deadline=10))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return render(eng)
    assert 'websearch_source_requests_total{source="slow",state="cancelled"} 1' in asyncio.run(go())


def test_counters_are_buffered_not_written_per_request(tmp_path, monkeypatch):
    writes = []
    orig = StateStore.record_stats
    monkeypatch.setattr(StateStore, "record_stats", lambda self, rows: (writes.append(rows), orig(self, rows))[1])
    eng = engine(tmp_path, Dated("a"), Dated("b"), kinds=("web",))

    async def go():
        async with eng:
            for q in ("x", "y", "z"):
                await eng.search(q)
    asyncio.run(go())
    assert len(writes) == 1                     # einmal beim Schließen statt 3 Suchen x 3 Zähler
    assert sum(c for _, _, c, _ in writes[0]) == 9


def test_locked_state_file_while_counting_does_not_disable_rate_limits(tmp_path, monkeypatch):
    import sqlite3
    eng = engine(tmp_path, Dated("a"), kinds=("web",))

    def locked(self, rows):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(StateStore, "record_stats", locked)

    async def go():
        async with eng:
            await eng.search("x")
            eng.flush_stats()
            return eng.store.degraded, dict(eng._stats)
    degraded, kept = asyncio.run(go())
    assert degraded is False and kept           # Zähler bleiben für später im Speicher


def test_old_providers_without_days_are_post_filtered_not_broken(tmp_path, caplog):
    class Legacy(Provider):
        name, min_interval, time_windows = "legacy", 0, ()

        async def search(self, client, query, n, lang, kind):
            return [Result("neu", "https://l.example/neu", source="legacy", rank=1, published=iso(1)),
                    Result("alt", "https://l.example/alt", source="legacy", rank=2, published=iso(30))]
    with caplog.at_level("WARNING"):
        res = search(engine(tmp_path, Legacy(), kinds=("web",)), "x", days=7)
    assert [r["title"] for r in res["results"]] == ["neu"] and "Antwortformat" not in caplog.text


def test_cache_is_shared_by_days_values_that_send_the_same_request(tmp_path):
    class Windowed(Dated):
        time_windows = (1, 7, 31, 366)
    w, plain = Windowed("w", native=366), Dated("p")
    plain.time_windows = None                   # Anfrage hängt nicht von days ab (nur Nachfilter)
    eng = engine(tmp_path, w, plain, kinds=("web",))

    async def go():
        async with eng:
            for d in (3, 5, 7, 2, None):
                await eng.search("x", days=d)
    asyncio.run(go())
    assert w.seen_days == [7, None]             # 3/5/7/2 = alle "week" -> ein Request (ganze Woche); dann ohne
    assert plain.seen_days == [None]            # ignoriert days -> immer derselbe Eintrag


def test_undated_results_only_kept_when_the_window_matches_exactly(tmp_path):
    class Windowed(Dated):
        time_windows = (1, 7, 31, 366)
    assert [r["title"] for r in search(engine(tmp_path, Windowed("a", native=366), kinds=("web",)), "x",
                                       days=7)["results"]] == ["neu", "ohne"]
    assert [r["title"] for r in search(engine(tmp_path / "b", Windowed("b", native=366), kinds=("web",)), "x",
                                       days=3)["results"]] == ["neu"]          # Woche abgefragt, 3 Tage verlangt


def test_mojeek_does_not_claim_a_native_time_filter():
    m = P.DDGS("mojeek")
    assert m.max_native_days == 0 and m.time_windows is None
    assert P.DDGS("duckduckgo").time_windows == (1, 7, 31, 366)


def test_empty_answer_with_days_is_no_silent_block(tmp_path):
    src = Dated("s", rows=[])
    src.scraped = True
    other = Dated("o")
    eng = engine(tmp_path, src, other, kinds=("web",))

    async def go():
        async with eng:
            for q in ("a", "b", "c", "d"):
                await eng.search(q, days=1)
            return src.breaker.is_open()
    assert asyncio.run(go()) is False


def test_paid_tier_with_days_only_uses_sources_that_filter_themselves(tmp_path):
    class Paid(Dated):
        paid = True
    fails = Dated("free", rows=[])
    fails.scraped = False

    async def broken(*a, **k):
        raise BlockedError("captcha")
    from websearch.core import BlockedError
    fails.search = broken
    no_filter, filters = Paid("p1"), Paid("p2", native=3650)
    no_filter.time_windows = None
    pol = TierPolicy(primary={"web": ["free"]}, paid={"web": ["p1", "p2"]})
    eng = SearchEngine({p.name: p for p in (fails, no_filter, filters)}, policy=pol, cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("x", days=7)
    res = asyncio.run(go())
    assert no_filter.seen_days == [] and filters.seen_days == [7] and "p1" not in res["sources"]


def test_search_many_reports_bad_days_once(tmp_path):
    eng = engine(tmp_path, Dated("a"), kinds=("ref",))

    async def go():
        async with eng:
            return await eng.search_many(["a", "b"], kind="ref", days=7)
    with pytest.raises(InputError, match="ref"):
        asyncio.run(go())


def test_window_sources_cache_the_whole_window_and_engine_filters_exactly(tmp_path):
    """Regression: days=2 und danach days=7 teilen sich den Eintrag "week" - der muss die ganze Woche enthalten."""
    class Windowed(Dated):
        time_windows = (1, 7, 31, 366)

        async def search(self, client, query, n, lang, kind, days=None):
            self.seen_days.append(days)
            rows = [("eins", iso(1)), ("fuenf", iso(5)), ("zwanzig", iso(20))]
            cutoff = iso(days) if days else ""
            return [Result(t, f"https://w.example/{t}", source=self.name, rank=i, published=d)
                    for i, (t, d) in enumerate(rows, 1) if d >= cutoff]       # filtert selbst wie Reddit
    src = Windowed("w", native=366)
    eng = engine(tmp_path, src, kinds=("web",))

    async def go():
        async with eng:
            a = await eng.search("x", days=2)
            b = await eng.search("x", days=7)
        return a, b
    a, b = asyncio.run(go())
    assert src.seen_days == [7]                                  # Quelle bekommt das ganze Fenster
    assert [r["title"] for r in a["results"]] == ["eins"]
    assert [r["title"] for r in b["results"]] == ["eins", "fuenf"] and b["sources"]["w"]["state"] == "cached"


def test_media_with_days_keeps_undated_hits_from_the_window(tmp_path):
    class Windowed(Dated):
        time_windows = (1, 7, 31, 366)
    res = search(engine(tmp_path, Windowed("m", native=366, rows=[("bild", None)])), "x", kind="images", days=3)
    assert [r["title"] for r in res["results"]] == ["bild"]


def test_sources_not_awaited_because_others_had_enough_count_as_early(tmp_path):
    eng = engine(tmp_path, Slow("slow", 5), Dated("fast", rows=[(f"r{i}", None) for i in range(12)]), kinds=("web",))

    async def go():
        async with eng:
            await eng.search("x", n=5, deadline=20)
            return render(eng)
    assert 'websearch_source_requests_total{source="slow",state="early"} 1' in asyncio.run(go())


def test_paid_sources_with_coarser_windows_are_not_paid_for_nothing(tmp_path):
    """Serper/Tavily filtern nur in Fenstern und liefern undatierte Treffer: bei days=3 würde der Nachfilter
    alles verwerfen - also gar nicht erst bezahlen. Bei days=7 (passt genau) schon."""
    from websearch.core import BlockedError

    class Broken(Dated):
        async def search(self, *a, **k):
            raise BlockedError("captcha")

    class WindowPaid(Dated):
        paid, time_windows = True, (1, 7, 31, 366)
    pol = TierPolicy(primary={"web": ["free"]}, paid={"web": ["serper"]})

    def run(days):
        serper = WindowPaid("serper", native=366, rows=[("undatiert", None)])
        eng = SearchEngine({"free": Broken("free"), "serper": serper}, policy=pol, cache_path=tmp_path / f"c{days}.db")

        async def go():
            async with eng:
                return await eng.search("x", days=days)
        return asyncio.run(go()), serper
    res, serper = run(3)
    assert "serper" not in res["sources"] and serper.seen_days == []
    res, serper = run(7)
    assert [r["title"] for r in res["results"]] == ["undatiert"] and serper.seen_days == [7]


def test_paid_tier_continues_when_a_paid_answer_was_filtered_to_nothing(tmp_path):
    from websearch.core import BlockedError

    class Broken(Dated):
        async def search(self, *a, **k):
            raise BlockedError("captcha")

    class Paid(Dated):
        paid, time_windows = True, ()
    old_only = Paid("p1", native=3650, rows=[("alt", iso(30))])        # filtert angeblich, liefert aber Altes
    good = Paid("p2", native=3650, rows=[("neu", iso(1))])
    pol = TierPolicy(primary={"web": ["free"]}, paid={"web": ["p1", "p2"]})
    eng = SearchEngine({"free": Broken("free"), "p1": old_only, "p2": good}, policy=pol, cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("x", days=7)
    res = asyncio.run(go())
    assert [r["title"] for r in res["results"]] == ["neu"]


def test_media_beyond_the_largest_window_keeps_undated_hits(tmp_path):
    class Windowed(Dated):
        time_windows = (1, 7, 31, 366)
    res = search(engine(tmp_path, Windowed("m", native=366, rows=[("bild", None)])), "x", kind="images", days=400)
    assert [r["title"] for r in res["results"]] == ["bild"]


def test_paid_sources_in_custom_tiers_respect_paid_false(tmp_path):
    class PaidPrimary(Dated):
        paid = True
    src = PaidPrimary("p")
    eng = SearchEngine({"p": src}, policy=TierPolicy(primary={"web": ["p"]}), cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("x", paid=False)
    res = asyncio.run(go())
    assert src.seen_days == [] and res["sources"]["p"]["state"] == "not_configured"


def test_paused_side_source_does_not_trigger_paid_tier(tmp_path):
    """SearXNG antwortet normal mit wenigen Treffern (seltene Suche), eine Nebenquelle ist gesperrt -> nicht zahlen."""
    from websearch.core import BlockedError

    class Blocked(Dated):
        async def search(self, *a, **k):
            raise BlockedError("captcha")

    class Paid(Dated):
        paid = True
    primary, side, paid = Dated("sx", rows=[("einer", None)]), Blocked("side"), Paid("p")
    pol = TierPolicy(primary={"web": ["sx"]}, fallback={"web": ["side"]}, paid={"web": ["p"]})
    eng = SearchEngine({s.name: s for s in (primary, side, paid)}, policy=pol, cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("seltene marke")
    res = asyncio.run(go())
    assert res["sources"]["side"]["state"] == "blocked" and paid.seen_days == []


def test_hanging_fallback_leaves_time_for_the_paid_tier(tmp_path):
    from websearch.core import BlockedError

    class Blocked(Dated):
        async def search(self, *a, **k):
            raise BlockedError("captcha")

    class Paid(Dated):
        paid = True
    pol = TierPolicy(primary={"web": ["sx"]}, fallback={"web": ["slow"]}, paid={"web": ["p"]})
    paid = Paid("p")
    eng = SearchEngine({"sx": Blocked("sx"), "slow": Slow("slow", 30), "p": paid}, policy=pol,
                       cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("x", deadline=6)
    res = asyncio.run(go())
    assert paid.seen_days == [None] and res["results"]


def test_paid_only_setup_still_searches(tmp_path):
    class Paid(Dated):
        paid = True
    paid = Paid("exa")
    eng = SearchEngine({"exa": paid}, policy=TierPolicy(primary={"web": []}, paid={"web": ["exa"]}),
                       cache_path=tmp_path / "c.db")

    async def go():
        async with eng:
            return await eng.search("x")
    res = asyncio.run(go())
    assert res["results"] and paid.seen_days == [None]
