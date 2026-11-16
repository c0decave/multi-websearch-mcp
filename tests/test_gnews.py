"""Offline-Tests für das Auflösen von Google-News-Links (websearch/gnews.py) und die Einbindung in fetch_url.

Antworten von Google sind im Format echter Antworten nachgebaut (aufgezeichnet 2026-11, gekürzt).
"""
import asyncio
import base64
import json
import socket
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from websearch import fetch as F
from websearch import gnews as G
from websearch.core import StateStore

PUBLIC = "93.184.215.14"

# Altes Format (bis 2024 in Google-News-Feeds): Protobuf {1: 19, 4: "<URL>", 26: "<AMP-URL>"}, base64url
OLD_ID = ("CBMiTWh0dHBzOi8vd3d3LnRhZ2Vzc2NoYXUuZGUvd2lydHNjaGFmdC9lbmVyZ2llL2VuZXJnaWV3ZW5kZS1uZXR6YXVzYmF1LTEwMC"
          "5odG1s0gFRaHR0cHM6Ly93d3cudGFnZXNzY2hhdS5kZS93aXJ0c2NoYWZ0L2VuZXJnaWUvZW5lcmdpZXdlbmRlLW5ldHphdXNiYXUtMT"
          "AwfmFtcC5odG1s")
OLD_TARGET = "https://www.tagesschau.de/wirtschaft/energie/energiewende-netzausbau-100.html"
# Neues Format (echte ID aus einem Feed vom 2026-11-06): Protobuf {1: 19, 4: "AU_yqL..."}
NEW_ID = ("CBMi1gFBVV95cUxPem0tQ3VyTWJPOGs3WWtkYjc0WHN2UmdSNkIwd0V0Szg3NWt1TExsWWdxQWNLYktoLWw3dFIwR3RuVTZCN3N0SktDaGl"
          "iT014MWdpNjVFYlV5U1BzLWR2SFViOHFEVDNsMjZkQTFzWEVUcDVuMjZILXR0dVRkSUZ0amJOazI0SFBWamhNbWM3LUlBWVBxVHVpbHhLaF"
          "JlTUZEZkhHaGpzMG82RVpKNzlQYzFLUjY1OXpiT1hQbmsyM1F2T2tSV1o0SU1PNnBmdjY2bkRfYVpn")
NEW_TARGET = ("https://www.pv-magazine.de/2026/11/05/studie-deutschlands-kann-durch-energiewende-jaehrlich-75-"
              "milliarden-euro-an-importausgaben-einsparen/")
SIG, TS = "AdwtSYPGrsguqJnRknFn8cagsKZM", "1793957245"


def gnews_url(art_id: str) -> str:
    return f"https://news.google.com/rss/articles/{art_id}?oc=5"


def old_id(url: str) -> str:
    """ID im alten Format für eine beliebige Ziel-URL bauen."""
    raw = b'\x08\x13"' + bytes([len(url)]) + url.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def article_page(art_id=NEW_ID, sig=SIG, ts=TS) -> str:
    return ('<!doctype html><html lang="en-US"><head><base href="https://news.google.com/"></head><body>'
            '<c-wiz jsrenderer="lW1Lhc" data-n-zris="1" jsshadow jsdata="deferred-i1">'
            + "<script>/* viel JavaScript */</script>" * 50 +
            f'<div jscontroller="aLI87" jsaction="rcuQ6b:npT2md" data-n-a-id="{art_id}" data-n-a-ts="{ts}" '
            f'data-n-a-sg="{sig}" data-n-dnlg="false"></div></c-wiz></body></html>')


def batch_response(url=NEW_TARGET) -> str:
    payload = json.dumps(["garturlres", url, 1])
    rows = [["wrb.fr", "Fbv4je", payload, None, None, None, "generic"], ["di", 38],
            ["af.httprm", 38, "6849536506283790556", 17]]
    return ")]}'\n\n" + json.dumps(rows)


