"""Offline-Tests des MCP-Servers: prozessintern ueber mcp.Client mit Fake-Quellen (kein Netz)."""
import asyncio
import json

import pytest
from mcp import Client

from websearch.core import BlockedError, Result
from websearch.engine import SearchEngine, TierPolicy
from websearch.mcp_server import MAX_QUERIES, build_server
from websearch.providers import Provider


class Fake(Provider):
    min_interval = 0
    kinds = ("web", "news", "ref")

    def __init__(self, name, n=3, block=False):
        self.name, self._n, self._block = name, n, block
        super().__init__()

    async def search(self, client, query, n, lang, kind):
        if self._block:
            raise BlockedError("captcha")
        return [Result(f"{query} {i}", f"https://{self.name}.example/{query}/{i}", snippet="s",
                       source=self.name, rank=i) for i in range(1, self._n + 1)]


@pytest.fixture
def server(tmp_path):
    def factory():
        provs = {"good": Fake("good"), "bad": Fake("bad", block=True)}
        return SearchEngine(provs, policy=TierPolicy(primary={k: ["good", "bad"] for k in ("web", "news", "ref")}),
                            cache_path=tmp_path / "c.db")
    return build_server(factory)


def call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go())


def test_tool_schemas(server):
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    assert set(tools) == {"web_search", "news_search", "search_many", "fetch_url", "source_status",
                          "image_search", "video_search"}
    assert all(t.output_schema for t in tools.values())
    props = tools["search_many"].input_schema["properties"]
    assert props["kind"]["enum"] == ["web", "news", "ref"]
    assert props["max_results"]["maximum"] == 30 and props["queries"]["maxItems"] == MAX_QUERIES


def test_web_search_compact_output(server):
    res = call(server, "web_search", {"query": "abc", "max_results": 2})
    assert not res.is_error
    out = res.structured_content
    assert [r["url"] for r in out["results"]] == ["https://good.example/abc/1", "https://good.example/abc/2"]
    assert out["results"][0]["found_by"] == ["good"]
    assert out["source_errors"]["bad"].startswith("blocked:")


def test_search_many_and_limits(server):
    res = call(server, "search_many", {"queries": ["a", "b"], "kind": "news"})
    assert [r["query"] for r in res.structured_content["result"]] == ["a", "b"]
    too_many = call(server, "search_many", {"queries": [str(i) for i in range(MAX_QUERIES + 1)]})
    assert too_many.is_error
    bad_kind = call(server, "search_many", {"queries": ["a"], "kind": "bilder"})
    assert bad_kind.is_error
    big = call(server, "web_search", {"query": "a", "max_results": 31})
    assert big.is_error


