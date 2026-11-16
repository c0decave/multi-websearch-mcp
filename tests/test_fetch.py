"""Offline-Tests fuer fetch_url: SSRF-Schutz, robots.txt, Weiterleitungen, Groessenlimit, Inhaltstypen."""
import asyncio

import httpx
import pytest

from websearch import fetch as F
from websearch.core import StateStore

PUBLIC = "93.184.215.14"     # globale IP-Literale -> keine DNS-Aufloesung im Test noetig
HTML = ("<html><head><title>Testseite</title></head><body><nav>Menue</nav><article><h1>Ueberschrift</h1>"
        "<p>Ein Absatz mit <a href='/x'>Link</a>. " + "Inhalt " * 200 + "</p></article></body></html>")


@pytest.fixture(autouse=True)
def _german(monkeypatch):
    """Die Tests pruefen deutsche Meldungstexte (t(de, en))."""
    monkeypatch.setenv("WEBSEARCH_LANG", "de")


def fetch(tmp_path, handler, url=f"https://{PUBLIC}/seite", **kw):
    async def go():
        f = F.Fetcher(StateStore(tmp_path / "c.db"), per_host_interval=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await f.fetch(c, url, **kw)
    return asyncio.run(go())


def site(robots="", pages=None):
    pages = pages or {"/seite": (200, {"content-type": "text/html; charset=utf-8"}, HTML)}

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots is not None else httpx.Response(404)
        code, headers, body = pages.get(req.url.path, (404, {}, "nicht da"))
        return httpx.Response(code, headers=headers, content=body.encode() if isinstance(body, str) else body)
    return h


def test_html_to_markdown(tmp_path):
    page = fetch(tmp_path, site())
    assert page["title"] in ("Testseite", "Ueberschrift") and page["final_url"].endswith("/seite")
    assert "# Ueberschrift" in page["content"] and f"[Link](https://{PUBLIC}/x)" in page["content"]
    assert "Menue" not in page["content"] and not page["truncated"] and not page["cached"]


def test_truncation_and_cache(tmp_path):
    h = site()
    assert fetch(tmp_path, h, max_chars=500)["truncated"]
    again = fetch(tmp_path, lambda r: httpx.Response(500), max_chars=100_000)    # Server weg -> Cache
    assert again["cached"] and not again["truncated"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://10.0.0.5/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
])
def test_ssrf_internal_targets_refused(tmp_path, url):
    with pytest.raises(F.FetchError, match="internen Netz"):
        fetch(tmp_path, site(), url=url)


@pytest.mark.parametrize("url,msg", [("ftp://example.org/x", "http"), ("file:///etc/passwd", "http"),
                                     ("https://user:pw@example.org/", "Zugangsdaten"), ("https:///x", "Host")])
def test_bad_urls_refused(tmp_path, url, msg):
    with pytest.raises(F.FetchError, match=msg):
        fetch(tmp_path, site(), url=url)


def test_redirect_to_internal_target_refused(tmp_path):
    h = site(pages={"/seite": (302, {"location": "http://127.0.0.1:8080/admin"}, "")})
    with pytest.raises(F.FetchError, match="internen Netz"):
        fetch(tmp_path, h)


def test_redirect_loop_and_relative_redirect(tmp_path):
    loop = site(pages={"/seite": (302, {"location": "/seite"}, "")})
    with pytest.raises(F.FetchError, match="Weiterleitungen"):
        fetch(tmp_path, loop)
    rel = site(pages={"/seite": (301, {"location": "/neu"}, ""),
                      "/neu": (200, {"content-type": "text/plain"}, "Hallo")})
    page = fetch(tmp_path, rel)
    assert page["final_url"] == f"https://{PUBLIC}/neu" and page["content"] == "Hallo"