class Google:
    """Nachgebautes news.google.com: Artikelseite + batchexecute. Zählt alle Requests."""

    def __init__(self, page=None, batch=None, page_status=200, batch_status=200, page_headers=None):
        self.requests: list[httpx.Request] = []
        self.page, self.batch = page or article_page(), batch or batch_response()
        self.page_status, self.batch_status, self.page_headers = page_status, batch_status, page_headers or {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host != "news.google.com":
            raise AssertionError(f"unerwarteter Request an {req.url}")
        if req.method == "GET" and req.url.path.startswith("/rss/articles/"):
            return httpx.Response(self.page_status, text=self.page,
                                  headers={"content-type": "text/html; charset=utf-8", **self.page_headers})
        if req.method == "POST" and req.url.path == "/_/DotsSplashUi/data/batchexecute":
            return httpx.Response(self.batch_status, text=self.batch,
                                  headers={"content-type": "application/json; charset=utf-8"})
        return httpx.Response(404)


def decode(store, handler, url):
    async def go():
        dec = G.GNewsDecoder(store, interval=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
            return await dec.decode(c, url)
    return asyncio.run(go())


@pytest.fixture
def store(tmp_path):
    st = StateStore(tmp_path / "c.db")
    yield st
    st.close()


def no_network(req):
    raise AssertionError(f"kein Request erwartet: {req.url}")


# --------------------------------------------------------------------------- Format

def test_article_id_variants():
    assert G.article_id(gnews_url("CBMiABC")) == "CBMiABC"
    assert G.article_id("https://news.google.com/articles/CBMiABC?hl=de") == "CBMiABC"
    assert G.article_id("https://news.google.com/read/CBMiABC") == "CBMiABC"
    assert G.article_id("https://news.google.com/search?q=x") is None
    assert G.article_id("https://news.google.com.evil.example/rss/articles/CBMiABC") is None


def test_old_style_is_decoded_offline(store):
    assert decode(store, no_network, gnews_url(OLD_ID)) == OLD_TARGET
    assert G.decode_offline(OLD_ID) == OLD_TARGET


def test_new_style_needs_google_and_garbage_is_refused_offline(store):
    assert G.decode_offline(NEW_ID) is None
    for junk in ("CBMi123", "Zm9vYmFy", "AAAA"):
        with pytest.raises(F.FetchError, match="unbekanntes Format"):
            decode(store, no_network, gnews_url(junk))


def test_parse_signature_and_batch_formats():
    assert G.parse_signature(article_page()) == (SIG, TS)
    reordered = f'<c-wiz><div data-n-a-sg="S1" jscontroller="x" data-n-a-ts="42"></div></c-wiz>'
    assert G.parse_signature(reordered) == ("S1", "42")
    assert G.parse_signature("<html>nichts</html>") is None
    assert G.parse_batch(batch_response()) == NEW_TARGET
    rows = json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://a.example/x", 1]), None, None, None,
                        "generic"]])
    chunked = f")]}}'\n\n{len(rows)}\n{rows}\n25\n[[\"di\",12]]\n"
    assert G.parse_batch(chunked) == "https://a.example/x"
    assert G.parse_batch(")]}'\n\n[[\"er\",null,null,null,null,400]]") is None


# --------------------------------------------------------------------------- online-Verfahren

def test_new_style_via_batchexecute_and_cache(store):
    g = Google()
    assert decode(store, g, gnews_url(NEW_ID) + "&hl=de&gl=DE&ceid=DE:de") == NEW_TARGET
    get, post = g.requests
    assert get.url.path == f"/rss/articles/{NEW_ID}" and parse_qs(get.url.query.decode())["hl"] == ["de"]
    assert "SOCS=CAI" in get.headers["cookie"]                 # Consent-Banner: "Alle ablehnen"
    form = unquote(post.content.decode())
    assert form.startswith("f.req=") and "Fbv4je" in form and "garturlreq" in form
    inner = json.loads(json.loads(form[len("f.req="):])[0][0][1])
    assert inner[0] == "garturlreq" and inner[2:] == [NEW_ID, int(TS), SIG]
    # zweiter Aufruf (auch mit neuer Instanz = anderer Prozess): aus dem StateStore, kein Request
    assert decode(store, g, gnews_url(NEW_ID)) == NEW_TARGET
    assert len(g.requests) == 2


def test_requests_use_the_shared_google_news_slot(store):
    async def go():
        dec = G.GNewsDecoder(store)                  # Standardtempo
        async with httpx.AsyncClient(transport=httpx.MockTransport(Google())) as c:
            return await dec.decode(c, gnews_url(NEW_ID))
    import time
    t0 = time.monotonic()
    assert asyncio.run(go()) == NEW_TARGET
    assert time.monotonic() - t0 >= G.DECODE_INTERVAL * 0.9                 # Abstand zwischen GET und POST
    assert store.peek_slot("google_news") > 0      # gleicher hostweiter Slot wie die Suchquelle google_news


