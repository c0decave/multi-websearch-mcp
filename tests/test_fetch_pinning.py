"""DNS-Pinning in fetch_url: Schutz gegen DNS-Rebinding (Prüfung und Verbindung sehen dieselbe Adresse).

Die meisten Tests laufen über ein nachgebautes Netz (FakeNet) unter dem _PinnedBackend: es zeichnet auf,
zu welcher IP verbunden wurde, welcher TLS-Servername (SNI) und welcher Host-Header ankam. Ein Test nutzt
echte Sockets auf 127.0.0.1/127.0.0.2.
"""
import asyncio
import http.server
import socket
import threading
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from websearch import fetch as F
from websearch import gnews as G
from websearch.core import StateStore

PUBLIC, PUBLIC2, GOOGLE_IP = "93.184.215.14", "93.184.215.15", "142.250.185.78"


# --------------------------------------------------------------------------- nachgebautes Netz

class FakeStream(httpcore.AsyncNetworkStream):
    """Eine TCP-Verbindung im FakeNet: sammelt den Request, antwortet mit "Connection: close"."""

    def __init__(self, net: "FakeNet", ip: str, port: int):
        self.net, self.ip, self.port = net, ip, port
        self.sni: str | None = None
        self.sent, self.out = b"", None

    async def write(self, buffer, timeout=None):
        self.sent += buffer

    async def read(self, max_bytes, timeout=None):
        if self.out is None:
            self.out = self.net.respond(self)
        chunk, self.out = self.out[:max_bytes], self.out[max_bytes:]
        return chunk

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        return self

    def get_extra_info(self, info):
        return None


class FakeNet(httpcore.AsyncNetworkBackend):
    """Ersetzt das echte Netzwerk-Backend. handler(req) -> (Status, Header, Body)."""

    def __init__(self, handler, unreachable=()):
        self.handler, self.unreachable = handler, set(unreachable)
        self.connects: list[tuple[str, int]] = []
        self.log: list[SimpleNamespace] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        if host in self.unreachable:
            raise httpcore.ConnectError(f"{host} nicht erreichbar")
        return FakeStream(self, host, port)

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)

    def respond(self, stream: FakeStream) -> bytes:
        head, _, body = stream.sent.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:])}
        req = SimpleNamespace(ip=stream.ip, port=stream.port, sni=stream.sni, method=method, path=path,
                              host=headers.get("host", ""), headers=headers, body=body)
        self.log.append(req)
        status, rheaders, rbody = self.handler(req)
        rbody = rbody.encode() if isinstance(rbody, str) else rbody
        rheaders = {"content-length": str(len(rbody)), **rheaders, "connection": "close"}
        out = f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in rheaders.items()) + "\r\n"
        return out.encode() + rbody


@pytest.fixture
def dns(monkeypatch):
    """Resolver der SSRF-Prüfung ersetzen; jede andere Namensauflösung ist ein Fehler (beim Pinning
    darf httpx nicht selbst auflösen)."""
    table: dict[str, list[str]] = {}

    async def resolve(host, port):
        if host not in table:
            raise socket.gaierror(f"unbekannt: {host}")
        return list(table[host])

    async def forbidden(self, host, *a, **k):
        raise AssertionError(f"unerwartete DNS-Abfrage für {host}")
    monkeypatch.setattr(F, "_resolve", resolve)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", forbidden)
    return table


def use_net(monkeypatch, handler, **kw) -> FakeNet:
    net = FakeNet(handler, **kw)
    monkeypatch.setattr(F, "_inner_backend", lambda: net)
    return net


def run_fetch(tmp_path, url, gnews_decoder=False):
    async def go():
        f = F.Fetcher(StateStore(tmp_path / "c.db"), per_host_interval=0)
        if gnews_decoder:
            f._gnews = G.GNewsDecoder(f.store, interval=0)
        async with httpx.AsyncClient(trust_env=False) as c:     # direkt (ohne Proxy) -> Pinning
            return await f.fetch(c, url)
    return asyncio.run(go())


def text(body, status=200, ctype="text/plain"):
    return status, {"content-type": ctype}, body


# --------------------------------------------------------------------------- Tests

def test_connection_goes_to_checked_ip_with_original_host_and_sni(tmp_path, monkeypatch, dns):
    dns["www.example.org"] = [PUBLIC]
    net = use_net(monkeypatch, lambda r: text("", 404) if r.path == "/robots.txt" else text("Hallo"))
    page = run_fetch(tmp_path, "https://www.example.org/seite")
    assert page["content"] == "Hallo" and page["final_url"] == "https://www.example.org/seite"
    assert [r.path for r in net.log] == ["/robots.txt", "/seite"]
    for r in net.log:
        assert (r.ip, r.port, r.sni, r.host) == (PUBLIC, 443, "www.example.org", "www.example.org")


