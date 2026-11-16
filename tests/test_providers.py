"""Offline-Tests aller Quellen gegen nachgebaute Server-Antworten (httpx.MockTransport).

Prueft Parsing, Sperr-Erkennung (Captcha/429/403/202) und dass harmlose Treffer, die zufaellig
das Wort "captcha" enthalten, KEINE Sperre ausloesen.
"""
import asyncio
import json
import os

import httpx
import pytest
from xml.sax.saxutils import escape


from websearch import providers as P
from websearch.core import BlockedError, SourceSkipped, SourceUnavailable


def run(provider, handler, query="test", n=10, lang="de", kind="web"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
            return await provider.search(c, query, n, lang, kind)
    return asyncio.run(go())


def rss(items, title="feed"):
    body = "".join(
        f"<item><title>{escape(t)}</title><link>{escape(u)}</link><description>{escape(d)}</description>"
        f"<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate></item>" for t, u, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{body}</channel></rss>'


# --------------------------------------------------------------------------- SearXNG

def test_searxng_parses_and_reports_failed_engines(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://sx.local")
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={
            "results": [{"title": "A", "url": "https://a.com", "content": "about captcha solving",
                         "engines": ["bing", "brave"]},
                        {"title": "no url"}],
            "unresponsive_engines": [["google", "CAPTCHA"]]})
    p = P.SearXNG()
    res = run(p, h, kind="news", query="captcha solving")
    assert [r.url for r in res] == ["https://a.com"]          # Eintrag ohne URL wird ignoriert
    assert res[0].engines == ["searxng:bing", "searxng:brave"]
    assert seen["format"] == "json" and seen["categories"] == "news" and seen["language"] == "de"
    assert res.meta["engines_failed"] == [["google", "CAPTCHA"]]   # pro Antwort, kein geteilter Zustand


def test_searxng_unreachable(monkeypatch):
    def h(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(SourceUnavailable):
        run(P.SearXNG("http://sx.local"), h)


# --------------------------------------------------------------------------- Sperr-Erkennung

@pytest.mark.parametrize("status,body,ctype", [
    (429, "slow down", "text/plain"),
    (403, "forbidden", "text/html"),
    (200, "<html>Our systems have detected unusual traffic ... /sorry/index</html>", "text/html"),
    (200, "<html>JavaScript is required to complete this challenge</html>", "text/html"),
])
def test_block_detection_html(status, body, ctype):
    def h(req):
        return httpx.Response(status, text=body, headers={"content-type": ctype})
    with pytest.raises(BlockedError):
        run(P.BingRSS(), h)


def test_http_202_is_not_a_block_for_normal_sources():
    # DDGs 202-Anomalie-Seite wird nur fuer die ddgs-Quelle ausgewertet (siehe ddgs-Tests unten)
    resp = httpx.Response(202, text="accepted", request=httpx.Request("GET", "https://api.example.com/"))
    P._check_http(resp, "x")


def test_word_captcha_in_json_or_rss_is_not_a_block():
    def h(req):
        if "wikipedia" in req.url.host:
            return httpx.Response(200, json={"query": {"search": [
                {"title": "CAPTCHA", "snippet": "A <b>CAPTCHA</b> is a test; unusual traffic"}]}})
        return httpx.Response(200, text=rss([("Captcha-Solver im Test", "https://x.de/a", "captcha")]),
                              headers={"content-type": "application/rss+xml"})
    assert run(P.Wikipedia(), h, query="captcha")[0].title == "CAPTCHA"
    assert run(P.BingRSS(), h, query="captcha")[0].url == "https://x.de/a"


def test_server_error_is_unavailable_not_block():
    with pytest.raises(SourceUnavailable):
        run(P.HackerNews(), lambda r: httpx.Response(502, text="bad gateway"))


# --------------------------------------------------------------------------- Feeds / APIs

def test_bing_rss_unwraps_news_redirect_and_dates():
    link = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&url=https%3a%2f%2fexample.com%2fx&c=1"

    def h(req):
        assert req.url.params["format"] == "rss"
        assert req.url.path == "/news/search"
        return httpx.Response(200, text=rss([("T", link, "<b>d</b>")]), headers={"content-type": "text/xml"})
    r = run(P.BingRSS(), h, kind="news")[0]
    assert r.url == "https://example.com/x"
    assert r.published == "Tue, 03 Nov 2026 10:00:00 GMT"    # normalisiert wird zentral in der Engine
    assert r.snippet == "d"


def test_google_news_params_de():
    def h(req):
        assert req.url.params["hl"] == "de" and req.url.params["ceid"] == "DE:de"
        return httpx.Response(200, text=rss([("A - Quelle", "https://news.google.com/rss/articles/x", "")]))
    assert run(P.GoogleNewsRSS(), h, kind="news")[0].title == "A - Quelle"


def test_google_news_params_en():
    def h(req):
        assert req.url.params["hl"] == "en-US" and req.url.params["ceid"] == "US:en"
        return httpx.Response(200, text=rss([]))
    assert run(P.GoogleNewsRSS(), h, lang="en", kind="news") == []


def test_invalid_rss_is_unavailable():
    with pytest.raises(SourceUnavailable):
        run(P.BingRSS(), lambda r: httpx.Response(200, text="<html>kaputt", headers={"content-type": "text/xml"}))


def test_wikipedia_language_and_url(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_UA", "test-ua (t@example.com)")
    def h(req):
        assert req.url.host == "de.wikipedia.org"
        assert req.headers["user-agent"] == "test-ua (t@example.com)"
        return httpx.Response(200, json={"query": {"search": [{"title": "Künstliche Intelligenz", "snippet": "x"}]}})
    r = run(P.Wikipedia(), h, lang="de", query="künstliche intelligenz")[0]
    assert r.url == "https://de.wikipedia.org/wiki/K%C3%BCnstliche_Intelligenz"


def test_hackernews_fallback_url_and_filter():
    def h(req):
        assert req.url.path == "/api/v1/search" and "created_at_i>" in req.url.params["numericFilters"]
        return httpx.Response(200, json={"hits": [
            {"objectID": "42", "title": "Ask HN", "url": None, "points": 3, "num_comments": 1,
             "created_at": "2026-11-01T00:00:00Z"}]})
    r = run(P.HackerNews(), h, kind="news", query="ask hn")[0]
    assert r.url == "https://news.ycombinator.com/item?id=42"


def test_gdelt_parse_and_rate_limit():
    def ok(req):
        return httpx.Response(200, json={"articles": [
            {"title": "T", "url": "https://n.de/a", "domain": "n.de", "seendate": "20261103T120000Z"}]})
    r = run(P.GDELT(), lambda q: httpx.Response(200, json={"articles": [
        {"title": "Testing news", "url": "https://n.de/a", "domain": "n.de", "seendate": "20261103T120000Z"}]}),
        kind="news", query="testing news")[0]
    assert r.url == "https://n.de/a" and r.published == "20261103T120000Z"

    def limited(req):
        return httpx.Response(200, text="Please limit requests to one every 5 seconds")
    with pytest.raises(BlockedError):
        run(P.GDELT(), limited, kind="news")

    def text_error(req):
        return httpx.Response(200, text="The specified phrase is too short.")
    with pytest.raises(SourceUnavailable):
        run(P.GDELT(), text_error, kind="news")


def test_marginalia_rate_limits(monkeypatch):
    monkeypatch.setenv("MARGINALIA_KEY", "k")

    def h(req):
        assert req.headers["API-Key"] == "k"
        return httpx.Response(200, json={"results": [{"url": "https://m.org", "title": "M", "description": "d"}]})
    assert run(P.Marginalia(), h)[0].url == "https://m.org"
    for code in (503, 429):
        with pytest.raises(BlockedError):
            run(P.Marginalia(), lambda r, c=code: httpx.Response(c, text="QPM Limit Exceeded"))


def test_brave_api(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert not P.BraveAPI().available
    monkeypatch.setenv("BRAVE_API_KEY", "b")

    def h(req):
        assert req.headers["X-Subscription-Token"] == "b"
        if "/news/" in req.url.path:
            return httpx.Response(200, json={"results": [{"title": "N", "url": "https://n.com"}]})
        return httpx.Response(200, json={"web": {"results": [{"title": "W", "url": "https://w.com",
                                                              "description": "<strong>x</strong>"}]}})
    assert run(P.BraveAPI(), h)[0].snippet == "x"
    assert run(P.BraveAPI(), h, kind="news")[0].url == "https://n.com"


def test_wikipedia_rejects_host_injection():
    def h(req):
        assert req.url.host == "en.wikipedia.org"
        return httpx.Response(200, json={"query": {"search": []}})
    assert run(P.Wikipedia(), h, lang="evil.com/x#") == []


def test_paid_quota_and_bad_key(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "s")
    with pytest.raises(BlockedError):
        run(P.SerperAPI(), lambda r: httpx.Response(402, json={"message": "Not enough credits"}))
    # falscher Key = Sperre (lange Pause), damit die naechste Bezahlquelle drankommt
    with pytest.raises(BlockedError, match="401"):
        run(P.SerperAPI(), lambda r: httpx.Response(401, json={"message": "Unauthorized"}))
    assert P.SerperAPI.base_cooldown == 3600


def test_serper(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "s")

    def h(req):
        assert req.headers["X-API-KEY"] == "s" and req.method == "POST"
        body = json.loads(req.content)
        assert body["q"] == "test" and body["hl"] == "de" and body["gl"] == "de" and body["num"] == 10
        if req.url.path == "/news":
            return httpx.Response(200, json={"news": [{"title": "N", "link": "https://n.de", "date": "vor 1 Std."}]})
        return httpx.Response(200, json={"organic": [{"title": "G", "link": "https://g.de", "snippet": "s"},
                                                     {"title": "kein link"}]})
    assert [r.url for r in run(P.SerperAPI(), h)] == ["https://g.de"]
    assert run(P.SerperAPI(), h, kind="news")[0].published == "vor 1 Std."   # Engine macht daraus None


def test_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")

    def h(req):
        assert req.headers["Authorization"] == "Bearer t"
        body = json.loads(req.content)
        assert body["topic"] == "news" and body["search_depth"] == "basic"
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.com", "content": "c",
                                                      "published_date": "Tue, 03 Nov 2026 10:00:00 GMT"}]})
    assert run(P.TavilyAPI(), h, kind="news")[0].url == "https://t.com"
    with pytest.raises(BlockedError):
        run(P.TavilyAPI(), lambda r: httpx.Response(432, json={"detail": "plan limit"}))


def test_ddgs_disabled_backend_is_reported_not_auto():
    with pytest.raises(SourceUnavailable, match="deaktiviert"):
        run(P.DDGS("google"), lambda r: httpx.Response(500))


def test_disable_env(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_DISABLE", "bing_rss, gdelt")
    names = set(P.default_providers())
    assert "bing_rss" not in names and "gdelt" not in names and "searxng" in names


def test_ddgs_detects_block_hidden_behind_no_results(monkeypatch):
    """ddgs gibt bei HTTP 403/429/Captcha nur 'No results found' zurueck - wir erkennen die Sperre trotzdem."""
    import ddgs
    from ddgs.exceptions import DDGSException

    class FakeResp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    class FakeHttp:
        def __init__(self, resp):
            self.resp = resp

        def request(self, *a, **k):
            return self.resp

    def fake_ddgs(resp):
        class FakeDDGS:
            def __init__(self, **kw):
                self.eng = type("E", (), {})()
                self.eng.http_client = FakeHttp(resp)

            def _get_engines(self, cat, backend):
                return [self.eng]

            def text(self, *a, **k):
                self.eng.http_client.request("GET", "https://x")
                raise DDGSException("No results found.")
            news = text
        return FakeDDGS

    monkeypatch.setattr(ddgs, "DDGS", fake_ddgs(FakeResp(429, "slow down")))
    with pytest.raises(BlockedError, match="HTTP 429"):
        run(P.DDGS("brave"), lambda r: httpx.Response(500))
    monkeypatch.setattr(ddgs, "DDGS", fake_ddgs(FakeResp(200, "<html>please solve this captcha</html>")))
    with pytest.raises(BlockedError, match="Captcha"):
        run(P.DDGS("yahoo"), lambda r: httpx.Response(500))
    monkeypatch.setattr(ddgs, "DDGS", fake_ddgs(FakeResp(200, "<html>keine Treffer</html>")))
    assert run(P.DDGS("yahoo"), lambda r: httpx.Response(500)) == []


def test_ddgs_slot_held_until_thread_finishes(monkeypatch):
    """Nach einer Deadline darf kein zweiter Request parallel zur selben Engine starten."""
    import threading
    import time as _t
    import ddgs
    active, peak, lock = 0, 0, threading.Lock()

    class SlowDDGS:
        def __init__(self, **kw):
            pass

        def _get_engines(self, cat, backend):
            return []

        def text(self, *a, **k):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            _t.sleep(0.3)
            with lock:
                active -= 1
            return [{"title": "t", "href": "https://x.com", "body": ""}]

    monkeypatch.setattr(ddgs, "DDGS", SlowDDGS)
    prov = P.DDGS("yahoo")

    async def go():
        async with httpx.AsyncClient() as c:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(prov.search(c, "q", 5, "de", "web"), 0.05)   # Deadline
            return await prov.search(c, "q2", 5, "de", "web")                       # wartet auf Thread 1
    assert len(asyncio.run(go())) == 1 and peak == 1


def test_serper_out_of_credits_400(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "s")
    with pytest.raises(BlockedError):
        run(P.SerperAPI(), lambda r: httpx.Response(400, json={"message": "Not enough credits", "statusCode": 400}))


def test_gdelt_title_with_phrase_is_not_a_block():
    def h(req):
        return httpx.Response(200, json={"articles": [
            {"title": "Please limit requests to the API, says vendor", "url": "https://n.de/a", "seendate": ""}]})
    assert run(P.GDELT(), h, kind="news", query="limit requests vendor")[0].url == "https://n.de/a"


def test_gdelt_drops_short_words():
    assert P.GDELT.build_query("EU AI Act") == "Act"
    assert P.GDELT.build_query('"EU AI Act" Regulierung') == "Act Regulierung"
    assert P.GDELT.build_query('"AI Act Umsetzung" sourcecountry:GM') == '"Act Umsetzung" sourcecountry:GM'
    with pytest.raises(SourceSkipped, match="kürzer als 3 Zeichen"):
        P.GDELT().precheck("KI EU", "news")


def test_google_news_marks_redirect_urls():
    def h(req):
        return httpx.Response(200, text=rss([("A - Q", "https://news.google.com/rss/articles/x", "")]))
    assert run(P.GoogleNewsRSS(), h, kind="news")[0].redirect is True


def test_exa(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e")

    def h(req):
        assert req.headers["x-api-key"] == "e" and req.url.path == "/search"
        body = json.loads(req.content)
        assert body["numResults"] == 10 and body["category"] == "news" and body["type"] == "fast"
        return httpx.Response(200, json={"results": [
            {"title": "E", "url": "https://e.com", "publishedDate": "2026-11-01T00:00:00.000Z", "highlights": ["h1"]},
            {"title": "ohne url"}]})
    res = run(P.ExaAPI(), h, kind="news")
    assert [r.url for r in res] == ["https://e.com"] and res[0].snippet == "h1"


def test_perplexity(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "p")

    def h(req):
        assert req.headers["Authorization"] == "Bearer p"
        assert json.loads(req.content) == {"query": "test", "max_results": 10, "search_type": "fast"}
        return httpx.Response(200, json={"results": [{"title": "P", "url": "https://p.com", "snippet": "s",
                                                      "date": "2026-10-30"}]})
    r = run(P.PerplexityAPI(), h)[0]
    assert r.url == "https://p.com" and r.published == "2026-10-30"


def test_ddgs_5xx_is_unavailable_not_empty(monkeypatch):
    import ddgs
    from ddgs.exceptions import DDGSException

    class FakeResp:
        status_code, text = 500, "server error"

    class FakeDDGS:
        def __init__(self, **kw):
            self.eng = type("E", (), {})()
            self.eng.http_client = type("H", (), {"request": lambda self, *a, **k: FakeResp()})()

        def _get_engines(self, cat, backend):
            return [self.eng]

        def text(self, *a, **k):
            self.eng.http_client.request("GET", "x")
            raise DDGSException("No results found.")
    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    with pytest.raises(SourceUnavailable, match="HTTP 500"):
        run(P.DDGS("yahoo"), lambda r: httpx.Response(500))


def test_all_default_providers_have_unique_names_and_policy_is_consistent():
    from websearch.engine import DEFAULT_POLICY
    provs = P.default_providers()
    assert len(provs) == 19 and DEFAULT_POLICY.names() <= set(provs)
    for name, p in provs.items():
        assert p.name == name and p.kinds and p.min_interval >= 0


def test_searxng_all_engines_failed_is_not_an_empty_ok(monkeypatch):
    def h(req):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"],
                                                                                 ["bing", "timeout"]]})
    with pytest.raises(SourceUnavailable, match="google .CAPTCHA., bing .timeout."):
        run(P.SearXNG("http://sx.local"), h)
    ok = lambda req: httpx.Response(200, json={"results": [], "unresponsive_engines": []})   # noqa: E731
    assert run(P.SearXNG("http://sx.local"), ok) == []          # legitim leer bleibt leer