@pytest.mark.parametrize("google", [
    Google(page_status=302, page_headers={"location": "https://consent.google.com/m?continue=x&gl=DE"}),
    Google(page_status=302, page_headers={"location": "https://www.google.com/sorry/index?continue=x"}),
    Google(page_status=429),
    Google(page="<html><body>Our systems have detected unusual traffic from your computer network</body></html>"),
    Google(batch_status=429),
], ids=["consent", "sorry", "429-page", "captcha", "429-batch"])
def test_blocks_pause_without_retries(store, google):
    with pytest.raises(F.FetchError, match="sperrt"):
        decode(store, google, gnews_url(NEW_ID))
    n = len(google.requests)
    assert n <= 2
    # Pause gilt hostweit: nächster Versuch (neue Instanz) fragt Google gar nicht erst
    with pytest.raises(F.FetchError, match="gesperrt - Pause noch"):
        decode(store, google, gnews_url(NEW_ID))
    assert len(google.requests) == n
    # offline auflösbare Links funktionieren trotz Pause
    assert decode(store, no_network, gnews_url(OLD_ID)) == OLD_TARGET


def test_blocked_message_in_english(store, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with pytest.raises(F.FetchError, match="Google News is currently blocking"):
        decode(store, Google(page_status=429), gnews_url(NEW_ID))


@pytest.mark.parametrize("google,msg", [
    (lambda: Google(page_status=503), "HTTP 503"),
    (lambda: Google(page_status=404), "HTTP 404"),
    (lambda: Google(page="<html><c-wiz><div jscontroller='x'></div></c-wiz></html>"), "Signatur"),
    (lambda: Google(batch=")]}'\n\n[[\"er\",null,null,null,null,400]]"), "keine Ziel-URL"),
    (lambda: Google(batch=batch_response("https://news.google.com/x")), "unbrauchbare"),
    (lambda: Google(batch=batch_response("javascript:alert(1)")), "unbrauchbare"),
    (lambda: Google(batch=batch_response("http://[::1")), "unbrauchbare"),     # urlsplit-ValueError
])
def test_server_errors_and_format_changes_are_not_blocks(store, google, msg):
    with pytest.raises(F.FetchError, match=msg):
        decode(store, google(), gnews_url(NEW_ID))
    assert not G.GNewsDecoder(store).breaker.is_open()          # keine Pause wegen solcher Fehler


def test_failures_are_cached_for_an_hour(store, monkeypatch):
    """Kaputte/unbekannte IDs kosten nicht bei jedem Versuch zwei Requests an Google."""
    google = Google(batch=")]}'\n\n[[\"er\",null,null,null,null,400]]")
    with pytest.raises(F.FetchError, match="keine Ziel-URL"):
        decode(store, google, gnews_url(NEW_ID))
    assert len(google.requests) == 2
    with pytest.raises(F.FetchError, match="keine Ziel-URL.*früheren Versuch.*60 min"):
        decode(store, google, gnews_url(NEW_ID))
    assert len(google.requests) == 2                            # aus dem Fehler-Cache, kein Request
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with pytest.raises(F.FetchError, match="no target URL.*earlier attempt"):
        decode(store, google, gnews_url(NEW_ID))
    monkeypatch.setattr(G, "FAIL_TTL", 0)                       # abgelaufen -> neuer Versuch
    with pytest.raises(F.FetchError):
        decode(store, google, gnews_url(NEW_ID))
    assert len(google.requests) == 4


def test_cached_http_failure_and_success_after_expiry(store, monkeypatch):
    with pytest.raises(F.FetchError, match="HTTP 404"):
        decode(store, Google(page_status=404), gnews_url(NEW_ID))
    with pytest.raises(F.FetchError, match="HTTP 404.*früheren Versuch"):
        decode(store, no_network, gnews_url(NEW_ID))
    monkeypatch.setattr(G, "FAIL_TTL", 0)
    assert decode(store, Google(), gnews_url(NEW_ID)) == NEW_TARGET


def test_server_errors_and_blocks_are_not_cached_as_failures(store):
    g = Google(page_status=503)
    for _ in range(2):
        with pytest.raises(F.FetchError, match="HTTP 503"):
            decode(store, g, gnews_url(NEW_ID))
    assert len(g.requests) == 2                                 # 5xx: jedes Mal neu versucht
    with pytest.raises(F.FetchError, match="sperrt"):
        decode(store, Google(page_status=429), gnews_url(NEW_ID))
    # Sperren regelt der CircuitBreaker - kein gemerkter Fehlschlag, der die Pause überdauern würde
    assert G.GNewsDecoder(store)._fail_get(G.article_id(gnews_url(NEW_ID))) is None


def test_batch_body_only_converts_ascii_digit_timestamps():
    for ts in ("²", "١٢٣", "1" * 5000):                         # isdigit() wäre True, int() würde scheitern
        body = json.loads(json.loads(unquote(G.batch_body(NEW_ID, ts, SIG)[len("f.req="):]))[0][0][1])
        assert body[3] == ts
    body = json.loads(json.loads(unquote(G.batch_body(NEW_ID, TS, SIG)[len("f.req="):]))[0][0][1])
    assert body[3] == int(TS)


# --------------------------------------------------------------------------- Einbindung in fetch_url

@pytest.fixture
def fake_dns(monkeypatch):
    """news.google.com ohne Netz auf eine öffentliche Adresse auflösen (für die SSRF-Prüfung)."""
    real = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def getaddrinfo(self, host, port, *a, **k):
        if host == "news.google.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.185.78", port))]
        return await real(self, host, port, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)