def test_http_without_tls_and_explicit_port(tmp_path, monkeypatch, dns):
    dns["www.example.org"] = [PUBLIC]
    net = use_net(monkeypatch, lambda r: text("", 404) if r.path == "/robots.txt" else text("ok"))
    assert run_fetch(tmp_path, "http://www.example.org:8080/x")["content"] == "ok"
    assert {(r.ip, r.port, r.sni, r.host) for r in net.log} == {(PUBLIC, 8080, None, "www.example.org:8080")}


def test_every_redirect_hop_is_resolved_checked_and_pinned(tmp_path, monkeypatch, dns):
    dns.update({"a.example": [PUBLIC], "b.example": [PUBLIC2]})

    def h(r):
        if r.path == "/robots.txt":
            return text("", 404)
        if r.host == "a.example":
            return 302, {"location": "https://b.example/ziel"}, b""
        return text("Ziel")
    net = use_net(monkeypatch, h)
    assert run_fetch(tmp_path, "https://a.example/start")["content"] == "Ziel"
    hops = {(r.host, r.ip, r.sni) for r in net.log}
    assert hops == {("a.example", PUBLIC, "a.example"), ("b.example", PUBLIC2, "b.example")}
    assert ("b.example", "/robots.txt") in {(r.host, r.path) for r in net.log}   # robots.txt auch gepinnt


def test_redirect_to_name_resolving_internally_is_refused(tmp_path, monkeypatch, dns):
    dns.update({"a.example": [PUBLIC], "intern.example": ["10.0.0.1"]})
    net = use_net(monkeypatch, lambda r: text("", 404) if r.path == "/robots.txt"
                  else (302, {"location": "http://intern.example/admin"}, b""))
    with pytest.raises(F.FetchForbidden, match="internen Netz"):
        run_fetch(tmp_path, "https://a.example/start")
    assert {ip for ip, _ in net.connects} == {PUBLIC}


def test_all_resolved_addresses_must_be_public(tmp_path, monkeypatch, dns):
    dns["mixed.example"] = [PUBLIC, "127.0.0.1"]
    net = use_net(monkeypatch, lambda r: text("x"))
    with pytest.raises(F.FetchForbidden):
        run_fetch(tmp_path, "https://mixed.example/")
    assert net.connects == []


def test_pinned_backend_refuses_unchecked_hosts_and_tries_next_checked_address():
    async def go():
        net = FakeNet(lambda r: text("x"), unreachable={PUBLIC})
        be = F._PinnedBackend(inner=net)
        with pytest.raises(httpcore.ConnectError, match="ohne vorherige Prüfung"):
            await be.connect_tcp("nicht-geprueft.example", 443)
        assert net.connects == []
        be.pin(F.Target("multi.example", (PUBLIC, PUBLIC2)))
        stream = await be.connect_tcp("Multi.Example.", 443)      # Schreibweise egal
        assert isinstance(stream, FakeStream) and stream.ip == PUBLIC2
        assert net.connects == [(PUBLIC, 443), (PUBLIC2, 443)]
        with pytest.raises(httpcore.ConnectError):
            await be.connect_unix_socket("/var/run/docker.sock")
    asyncio.run(go())


def test_idn_hosts_are_checked_and_pinned_in_their_ascii_form(tmp_path, monkeypatch, dns):
    dns["xn--bcher-kva.example"] = [PUBLIC]
    net = use_net(monkeypatch, lambda r: text("", 404) if r.path == "/robots.txt" else text("ok"))
    assert run_fetch(tmp_path, "https://bücher.example/")["content"] == "ok"
    assert {(r.ip, r.sni, r.host) for r in net.log} == {(PUBLIC, "xn--bcher-kva.example", "xn--bcher-kva.example")}


def test_size_cap_also_applies_to_pinned_client(tmp_path, monkeypatch, dns):
    dns["big.example"] = [PUBLIC]
    use_net(monkeypatch, lambda r: text("", 404) if r.path == "/robots.txt"
            else (200, {"content-type": "application/zip", "content-length": str(20 * 1024 * 1024)}, b"x"))
    with pytest.raises(F.FetchError, match="zu groß"):
        run_fetch(tmp_path, "https://big.example/x.zip")