def test_bing_rss_drops_off_topic_web_results():
    items = [("OpenAI launches model", "https://openai.com/x", "news"),
             ("SearXNG: a free metasearch engine", "https://docs.searxng.org", "open source metasearch")]
    h = lambda req: httpx.Response(200, text=rss(items))   # noqa: E731
    assert [r.url for r in run(P.BingRSS(), h, query="open source metasearch engine")] == ["https://docs.searxng.org"]
    assert len(run(P.BingRSS(), h, query="open source metasearch engine", kind="news")) == 2   # News: kein Filter


def test_relevance_helper():
    assert P.relevant("Wärmepumpe Förderung", "BAFA Förderung für Wärmepumpen und Wärmepumpe")
    assert not P.relevant("open source metasearch", "OpenAI and Openbank")
    assert P.relevant("KI", "irgendwas")                      # nur kurze Woerter -> kein Filter
    assert P.relevant("site:example.org python asyncio", "asyncio in Python")   # Operatoren ignoriert


def test_linkup(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "l")

    def h(req):
        assert req.headers["Authorization"] == "Bearer l" and req.url.host == "api.linkup.so"
        assert json.loads(req.content)["outputType"] == "searchResults"
        return httpx.Response(200, json={"results": [{"type": "text", "name": "L", "url": "https://l.com",
                                                      "content": "c"}]})
    assert run(P.LinkupAPI(), h)[0].title == "L"


