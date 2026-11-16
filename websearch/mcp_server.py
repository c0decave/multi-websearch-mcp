"""MCP-Server: stellt Suche und Seitenabruf KI-Agents (Claude Code, Claude Desktop, ...) als Tools bereit.

Einbinden in Claude Code:
  claude mcp add -s user websearch -- /pfad/zum/repo/.venv/bin/python -m websearch --env-file /pfad/.env mcp  (stdio)
  T=$(… -m websearch.envfile --get MCP_AUTH_TOKEN .env) && \
    claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"

Umgebung:
  WEBSEARCH_MCP_PAID=0   Agents dürfen Bezahl-APIs nie nutzen
  MCP_HOST / MCP_PORT    (HTTP) Bind-Adresse und Port, Standard 127.0.0.1:8765
  MCP_ALLOWED_HOSTS      (HTTP) erlaubte Host-Header, Standard 127.0.0.1:*,localhost:*,[::1]:*
  MCP_AUTH_TOKEN         (HTTP) gesetzt -> jede Anfrage (außer /healthz) braucht "Authorization: Bearer <token>"

HTTP-Endpunkte: /mcp (streamable-http), /healthz (immer offen, "ok"), /metrics (Prometheus-Text,
mit Token geschützt, falls gesetzt).

Tool-Beschreibungen sind englisch (liest das Modell), Fehlermeldungen kommen aus der Engine bzw. t().
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hmac
import ipaddress
import logging
import os
import sys
from typing import TYPE_CHECKING, Annotated, Any, Callable, Literal, Mapping, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from typing_extensions import NotRequired, TypedDict

from . import __version__
from .core import InputError, SourceInfo, WebsearchError, env_flag, in_container, is_benign
from .engine import SearchEngine
from .i18n import t

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("websearch")

MAX_QUERIES = 20
MCP_PATH = "/mcp"
OPEN_PATHS = frozenset({"/healthz"})            # ohne Token erreichbar (Docker-Healthcheck)
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4"   # Starlette hängt "; charset=utf-8" an

Kind = Literal["web", "news", "ref"]
MaxResults = Annotated[int, Field(ge=1, le=30, description="Number of results to return (1-30).")]
Language = Annotated[str, Field(max_length=10, description="Language code like 'de' or 'en', or 'auto'.")]
Query = Annotated[str, Field(min_length=1, max_length=2000, description="Search query.")]
Days = Annotated[int | None, Field(ge=1, le=3650, description="Only results from the last N days")]


class Hit(TypedDict):
    title: str
    url: str
    snippet: NotRequired[str]
    published: NotRequired[str]
    found_by: list[str]
    redirect: NotRequired[Annotated[bool, Field(description=(
        "True if url is a redirect link (e.g. Google News). fetch_url resolves it to the publisher page "
        "(costs two extra requests to Google); prefer another hit with the same headline if there is one."
        ))]]


class ImageHit(TypedDict):
    title: str
    url: Annotated[str, Field(description="Page that shows the image.")]
    image: NotRequired[Annotated[str, Field(description="Direct URL of the image file.")]]
    thumbnail: NotRequired[str]
    found_by: list[str]


class VideoHit(TypedDict):
    title: str
    url: Annotated[str, Field(description="Page of the video (e.g. YouTube watch URL).")]
    thumbnail: NotRequired[str]
    duration: NotRequired[Annotated[str, Field(description="Running time such as '12:34'.")]]
    published: NotRequired[str]
    found_by: list[str]


class SearchOutput(TypedDict):
    query: str
    results: list[Hit]
    source_errors: dict[str, str]
    error: NotRequired[str]


class ImageSearchOutput(TypedDict):
    query: str
    results: list[ImageHit]
    source_errors: dict[str, str]
    error: NotRequired[str]


class VideoSearchOutput(TypedDict):
    query: str
    results: list[VideoHit]
    source_errors: dict[str, str]
    error: NotRequired[str]


class PageOutput(TypedDict):
    url: str
    final_url: str
    title: str | None
    content: str
    truncated: bool
    content_type: str
    cached: bool


# optionale Felder je Trefferart (nur nicht-leere werden ausgegeben)
_WEB_FIELDS = ("snippet", "published", "redirect")
_IMAGE_FIELDS = ("image", "thumbnail")
_VIDEO_FIELDS = ("thumbnail", "duration", "published")


def _paid() -> bool:
    raw = os.environ.get("WEBSEARCH_MCP_PAID", "").strip()
    known = ("1", "true", "yes", "on", "ja", "0", "false", "no", "off", "nein")
    if raw and raw.lower() not in known:                                  # Tippfehler: lieber nicht bezahlen
        log.warning(t("WEBSEARCH_MCP_PAID=%r unbekannt - Bezahl-APIs bleiben aus",
                      "WEBSEARCH_MCP_PAID=%r is unknown - paid APIs stay off"), raw)
        return False
    return env_flag("WEBSEARCH_MCP_PAID", default=True)


def _compact(res: Mapping[str, Any], fields: tuple[str, ...] = _WEB_FIELDS) -> Any:
    """Für LLMs: nur das Nötige, Quellenstatus knapp. fields = optionale Trefferfelder der Trefferart."""
    out: dict[str, Any] = {
        "query": res["query"],
        "results": [{"title": r.get("title") or "", "url": r["url"]}
                    | {k: r[k] for k in fields if r.get(k)}
                    | {"found_by": (r.get("engines") or [])[:4]} for r in res["results"]],
        # nur echte Ausfälle - übersprungene/gedrosselte/nicht abgewartete Quellen sind kein Problem
        "source_errors": {k: f"{v['state']}: {v['message']}" for k, v in res["sources"].items()
                          if not is_benign(v)},
    }
    if res.get("error"):
        out["error"] = res["error"]
    return out


def _days_kw(days: int | None) -> dict[str, Any]:
    """days nur weiterreichen, wenn gesetzt (Engine-Standard: keine Zeitgrenze)."""
    return {} if days is None else {"days": days}


def _expected_errors(fn):
    """Eingabe- und Abruffehler als ToolError melden: das Modell sieht die Meldung und kann sich
    korrigieren, statt nur 'Error executing tool' (und der Server loggt keinen Traceback)."""

    @functools.wraps(fn)
    async def wrapper(*a, **k):
        try:
            return await fn(*a, **k)
        except WebsearchError as e:            # erwartete Fehler (Eingabe, Abruf) - keine Bugs
            raise ToolError(str(e)) from e
    return wrapper


def build_server(engine_factory: Callable[[], SearchEngine] = SearchEngine) -> MCPServer:
    """Server mit eigener Engine-Instanz bauen (Tests übergeben eine Factory mit Fake-Quellen)."""
    state: dict[str, Any] = {"engine": None}
    lock = asyncio.Lock()

    async def engine() -> SearchEngine:
        async with lock:
            if state["engine"] is None:
                state["engine"] = await engine_factory().__aenter__()
        return state["engine"]

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        try:
            yield state
        finally:
            if state["engine"] is not None:
                await state["engine"].aclose()
                state["engine"] = None

    server = MCPServer(
        name="websearch",
        version=__version__,
        instructions=(
            "Parallel web search over several free sources (self-hosted SearXNG meta search with "
            "Google/Bing/Brave/DuckDuckGo/Qwant, Bing RSS, Google News, GDELT, Hacker News, Wikipedia). "
            "Results are de-duplicated and ranked; 'found_by' lists the sources that returned a hit. "
            "Use search_many for several sub-questions instead of many single calls, and fetch_url "
            "to read a result page as Markdown. web_search, news_search and search_many accept 'days' "
            "to restrict results to the last N days (e.g. days=7 for the past week). image_search returns "
            "image hits (url = page showing the image, image = direct file URL, thumbnail); video_search "
            "returns video hits (url, thumbnail, duration, published)."
        ),
        lifespan=lifespan,
    )

    @server.tool()
    @_expected_errors
    async def web_search(query: Query, max_results: MaxResults = 10, language: Language = "auto",
                         days: Days = None) -> SearchOutput:
        """General web search across several search engines in parallel."""
        eng = await engine()
        return cast(SearchOutput, _compact(await eng.search(query, n=max_results, kind="web", lang=language,
                                                            paid=_paid(), **_days_kw(days))))

    @server.tool()
    @_expected_errors
    async def news_search(query: Query, max_results: MaxResults = 15, language: Language = "auto",
                          days: Days = None) -> SearchOutput:
        """Recent news (Google News, Bing News, GDELT, Hacker News, SearXNG news), newest first."""
        eng = await engine()
        return cast(SearchOutput, _compact(await eng.search(query, n=max_results, kind="news", lang=language,
                                                            paid=_paid(), **_days_kw(days))))

    @server.tool()
    @_expected_errors
    async def image_search(query: Query, max_results: MaxResults = 10,
                           language: Language = "auto", days: Days = None) -> ImageSearchOutput:
        """Image search. Each hit has the page url, the direct image URL and a thumbnail."""
        eng = await engine()
        # Bild-/Videosuche nie über Bezahl-APIs
        res = await eng.search(query, n=max_results, kind="images", lang=language, paid=False, **_days_kw(days))
        return cast(ImageSearchOutput, _compact(res, _IMAGE_FIELDS))

    @server.tool()
    @_expected_errors
    async def video_search(query: Query, max_results: MaxResults = 10,
                           language: Language = "auto", days: Days = None) -> VideoSearchOutput:
        """Video search (e.g. YouTube and other video platforms) with thumbnail, duration and date."""
        eng = await engine()
        res = await eng.search(query, n=max_results, kind="videos", lang=language, paid=False, **_days_kw(days))
        return cast(VideoSearchOutput, _compact(res, _VIDEO_FIELDS))

    @server.tool()
    @_expected_errors
    async def search_many(
        queries: Annotated[list[str], Field(min_length=1, max_length=MAX_QUERIES,
                                            description=f"Up to {MAX_QUERIES} queries.")],
        max_results: MaxResults = 8, kind: Kind = "web", language: Language = "auto", days: Days = None,
    ) -> list[SearchOutput]:
        """Run several searches concurrently. kind: web | news | ref (ref = reference works such as
        Wikipedia). Per-source rate limits are respected automatically."""
        eng = await engine()
        # Gesamtbudget, damit 20 Anfragen nicht länger dauern als übliche Client-Timeouts
        res = await eng.search_many(queries, n=max_results, kind=kind, lang=language, paid=_paid(),
                                    total_deadline=45.0, **_days_kw(days))
        return [cast(SearchOutput, _compact(r)) for r in res]

    @server.tool()
    @_expected_errors
    async def fetch_url(
        url: Annotated[str, Field(min_length=8, max_length=4000, description="http(s) URL of the page.")],
        max_chars: Annotated[int, Field(ge=500, le=100_000, description="Truncate content to this length.")] = 20_000,
    ) -> PageOutput:
        """Fetch a web page and return its main content as Markdown (respects the site's robots.txt,
        refuses internal network targets, cached for 6 h). Google News redirect links are resolved to
        the publisher page first."""
        eng = await engine()
        return cast(PageOutput, await eng.fetch(url, max_chars=max_chars))

    @server.tool()
    @_expected_errors
    async def source_status() -> dict[str, SourceInfo]:
        """Which sources are available and which are currently paused because of blocks."""
        return cast("dict[str, SourceInfo]", (await engine()).status())

    # --- HTTP-Nebenendpunkte (nur bei streamable-http; Token-Prüfung macht BearerAuth) ---------------
    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    @server.custom_route("/metrics", methods=["GET"], include_in_schema=False)
    async def metrics(_request: Request) -> Response:
        from . import metrics as _metrics
        return Response(_metrics.render(await engine()), media_type=METRICS_CONTENT_TYPE)

    return server


# stdout gehört dem MCP-Protokoll; Bibliotheks-Logs leise halten
for _lg in ("httpx", "httpcore", "primp", "ddgs", "trafilatura"):
    logging.getLogger(_lg).setLevel(logging.WARNING)

server = build_server()


def transport_security() -> "TransportSecuritySettings":
    """Schutz gegen DNS-Rebinding: ohne ihn könnte eine beliebige Webseite im Browser über eine
    umgebogene Domain die Tools aufrufen (Guthaben verbrauchen, IP sperren lassen). Das MCP-SDK
    aktiviert den Schutz nur automatisch bei host=127.0.0.1 - im Container (0.0.0.0) nicht."""
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = allowed_hosts()
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


def auth_token() -> str | None:
    """MCP_AUTH_TOKEN (leer/nur Leerzeichen = kein Token)."""
    return os.environ.get("MCP_AUTH_TOKEN", "").strip() or None


def is_loopback(host: str) -> bool:
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:                           # Hostname: kann auf alles zeigen -> nicht lokal
        return False


from .core import MIN_TOKEN_LEN  # noqa: E402  (eine Quelle, auch für doctor)


def allowed_hosts() -> list[str]:
    return [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*").split(",")
            if h.strip()]


def _host_part(entry: str) -> str:
    """'name:*' / '[::1]:8765' / 'name' -> Hostteil ohne Port."""
    entry = entry.strip()
    if entry.startswith("["):
        return entry[1:entry.find("]")] if "]" in entry else entry
    return entry.rsplit(":", 1)[0] if entry.count(":") == 1 else entry


def exposure(host: str, hosts: list[str]) -> list[str]:
    """Warum der Server über den eigenen Rechner hinaus erreichbar sein könnte (leer = nur lokal)."""
    reasons = []
    if not is_loopback(host):
        reasons.append(f"MCP_HOST={host}")
    if remote := [h for h in hosts if not is_loopback(_host_part(h))]:
        reasons.append(f"MCP_ALLOWED_HOSTS: {', '.join(remote)}")
    return reasons


def check_exposure(host: str, token: str | None, hosts: list[str] | None = None) -> None:
    """Fail closed: ohne Token nie über den eigenen Rechner hinaus lauschen - außer bewusst erlaubt mit
    MCP_ALLOW_NO_AUTH=1 in einem Container (für Einzelplatzrechner, die den Port nur auf 127.0.0.1 veröffentlichen).
    Der Host-Header-Schutz ist keine Zugangskontrolle: jeder Nicht-Browser-Client kann ihn fälschen."""
    hosts = allowed_hosts() if hosts is None else hosts
    if token and len(token) < MIN_TOKEN_LEN:
        log.warning(t(f"MCP_AUTH_TOKEN ist kürzer als {MIN_TOKEN_LEN} Zeichen - erzeugen mit: openssl rand -hex 32",
                      f"MCP_AUTH_TOKEN is shorter than {MIN_TOKEN_LEN} characters - generate one with: "
                      "openssl rand -hex 32"))
    if token and not token.isascii():
        raise InputError(t("MCP_AUTH_TOKEN darf nur ASCII-Zeichen enthalten (HTTP-Header sind latin-1)",
                           "MCP_AUTH_TOKEN must contain ASCII characters only (HTTP headers are latin-1)"))
    reasons = exposure(host, hosts)
    if not token and not reasons:
        log.warning(t("MCP-Server ohne MCP_AUTH_TOKEN: nur lokal erreichbar, aber jeder Nutzer dieses Rechners "
                      "kann ihn (und konfigurierte Bezahl-APIs) verwenden - Token setzen (websearch init erzeugt "
                      "eines)",
                      "MCP server without MCP_AUTH_TOKEN: only reachable locally, but every user of this machine "
                      "can use it (and configured paid APIs) - set a token (websearch init creates one)"))
    if token or not reasons:
        return
    why = "; ".join(reasons)
    # MCP_ALLOW_NO_AUTH entschuldigt nur das Lauschen auf 0.0.0.0 im Container - ein öffentlicher Hostname
    # in MCP_ALLOWED_HOSTS heißt "Zugriff von außen gewollt", und der geht nie ohne Token
    if env_flag("MCP_ALLOW_NO_AUTH") and in_container() and is_loopback(host) is False and len(reasons) == 1:
        warn_if_exposed(host, token)
        return
    raise InputError(t(f"MCP-Server wäre ohne Anmeldung von außen erreichbar ({why}). MCP_AUTH_TOKEN setzen "
                       "(openssl rand -hex 32) - oder, nur wenn der Port ausschließlich auf 127.0.0.1 "
                       "veröffentlicht ist, MCP_ALLOW_NO_AUTH=1.",
                       f"the MCP server would be reachable from outside without authentication ({why}). Set "
                       "MCP_AUTH_TOKEN (openssl rand -hex 32) - or, only if the port is published on "
                       "127.0.0.1 exclusively, MCP_ALLOW_NO_AUTH=1."))


def warn_if_exposed(host: str, token: str | None) -> bool:
    """Warnen, wenn ohne Token auf einer Nicht-Loopback-Adresse gelauscht wird (mit MCP_ALLOW_NO_AUTH=1
    erlaubt, z.B. im Container mit Port nur auf 127.0.0.1)."""
    if token or is_loopback(host):
        return False
    log.warning(t("MCP-Server lauscht auf %s ohne MCP_AUTH_TOKEN: jeder, der diesen Port erreicht, kann "
                  "die Tools nutzen. Token setzen oder den Port nur auf 127.0.0.1 veröffentlichen "
                  "(Docker: \"127.0.0.1:8765:8765\").",
                  "MCP server listens on %s without MCP_AUTH_TOKEN: anyone who can reach this port can use "
                  "the tools. Set a token or publish the port on 127.0.0.1 only "
                  "(Docker: \"127.0.0.1:8765:8765\")."), host)
    return True


class BearerAuth:
    """ASGI-Hülle um die Starlette-App des SDK: statisches Bearer-Token (konstante Laufzeit) für alle
    Pfade außer OPEN_PATHS, plus Host-/Origin-Prüfung (DNS-Rebinding) für die eigenen Routen - die
    des SDK gilt nur für /mcp. Die SDK-eigene Auth setzt OAuth (issuer_url, Resource-Metadaten) voraus,
    für ein festes Token ist diese Hülle einfacher. lifespan-Nachrichten gehen unverändert durch."""

    def __init__(self, app: "ASGIApp", token: str | None,
                 security: "TransportSecuritySettings | None" = None) -> None:
        self.app = app
        self._token = token.encode() if token else None
        self._security = security

    def _authorized(self, scope: "Scope") -> bool:
        if self._token is None:
            return True
        scheme, _, cred = Headers(scope=scope).get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), self._token)

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in OPEN_PATHS):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":            # z.B. websocket: gibt es hier nicht -> ablehnen statt durchreichen
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if not self._authorized(scope):
            has_header = "authorization" in Headers(scope=scope)
            challenge = 'Bearer realm="websearch"' + (', error="invalid_token"' if has_header else "")
            msg = (t("Token ungültig", "invalid token") if has_header
                   else t("Authorization: Bearer <token> fehlt", "missing Authorization: Bearer <token>"))
            await PlainTextResponse(msg + "\n", status_code=401,
                                    headers={"WWW-Authenticate": challenge})(scope, receive, send)
            return
        if scope["path"] != MCP_PATH and self._security is not None:
            from mcp.server.transport_security import TransportSecurityMiddleware
            denied = await TransportSecurityMiddleware(self._security).validate_request(Request(scope))
            if denied is not None:
                await denied(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(srv: MCPServer | None = None, *, token: str | None = None, host: str = "127.0.0.1",
                   security: "TransportSecuritySettings | None" = None) -> BearerAuth:
    """ASGI-App für streamable-http (inkl. /healthz, /metrics). Jede App hat einen eigenen
    Session-Manager des SDK und lässt sich daher nur einmal starten."""
    srv = server if srv is None else srv
    security = transport_security() if security is None else security
    app = srv.streamable_http_app(streamable_http_path=MCP_PATH, transport_security=security, host=host)
    return BearerAuth(app, token, security)


def serve(http: bool = False) -> None:
    if http:
        try:
            port = int(os.environ.get("MCP_PORT", "8765"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise InputError(t("MCP_PORT muss ein Port zwischen 1 und 65535 sein (ist: {value})",
                               "MCP_PORT must be a port between 1 and 65535 (is: {value})")
                             .format(value=repr(os.environ.get("MCP_PORT")))) from None
        import uvicorn
        host = os.environ.get("MCP_HOST", "").strip() or "127.0.0.1"
        token = auth_token()
        check_exposure(host, token)
        app = build_http_app(server, token=token, host=host, security=transport_security())
        uvicorn.run(app, host=host, port=port, log_level=server.settings.log_level.lower())
    else:
        if sys.stdin.isatty():           # jemand hat 'websearch mcp' im Terminal gestartet
            print(t("MCP-Server (stdio) wartet auf einen MCP-Client auf stdin - nicht für die direkte Eingabe. "
                    "Beenden mit Strg+C; HTTP-Dienst: websearch mcp --http",
                    "MCP server (stdio) is waiting for an MCP client on stdin - not meant for typing. "
                    "Stop with Ctrl+C; HTTP service: websearch mcp --http"), file=sys.stderr)
        server.run("stdio")