def test_google_news_requests_and_decoded_target_are_pinned(tmp_path, monkeypatch, dns):
    from test_gnews import NEW_ID, article_page, batch_response, gnews_url
    dns.update({"news.google.com": [GOOGLE_IP], "pub.example": [PUBLIC]})

    def h(r):
        if r.host == "news.google.com":
            if r.method == "GET" and r.path.startswith("/rss/articles/"):
                return text(article_page(), ctype="text/html; charset=utf-8")
            if r.method == "POST" and r.path == "/_/DotsSplashUi/data/batchexecute":
                return text(batch_response("https://pub.example/artikel"), ctype="application/json")
            return text("", 404)
        if r.path == "/robots.txt":
            return text("", 404)
        return text("Artikeltext")
    net = use_net(monkeypatch, h)
    page = run_fetch(tmp_path, gnews_url(NEW_ID), gnews_decoder=True)
    assert page["final_url"] == "https://pub.example/artikel" and page["content"] == "Artikeltext"
    google = [r for r in net.log if r.host == "news.google.com"]
    assert len(google) == 2 and all((r.ip, r.sni) == (GOOGLE_IP, "news.google.com") for r in google)
    assert all((r.ip, r.sni) == (PUBLIC, "pub.example") for r in net.log if r.host == "pub.example")


def test_client_detection():
    from websearch.engine import _CappedTransport

    def mock(r):
        return httpx.Response(200)
    assert F._can_pin(httpx.AsyncClient(trust_env=False))
    assert F._can_pin(httpx.AsyncClient(trust_env=False, transport=_CappedTransport()))     # wie die Engine
    assert not F._can_pin(httpx.AsyncClient(trust_env=False, transport=_CappedTransport(proxy="http://127.0.0.1:9")))
    assert not F._can_pin(httpx.AsyncClient(trust_env=False, proxy="http://127.0.0.1:9"))
    assert not F._can_pin(httpx.AsyncClient(transport=httpx.MockTransport(mock)))


# --------------------------------------------------------------------------- echte Sockets

def _server(ip: str, port: int, body: bytes) -> http.server.HTTPServer:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer((ip, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_dns_rebinding_cannot_reach_internal_server(tmp_path, monkeypatch):
    """Nachgestellter Angriff: die Prüfung sieht eine öffentliche Adresse (PUBLIC, hier "gespielt" von
    127.0.0.2), jede spätere Auflösung des Namens liefert 127.0.0.1 mit einem internen Geheimnis."""
    try:
        public = _server("127.0.0.2", 0, b"OEFFENTLICH")
        internal = _server("127.0.0.1", public.server_address[1], b"GEHEIM")
    except OSError as e:
        pytest.skip(f"Loopback-Adressen nicht nutzbar: {e}")
    port = public.server_address[1]
    try:
        async def check_resolver(host, p):          # Auflösung der SSRF-Prüfung: öffentlich
            assert host == "rebind.example"
            return [PUBLIC]
        real_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo

        async def later_lookups(self, host, *a, **k):   # jede weitere Auflösung: intern (Rebinding)
            if host in ("rebind.example", b"rebind.example"):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return await real_getaddrinfo(self, host, *a, **k)
        monkeypatch.setattr(F, "_resolve", check_resolver)
        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", later_lookups)
        connects: list[str] = []

        class Internet(httpcore.AsyncNetworkBackend):        # PUBLIC liegt "im Internet" = 127.0.0.2
            def __init__(self):
                self.real = httpcore.AnyIOBackend()

            async def connect_tcp(self, host, port, **kw):
                connects.append(host)
                return await self.real.connect_tcp("127.0.0.2" if host == PUBLIC else host, port, **kw)

            async def sleep(self, s):
                await self.real.sleep(s)
        monkeypatch.setattr(F, "_inner_backend", Internet)

        url = f"http://rebind.example:{port}/latest/meta-data/"
        page = run_fetch(tmp_path / "a", url)
        assert page["content"] == "OEFFENTLICH" and set(connects) == {PUBLIC}

        # Gegenprobe: ohne Pinning (httpx löst selbst auf) landet derselbe Abruf beim internen Server
        monkeypatch.setattr(F, "_can_pin", lambda c: False)
        assert run_fetch(tmp_path / "b", url)["content"] == "GEHEIM"
    finally:
        public.shutdown()
        internal.shutdown()


@pytest.mark.parametrize("addr", ["::127.0.0.1", "::ffff:0:7f00:1", "::ffff:0:a00:1", "::a00:1"])
def test_legacy_ipv6_forms_embedding_ipv4_are_checked(addr):
    import ipaddress
    from websearch.fetch import _embedded_addresses
    embedded = list(_embedded_addresses(ipaddress.ip_address(addr)))
    assert any(isinstance(a, ipaddress.IPv4Address) and (a.is_loopback or a.is_private) for a in embedded)