def test_searxng_cache_scope_depends_on_url():
    assert P.SearXNG("http://a:8888").cache_scope != P.SearXNG("http://b:8888").cache_scope


def test_gdelt_skips_queries_that_would_lose_most_words():
    for q in ("EU AI Act", "AI Act"):
        with pytest.raises(SourceSkipped, match="kürzer als 3 Zeichen"):
            P.GDELT().precheck(q, "news")
    P.GDELT().precheck("AI Act Umsetzung", "news")            # 2 von 3 Wörtern bleiben -> wird gefragt


def test_gdelt_filters_off_topic_titles():
    def h(req):
        return httpx.Response(200, json={"articles": [
            {"title": "Champions League result", "url": "https://x.de/1"},
            {"title": "Artificial Intelligence regulation update", "url": "https://x.de/2"}]})
    res = run(P.GDELT(), h, query="artificial intelligence regulation", kind="news")
    assert [r.url for r in res] == ["https://x.de/2"]


def test_wikipedia_drops_irrelevant_hits():
    def h(req):
        return httpx.Response(200, json={"query": {"search": [
            {"title": "Identity fusion", "snippet": "psychology"},
            {"title": "Rank fusion", "snippet": "reciprocal rank fusion combines rankings"}]}})
    assert [r.title for r in run(P.Wikipedia(), h, query="Reciprocal Rank Fusion")] == ["Rank fusion"]


