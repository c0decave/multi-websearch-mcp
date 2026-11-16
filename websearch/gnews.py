"""Google-News-Links (news.google.com/rss/articles/<id>) in die URL des Herausgebers auflösen.

Zwei Formate der Artikel-ID (base64url-kodiertes Protobuf):
  * alt: das Protobuf enthält die Ziel-URL direkt -> offline dekodierbar, kein Request
  * neu ("AU_yqL..."): die URL steht nur bei Google. Verfahren wie im Open-Source-Paket
    `googlenewsdecoder`:
      1. GET https://news.google.com/rss/articles/<id>  -> im <c-wiz>-Block stehen Signatur
         (data-n-a-sg) und Zeitstempel (data-n-a-ts)
      2. POST https://news.google.com/_/DotsSplashUi/data/batchexecute mit dem RPC "Fbv4je"
         ("garturlreq") -> Antwort enthält ["garturlres", "<URL>", ...]
    Das sind zwei Requests an news.google.com pro Link.

Schutz vor Sperren (Grundprinzip des Projekts: vermeiden statt lösen):
  * Tempo hostweit über denselben Slot wie die Suchquelle "google_news" (ein Budget für alle
    Requests an news.google.com, alle Prozesse) - DECODE_INTERVAL s Abstand pro Request
  * Ergebnis im StateStore gecacht (bis zu 7 Tage, dann räumt der StateStore auf); Fehlschläge, die
    keine Sperre sind (unbekannte/kaputte ID, HTTP 4xx, Formatänderung), FAIL_TTL s lang - so kostet
    ein kaputter Link nicht bei jedem Versuch zwei Requests. Sperren regelt der CircuitBreaker.
  * Consent-Seite, /sorry/-Captcha, 429/403 -> FetchError und hostweite Pause (CircuitBreaker
    "gnews_decode", ab 15 min wachsend); keine Wiederholungen
  * Auf das Consent-Banner (EU-IPs) antworten wir wie ein Browser nach "Alle ablehnen":
    Cookie SOCS=CAI. Nur notwendige Cookies, keine Zustimmung zu Tracking.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import re
import sqlite3
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from .core import CircuitBreaker, RateLimited, RateLimiter, SourceUnavailable, StateStore, env_flag, log
from .fetch import FetchError
from .i18n import t
from .providers import BROWSER_UA

HOST = "news.google.com"
ARTICLE_URL = "https://news.google.com/rss/articles/{id}"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
SLOT_NAME = "google_news"          # = Tempo-Slot der Suchquelle GoogleNewsRSS (hostweit geteilt)
BREAKER_NAME = "gnews_decode"
DECODE_INTERVAL = 2.0              # Sekunden zwischen zwei Requests an news.google.com
SLOT_BUDGET = 10.0                 # länger wird nicht auf einen Slot gewartet
CACHE_TTL = 365 * 86400            # aufgelöste Links ändern sich nie (StateStore räumt nach 7 Tagen auf)
FAIL_TTL = 3600                    # so lange wird ein Fehlschlag (keine Sperre) nicht erneut versucht
MAX_PAGE_BYTES = 4 * 1024 * 1024   # Artikelseite ist ~600 KB, die Signatur steht am Ende
TIMEOUT = 15.0
CONSENT_COOKIE = "SOCS=CAI"        # Antwort "Alle ablehnen" auf das Consent-Banner

# Kontext-Block des garturlreq-RPC (so schickt ihn die Google-News-Webseite)
_GARTURLREQ_CTX = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                   "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0]
_DIV_WITH_SIG = re.compile(r"<div\b[^>]*\bdata-n-a-sg=[^>]*>", re.I)
_ATTR = re.compile(r"""\b(data-n-a-sg|data-n-a-ts|data-n-a-id)\s*=\s*["']([^"']*)["']""", re.I)
_BLOCK_MARKERS = ("unusual traffic", "/sorry/index", "captcha", "consent.google.com")


def enabled() -> bool:
    """WEBSEARCH_GNEWS_DECODE=0 schaltet das Auflösen ab (dann lehnt fetch_url Google-News-Links ab)."""
    return env_flag("WEBSEARCH_GNEWS_DECODE", True)


def is_gnews_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == HOST or host.endswith("." + HOST)


def article_id(url: str) -> str | None:
    """ID aus .../rss/articles/<id>, .../articles/<id> oder .../read/<id>; sonst None."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if not is_gnews_host(p.hostname):
        return None
    parts = [s for s in p.path.split("/") if s]
    if len(parts) >= 2 and parts[-2] in ("articles", "read") and re.fullmatch(r"[A-Za-z0-9_-]+", parts[-1]):
        return parts[-1]
    return None