HTML = ("<html><head><title>Artikel</title></head><body><article><h1>Schlagzeile</h1><p>"
        + "Inhalt des Artikels. " * 60 + "</p></article></body></html>")


def publisher_and_google(google=None, seen=None):
    google = google or Google()

    def h(req):
        if seen is not None:
            seen.append(str(req.url))
        if req.url.host == "news.google.com":
            return google(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/artikel":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=HTML)
        return httpx.Response(404)
    return h


def fetch(tmp_path, handler, url):
    async def go():
        f = F.Fetcher(StateStore(tmp_path / "f.db"), per_host_interval=0)
        f._gnews = G.GNewsDecoder(f.store, interval=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await f.fetch(c, url)
    return asyncio.run(go())


def test_fetch_resolves_old_style_link_offline(tmp_path, fake_dns):
    seen: list[str] = []
    url = gnews_url(old_id(f"https://{PUBLIC}/artikel"))
    page = fetch(tmp_path, publisher_and_google(seen=seen), url)
    assert page["url"] == url and page["final_url"] == f"https://{PUBLIC}/artikel"
    assert "Inhalt des Artikels" in page["content"]
    assert not any("news.google.com" in u for u in seen)


def test_fetch_resolves_new_style_link(tmp_path, fake_dns):
    google = Google(batch=batch_response(f"https://{PUBLIC}/artikel"))
    page = fetch(tmp_path, publisher_and_google(google), gnews_url(NEW_ID))
    assert page["final_url"] == f"https://{PUBLIC}/artikel" and page["title"]
    again = fetch(tmp_path, publisher_and_google(google), gnews_url(NEW_ID))
    assert again["cached"] and len(google.requests) == 2


@pytest.mark.parametrize("target", ["http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data",
                                    "http://[::ffff:10.0.0.1]/"])
def test_ssrf_check_applies_to_decoded_target(tmp_path, fake_dns, target):
    seen: list[str] = []
    with pytest.raises(F.FetchForbidden, match="internen Netz"):
        fetch(tmp_path, publisher_and_google(seen=seen), gnews_url(old_id(target)))
    seen.clear()
    google = Google(batch=batch_response(target))
    with pytest.raises(F.FetchForbidden, match="internen Netz"):
        fetch(tmp_path / "neu", publisher_and_google(google, seen=seen), gnews_url(NEW_ID))
    assert all("news.google.com" in u for u in seen)          # das interne Ziel wurde nie angefragt


def test_disable_switch_restores_refusal(tmp_path, fake_dns, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_GNEWS_DECODE", "0")
    seen: list[str] = []
    with pytest.raises(F.FetchError, match="Google-News-Links sind Weiterleitungen"):
        fetch(tmp_path, publisher_and_google(seen=seen), gnews_url(OLD_ID))
    assert seen == []
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with pytest.raises(F.FetchError, match="Google News links are redirects"):
        fetch(tmp_path, publisher_and_google(), gnews_url(OLD_ID))


def test_blocked_decode_is_a_fetch_error(tmp_path, fake_dns):
    google = Google(page_status=302, page_headers={"location": "https://consent.google.com/m?x=1"})
    with pytest.raises(F.FetchError, match="sperrt"):
        fetch(tmp_path, publisher_and_google(google), gnews_url(NEW_ID))
    assert len(google.requests) == 1