def test_searxng_filters_bing_only_off_topic_but_keeps_consensus():
    def h(req):
        return httpx.Response(200, json={"results": [
            {"title": "OpenAI news", "url": "https://openai.com", "content": "", "engines": ["bing"]},
            {"title": "OpenAI on two engines", "url": "https://two.com", "content": "", "engines": ["bing", "brave"]},
            {"title": "SearXNG metasearch engine", "url": "https://docs.searxng.org", "engines": ["bing"]}]})
    res = run(P.SearXNG("http://sx.local"), h, query="open source metasearch engine")
    assert [r.url for r in res] == ["https://two.com", "https://docs.searxng.org"]


def test_searxng_all_failed_keeps_failed_engines_in_meta():
    def h(req):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]})
    with pytest.raises(SourceUnavailable) as ei:
        run(P.SearXNG("http://sx.local"), h)
    assert ei.value.meta == {"engines_failed": [["duckduckgo", "CAPTCHA"]]}


def test_bing_rss_reports_filtered_count():
    items = [("OpenAI launches model", "https://openai.com/x", "news")]
    res = run(P.BingRSS(), lambda r: httpx.Response(200, text=rss(items)), query="open source metasearch engine")
    assert list(res) == [] and res.meta == {"filtered": 1}


def test_gdelt_requires_dropped_short_words_in_title():
    def h(req):
        assert req.url.params["query"].startswith("Act Umsetzung")
        return httpx.Response(200, json={"articles": [
            {"title": "Clean Water Act Umsetzung im Senat", "url": "https://x.de/1"},
            {"title": "AI Act: Umsetzung in Deutschland", "url": "https://x.de/2"}]})
    assert [r.url for r in run(P.GDELT(), h, query="AI Act Umsetzung", kind="news")] == ["https://x.de/2"]