def test_errors_reach_the_model(server, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
    res = call(server, "web_search", {"query": "   "})
    assert res.is_error and "leere Suchanfrage" in res.content[0].text
    res = call(server, "fetch_url", {"url": "http://127.0.0.1/admin"})
    assert res.is_error and "internen Netz" in res.content[0].text


def test_source_status(server):
    call(server, "web_search", {"query": "x"})
    st = call(server, "source_status", {}).structured_content
    assert st["bad"]["blocked"] and not st["good"]["blocked"]


def test_redirect_links_are_marked(tmp_path):
    class G(Fake):
        async def search(self, client, query, n, lang, kind):
            return [Result("Schlagzeile - Quelle", "https://news.google.com/rss/articles/X", source=self.name,
                           rank=1, redirect=True)]

    def factory():
        return SearchEngine({"g": G("g")}, policy=TierPolicy(primary={k: ["g"] for k in ("web", "news", "ref")}),
                            cache_path=tmp_path / "c.db")
    res = call(build_server(factory), "news_search", {"query": "x"}).structured_content
    assert res["results"][0]["redirect"] is True


def test_redirect_field_is_documented_in_schema(server):
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    schema = asyncio.run(go())["news_search"].output_schema
    assert "fetch_url resolves it" in json.dumps(schema)


def test_paid_switch_accepts_off(monkeypatch):
    from websearch import mcp_server
    monkeypatch.setenv("WEBSEARCH_MCP_PAID", "off")
    assert mcp_server._paid() is False


# --- Bild-/Videosuche und days mit Fake-Engine (unabhängig von der Engine-Unterstützung) ----------

class FakeEngine:
    """Minimaler Ersatz für SearchEngine: zeichnet Aufrufe auf und akzeptiert jede kind/days."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or []
        self.closed = False

    async def __aenter__(self):
        return self

    async def aclose(self):
        self.closed = True

    def _resp(self, query, kind):
        return {"query": query, "kind": kind, "results": self.results,
                "sources": {"good": {"state": "ok", "message": "", "count": len(self.results)}}}

    async def search(self, query, **kw):
        self.calls.append(("search", query, kw))
        return self._resp(query, kw.get("kind"))

    async def search_many(self, queries, **kw):
        self.calls.append(("search_many", list(queries), kw))
        return [self._resp(q, kw.get("kind")) for q in queries]

    def status(self):
        return {"good": {"available": True, "kinds": ["web"], "min_interval_s": 0.0, "scraped": False,
                         "blocked": False}}


def _media(**extra):
    base = {"title": "T", "url": "https://page.example/1", "snippet": "", "source": "x", "rank": 1,
            "published": None, "engines": ["searxng", "bing", "ddg", "brave", "qwant"], "score": 1.0,
            "redirect": False, "image": "", "thumbnail": "", "duration": ""}
    return base | extra


@pytest.fixture
def fake():
    eng = FakeEngine()
    return eng, build_server(lambda: eng)


def test_image_search_output(fake):
    eng, srv = fake
    eng.results = [_media(image="https://img.example/a.jpg", thumbnail="https://img.example/a_t.jpg",
                          duration="1:00", snippet="ignored")]
    res = call(srv, "image_search", {"query": "katze", "max_results": 5, "language": "de"})
    assert not res.is_error
    hit = res.structured_content["results"][0]
    assert hit == {"title": "T", "url": "https://page.example/1", "image": "https://img.example/a.jpg",
                   "thumbnail": "https://img.example/a_t.jpg", "found_by": ["searxng", "bing", "ddg", "brave"]}
    assert eng.calls == [("search", "katze", {"n": 5, "kind": "images", "lang": "de", "paid": False})]


def test_video_search_output_drops_empty_fields(fake):
    eng, srv = fake
    eng.results = [_media(thumbnail="https://i.ytimg.com/x.jpg", duration="12:34",
                          published="2026-11-01T00:00:00+00:00", image="https://img.example/no.jpg"),
                   _media(url="https://page.example/2", title="")]
    out = call(srv, "video_search", {"query": "vortrag"}).structured_content
    assert out["results"][0] == {"title": "T", "url": "https://page.example/1",
                                 "thumbnail": "https://i.ytimg.com/x.jpg", "duration": "12:34",
                                 "published": "2026-11-01T00:00:00+00:00",
                                 "found_by": ["searxng", "bing", "ddg", "brave"]}
    assert out["results"][1] == {"title": "", "url": "https://page.example/2",
                                 "found_by": ["searxng", "bing", "ddg", "brave"]}
    assert eng.calls[0][2] == {"n": 10, "kind": "videos", "lang": "auto", "paid": False}
    assert out["source_errors"] == {}


def test_media_tool_schemas(fake):
    _, srv = fake

    async def go():
        async with Client(srv) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    img = json.dumps(tools["image_search"].output_schema)
    assert "image" in tools["image_search"].output_schema["$defs"]["ImageHit"]["properties"]
    assert "Direct URL of the image file" in img
    vid = tools["video_search"].output_schema["$defs"]["VideoHit"]["properties"]
    assert {"thumbnail", "duration", "published", "found_by"} <= set(vid)
    assert "days" in tools["image_search"].input_schema["properties"]          # Zeitfilter auch für Medien
    assert "days" in tools["video_search"].input_schema["properties"]


@pytest.mark.parametrize("tool,args", [("web_search", {"query": "a"}), ("news_search", {"query": "a"}),
                                       ("search_many", {"queries": ["a", "b"]})])
def test_days_passthrough(fake, tool, args):
    eng, srv = fake
    assert not call(srv, tool, args).is_error
    assert "days" not in eng.calls[-1][2]                       # Standard: keine Zeitgrenze
    assert not call(srv, tool, args | {"days": 7}).is_error
    assert eng.calls[-1][2]["days"] == 7
    assert call(srv, tool, args | {"days": 0}).is_error
    assert call(srv, tool, args | {"days": 3651}).is_error


def test_days_schema(fake):
    _, srv = fake

    async def go():
        async with Client(srv) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    for name in ("web_search", "news_search", "search_many"):
        days = tools[name].input_schema["properties"]["days"]
        assert days["description"] == "Only results from the last N days"
        assert days.get("default") is None and "days" not in tools[name].input_schema.get("required", [])
        assert {"minimum": 1, "maximum": 3650, "type": "integer"} in days["anyOf"]


def test_instructions_mention_new_tools(fake):
    _, srv = fake
    assert all(w in srv.instructions for w in ("image_search", "video_search", "days"))


# --- HTTP: Token, /healthz, /metrics, DNS-Rebinding ----------------------------------------------

from starlette.testclient import TestClient  # noqa: E402

from websearch import mcp_server  # noqa: E402

BASE = "http://127.0.0.1:8765"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def http_client(srv, token=None):
    app = mcp_server.build_http_app(srv, token=token, security=mcp_server.transport_security())
    return TestClient(app, base_url=BASE)


def test_http_without_token_stays_open(server, monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    with http_client(server) as c:
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200
        assert c.get("/healthz").text == "ok"
        assert c.get("/metrics").status_code == 200
        # DNS-Rebinding-Schutz bleibt aktiv - für /mcp (SDK) und /metrics (eigene Hülle)
        evil = {"Host": "evil.example"}
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS | evil).status_code == 421
        assert c.get("/metrics", headers=evil).status_code == 421
        assert c.get("/metrics", headers={"Origin": "https://evil.example"}).status_code == 403


def test_http_token_auth(server, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    with http_client(server, token="s3cret-token") as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="websearch"'
        assert "missing Authorization" in r.text
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": "Bearer wrong"})
        assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
        assert "invalid token" in r.text
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": "Basic s3cret-token"}
                      ).status_code == 401
        ok = MCP_HEADERS | {"Authorization": "Bearer s3cret-token"}
        assert c.post("/mcp", json=INIT, headers=ok).status_code == 200
        assert c.post("/mcp", json=INIT, headers=ok | {"Authorization": "bearer s3cret-token"}).status_code == 200
        # Token schützt nicht vor DNS-Rebinding-Prüfung (beides gilt)
        assert c.post("/mcp", json=INIT, headers=ok | {"Host": "evil.example"}).status_code == 421
        # /healthz offen, /metrics geschützt
        assert c.get("/healthz").status_code == 200
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
        m = c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
        assert m.status_code == 200
        assert m.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert "websearch_" in m.text and m.text.endswith("\n")
        assert c.get("/nope").status_code == 401                  # unbekannte Pfade verraten nichts


def test_http_token_messages_german(server, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
    with http_client(server, token="x") as c:
        assert "fehlt" in c.get("/metrics").text
        assert "ungültig" in c.get("/metrics", headers={"Authorization": "Bearer y"}).text


def test_metrics_calls_render_with_shared_engine(fake, monkeypatch):
    eng, srv = fake
    seen = []

    def render(e):
        seen.append(e)
        return "websearch_up 1\n"
    monkeypatch.setattr("websearch.metrics.render", render)
    with http_client(srv) as c:
        r = c.get("/metrics")
    assert r.text == "websearch_up 1\n" and seen == [eng]
    assert eng.closed                                    # Engine wird beim Herunterfahren geschlossen


def test_auth_token_env(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "  ")
    assert mcp_server.auth_token() is None
    monkeypatch.setenv("MCP_AUTH_TOKEN", " abc ")
    assert mcp_server.auth_token() == "abc"


@pytest.mark.parametrize("host,loop", [("127.0.0.1", True), ("::1", True), ("[::1]", True), ("localhost", True),
                                       ("127.0.0.2", True), ("0.0.0.0", False), ("::", False),
                                       ("192.168.1.5", False), ("myhost", False)])
def test_is_loopback(host, loop):
    assert mcp_server.is_loopback(host) is loop


def test_warning_on_exposed_host_without_token(monkeypatch, caplog):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with caplog.at_level("WARNING", logger="websearch"):
        assert mcp_server.warn_if_exposed("0.0.0.0", None) is True
    assert "0.0.0.0 without MCP_AUTH_TOKEN" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="websearch"):
        assert mcp_server.warn_if_exposed("0.0.0.0", "tok") is False
        assert mcp_server.warn_if_exposed("127.0.0.1", None) is False
    assert caplog.text == ""
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
    with caplog.at_level("WARNING", logger="websearch"):
        mcp_server.warn_if_exposed("10.0.0.1", None)
    assert "ohne MCP_AUTH_TOKEN" in caplog.text


def test_serve_http_wires_token_and_uvicorn(monkeypatch, caplog):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9999")
    token = "t" * 40
    monkeypatch.setenv("MCP_AUTH_TOKEN", token)
    with caplog.at_level("WARNING", logger="websearch"):
        mcp_server.serve(http=True)
    assert seen["host"] == "0.0.0.0" and seen["port"] == 9999
    assert isinstance(seen["app"], mcp_server.BearerAuth) and seen["app"]._token == token.encode()
    assert "MCP_AUTH_TOKEN" not in caplog.text
    # ohne Token und von außen erreichbar: Start verweigern (fail closed) ...
    monkeypatch.delenv("MCP_AUTH_TOKEN")
    seen.clear()
    from websearch.core import InputError
    with pytest.raises(InputError, match="MCP_AUTH_TOKEN"):
        mcp_server.serve(http=True)
    assert not seen
    # ... außer bewusst erlaubt (docker-compose: Port nur auf 127.0.0.1) - dann mit Warnung, nur im Container
    monkeypatch.setenv("MCP_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "0")
    with pytest.raises(InputError):
        mcp_server.serve(http=True)
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "1")
    with caplog.at_level("WARNING", logger="websearch"):
        mcp_server.serve(http=True)
    assert seen["app"]._token is None and "MCP_AUTH_TOKEN" in caplog.text


def test_exposure_rules(monkeypatch, caplog):
    from websearch.core import InputError
    check = mcp_server.check_exposure
    check("127.0.0.1", None, ["127.0.0.1:*", "localhost:*", "[::1]:*"])            # nur lokal: ok
    with pytest.raises(InputError, match="search.example.org"):                    # öffentlicher Hostname
        check("127.0.0.1", None, ["127.0.0.1:*", "search.example.org"])
    with pytest.raises(InputError, match="MCP_HOST=10.0.0.5"):
        check("10.0.0.5", None, [])
    check("0.0.0.0", "x" * 32, ["search.example.org:*"])                           # mit Token: ok
    with caplog.at_level("WARNING", logger="websearch"):
        check("127.0.0.1", "kurz", [])
    assert "32" in caplog.text                                                      # kurzes Token: Warnung
    with pytest.raises(InputError, match="ASCII"):
        check("127.0.0.1", "töken" * 10, [])
    monkeypatch.setenv("MCP_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "0")
    with pytest.raises(InputError):                                                # Opt-out nur im Container
        check("0.0.0.0", None, ["127.0.0.1:*"])
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "1")
    check("0.0.0.0", None, ["127.0.0.1:*"])                                        # Container-Fall: erlaubt
    with pytest.raises(InputError):                                                # öffentlich: nie ohne Token
        check("0.0.0.0", None, ["127.0.0.1:*", "search.example.org"])
    assert mcp_server._host_part("[::1]:8765") == "::1" and mcp_server._host_part("a.example:*") == "a.example"


def test_websocket_scopes_are_rejected():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])
    sent = []

    async def send(msg):
        sent.append(msg)
    auth = mcp_server.BearerAuth(app, None)
    asyncio.run(auth({"type": "websocket", "path": "/mcp", "headers": []}, None, send))
    asyncio.run(auth({"type": "lifespan"}, None, send))
    assert called == ["lifespan"] and sent == [{"type": "websocket.close", "code": 1008}]


def test_serve_rejects_bad_port(monkeypatch):
    from websearch.core import InputError
    monkeypatch.setenv("MCP_PORT", "70000")
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    with pytest.raises(InputError, match="must be a port between 1 and 65535"):
        mcp_server.serve(http=True)
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
    with pytest.raises(InputError, match="muss ein Port"):
        mcp_server.serve(http=True)


@pytest.mark.parametrize("container", ["0", "1"])
@pytest.mark.parametrize("host,hosts,token,allow", [
    ("127.0.0.1", "127.0.0.1:*,localhost:*", "", ""), ("0.0.0.0", "127.0.0.1:*", "", ""),
    ("0.0.0.0", "127.0.0.1:*", "", "1"), ("0.0.0.0", "127.0.0.1:*,search.example.org", "", "1"),
    ("127.0.0.1", "search.example.org:*", "", ""), ("0.0.0.0", "search.example.org", "x" * 40, ""),
    ("", "127.0.0.1:*", "", ""),
])
def test_doctor_and_server_agree_on_exposure(monkeypatch, host, hosts, token, allow, container):
    """doctor sagt genau dann 'startet nicht', wenn der Server den Start verweigert."""
    from websearch.core import InputError
    from websearch.doctor import check_mcp
    for k, v in (("MCP_HOST", host), ("MCP_ALLOWED_HOSTS", hosts), ("MCP_AUTH_TOKEN", token),
                 ("MCP_ALLOW_NO_AUTH", allow), ("WEBSEARCH_IN_CONTAINER", container)):
        monkeypatch.setenv(k, v)
    try:
        mcp_server.check_exposure(host.strip() or "127.0.0.1", token or None)
        refused = False
    except InputError:
        refused = True
    assert any(c.level == "fail" for c in check_mcp()) is refused
