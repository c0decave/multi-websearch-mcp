"""Seiteninhalt abrufen und als Markdown liefern - der Schritt nach der Suche (für KI-Agents).

Schutzmassnahmen, weil die URL von einem Agent (also letztlich aus dem Internet) kommt:
  * nur http/https, keine Zugangsdaten in der URL
  * SSRF-Schutz: Ziele im internen Netz (localhost, 10.x, 192.168.x, 169.254.x, fc00::/7 ...) werden
    abgelehnt - auch nach jeder Weiterleitung und für robots.txt neu geprüft. Für Intranet-Nutzung
    bewusst abschaltbar mit WEBSEARCH_FETCH_ALLOW_PRIVATE=1.
    Gegen DNS-Rebinding wird der Name genau einmal pro Abruf-Schritt aufgelöst, die Adressen werden
    geprüft und die TCP-Verbindung geht genau an eine dieser geprüften Adressen ("Pinning", siehe
    _PinnedBackend). Host-Header, TLS-SNI und Zertifikatsprüfung bleiben beim ursprünglichen Namen.
    Ausnahme: Läuft der Abruf über einen Proxy (WEBSEARCH_PROXY), löst der Proxy die Namen selbst auf -
    dann ist die Prüfung nur "nach bestem Wissen" (ein Rebinding-Name könnte beim Proxy intern landen;
    der Proxy sollte interne Ziele deshalb selbst sperren). Dasselbe gilt für fremde httpx-Transporte
    (z.B. MockTransport in Tests).
  * robots.txt wird respektiert (24 h gecacht), abschaltbar mit WEBSEARCH_FETCH_IGNORE_ROBOTS=1
  * hostweites Tempo pro Ziel-Domain (Standard 1 Abruf / s), Größenlimit, Timeout
  * Inhaltserkennung (trafilatura) in höchstens MAX_EXTRACT_THREADS gleichzeitigen Daemon-Threads
  * Ergebnis 6 h im Cache
  * Google-News-Links (Weiterleitungen) werden vorher in die Herausgeber-URL aufgelöst (gnews.py);
    die aufgelöste URL durchläuft dieselben Prüfungen (inkl. Pinning). Abschaltbar mit
    WEBSEARCH_GNEWS_DECODE=0.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ipaddress
import re
import socket
import sqlite3
import ssl
import threading
import typing
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx

from .core import (PageResponse, RateLimited, RateLimiter, SourceUnavailable, StateStore, WebsearchError, env_flag,
                   log)
from .i18n import t
from .providers import BROWSER_UA, bot_ua

if TYPE_CHECKING:
    from .gnews import GNewsDecoder

MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_TIMEOUT = 30.0              # gesamter Abruf inkl. robots.txt und Weiterleitungen (gegen "Tröpfel"-Server)
MAX_EXTRACT_CHARS = 1_500_000     # so viel HTML bekommt die Inhaltserkennung höchstens
MAX_EXTRACT_THREADS = 3           # gleichzeitige trafilatura-Threads (prozessweit)
EXTRACT_WAIT = 10.0               # so lange wird höchstens auf einen freien Thread gewartet
MAX_PINNED_ADDRS = 3              # so viele geprüfte Adressen eines Namens werden nacheinander versucht
FETCH_TTL = 6 * 3600
ROBOTS_TTL = 24 * 3600
TEXT_TYPES = ("text/plain", "text/markdown", "application/json", "text/csv", "application/xml", "text/xml")

# Prozessweite Grenze für trafilatura-Threads: ein Thread läuft nach einem Timeout weiter (Python-Threads
# lassen sich nicht abbrechen) und gibt seinen Platz erst frei, wenn er wirklich fertig ist.
_EXTRACT_SLOTS = threading.BoundedSemaphore(MAX_EXTRACT_THREADS)


async def _extract_in_thread(fn, *args):
    """fn(*args) in einem Daemon-Thread (hält das Programmende nicht auf), höchstens
    MAX_EXTRACT_THREADS gleichzeitig. Wirft FetchError, wenn nach EXTRACT_WAIT s kein Platz frei ist."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EXTRACT_WAIT
    slots = _EXTRACT_SLOTS
    while not slots.acquire(blocking=False):
        if loop.time() >= deadline:
            raise FetchError(t("Inhaltserkennung ausgelastet ({n} Seiten in Arbeit) - bitte gleich erneut versuchen",
                               "content extraction is busy ({n} pages in progress) - please retry shortly"
                               ).format(n=MAX_EXTRACT_THREADS))
        await asyncio.sleep(0.05)
    fut = loop.create_future()

    def deliver(exc: BaseException | None, res=None) -> None:
        if not fut.done():
            fut.set_exception(exc) if exc is not None else fut.set_result(res)

    def runner():
        exc, res = None, None
        try:
            res = fn(*args)
        except BaseException as e:   # noqa: BLE001 - wird im Event-Loop weitergereicht
            exc = e
        finally:
            slots.release()          # erst hier: der Thread ist wirklich fertig
        with contextlib.suppress(RuntimeError):          # Loop schon geschlossen
            loop.call_soon_threadsafe(deliver, exc, res)
    try:
        threading.Thread(target=runner, name="trafilatura", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    return await fut


class FetchError(WebsearchError):
    """Abruf nicht möglich oder nicht erlaubt (Meldung ist für Menschen/LLMs gedacht)."""


class FetchForbidden(FetchError):
    """Ziel ist verboten (internes Netz, falsches Schema, Zugangsdaten) - nie erneut versuchen."""


def _allow_private() -> bool:
    return env_flag("WEBSEARCH_FETCH_ALLOW_PRIVATE")


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT = ipaddress.ip_network("::/96")            # veraltet: ::a.b.c.d
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")        # IPv4-translated: ::ffff:0:a.b.c.d


def _embedded_addresses(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Die Adresse selbst plus darin eingebettete IPv4-Ziele (IPv4-mapped, NAT64, 6to4, Teredo):
    sonst käme man z.B. über 64:ff9b::a00:1 an 10.0.0.1."""
    yield addr
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            yield addr.ipv4_mapped
        if addr in _NAT64 or addr in _SIIT or (addr in _IPV4_COMPAT and int(addr) > 1):
            yield ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if addr.sixtofour:
            yield addr.sixtofour
        if addr.teredo:
            yield from addr.teredo


class Target(NamedTuple):
    """Ergebnis der Prüfung: host = Name, wie httpx ihn verbindet (IDNA/ASCII, klein, ohne Schlusspunkt);
    addrs = geprüfte Adressen (Reihenfolge des Resolvers) oder None (WEBSEARCH_FETCH_ALLOW_PRIVATE=1)."""
    host: str
    addrs: tuple[str, ...] | None


def _norm_host(host: str) -> str:
    return host.rstrip(".").lower()


async def _resolve(host: str, port: int) -> list[str]:
    """Name -> Adressen (eigene Funktion, damit Tests den Resolver gezielt ersetzen können)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


async def _check_target(url: str) -> Target:
    """URL prüfen, den Namen EINMAL auflösen und alle Adressen prüfen. Wirft FetchError bei unerlaubten
    Zielen. Die zurückgegebenen Adressen sind die einzigen, zu denen danach verbunden werden darf."""
    try:
        p = urlsplit(url)
    except ValueError as e:
        raise FetchError(t("ungültige URL: {e}", "invalid URL: {e}").format(e=e)) from None
    if p.scheme not in ("http", "https"):
        raise FetchForbidden(t("nur http:// und https:// werden abgerufen",
                               "only http:// and https:// URLs are fetched"))
    if p.username or p.password:
        raise FetchForbidden(t("URLs mit Zugangsdaten werden nicht abgerufen",
                                 "URLs containing credentials are not fetched"))
    try:
        port = p.port
    except ValueError:
        raise FetchForbidden(t("ungültiger Port in der URL", "invalid port in the URL")) from None
    if not (p.hostname or "").rstrip("."):
        raise FetchError(t("URL ohne Host", "URL without host"))
    # Den Host so bestimmen, wie httpx ihn später verbindet (gleicher Parser, IDNA 2008): sonst könnten
    # Prüfung und Verbindung bei Sonderfällen unterschiedliche Hosts sehen
    try:
        hx = httpx.URL(url)
        host = _norm_host(hx.raw_host.decode("ascii"))
    except (httpx.InvalidURL, UnicodeError, ValueError) as e:
        raise FetchError(t("ungültige URL: {e}", "invalid URL: {e}").format(e=e)) from None
    if hx.scheme not in ("http", "https"):
        raise FetchForbidden(t("nur http:// und https:// werden abgerufen",
                               "only http:// and https:// URLs are fetched"))
    if hx.userinfo:
        raise FetchForbidden(t("URLs mit Zugangsdaten werden nicht abgerufen",
                               "URLs containing credentials are not fetched"))
    if not host:
        raise FetchError(t("URL ohne Host", "URL without host"))
    if _allow_private():
        return Target(host, None)
    try:
        ip = ipaddress.ip_address(host)
        found = [str(ip)]
    except ValueError:
        try:
            found = await _resolve(host, port or (443 if hx.scheme == "https" else 80))
        except (socket.gaierror, UnicodeError) as e:
            raise FetchError(t("Host nicht auflösbar: {host}", "host cannot be resolved: {host}"
                               ).format(host=host)) from e
    addrs = tuple(dict.fromkeys(a.split("%")[0] for a in found))
    if not addrs:
        raise FetchError(t("Host nicht auflösbar: {host}", "host cannot be resolved: {host}").format(host=host))
    for text in addrs:
        for a in _embedded_addresses(ipaddress.ip_address(text)):
            if not a.is_global or a.is_multicast or getattr(a, "is_site_local", False):
                raise FetchForbidden(t("Ziel im internen Netz ({a}) wird nicht abgerufen "
                                       "(WEBSEARCH_FETCH_ALLOW_PRIVATE=1 erlaubt das)",
                                       "target in an internal network ({a}) is not fetched "
                                       "(WEBSEARCH_FETCH_ALLOW_PRIVATE=1 allows it)").format(a=a))
    return Target(host, addrs)


# --------------------------------------------------------------------------- DNS-Pinning

def _inner_backend() -> httpcore.AsyncNetworkBackend:
    """Das echte Netzwerk-Backend unter dem Pinning (Tests ersetzen es durch ein nachgebautes Netz)."""
    return httpcore.AnyIOBackend()


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Netzwerk-Backend für httpcore, das NICHT selbst auflöst: connect_tcp(host) verbindet nur zu den
    vorher von _check_target geprüften Adressen dieses Hosts (pin()). Ein nicht gepinnter Host wird
    abgelehnt. httpcore ruft danach start_tls(server_hostname=<ursprünglicher Host>) auf, und der
    Host-Header kommt aus der URL - SNI, Zertifikatsprüfung und virtuelle Hosts bleiben also korrekt."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or _inner_backend()
        self._pins: dict[str, tuple[str, ...] | None] = {}

    def pin(self, target: Target) -> None:
        """Adressen für den Host festlegen (None = normal auflösen, nur bei ALLOW_PRIVATE)."""
        self._pins[_norm_host(target.host)] = target.addrs

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None
                          ) -> httpcore.AsyncNetworkStream:
        key = _norm_host(host)
        if key not in self._pins:
            raise httpcore.ConnectError(t("Verbindung zu {host} ohne vorherige Prüfung verweigert",
                                          "connection to {host} refused: not checked before").format(host=host))
        addrs = self._pins[key]
        if addrs is None:         # WEBSEARCH_FETCH_ALLOW_PRIVATE=1: keine Prüfung, also auch kein Pinning
            return await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                                 socket_options=socket_options)
        last: Exception | None = None
        for ip in addrs[:MAX_PINNED_ADDRS]:
            try:
                return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None
                                  ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not used for fetch")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Einmal pro Prozess (das Laden der CA-Liste kostet Zeit). Wie der _CappedTransport der Engine:
    SSL_CERT_FILE/SSL_CERT_DIR werden beachtet (Proxys aus der Umgebung betrifft das nicht)."""
    return httpx.create_ssl_context()


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx-Standard-Transport mit _PinnedBackend; Antworten wie in der Engine auf
    engine.MAX_RESPONSE_BYTES (übertragen) begrenzt."""

    def __init__(self, backend: _PinnedBackend):
        super().__init__(verify=_ssl_context())
        # httpx erlaubt kein eigenes network_backend -> Pool mit denselben Werten selbst bauen
        self._pool = httpcore.AsyncConnectionPool(ssl_context=_ssl_context(), max_connections=10,
                                                  max_keepalive_connections=10, keepalive_expiry=5.0,
                                                  network_backend=backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from .engine import MAX_RESPONSE_BYTES, _CappedStream
        resp = await super().handle_async_request(request)
        resp.stream = _CappedStream(resp.stream, MAX_RESPONSE_BYTES)  # type: ignore[arg-type]
        return resp


class _PinnedClient(httpx.AsyncClient):
    """Kurzlebiger Client für einen Abruf: verbindet nur zu geprüften und gepinnten Adressen."""

    def __init__(self) -> None:
        from .engine import _size_guard
        self.backend = _PinnedBackend()
        super().__init__(transport=_PinnedTransport(self.backend), trust_env=False, follow_redirects=False,
                         event_hooks={"response": [_size_guard]})


def _can_pin(client: httpx.AsyncClient) -> bool:
    """True, wenn client direkt (ohne Proxy) über den httpx-Standard-Transport verbindet - dann ersetzt
    der Fetcher ihn durch einen _PinnedClient. Mit Proxy (WEBSEARCH_PROXY, proxy=/mounts=) löst der
    Proxy die Namen auf; fremde Transporte (MockTransport) bleiben unangetastet."""
    if getattr(client, "_mounts", None):
        return False
    tr = getattr(client, "_transport", None)
    if not isinstance(tr, httpx.AsyncHTTPTransport):
        return False
    pool = getattr(tr, "_pool", None)
    return isinstance(pool, httpcore.AsyncConnectionPool) and not isinstance(
        pool, (httpcore.AsyncHTTPProxy, httpcore.AsyncSOCKSProxy))


async def _check(client: httpx.AsyncClient, url: str) -> Target:
    """_check_target + die geprüften Adressen im Pinning-Client festlegen (falls client einer ist)."""
    target = await _check_target(url)
    if isinstance(client, _PinnedClient):
        client.backend.pin(target)
    return target


def _to_markdown(html: str, url: str) -> tuple[str | None, str]:
    import trafilatura
    text = trafilatura.extract(html, output_format="markdown", include_links=True, include_tables=True,
                               url=url, favor_recall=True)
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else None)
    if not text:   # z.B. sehr kurze Seiten: Fallback auf reinen Text
        text = trafilatura.html2txt(html) or ""
    # trafilatura löst reine Anker (#cite_note-1) gegen den Host auf -> auf die Seite selbst beziehen
    p = urlsplit(url)
    text = text.replace(f"]({p.scheme}://{p.netloc}#", f"]({url.split('#', 1)[0]}#")
    text = re.sub(r"\n{3,}", "\n\n", text)            # lange Leerzeilen-Strecken zusammenfassen
    return title, text.strip()