def test_hackernews_drops_off_topic_hits():
    def h(req):
        assert req.url.params["typoTolerance"] == "false"
        return httpx.Response(200, json={"hits": [
            {"objectID": "1", "title": "Show HN: Ferry file manager", "url": "https://f.com"},
            {"objectID": "2", "title": "SearXNG 2026.11 released", "url": "https://github.com/searxng/searxng"}]})
    assert [r.url for r in run(P.HackerNews(), h, kind="news", query="SearXNG")] == ["https://github.com/searxng/searxng"]


def test_html_entities_are_unescaped_and_google_news_publisher_shown():
    body = ('<?xml version="1.0"?><rss><channel><item><title>T</title><link>https://news.google.com/a</link>'
            '<description>&lt;a href="x"&gt;Text&lt;/a&gt;&amp;nbsp;&amp;nbsp;mehr</description>'
            '<source url="https://www.tagesschau.de">tagesschau.de</source></item></channel></rss>')
    r = run(P.GoogleNewsRSS(), lambda req: httpx.Response(200, text=body), kind="news")[0]
    assert r.snippet == "tagesschau.de (https://www.tagesschau.de) - Text mehr"


def test_google_news_snippet_does_not_repeat_the_headline():
    body = ('<?xml version="1.0"?><rss><channel><item><title>Große Schlagzeile - tagesschau.de</title>'
            '<link>https://news.google.com/a</link><description>&lt;a&gt;Große Schlagzeile&lt;/a&gt; tagesschau.de'
            '</description><source url="https://www.tagesschau.de">tagesschau.de</source></item></channel></rss>')
    r = run(P.GoogleNewsRSS(), lambda req: httpx.Response(200, text=body), kind="news")[0]
    assert r.snippet == "tagesschau.de (https://www.tagesschau.de)"


