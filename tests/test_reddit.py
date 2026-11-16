"""Offline-Tests der Reddit-Quelle (OAuth app-only) gegen nachgebaute API-Antworten."""
import asyncio
import base64
import time

import httpx
import pytest

from websearch.core import BlockedError, SourceUnavailable
from websearch.reddit import Reddit, reddit_window as time_window

NOW = time.time()


@pytest.fixture(autouse=True)
def _no_relevance_filter(request, monkeypatch):
    """Die Mapping-Tests nutzen Platzhalter-Titel; den Relevanzfilter prüft ein eigener Test (Marker)."""
    if request.node.get_closest_marker("relevance") is None:
        monkeypatch.setattr("websearch.reddit.relevant", lambda *a, **k: True)


def post(**kw):
    d = {"title": "Wärmepumpe Erfahrungen", "subreddit": "de", "score": 42, "num_comments": 7,
         "created_utc": NOW - 3600, "permalink": "/r/de/comments/abc/waermepumpe/", "is_self": True,
         "selftext": "", "url": "https://www.reddit.com/r/de/comments/abc/waermepumpe/", "over_18": False}
    d.update(kw)
    return {"kind": "t3", "data": d}


def listing(*children):
    return {"kind": "Listing", "data": {"children": list(children), "after": None}}


class RedditAPI:
    """Nachgebaute Reddit-API: Token-Endpunkt + /search. Zählt Requests und vergibt Tokens tok1, tok2 ..."""

    def __init__(self, results=None, search_status=200, headers=None, expires_in=86400, token_status=200,
                 reject_tokens=()):
        self.results = results if results is not None else listing(post())
        self.search_status, self.headers, self.expires_in = search_status, headers or {}, expires_in
        self.token_status, self.reject_tokens = token_status, set(reject_tokens)
        self.tokens = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["user-agent"].startswith("websearch/") or "REDDIT" in req.headers["user-agent"]
        if req.url.host == "www.reddit.com" and req.url.path == "/api/v1/access_token":
            assert req.method == "POST" and req.content == b"grant_type=client_credentials"
            assert req.headers["authorization"] == "Basic " + base64.b64encode(b"cid:geheim").decode()
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"message": "Unauthorized", "error": 401})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "token_type": "bearer",
                                             "expires_in": self.expires_in, "scope": "*"})
        if req.url.host == "oauth.reddit.com" and req.url.path == "/search":
            token = req.headers.get("authorization", "").removeprefix("bearer ")
            if token in self.reject_tokens:
                return httpx.Response(401, json={"message": "Unauthorized", "error": 401})
            return httpx.Response(self.search_status, json=self.results,
                                  headers={"x-ratelimit-remaining": "99.0", "x-ratelimit-used": "1",
                                           "x-ratelimit-reset": "420", **self.headers})
        return httpx.Response(404)

    def searches(self):
        return [r for r in self.requests if r.url.host == "oauth.reddit.com"]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "geheim")
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)
    monkeypatch.setenv("WEBSEARCH_LANG", "de")


def run(provider, api, query="wärmepumpe", n=10, kind="web", days=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as c:
            return await provider.search(c, query, n, "de", kind, days=days)
    return asyncio.run(go())


def test_available_only_with_id_and_secret(monkeypatch):
    p = Reddit()
    assert p.available and p.max_native_days == 366 and p.sparse and not p.paid and not p.scraped
    assert p.kinds == ("web", "news")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET")
    assert not p.available
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "  ")
    assert not p.available


def test_token_flow_and_search_params():
    api, p = RedditAPI(), Reddit()
    res = run(p, api, query="wärmepumpe test", n=5)
    search = api.searches()[0]
    q = dict(search.url.params)
    assert q == {"q": "wärmepumpe test", "sort": "relevance", "t": "all", "limit": "5", "type": "link",
                 "raw_json": "1"}
    assert search.headers["authorization"] == "bearer tok1"
    assert len(res) == 1
    # Token wird wiederverwendet
    run(p, api)
    assert api.tokens == 1 and len(api.searches()) == 2


def test_token_refreshed_shortly_before_expiry():
    api, p = RedditAPI(expires_in=100), Reddit()
    run(p, api)
    assert p._token_until < time.time() + 100            # Puffer vor dem Ablauf
    p._token_until = time.time() - 1                       # "abgelaufen"
    run(p, api)
    assert api.tokens == 2 and api.searches()[-1].headers["authorization"] == "bearer tok2"


def test_refresh_on_401_exactly_once():
    api, p = RedditAPI(reject_tokens={"tok1"}), Reddit()
    assert len(run(p, api)) == 1
    assert api.tokens == 2 and [r.headers["authorization"] for r in api.searches()] == ["bearer tok1", "bearer tok2"]
    api2, p2 = RedditAPI(reject_tokens={"tok1", "tok2"}), Reddit()
    with pytest.raises(BlockedError, match="401"):
        run(p2, api2)
    assert api2.tokens == 2 and len(api2.searches()) == 2   # kein Hämmern


def test_reused_token_hitting_the_403_block_page_is_refreshed_once():
    class Api(RedditAPI):
        def __call__(self, req):
            if req.url.host == "oauth.reddit.com" and req.headers["authorization"] == "bearer tok1" and self.tokens == 2:
                self.requests.append(req)
                return httpx.Response(403, text="<html><title>Blocked</title></html>",
                                      headers={"content-type": "text/html"})
            return super().__call__(req)
    api, p = Api(), Reddit()
    run(p, api)                                   # tok1 frisch geholt, funktioniert
    api.tokens = 2                                # ab jetzt wird tok1 abgelehnt (403-Sperrseite)
    assert len(run(p, api)) == 1
    assert api.searches()[-1].headers["authorization"] == "bearer tok3"