def test_robots_txt_respected(tmp_path, monkeypatch):
    h = site(robots="User-agent: *\nDisallow: /seite\n")
    with pytest.raises(F.FetchError, match="robots.txt"):
        fetch(tmp_path, h)
    monkeypatch.setenv("WEBSEARCH_FETCH_IGNORE_ROBOTS", "1")
    assert fetch(tmp_path / "b", h)["title"] in ("Testseite", "Ueberschrift")


def test_missing_robots_means_allowed(tmp_path):
    assert fetch(tmp_path, site(robots=None))["title"] in ("Testseite", "Ueberschrift")


def test_size_limit_and_types(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "MAX_BYTES", 1000)
    big = site(pages={"/seite": (200, {"content-type": "text/plain"}, "x" * 5000)})
    with pytest.raises(F.FetchError, match="größer"):
        fetch(tmp_path, big)
    pdf = site(pages={"/seite": (200, {"content-type": "application/pdf"}, b"%PDF")})
    with pytest.raises(F.FetchError, match="PDF"):
        fetch(tmp_path / "p", pdf)
    img = site(pages={"/seite": (200, {"content-type": "image/png"}, b"\x89PNG")})
    with pytest.raises(F.FetchError, match="image/png"):
        fetch(tmp_path / "i", img)
    err = site(pages={"/seite": (404, {}, "weg")})
    with pytest.raises(F.FetchError, match="HTTP 404"):
        fetch(tmp_path / "e", err)


def test_allow_private_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_FETCH_ALLOW_PRIVATE", "1")
    assert fetch(tmp_path, site(), url="http://192.168.1.10/seite")["title"] in ("Testseite", "Ueberschrift")


def test_robots_txt_redirect_to_internal_is_refused(tmp_path):
    """SSRF ueber die robots.txt: deren Weiterleitung wird genauso geprueft wie jede andere."""
    requested = []

    def h(req):
        requested.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)
    with pytest.raises(F.FetchError, match="internen Netz"):
        fetch(tmp_path, h)
    assert not any("169.254" in u for u in requested)


def test_robots_checked_for_new_host_after_redirect(tmp_path):
    other = "93.184.215.15"

    def h(req):
        if req.url.host == PUBLIC:
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": f"https://{other}/ziel"})
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="geheim")
    with pytest.raises(F.FetchError, match=f"robots.txt von {other}"):
        fetch(tmp_path, h)


def test_meta_charset_is_respected(tmp_path):
    body = '<html><head><meta charset="iso-8859-1"><title>Gr\xfc\xdfe</title></head><body><article><p>' \
           + "Gr\xfc\xdfe aus M\xfcnchen. " * 40 + "</p></article></body></html>"
    h = site(pages={"/seite": (200, {"content-type": "text/html"}, body.encode("latin-1"))})
    assert "Grüße aus München" in fetch(tmp_path, h)["content"]


def test_google_news_links_get_a_helpful_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_GNEWS_DECODE", "0")       # Auflösen abgeschaltet -> alte Ablehnung

    async def resolve(host, port):                          # offline: keine echte DNS-Abfrage
        return [PUBLIC]
    monkeypatch.setattr(F, "_resolve", resolve)
    with pytest.raises(F.FetchError, match="Google-News"):
        fetch(tmp_path, site(), url="https://news.google.com/rss/articles/CBMi123")


def test_total_timeout_against_drip_server(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "TOTAL_TIMEOUT", 0.5)

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"x"

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=drip())
    import time as _t
    t = _t.monotonic()
    with pytest.raises(F.FetchError, match="länger als"):
        fetch(tmp_path, h)
    assert _t.monotonic() - t < 2


@pytest.mark.parametrize("url", ["http://[64:ff9b::a00:1]/", "http://[2002:a00:1::]/"])
def test_ipv6_transition_addresses_to_internal_targets_refused(tmp_path, url):
    with pytest.raises(F.FetchForbidden):
        fetch(tmp_path, site(), url=url)


def test_invalid_port_and_site_local_are_refused(tmp_path):
    with pytest.raises(F.FetchForbidden, match="Port"):
        fetch(tmp_path, site(), url="http://example.com:99999/")
    with pytest.raises(F.FetchForbidden):
        fetch(tmp_path, site(), url="http://[fec0::1]/")