def test_wikipedia_strict_relevance_for_long_queries():
    def h(req):
        return httpx.Response(200, json={"query": {"search": [
            {"title": "Identity fusion", "snippet": "fusion of rank and identity"},
            {"title": "Reciprocal rank fusion", "snippet": "method"}]}})
    assert [r.title for r in run(P.Wikipedia(), h, query="Reciprocal Rank Fusion")] == ["Reciprocal rank fusion"]


@pytest.mark.parametrize("query,text,ok", [
    ("heat pump", "Heat pumps explained", True),
    ("Wärmepumpe Förderung", "Neue Wärmepumpenförderung 2026", True),
    ("Ärzte Streik", "ärzte streiken heute", True),
    ("open source metasearch engine", "OpenAI and Openbank", False),
])
def test_relevance_tolerates_plurals_and_compounds(query, text, ok):
    assert P.relevant(query, text) is ok



def test_relevance_plural_in_query_singular_in_text():
    assert P.relevant("heat pumps prices", "heat pump price rises")
    assert P.relevant("Streiks Bahn", "Streik bei der Bahn")



@pytest.mark.parametrize("query,text", [("apple", "pineapple juice recipe"), ("open source metasearch", "OpenAI news today")])
def test_relevance_avoids_obvious_false_positives(query, text):
    assert not P.relevant(query, text)


