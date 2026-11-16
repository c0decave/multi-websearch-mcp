"""Reddit als Suchquelle über die offizielle API (OAuth, "application-only").

Ablauf:
  1. Token: POST https://www.reddit.com/api/v1/access_token (Basic-Auth mit Client-ID/Secret,
     grant_type=client_credentials). Gilt meist 24 h; wird im Speicher gehalten und kurz vor Ablauf
     (oder nach einem 401) neu geholt.
  2. Suche: GET https://oauth.reddit.com/search?q=...&sort=relevance|new&t=...&type=link&raw_json=1

Für "script"-Apps kostenlos, Limit ~100 Anfragen/min pro Client-ID. Reddit meldet den Stand in den
Kopfzeilen X-Ratelimit-Remaining / X-Ratelimit-Reset: Ist das Kontingent aufgebraucht, wird bis zum
Reset nicht mehr gefragt (BlockedError -> Pause der Quelle).
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .core import BlockedError, Result, SourceUnavailable
from .i18n import t
from .providers import _SX_RANGES, Provider, _json, bot_ua, relevant, time_window

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"
_REDDIT_HOSTS = ("reddit.com", "redd.it", "redditmedia.com")


def reddit_window(days: int | None, kind: str) -> str:
    """Reddit-Parameter t: kleinstes Fenster (day/week/month/year - dieselben Namen wie bei SearXNG), das
    `days` Tage abdeckt, sonst "all"."""
    if days is None:            # ohne Zeitfilter: alles (News sind ohnehin "neueste zuerst" sortiert)
        return "all"
    return time_window(max(days, 1), _SX_RANGES) or "all"


def _is_reddit_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _REDDIT_HOSTS)


def _excerpt(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


class Reddit(Provider):
    """Reddit-Suche über die offizielle API (OAuth, App-only). Kostenlos für "script"-Apps, ~100 Anfragen/min."""
    name = "reddit"
    kinds = ("web", "news")
    min_interval = 1.0
    concurrency = 1
    base_cooldown = 600            # Reddits Rate-Limit-Fenster ist 10 min lang
    sparse = True                  # für viele Anfragen legitim leer
    scraped = False
    paid = False
    via_proxy = False              # offizielle API mit Zugangsdaten: nicht über fremde Proxys
    max_native_days = 366          # t=day|week|month|year; länger -> t=all und genaue Nachfilterung
    time_windows = (1, 7, 31, 366)

    def __init__(self) -> None:
        super().__init__()
        self._token: str | None = None
        self._token_until = 0.0            # time.time(), ab der das Token als abgelaufen gilt
        self._exhausted_until = 0.0        # Rate-Limit laut Kopfzeilen aufgebraucht bis ...

    @property
    def available(self) -> bool:
        return bool(os.environ.get("REDDIT_CLIENT_ID", "").strip()
                    and os.environ.get("REDDIT_CLIENT_SECRET", "").strip())

    @staticmethod
    def user_agent() -> str:
        """Reddit verlangt einen beschreibenden User-Agent; REDDIT_USER_AGENT überschreibt ihn
        (empfohlen: "<plattform>:<app-id>:<version> (by /u/<name>)")."""
        return os.environ.get("REDDIT_USER_AGENT", "").strip() or bot_ua()

    # ------------------------------------------------------------------ Token

    async def _get_token(self, client: httpx.AsyncClient, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_until:
            return self._token
        self._token = None
        cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
        r = await self._send(client, "POST", TOKEN_URL, auth=(cid, secret),
                             data={"grant_type": "client_credentials"})
        if r.status_code in (400, 401, 403):
            # falsche Zugangsdaten ändern sich nicht von selbst -> als Sperre behandeln (lange Pause)
            raise BlockedError(t("reddit: Token abgelehnt (HTTP {code}) - REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET "
                                 "prüfen (App-Typ \"script\")",
                                 "reddit: token rejected (HTTP {code}) - check REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET "
                                 "(app type \"script\")").format(code=r.status_code))
        self._classify(r)
        data = _json(r, self.name)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            err = data.get("error") if isinstance(data, dict) else None
            if err:     # z.B. "unauthorized_client", "unsupported_grant_type": Konfigurationsfehler
                raise BlockedError(t("reddit: Token abgelehnt ({err}) - App-Typ \"script\" verwenden",
                                     "reddit: token rejected ({err}) - use app type \"script\"").format(err=err))
            raise SourceUnavailable(t("reddit: Token-Antwort ohne access_token",
                                      "reddit: token response without access_token"))
        try:
            expires = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires = 3600.0
        # kurz vor Ablauf erneuern (10 %, höchstens 5 min Puffer)
        self._token, self._token_until = token, time.time() + max(30.0, expires - min(300.0, expires * 0.1))
        return token

    # ------------------------------------------------------------------ HTTP

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
        headers = {"User-Agent": self.user_agent(), "Accept": "application/json", **kw.pop("headers", {})}
        try:
            return await client.request(method, url, headers=headers, timeout=self.timeout, **kw)
        except httpx.TimeoutException as e:
            raise SourceUnavailable(t("reddit: Zeitüberschreitung ({e})", "reddit: timeout ({e})").format(
                e=type(e).__name__)) from None
        except httpx.TransportError as e:
            raise SourceUnavailable(t("reddit: nicht erreichbar ({e})", "reddit: not reachable ({e})").format(
                e=e)) from None

    def _note_ratelimit(self, resp: httpx.Response) -> None:
        """X-Ratelimit-Remaining/-Reset merken: bei 0 bis zum Reset nicht mehr fragen."""
        try:
            remaining = float(resp.headers["x-ratelimit-remaining"])
            reset = float(resp.headers.get("x-ratelimit-reset") or 60)
        except (KeyError, ValueError):
            return
        if remaining < 1:
            self._exhausted_until = time.time() + max(1.0, reset)

    def _classify(self, resp: httpx.Response) -> None:
        code = resp.status_code
        if code == 429:
            reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after") or "?"
            raise BlockedError(t("reddit: HTTP 429 - Rate-Limit, Reset in {s}s",
                                 "reddit: HTTP 429 - rate limit, reset in {s}s").format(s=reset))
        if code == 403:
            raise BlockedError(t("reddit: HTTP 403 - Zugriff verweigert", "reddit: HTTP 403 - access denied"))
        if code >= 500:
            raise SourceUnavailable(t("reddit: HTTP {code}", "reddit: HTTP {code}").format(code=code))
        if code >= 400:
            raise SourceUnavailable(t("reddit: HTTP {code}: {body}", "reddit: HTTP {code}: {body}").format(
                code=code, body=resp.text[:120]))

    # ------------------------------------------------------------------ Suche

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        wait = self._exhausted_until - time.time()
        if wait > 0:
            raise BlockedError(t("reddit: Rate-Limit aufgebraucht (X-Ratelimit-Remaining 0), Reset in {s}s",
                                 "reddit: rate limit used up (X-Ratelimit-Remaining 0), reset in {s}s"
                                 ).format(s=f"{wait:.0f}"))
        params: dict[str, str | int] = {
            "q": query, "sort": "new" if kind == "news" else "relevance", "t": reddit_window(days, kind),
            "limit": min(max(n, 1), 100), "type": "link", "raw_json": 1}
        reused = bool(self._token) and time.time() < self._token_until
        token = await self._get_token(client)
        r = await self._send(client, "GET", SEARCH_URL, params=params, headers={"Authorization": f"bearer {token}"})
        # Token abgelaufen/widerrufen -> genau einmal erneuern. Reddit antwortet auf ungültige Tokens
        # teils nicht mit 401, sondern mit seiner 403-Sperrseite (HTML) -> auch dann, aber nur bei
        # einem wiederverwendeten Token (ein frisches ist nicht das Problem)
        if r.status_code == 401 or (r.status_code == 403 and reused
                                    and "html" in r.headers.get("content-type", "").lower()):
            token = await self._get_token(client, force=True)
            r = await self._send(client, "GET", SEARCH_URL, params=params,
                                 headers={"Authorization": f"bearer {token}"})
            if r.status_code == 401:
                self._token = None
                raise BlockedError(t("reddit: HTTP 401 trotz neuem Token - Zugangsdaten prüfen",
                                     "reddit: HTTP 401 despite a fresh token - check the credentials"))
        self._note_ratelimit(r)
        self._classify(r)
        data = _json(r, self.name)
        children = ((data.get("data") or {}).get("children") or []) if isinstance(data, dict) else []
        since = time.time() - days * 86400 if days is not None else None
        out: list[Result] = []
        for child in children:
            if len(out) >= n:
                break
            if not isinstance(child, dict) or child.get("kind") not in (None, "t3"):
                continue
            d = child.get("data")
            if not isinstance(d, dict) or d.get("over_18"):        # NSFW nie liefern
                continue
            created = d.get("created_utc")
            if not isinstance(created, (int, float)) or isinstance(created, bool):
                created = None
            if since is not None and created is not None and created < since:
                continue                   # Zeitfenster von Reddit ist gröber als days
            # Reddit sucht unscharf (ein Wort irgendwo im Beitrag genügt) - wie bei HN/GDELT selbst prüfen
            if not relevant(query, f"{d.get('title') or ''} {d.get('selftext') or ''}"):
                continue
            if (res := self._to_result(d, created, len(out) + 1)) is not None:
                out.append(res)
        return out

    def _to_result(self, d: dict, created: float | None, rank: int) -> Result | None:
        permalink = d.get("permalink")
        permalink = f"https://www.reddit.com{permalink}" if isinstance(permalink, str) and permalink.startswith("/") \
            else ""
        external = d.get("url_overridden_by_dest") or d.get("url") or ""
        is_link = (not d.get("is_self") and isinstance(external, str)
                   and external.lower().startswith(("http://", "https://")) and not _is_reddit_host(external))
        url = external if is_link else permalink
        if not url:
            return None
        sub = d.get("subreddit") or "?"
        text = _excerpt(d.get("selftext") or "") if isinstance(d.get("selftext"), str) else ""
        snippet = text or f"r/{sub} · {d.get('score', 0)} points · {d.get('num_comments', 0)} comments"
        published = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="seconds")
                     if created is not None else None)
        return Result(title=d.get("title") or "", url=url, snippet=snippet, source=self.name, rank=rank,
                      published=published)