class Fetcher:
    def __init__(self, store: StateStore, per_host_interval: float = 1.0):
        self.store = store
        self.per_host_interval = per_host_interval
        self._limiters: dict[str, RateLimiter] = {}
        self._gnews: GNewsDecoder | None = None

    def _cache_get(self, url: str) -> dict | None:
        try:
            hit = self.store.get(StateStore.key("fetch", url), ttl=FETCH_TTL)
        except Exception:
            log.warning(t("Fetch-Cache nicht lesbar", "fetch cache not readable"), exc_info=True)
            return None
        return hit if isinstance(hit, dict) else None

    def _limiter(self, host: str) -> RateLimiter:
        """Ein Limiter pro Ziel-Host (hostweit über SQLite); begrenzt auf die letzten 512 Hosts."""
        lim = self._limiters.pop(host, None)
        if lim is None:
            lim = RateLimiter(self.per_host_interval, concurrency=2)
            lim.attach(self.store, f"fetch:{host}")
        self._limiters[host] = lim                    # dict = Einfügereihenfolge -> einfache LRU
        while len(self._limiters) > 512:
            self._limiters.pop(next(iter(self._limiters)))
        return lim

    async def _robots_allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        if env_flag("WEBSEARCH_FETCH_IGNORE_ROBOTS"):
            return True
        p = urlsplit(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        ck = StateStore.key("robots", robots_url)
        try:
            body = self.store.get(ck, ttl=ROBOTS_TTL)
        except sqlite3.Error:            # Zustandsdatei gesperrt: robots.txt eben neu holen
            body = None
        if body is None:
            try:
                status, text, _ = await self._get(client, robots_url, bot_ua(), max_bytes=500_000,
                                                  rate_limit=False)
                # 4xx = keine Regeln; 5xx = vorsichtshalber erlaubt, aber nicht cachen
                body = text if status == 200 else ""
                if status < 500:
                    with contextlib.suppress(sqlite3.Error):
                        self.store.set(ck, body)
            except FetchForbidden:   # robots.txt leitet ins interne Netz um -> verdächtig, abbrechen
                raise
            except FetchError as e:
                log.info(t("robots.txt von %s nicht abrufbar (%s) - behandle als erlaubt",
                           "robots.txt of %s not retrievable (%s) - treating as allowed"), p.netloc, e)
                return True
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return rp.can_fetch(bot_ua().split("/")[0], url) and rp.can_fetch("*", url)

    async def _get(self, client: httpx.AsyncClient, url: str, user_agent: str, max_bytes: int,
                   rate_limit: bool, check_robots: bool = False) -> tuple[int, str, httpx.Response]:
        """GET mit manuell verfolgten Weiterleitungen: JEDES Ziel wird auf interne Netze geprüft (und bei
        check_robots auf robots.txt, wenn der Host wechselt). -> (Status, Text, letzte Antwort)"""
        current, robots_ok_for = url, {urlsplit(url).netloc}
        for _ in range(MAX_REDIRECTS + 1):
            host = (await _check(client, current)).host       # jeder Schritt: neu auflösen, prüfen, pinnen
            netloc = urlsplit(current).netloc
            if check_robots and netloc not in robots_ok_for:
                if not await self._robots_allowed(client, current):
                    raise FetchError(t("robots.txt von {netloc} verbietet automatisierte Abrufe",
                                       "robots.txt of {netloc} disallows automated fetching").format(netloc=netloc))
                robots_ok_for.add(netloc)
            try:
                limiter = self._limiter(host) if rate_limit else None
                async with (limiter.slot(budget=10) if limiter else _nullslot()):
                    req = client.build_request("GET", current, timeout=15, headers={
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                        "Accept-Language": "de,en;q=0.8"})
                    resp = await client.send(req, stream=True, follow_redirects=False)
                    try:
                        if resp.is_redirect and "location" in resp.headers:
                            current = urljoin(current, resp.headers["location"])
                            continue
                        body = await _read_capped(resp, max_bytes)
                    finally:
                        await resp.aclose()
            except RateLimited as e:
                raise FetchError(t("zu viele Abrufe bei {host}, bitte in {s}s erneut versuchen",
                                   "too many requests to {host}, please retry in {s}s"
                                   ).format(host=host, s=f"{e.wait:.0f}")) from None
            except (httpx.HTTPError, SourceUnavailable) as e:    # SourceUnavailable: Antwort > 10 MB
                raise FetchError(t("Abruf fehlgeschlagen: {e}", "fetch failed: {e}").format(
                    e=str(e) or type(e).__name__)) from None
            return resp.status_code, _decode(body, resp), resp
        raise FetchError(t("mehr als {n} Weiterleitungen", "more than {n} redirects").format(n=MAX_REDIRECTS))

    async def fetch(self, client: httpx.AsyncClient, url: str, max_chars: int = 20_000) -> PageResponse:
        """Wie _fetch, aber mit Gesamtzeitlimit (TOTAL_TIMEOUT).

        Verbindet `client` direkt (kein Proxy, httpx-Standard-Transport), läuft der Abruf stattdessen über
        einen eigenen, kurzlebigen _PinnedClient: jede Verbindung (Seite, Weiterleitungen, robots.txt,
        news.google.com) geht nur an die eben geprüften Adressen - Schutz gegen DNS-Rebinding. Mit Proxy
        wird `client` unverändert benutzt; der Proxy löst dann selbst auf, die Prüfung ist nur
        "nach bestem Wissen"."""
        try:
            if _can_pin(client):
                async with _PinnedClient() as pinned:
                    return await asyncio.wait_for(self._fetch(pinned, url, max_chars), TOTAL_TIMEOUT)
            return await asyncio.wait_for(self._fetch(client, url, max_chars), TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            raise FetchError(t("Abruf dauerte länger als {s} s - abgebrochen",
                               "fetch took longer than {s} s - aborted").format(s=f"{TOTAL_TIMEOUT:.0f}")) from None

    async def _fetch(self, client: httpx.AsyncClient, url: str, max_chars: int = 20_000) -> PageResponse:
        """-> {url, final_url, title, content (Markdown), truncated, content_type, cached}"""
        url = url.strip()
        await _check(client, url)             # zuerst: wirft lesbare Fehler auch bei kaputten URLs
        from . import gnews
        target = url
        is_gnews = gnews.is_gnews_host(urlsplit(url).hostname)
        if is_gnews and not gnews.enabled():
            raise FetchError(t("Google-News-Links sind Weiterleitungen, die Google für Automaten sperrt. "
                               "Bitte die direkte Artikel-URL nutzen (z.B. aus einem anderen Treffer derselben "
                               "Schlagzeile) oder nach der Schlagzeile suchen.",
                               "Google News links are redirects that Google blocks for automated clients. "
                               "Please use the direct article URL (e.g. from another result with the same "
                               "headline) or search for the headline."))
        if (hit := self._cache_get(url)) is not None:
            return _cut(dict(hit, cached=True), max_chars)
        if is_gnews:
            # Weiterleitung selbst auflösen (offline oder über Google, hostweit gedrosselt und gecacht);
            # die Herausgeber-URL wird danach genauso geprüft wie jede andere (SSRF, robots.txt, Tempo)
            if self._gnews is None:
                self._gnews = gnews.GNewsDecoder(self.store)
            await _check(client, f"https://{gnews.HOST}/")      # Ziel der Decoder-Requests prüfen und pinnen
            target = await self._gnews.decode(client, url)
            await _check(client, target)
            if (hit := self._cache_get(target)) is not None:
                return _cut(dict(hit, url=url, cached=True), max_chars)
        if not await self._robots_allowed(client, target):
            raise FetchError(t("robots.txt der Seite verbietet automatisierte Abrufe",
                               "the site's robots.txt disallows automated fetching"))

        status, text, resp = await self._get(client, target, BROWSER_UA, max_bytes=MAX_BYTES, rate_limit=True,
                                             check_robots=True)
        current = str(resp.url)
        host = urlsplit(current).hostname or ""
        if status >= 400:
            raise FetchError(t("HTTP {status} von {host}", "HTTP {status} from {host}"
                               ).format(status=status, host=host))
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
            # in einem Daemon-Thread und mit begrenzter Eingabe: trafilatura kann bei riesigen Seiten viel CPU
            # brauchen, und ein normaler Worker-Thread würde das Programmende aufhalten; höchstens
            # MAX_EXTRACT_THREADS gleichzeitig (auch solche, die nach einem Timeout noch weiterlaufen)
            title, content = await _extract_in_thread(_to_markdown, text[:MAX_EXTRACT_CHARS], current)
        elif ctype.startswith(TEXT_TYPES) or ctype.startswith("text/"):
            title, content = None, text
        elif ctype == "application/pdf":
            raise FetchError(t("PDF-Dokumente werden nicht unterstützt", "PDF documents are not supported"))
        else:
            raise FetchError(t("Inhaltstyp {ctype} wird nicht unterstützt", "content type {ctype} is not supported"
                               ).format(ctype=ctype or t("unbekannt", "unknown")))
        out = {"url": url, "final_url": current, "title": title, "content": content,
               "content_type": ctype, "cached": False}
        try:
            self.store.set(StateStore.key("fetch", url), out)
        except Exception:   # Cache-Fehler dürfen den Abruf nicht verwerfen
            log.exception(t("Fetch-Cache konnte nicht geschrieben werden", "fetch cache could not be written"))
        return _cut(out, max_chars)


@contextlib.asynccontextmanager
async def _nullslot():
    yield


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def _decode(body: bytes, resp: httpx.Response) -> str:
    """Zeichensatz: HTTP-Header, sonst <meta charset> im HTML, sonst UTF-8."""
    enc = resp.charset_encoding
    if not enc and (m := _META_CHARSET.search(body[:4096])):
        enc = m.group(1).decode("ascii", "ignore")
    try:
        return body.decode(enc or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise FetchError(t("Seite größer als {mb} MB", "page larger than {mb} MB"
                               ).format(mb=max(1, max_bytes // 1024 // 1024)))
        chunks.append(chunk)
    return b"".join(chunks)


def _cut(d: dict, max_chars: int) -> PageResponse:
    content = d.get("content") or ""
    out = dict(d, truncated=len(content) > max_chars)
    out["content"] = content[:max_chars]
    return out  # type: ignore[return-value]