def test_searxng_some_engines_alive_means_rare_query_not_outage():
    def h(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"engines": [
                {"name": "google", "enabled": True, "categories": ["general"]},
                {"name": "bing", "enabled": True, "categories": ["general"]}]})
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]})
    res = run(P.SearXNG("http://sx.local"), h, query="sehr seltene marke")
    assert list(res) == [] and res.meta == {"engines_failed": [["google", "CAPTCHA"]]}

    def all_down(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"engines": [{"name": "google", "enabled": True, "categories": ["general"]}]})
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]})
    with pytest.raises(SourceUnavailable):
        run(P.SearXNG("http://sx.local"), all_down)


def test_searxng_bad_engines_field_keeps_the_row():
    def h(req):
        return httpx.Response(200, json={"results": [{"title": "A", "url": "https://a.com", "engines": 5}]})
    assert [r.url for r in run(P.SearXNG("http://sx.local"), h)] == ["https://a.com"]


def test_searxng_url_needs_scheme(monkeypatch):
    from websearch.core import InputError
    monkeypatch.setenv("SEARXNG_URL", "localhost:8888")
    with pytest.raises(InputError, match="http"):
        P.SearXNG()
    monkeypatch.setenv("SEARXNG_URL", "")
    assert P.SearXNG().base_url == "http://127.0.0.1:8888"


def test_searxng_web_outage_detected_although_wikipedia_never_fails():
    """general enthält Wikipedia/Wikidata - ein Totalausfall der Web-Indizes muss trotzdem auffallen."""
    def h(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"engines": [
                {"name": n, "enabled": True, "categories": ["general", "web"]} for n in ("google", "bing", "brave")]
                + [{"name": "wikipedia", "enabled": True, "categories": ["general"]}]})
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [
            ["google", "CAPTCHA"], ["bing", "CAPTCHA"], ["brave", "too many requests"]]})
    with pytest.raises(SourceUnavailable):
        run(P.SearXNG("http://sx.local"), h, query="seltene marke")


@pytest.mark.parametrize("query,text", [("Bahn Streik", "Großer Bahnstreik ab Montag"), ("Gas Preis", "Gaspreise steigen"),
                                        ("Rente Erhöhung", "Die Rentenerhöhung kommt"), ("Haus Kauf", "Tipps zum Hauskauf")])
def test_german_compounds_from_adjacent_terms(query, text):
    assert P.relevant(query, text)


def test_sparse_sources_are_marked():
    assert P.HackerNews.sparse and P.GDELT.sparse and not P.GoogleNewsRSS.sparse