def test_fetch_works_while_state_store_is_degraded(tmp_path, monkeypatch):
    import sqlite3

    async def go():
        st = StateStore(tmp_path / "c.db")
        f = F.Fetcher(st, per_host_interval=0)

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(st, "get", locked)
        monkeypatch.setattr(st, "set", locked)
        monkeypatch.setattr(st, "claim_slot", locked)
        async with httpx.AsyncClient(transport=httpx.MockTransport(site())) as c:
            return await f.fetch(c, f"https://{PUBLIC}/seite")
    assert asyncio.run(go())["title"]


def test_anchor_links_point_to_the_page(tmp_path):
    body = (HTML.replace("</article>", "<p>Beleg<a href='#cite_note-1'>[1]</a> " + "mehr Text " * 30
                         + "</p></article>"))
    h = site(pages={"/seite": (200, {"content-type": "text/html"}, body)})
    content = fetch(tmp_path, h)["content"]
    assert f"https://{PUBLIC}/seite#cite_note-1" in content and f"https://{PUBLIC}#cite" not in content


@pytest.mark.parametrize("url", ["http://[::1", "http://[::1]x/"])
def test_malformed_bracket_hosts_are_readable_errors(tmp_path, url):
    with pytest.raises(F.FetchError):
        fetch(tmp_path, site(), url=url)


def test_news_google_lookalike_is_not_treated_as_google(tmp_path):
    with pytest.raises(F.FetchError) as ei:
        fetch(tmp_path, site(), url="http://news.google.com.evil.example/x")
    assert "Google-News" not in str(ei.value)


def test_too_large_page_is_a_fetch_error_not_a_crash(tmp_path, monkeypatch):
    from websearch.engine import _size_guard

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "application/zip", "content-length": str(20 * 1024 * 1024)},
                              content=b"x")

    async def go():
        f = F.Fetcher(StateStore(tmp_path / "c.db"), per_host_interval=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(h), event_hooks={"response": [_size_guard]}) as c:
            return await f.fetch(c, f"https://{PUBLIC}/big.zip")
    with pytest.raises(F.FetchError, match="zu groß"):
        asyncio.run(go())


def test_extraction_threads_are_limited_until_they_really_finish(tmp_path, monkeypatch):
    """Ein trafilatura-Thread läuft nach dem Timeout weiter und belegt seinen Platz, bis er fertig ist;
    ist kein Platz frei, kommt nach EXTRACT_WAIT eine klare Meldung statt eines weiteren Threads."""
    import threading
    monkeypatch.setattr(F, "_EXTRACT_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(F, "EXTRACT_WAIT", 0.2)
    monkeypatch.setattr(F, "TOTAL_TIMEOUT", 0.5)
    started, release = threading.Event(), threading.Event()
    real = F._to_markdown

    def slow(html, url):
        started.set()
        release.wait(10)
        return real(html, url)
    monkeypatch.setattr(F, "_to_markdown", slow)
    with pytest.raises(F.FetchError, match="länger als"):
        fetch(tmp_path / "a", site())
    assert started.is_set()                                  # Thread läuft noch
    with pytest.raises(F.FetchError, match="ausgelastet"):
        fetch(tmp_path / "b", site())
    release.set()
    assert F._EXTRACT_SLOTS.acquire(timeout=5)               # Platz wird frei, sobald der Thread endet
    F._EXTRACT_SLOTS.release()
    monkeypatch.setattr(F, "_to_markdown", real)
    assert fetch(tmp_path / "c", site())["title"] in ("Testseite", "Ueberschrift")


def test_extraction_errors_release_the_slot(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(F, "_EXTRACT_SLOTS", threading.BoundedSemaphore(1))

    def boom(html, url):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(F, "_to_markdown", boom)
    for i in range(3):
        with pytest.raises(RuntimeError, match="kaputt"):
            fetch(tmp_path / str(i), site())