def test_bad_credentials_are_a_block():
    api = RedditAPI(token_status=401)
    with pytest.raises(BlockedError, match="REDDIT_CLIENT_ID"):
        run(Reddit(), api)
    assert api.searches() == []


def test_mapping_link_self_and_media_posts():
    api = RedditAPI(results=listing(
        post(title="Link", is_self=False, url="https://example.org/artikel?x=1", score=10, num_comments=3,
             subreddit="energy", created_utc=1793957245),
        post(title="Text", selftext="Hat jemand   Erfahrung\nmit Luft-Wasser-Wärmepumpen? " + "mehr " * 200),
        post(title="Bild", is_self=False, url="https://i.redd.it/abc.jpg", permalink="/r/pics/comments/x/bild/"),
        post(title="Crosspost", is_self=False, url_overridden_by_dest="https://news.example/a",
             url="https://www.reddit.com/r/x/comments/y/"),
    ))
    link, text, image, cross = run(Reddit(), api)
    assert link.url == "https://example.org/artikel?x=1" and link.source == "reddit" and link.rank == 1
    assert link.snippet == "r/energy · 10 points · 3 comments"
    assert link.published == "2026-11-06T09:27:25+00:00"
    assert text.url == "https://www.reddit.com/r/de/comments/abc/waermepumpe/"
    assert text.snippet.startswith("Hat jemand Erfahrung mit Luft-Wasser-Wärmepumpen?") and len(text.snippet) <= 302
    assert image.url == "https://www.reddit.com/r/pics/comments/x/bild/"         # reddit-eigene Medien -> Permalink
    assert cross.url == "https://news.example/a" and [r.rank for r in (link, text, image, cross)] == [1, 2, 3, 4]


def test_nsfw_and_broken_entries_are_skipped():
    api = RedditAPI(results=listing(post(title="NSFW", over_18=True), {"kind": "t3", "data": None}, "kaputt",
                                    post(title="ok", permalink=None, is_self=True), post(title="gut")))
    res = run(Reddit(), api)
    assert [r.title for r in res] == ["gut"] and res[0].rank == 1


def test_n_limits_results():
    api = RedditAPI(results=listing(*[post(title=f"P{i}") for i in range(10)]))
    assert [r.title for r in run(Reddit(), api, n=3)] == ["P0", "P1", "P2"]


@pytest.mark.parametrize("days,kind,expected", [
    (None, "web", "all"), (None, "news", "all"), (0, "web", "day"), (1, "web", "day"), (2, "web", "week"),
    (7, "news", "week"), (8, "web", "month"), (31, "web", "month"), (32, "web", "year"), (365, "web", "year"),
    (400, "web", "all"),
])
def test_days_to_time_window(days, kind, expected):
    assert time_window(days, kind) == expected


def test_days_and_news_kind_in_request_and_exact_cutoff():
    api = RedditAPI(results=listing(post(title="neu", created_utc=NOW - 86400),
                                    post(title="alt", created_utc=NOW - 5 * 86400)))
    res = run(Reddit(), api, kind="news", days=3)
    q = dict(api.searches()[0].url.params)
    assert q["sort"] == "new" and q["t"] == "week"
    assert [r.title for r in res] == ["neu"]                # Fenster "week" ist gröber als 3 Tage


def test_rate_limit_headers_pause_until_reset():
    api, p = RedditAPI(headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "120"}), Reddit()
    assert len(run(p, api)) == 1                 # diese Antwort ist gültig
    with pytest.raises(BlockedError, match=r"Reset in 1[12]\ds"):
        run(p, api)
    assert len(api.searches()) == 1              # bis zum Reset wird nicht gefragt
    p._exhausted_until = time.time() - 1
    assert len(run(p, api)) == 1


@pytest.mark.parametrize("status,exc,msg", [
    (429, BlockedError, "429"), (403, BlockedError, "403"), (500, SourceUnavailable, "500"),
    (503, SourceUnavailable, "503"), (400, SourceUnavailable, "400"),
])
def test_http_errors(status, exc, msg):
    with pytest.raises(exc, match=msg):
        run(Reddit(), RedditAPI(search_status=status))


def test_timeouts_are_unavailable():
    def slow(req):
        if req.url.host == "www.reddit.com":
            return RedditAPI()(req)
        raise httpx.ReadTimeout("zu langsam", request=req)
    with pytest.raises(SourceUnavailable, match="Zeitüberschreitung"):
        run(Reddit(), slow)


def test_english_messages(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with pytest.raises(BlockedError, match="token rejected"):
        run(Reddit(), RedditAPI(token_status=401))


def test_custom_user_agent(monkeypatch):
    monkeypatch.setenv("REDDIT_USER_AGENT", "linux:websearch-REDDIT:0.2 (by /u/someone)")
    api = RedditAPI()
    run(Reddit(), api)
    assert all(r.headers["user-agent"].startswith("linux:websearch-REDDIT") for r in api.requests)


@pytest.mark.relevance
def test_off_topic_posts_are_filtered():
    """Reddit findet ein Suchwort irgendwo im Beitrag - Themenfremdes wird wie bei HN verworfen."""
    api = RedditAPI(results=listing(post(title="Understanding ownership in Rust"),
                                    post(title="My car has rust problems"),
                                    post(title="Frage", selftext="How does Rust ownership work with closures?")))
    res = run(Reddit(), api, query="rust ownership")
    assert [r.title for r in res] == ["Understanding ownership in Rust", "Frage"]