# --------------------------------------------------------------------------- offline (altes Format)

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("varint")
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _proto_strings(buf: bytes) -> list[bytes]:
    """Alle length-delimited Felder der obersten Ebene (wire type 2), in Reihenfolge."""
    out, i = [], 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        wire = tag & 7
        if wire == 0:
            _, i = _varint(buf, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            size, i = _varint(buf, i)
            if i + size > len(buf):
                raise ValueError("length")
            out.append(buf[i:i + size])
            i += size
        else:
            raise ValueError("wire type")
    return out


def _is_new_style(value: bytes) -> bool:
    return value.startswith(b"AU_yqL")


def decode_offline(art_id: str) -> str | None:
    """Altes Format -> Ziel-URL; neues Format ("AU_yqL...") -> None (braucht Google).
    Unbekanntes/kaputtes Format -> FetchError (dann auch nicht online versuchen)."""
    try:
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
    except (binascii.Error, ValueError):
        raw = b""
    try:
        fields = _proto_strings(raw)
    except ValueError:              # manche alten IDs haben falsche Längenangaben -> direkt nach URL suchen
        fields = []
        if m := re.search(rb"https?://[\x21-\x7e]+", raw):
            fields = [m.group(0)]
    for value in fields:
        if _is_new_style(value):
            return None
        if value.startswith((b"http://", b"https://")):
            return value.decode("utf-8", errors="replace")
    raise FetchError(t("Google-News-Link hat ein unbekanntes Format - bitte die direkte Artikel-URL nutzen",
                       "Google News link has an unknown format - please use the direct article URL"))


# --------------------------------------------------------------------------- online (neues Format)

def parse_signature(html: str) -> tuple[str, str] | None:
    """(Signatur, Zeitstempel) aus dem <div data-n-a-sg=... data-n-a-ts=...> der Artikelseite."""
    for m in _DIV_WITH_SIG.finditer(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(m.group(0))}
        if attrs.get("data-n-a-sg") and attrs.get("data-n-a-ts"):
            return attrs["data-n-a-sg"], attrs["data-n-a-ts"]
    return None


def batch_body(art_id: str, timestamp: str, signature: str) -> str:
    # nur ASCII-Ziffern (und nicht zu lang): "²".isdigit() ist True, int("²") wirft aber ValueError
    ts: int | str = int(timestamp) if re.fullmatch(r"[0-9]{1,18}", timestamp) else timestamp
    inner = ["garturlreq", _GARTURLREQ_CTX, art_id, ts, signature]
    envelope = [[["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "generic"]]]
    return "f.req=" + quote(json.dumps(envelope, separators=(",", ":")))


def _json_rows(text: str) -> list:
    """batchexecute-Antwort: ")]}'" + Leerzeile + JSON (teils in Blöcken mit Längenzeilen)."""
    body = text.lstrip()
    if body.startswith(")]}'"):
        body = body[4:]
    with contextlib.suppress(ValueError, RecursionError):
        rows = json.loads(body)
        return rows if isinstance(rows, list) else []
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("["):
            with contextlib.suppress(ValueError, RecursionError):
                chunk = json.loads(line)
                if isinstance(chunk, list):
                    rows.extend(chunk)
    return rows


def parse_batch(text: str) -> str | None:
    """Ziel-URL aus der batchexecute-Antwort (["wrb.fr","Fbv4je","[\\"garturlres\\",\\"URL\\",1]",...])."""
    for row in _json_rows(text):
        if not (isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr" and row[1] == "Fbv4je"):
            continue
        payload = row[2]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, RecursionError):
                continue
        if isinstance(payload, list) and len(payload) >= 2 and payload[0] == "garturlres" \
                and isinstance(payload[1], str):
            return payload[1]
    return None


def _locale(url: str) -> dict[str, str]:
    """hl/gl/ceid aus dem Original-Link übernehmen (Google liefert die Seite sonst ggf. nicht)."""
    qs = parse_qs(urlsplit(url).query)
    return {"hl": qs.get("hl", ["en-US"])[0], "gl": qs.get("gl", ["US"])[0], "ceid": qs.get("ceid", ["US:en"])[0]}


def _blocked_reason(resp: httpx.Response, body: str = "") -> str | None:
    """Sperr-/Consent-Seite erkennen. -> Grund oder None."""
    code = resp.status_code
    if code in (403, 429):
        return f"HTTP {code}"
    if resp.is_redirect:
        loc = resp.headers.get("location", "")
        host = (urlsplit(urljoin(str(resp.url), loc)).hostname or "").lower()
        if host.startswith("consent.") or "/sorry/" in loc:
            return t("Consent-/Captcha-Seite ({loc})", "consent/captcha page ({loc})").format(loc=loc[:80])
    if body and "html" in resp.headers.get("content-type", "").lower() and "data-n-a-sg" not in body:
        head = body[:6000].lower()
        if any(m in head for m in _BLOCK_MARKERS):
            return t("Consent-/Captcha-Seite", "consent/captcha page")
    return None


class GNewsDecoder:
    """Löst Google-News-Links auf; Tempo, Pause und Cache hostweit über den StateStore."""

    def __init__(self, store: StateStore | None = None, interval: float = DECODE_INTERVAL):
        self.store = store
        self.limiter = RateLimiter(interval, concurrency=1)
        self.breaker = CircuitBreaker(BREAKER_NAME, base_cooldown=900, store=store)
        if store is not None:
            self.limiter.attach(store, SLOT_NAME)

    def _cache_get(self, art_id: str) -> str | None:
        if self.store is None:
            return None
        try:
            hit = self.store.get(StateStore.key("gnews", art_id), ttl=CACHE_TTL)
        except sqlite3.Error:
            return None
        return hit if isinstance(hit, str) else None

    def _cache_set(self, art_id: str, target: str) -> None:
        if self.store is not None:
            with contextlib.suppress(sqlite3.Error):
                self.store.set(StateStore.key("gnews", art_id), target)

    def _fail_get(self, art_id: str) -> dict | None:
        if self.store is None:
            return None
        try:
            hit = self.store.get(StateStore.key("gnews_fail", art_id), ttl=FAIL_TTL)
        except sqlite3.Error:
            return None
        return hit if isinstance(hit, dict) and isinstance(hit.get("kind"), str) else None

    def _fail(self, art_id: str, kind: str, **kw) -> FetchError:
        """Fehlschlag (keine Sperre) merken und als FetchError liefern."""
        if self.store is not None:
            with contextlib.suppress(sqlite3.Error, TypeError, ValueError):
                self.store.set(StateStore.key("gnews_fail", art_id), {"kind": kind, **kw})
        return FetchError(_failure_message(kind, **kw))

    def _blocked(self, reason: str) -> FetchError:
        with contextlib.suppress(Exception):
            self.breaker.trip(f"news.google.com: {reason}")
        return FetchError(t(
            "Google News sperrt das Auflösen des Links gerade ({reason}) - Pause {s}s. Bitte die direkte "
            "Artikel-URL nutzen (z.B. aus einem anderen Treffer derselben Schlagzeile) oder nach der "
            "Schlagzeile suchen.",
            "Google News is currently blocking link resolution ({reason}) - paused for {s}s. Please use the "
            "direct article URL (e.g. from another result with the same headline) or search for the headline."
        ).format(reason=reason, s=self.breaker.retry_in()))

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kw) -> tuple[httpx.Response, str]:
        """Ein Request an news.google.com im hostweiten Tempo-Slot (ohne Weiterleitungen, Größe begrenzt)."""
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8", "Cookie": CONSENT_COOKIE,
                   **kw.pop("headers", {})}
        try:
            async with self.limiter.slot(budget=SLOT_BUDGET):
                req = client.build_request(method, url, headers=headers, timeout=TIMEOUT, **kw)
                resp = await client.send(req, stream=True, follow_redirects=False)
                try:
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            raise FetchError(t("Antwort von Google News zu groß",
                                               "response from Google News too large"))
                        chunks.append(chunk)
                finally:
                    await resp.aclose()
        except RateLimited as e:
            raise FetchError(t("zu viele Abrufe bei news.google.com, bitte in {s}s erneut versuchen",
                               "too many requests to news.google.com, please retry in {s}s"
                               ).format(s=f"{e.wait:.0f}")) from None
        except (httpx.HTTPError, SourceUnavailable) as e:      # SourceUnavailable: Antwort > 10 MB (Engine)
            raise FetchError(t("Google News nicht erreichbar: {e}", "Google News not reachable: {e}"
                               ).format(e=e)) from None
        try:
            return resp, b"".join(chunks).decode(resp.charset_encoding or "utf-8", errors="replace")
        except LookupError:      # unbekannter Zeichensatz im Content-Type
            return resp, b"".join(chunks).decode("utf-8", errors="replace")

    async def decode(self, client: httpx.AsyncClient, url: str) -> str:
        """Google-News-Link -> URL des Herausgebers. Wirft FetchError (lesbar, ohne Wiederholungen)."""
        art_id = article_id(url)
        if art_id is None:
            raise FetchError(t("kein Google-News-Artikellink: {url}", "not a Google News article link: {url}"
                               ).format(url=url[:200]))
        target = decode_offline(art_id)
        if target is None:
            target = self._cache_get(art_id)
        if target is None:
            if (fail := self._fail_get(art_id)) is not None:
                msg = _failure_message(fail["kind"], code=str(fail.get("code", "?")), u=str(fail.get("u", "")))
                raise FetchError(msg + t(
                    " (Ergebnis eines früheren Versuchs; neuer Versuch frühestens in {m} min)",
                    " (result of an earlier attempt; next attempt in {m} min at the earliest)"
                ).format(m=max(1, FAIL_TTL // 60)))
            target = await self._decode_online(client, url, art_id)
        return _valid_target(target)

    async def _decode_online(self, client: httpx.AsyncClient, url: str, art_id: str) -> str:
        if self.breaker.is_open():
            raise FetchError(t(
                "Google News hat das Auflösen von Links gesperrt - Pause noch {s}s ({why}). Bitte die direkte "
                "Artikel-URL nutzen oder nach der Schlagzeile suchen.",
                "Google News has blocked link resolution - paused for another {s}s ({why}). Please use the "
                "direct article URL or search for the headline.").format(s=self.breaker.retry_in(),
                                                                         why=self.breaker.last_error))
        page_url = ARTICLE_URL.format(id=art_id)
        resp, html = await self._request(client, "GET", page_url, params=_locale(url))
        if reason := _blocked_reason(resp, html):
            raise self._blocked(reason)
        if resp.status_code >= 400 or resp.is_redirect:
            if _transient(resp):
                raise FetchError(_failure_message("page_http", code=resp.status_code))
            raise self._fail(art_id, "page_http", code=resp.status_code)
        sig = parse_signature(html)
        if sig is None:
            raise self._fail(art_id, "no_signature")
        signature, timestamp = sig
        resp, text = await self._request(
            client, "POST", BATCH_URL, content=batch_body(art_id, timestamp, signature),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "Origin": "https://news.google.com", "Referer": "https://news.google.com/"})
        if reason := _blocked_reason(resp, text):
            raise self._blocked(reason)
        if resp.status_code >= 400 or resp.is_redirect:
            if _transient(resp):
                raise FetchError(_failure_message("batch_http", code=resp.status_code))
            raise self._fail(art_id, "batch_http", code=resp.status_code)
        target = parse_batch(text)
        if not target:
            raise self._fail(art_id, "no_target")
        try:
            target = _valid_target(target)
        except FetchError:
            raise self._fail(art_id, "bad_target", u=target.strip()[:200]) from None
        with contextlib.suppress(Exception):
            self.breaker.success()
        self._cache_set(art_id, target)
        log.debug(t("Google-News-Link %s -> %s", "Google News link %s -> %s"), art_id[:24], target)
        return target


def _transient(resp: httpx.Response) -> bool:
    """5xx = Störung bei Google: nicht als Fehlschlag merken (beim nächsten Mal kann es klappen)."""
    return resp.status_code >= 500


def _failure_message(kind: str, code: int | str = "?", u: str = "") -> str:
    """Meldung für einen Fehlschlag (auch für gemerkte: immer in der aktuellen Sprache)."""
    if kind == "page_http":
        return t("Google News antwortet mit HTTP {code} auf den Link",
                 "Google News answered the link with HTTP {code}").format(code=code)
    if kind == "batch_http":
        return t("Google News (batchexecute) antwortet mit HTTP {code}",
                 "Google News (batchexecute) answered with HTTP {code}").format(code=code)
    if kind == "no_signature":
        return t("Google-News-Seite ohne Signatur (data-n-a-sg) - Google hat das Format vermutlich geändert",
                 "Google News page without signature (data-n-a-sg) - Google probably changed the format")
    if kind == "bad_target":
        return t("Google News lieferte eine unbrauchbare Ziel-URL: {u}",
                 "Google News returned an unusable target URL: {u}").format(u=u)
    return t("Google News lieferte keine Ziel-URL für den Link", "Google News returned no target URL for the link")


def _valid_target(target: str) -> str:
    target = target.strip()
    try:
        p = urlsplit(target)
        ok = p.scheme in ("http", "https") and bool(p.hostname) and not is_gnews_host(p.hostname)
    except ValueError:               # z.B. "http://[::1" - nur FetchError soll hier herauskommen
        ok = False
    if not ok:
        raise FetchError(_failure_message("bad_target", u=target[:200]))
    return target
