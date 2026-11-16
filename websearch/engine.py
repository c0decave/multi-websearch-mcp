"""Orchestrierung: fragt Quellen parallel ab, hält Tempo/Sperren ein, mergt die Ergebnisse.

Stufen pro Suchart (TierPolicy, frei konfigurierbar):
  1. primary  - kostenlos; SearXNG deckt die großen Engines ab, dazu offizielle Feeds
  2. fallback - kostenlos; nur wenn Stufe 1 zu wenig liefert. Überspringt Engines, die SearXNG
                gerade als gesperrt meldet (sonst trifft man dieselbe Suchmaschine doppelt)
  3. paid     - optional mit API-Key; nur wenn fast nichts kam UND das an Fehlern/Sperren lag.
                Bezahlquellen werden der Reihe nach probiert, bis EINE normal antwortet (auch mit
                0 Treffern - eine zweite findet einen seltenen Begriff auch nicht); hostweite
                Tages-/Monatslimits (WEBSEARCH_PAID_DAILY_LIMIT / _MONTHLY_LIMIT) begrenzen die Kosten.
                Mit days nur Bezahlquellen, die genau auf diese Spanne filtern (_filters_exactly); ist eine
                Antwort nach dem Datumsfilter leer, wird die nächste probiert.

Jede Quelle ist isoliert: ein Fehler in einer Quelle (auch ein Bug, ein kaputter Cache-Eintrag, eine
gesperrte Datenbank oder eine fehlerhafte Antwortzeile) kostet nur deren Ergebnisse, nie die ganze
Suche. Wird eine Suche abgebrochen (z.B. MCP-Client-Timeout), werden auch alle ihre Quellen-Anfragen
abgebrochen.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import math
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Collection, Iterable, Sequence

import httpx

from .core import (NON_FAILURE_STATES, BlockedError, BudgetExceeded, InputError, PageResponse, RateLimited,
                   Result, SearchResponse, SourceInfo, SourceSkipped, SourceStatus, SourceUnavailable, StateStore,
                   env_int, env_path, is_benign, log, make_status, merge_rrf, normalize_date)
from .i18n import t
from .providers import Provider, default_providers, disabled_sources

if TYPE_CHECKING:
    from .fetch import Fetcher

KINDS = ("web", "news", "ref", "images", "videos")
TEXT_KINDS = ("web", "news", "ref")
MEDIA_KINDS = ("images", "videos")
MAX_DAYS = 3650

# Standard-Cache-Alter je Suchart (s). Nachrichten müssen frisch sein, Websuche ändert sich langsam.
DEFAULT_MAX_AGE = {"web": 6 * 3600, "news": 20 * 60, "ref": 24 * 3600, "images": 24 * 3600, "videos": 6 * 3600}
# Quellen werden mit einer festen Seitengröße abgefragt (dann geschnitten): n=8 und n=10 teilen
# sich so einen Cache-Eintrag und damit einen Upstream-Request.
_PAGE_SIZES = (10, 20, 50, 100)
_TIERS = ("primary", "fallback", "paid")
# Anteil des Zeitbudgets, den Stufe 1 höchstens bekommt, wenn es Fallbacks gibt: eine hängende
# Quelle (z.B. SearXNG) darf die Fallbacks nicht aushungern
_PRIMARY_SHARE = 0.6


@dataclass(frozen=True, eq=False)
class TierPolicy:
    """Welche Quellen in welcher Stufe gefragt werden - je Suchart, Reihenfolge = Präferenz.

    Wird beim Anlegen geprüft (nur Sucharten aus KINDS, Listen von Quellnamen) und ist danach
    unveränderlich (auch die inneren Listen)."""
    primary: Mapping[str, Sequence[str]]
    fallback: Mapping[str, Sequence[str]] = field(default_factory=dict)
    paid: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for tier in _TIERS:
            value = getattr(self, tier)
            if not isinstance(value, Mapping):
                raise InputError(t(f"TierPolicy.{tier} muss ein Dict Suchart -> Quellenliste sein",
                                   f"TierPolicy.{tier} must be a dict kind -> list of sources"))
            frozen = {}
            for kind, names in value.items():
                if kind not in KINDS:
                    raise InputError(t(f"TierPolicy.{tier}: unbekannte Suchart {kind!r} (erlaubt: {list(KINDS)})",
                                       f"TierPolicy.{tier}: unknown kind {kind!r} (allowed: {list(KINDS)})"))
                if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) and n for n in names):
                    raise InputError(t(f"TierPolicy.{tier}[{kind!r}] muss eine Liste von Quellnamen sein",
                                       f"TierPolicy.{tier}[{kind!r}] must be a list of source names"))
                frozen[kind] = tuple(names)
            object.__setattr__(self, tier, MappingProxyType(frozen))

    def names(self) -> set[str]:
        return {n for tier in _TIERS for lst in getattr(self, tier).values() for n in lst}

    def for_kind(self, kind: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return (tuple(self.primary.get(kind, ())), tuple(self.fallback.get(kind, ())),
                tuple(self.paid.get(kind, ())))


DEFAULT_POLICY = TierPolicy(
    primary={
        # reddit greift nur mit eigenen (kostenlosen) OAuth-Zugangsdaten, sonst wird es übersprungen
        "web":  ["searxng", "reddit"],
        "news": ["searxng", "google_news", "bing_rss", "hackernews", "gdelt", "reddit"],
        "ref":  ["wikipedia", "searxng"],
        "images": ["searxng"],
        "videos": ["searxng"],
    },
    fallback={
        # ddgs:startpage / ddgs:mojeek bewusst nicht: beide nutzen seit 2026 ein Proof-of-Work-Captcha
        # (SearXNG führt sie als "inactive"). Explizit per sources=[...] weiter nutzbar.
        "web":  ["bing_rss", "ddgs:yahoo", "ddgs:duckduckgo", "ddgs:brave", "marginalia"],
        "news": ["ddgs:yahoo", "ddgs:duckduckgo"],
        "ref":  ["bing_rss", "ddgs:yahoo"],
        "images": ["ddgs:duckduckgo"],
        "videos": ["ddgs:duckduckgo"],
    },
    # Reihenfolge: erst Quellen mit monatlichem Gratiskontingent, dann die günstigsten Bezahlquellen.
    # Ohne Brave-Freiguthaben: perplexity vor brave_api setzen (eigene TierPolicy) oder Key weglassen.
    # Bild-/Videosuche: keine Bezahlquelle (die Such-APIs liefern dafür keine Medien-Treffer)
    paid={k: ["exa", "linkup", "tavily", "brave_api", "perplexity", "serper"] for k in TEXT_KINDS},
)


MAX_RESPONSE_BYTES = 10 * 1024 * 1024


async def _size_guard(resp: httpx.Response) -> None:
    """Antworten mit angekündigter Größe über 10 MB gar nicht erst lesen (Speicherschutz)."""
    try:
        size = int(resp.headers.get("content-length", "0"))
    except ValueError:
        size = 0
    if size > MAX_RESPONSE_BYTES:
        await resp.aclose()
        raise SourceUnavailable(t(f"Antwort zu groß ({size // 1024 // 1024} MB) von {resp.request.url.host}",
                                  f"response too large ({size // 1024 // 1024} MB) from {resp.request.url.host}"))


class _CappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int):
        self._inner, self._limit = inner, limit

    async def __aiter__(self):
        seen = 0
        async for chunk in self._inner:
            seen += len(chunk)
            if seen > self._limit:
                raise SourceUnavailable(t(f"Antwort größer als {self._limit // 1024 // 1024} MB - abgebrochen",
                                          f"response larger than {self._limit // 1024 // 1024} MB - aborted"))
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx.AsyncHTTPTransport):
    """Wie der Standard-Transport, aber Antworten über MAX_RESPONSE_BYTES (übertragen) werden abgebrochen -
    auch ohne Content-Length (chunked)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handle_async_request(request)
        resp.stream = _CappedStream(resp.stream, MAX_RESPONSE_BYTES)  # type: ignore[arg-type]
        return resp


def _proxy_from_env() -> httpx.Proxy | None:
    url = os.environ.get("WEBSEARCH_PROXY", "").strip()
    if not url:
        return None
    try:
        proxy = httpx.Proxy(url)
    except (ValueError, TypeError) as e:
        raise InputError(t(f"WEBSEARCH_PROXY ist keine gültige Proxy-URL: {e}",
                           f"WEBSEARCH_PROXY is not a valid proxy URL: {e}")) from None
    if proxy.url.scheme.startswith("socks"):
        try:
            import socksio  # noqa: F401
        except ImportError:
            raise InputError(t("SOCKS-Proxy braucht das Paket 'socksio' (pip install 'httpx[socks]')",
                               "a SOCKS proxy needs the package 'socksio' (pip install 'httpx[socks]')")) from None
    return proxy


def _page_size(n: int) -> int:
    return next(s for s in _PAGE_SIZES if n <= s)




async def _cancel_all(tasks: Sequence[asyncio.Task]) -> None:
    """Alle unfertigen Tasks abbrechen und auf ihr Ende warten - auch wenn der Aufrufer währenddessen
    erneut abgebrochen wird (der Abbruch wird danach weitergereicht)."""
    pending = [t for t in tasks if not t.done()]
    for t in pending:
        t.cancel()
    cancelled_again = False
    while pending:
        try:
            await asyncio.wait(pending)
        except asyncio.CancelledError:
            cancelled_again = True
        pending = [t for t in pending if not t.done()]
    for t in tasks:   # Ergebnisse/Fehler abholen, damit asyncio nicht "never retrieved" meldet
        if t.done() and not t.cancelled():
            t.exception()
    if cancelled_again:
        raise asyncio.CancelledError


def _unknown_sources_error(unknown: Iterable[str], known: Iterable[str]) -> InputError:
    known = sorted(known)
    hints = [t(f"{u!r} (gemeint: {m[0]!r}?)", f"{u!r} (did you mean {m[0]!r}?)")
             if (m := difflib.get_close_matches(u, known, 1)) else repr(u) for u in unknown]
    return InputError(t(f"Unbekannte Quellen: {', '.join(hints)}. Verfügbar: {', '.join(known)}",
                        f"unknown sources: {', '.join(hints)}. Available: {', '.join(known)}"))


class SearchEngine:
    """Parallele Suche über mehrere Quellen. Immer als `async with SearchEngine() as eng:` nutzen;
    eine Instanz ist nach dem Schließen verbraucht (für einen neuen Lauf neu anlegen).

    Args:
        providers: Name -> Provider (Standard: alle eingebauten, ohne WEBSEARCH_DISABLE). Eine
            Provider-Instanz gehört genau einer Engine (Tempo/Sperren hängen an ihr).
        policy: Stufen je Suchart (Standard: DEFAULT_POLICY). Unbekannte Quellnamen -> InputError,
            außer die Quelle ist per WEBSEARCH_DISABLE abgeschaltet.
        cache_path: SQLite-Datei für Cache + hostweiten Zustand (Standard: WEBSEARCH_CACHE
            oder ~/.cache/websearch/cache.db). Alle Prozesse eines Hosts sollten dieselbe nutzen.
        max_wait: Wie lange eine Quelle höchstens auf ihren nächsten Tempo-Slot warten darf;
            danach wird sie für diese Suche übersprungen (state "rate_limited").
        transport: optionaler httpx-Transport (Tests).

    Umgebung: WEBSEARCH_PROXY (Proxy für alle httpx-Quellen und fetch), WEBSEARCH_PAID_DAILY_LIMIT
    (Standard 100) und WEBSEARCH_PAID_MONTHLY_LIMIT (Standard 1000) für die Bezahlquellen.
    """

    def __init__(self, providers: dict[str, Provider] | None = None, policy: TierPolicy | None = None,
                 cache_path: str | Path | None = None, max_wait: float = 8.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.providers = providers if providers is not None else default_providers()
        self.policy = policy or DEFAULT_POLICY
        # Tippfehler in der Stufen-Konfiguration sofort melden (nicht still eine Quelle verlieren).
        # Ausnahme: eigene providers mit Standard-Policy (Tests, Teilmengen) - dann zählt, was da ist.
        unknown = self.policy.names() - set(self.providers) - disabled_sources()
        if unknown and (policy is not None or providers is None):
            raise InputError(t(f"Stufen nennen unbekannte Quellen: {sorted(unknown)}",
                               f"tiers name unknown sources: {sorted(unknown)}"))
        self.cache_path = Path(cache_path or env_path("WEBSEARCH_CACHE",
                                                      str(Path.home() / ".cache/websearch/cache.db")))
        self.max_wait = max_wait
        self.paid_daily_limit = env_int("WEBSEARCH_PAID_DAILY_LIMIT", 100)
        self.paid_monthly_limit = env_int("WEBSEARCH_PAID_MONTHLY_LIMIT", 1000)
        self._transport = transport
        self._store: StateStore | None = None
        self._client: httpx.AsyncClient | None = None
        self._proxied: httpx.AsyncClient | None = None
        self._fetcher: Fetcher | None = None
        self._stats: dict[tuple[str, str], list[int]] = {}      # (Quelle, Zustand) -> [Anzahl, Summe ms]
        self._stats_flushed = time.monotonic()
        self._days_ok: dict[type, bool] = {}
        self._state = "new"            # new -> open -> closed

    # ------------------------------------------------------------------ Lebenszyklus

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    @property
    def store(self) -> StateStore:
        if self._store is None:
            raise RuntimeError(t("SearchEngine bitte mit 'async with SearchEngine() as eng:' verwenden",
                                 "please use SearchEngine as 'async with SearchEngine() as eng:'"))
        return self._store

    async def __aenter__(self) -> "SearchEngine":
        if self._state != "new":
            raise RuntimeError(t("SearchEngine kann nur einmal geöffnet werden - bitte neu anlegen",
                                 "a SearchEngine can only be opened once - please create a new one"))
        self._store = StateStore(self.cache_path)
        try:
            for name, p in self.providers.items():
                p.breaker.attach(self._store, name)       # Sperrzustand hostweit teilen
                p.limiter.attach(self._store, name)       # Tempo hostweit einhalten
            limits = httpx.Limits(max_connections=50)
            # Direkt: eigene SearXNG-Instanz und Bezahl-APIs (nie über einen fremden Proxy)
            # trust_env=False: auch HTTP(S)_PROXY/ALL_PROXY aus der Umgebung nicht für die eigene Instanz nutzen
            self._client = httpx.AsyncClient(follow_redirects=True, transport=self._transport or _CappedTransport(),
                                             limits=limits, trust_env=False, event_hooks={"response": [_size_guard]})
            # Über WEBSEARCH_PROXY (falls gesetzt): öffentliche Quellen und fetch
            proxy = _proxy_from_env()
            # trust_env=False auch hier: sonst gewinnt ein HTTPS_PROXY aus der Umgebung gegen WEBSEARCH_PROXY
            self._proxied = self._client if not proxy else httpx.AsyncClient(
                follow_redirects=True, limits=limits, trust_env=False,
                transport=self._transport or _CappedTransport(proxy=proxy), event_hooks={"response": [_size_guard]})
        except BaseException:   # halb geöffnet: nichts offen zurücklassen
            self._store.close()
            self._store = None
            raise
        self._state = "open"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        if self._proxied is not None and self._proxied is not self._client:
            await self._proxied.aclose()
        self._proxied = None
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._store:
            self.flush_stats()
            self._store.close()

    def _require_open(self) -> httpx.AsyncClient:
        if self._state != "open" or self._client is None:
            raise RuntimeError(t("SearchEngine bitte mit 'async with SearchEngine() as eng:' verwenden "
                               "(und nach dem Schließen nicht weiterverwenden)",
                                 "please use SearchEngine as 'async with SearchEngine() as eng:' (and do not reuse it after closing)"))
        return self._client

    # ------------------------------------------------------------------ einzelne Quelle

    def _cache_get(self, name: str, key: str, max_age: float) -> tuple[list[Result], dict] | None:
        try:
            hit = self.store.get(key, ttl=max_age)
            if hit is None:
                return None
            return [Result.from_dict(r) for r in hit["results"]], dict(hit.get("meta") or {})
        except Exception as e:   # kaputter/fremder Eintrag oder DB-Fehler -> wie Cache-Miss behandeln
            if self.store.degraded:          # bekannt und schon einmal gewarnt -> nicht pro Quelle wiederholen
                log.debug(t("Cache für %s übersprungen (Zustandsdatei degradiert)",
                            "cache for %s skipped (state file degraded)"), name)
            else:
                log.warning(t("Cache-Eintrag für %s unbrauchbar (%s) - frage neu ab",
                              "cache entry for %s unusable (%s) - querying again"), name, e)
            return None

    async def _query_source(self, name: str, query: str, n: int, lang: str, kind: str,
                            max_age: float, budget: float, allow_paid: bool = True, days: int | None = None
                            ) -> tuple[list[Result], SourceStatus]:
        """Fragt EINE Quelle. Wirft nie (außer bei Abbruch) - jeder Fehler wird zu einem SourceStatus."""
        t0 = time.monotonic()
        results, status = await self._query_source_inner(name, query, n, lang, kind, max_age, budget, allow_paid,
                                                         days)
        status["ms"] = int((time.monotonic() - t0) * 1000)
        self._count(name, status["state"], status["ms"])
        return results, status

    # ------------------------------------------------------------------ Zähler (Metriken)

    STATS_FLUSH_S = 2.0

    def _count(self, name: str, state: str, ms: int) -> None:
        """Zähler für /metrics: erst im Speicher sammeln, höchstens alle STATS_FLUSH_S Sekunden in einer
        Transaktion hostweit schreiben (statt eines Schreibzugriffs pro Quelle und Suche)."""
        entry = self._stats.setdefault((name, state), [0, 0])
        entry[0] += 1
        entry[1] += max(0, int(ms))
        if time.monotonic() - self._stats_flushed >= self.STATS_FLUSH_S:
            self.flush_stats()

    def flush_stats(self) -> None:
        """Gesammelte Zähler schreiben. Schlägt das fehl (Datei gesperrt), bleiben sie für den nächsten
        Versuch im Speicher - und lösen anders als Cache/Tempo NICHT den degradierten Modus aus."""
        self._stats_flushed = time.monotonic()
        if not self._stats or self._store is None or self._store.degraded:
            return
        batch, self._stats = self._stats, {}
        try:
            self._store.record_stats([(n, st, c, ms) for (n, st), (c, ms) in batch.items()])
        except Exception as e:
            for k, (c, ms) in batch.items():          # zurücklegen, nichts verlieren
                entry = self._stats.setdefault(k, [0, 0])
                entry[0] += c
                entry[1] += ms
            log.debug(t("Zähler nicht geschrieben: %s", "counters not written: %s"), e)

    def _request_days(self, p: Provider, days: int | None) -> int | None:
        """Welcher Zeitfilter tatsächlich in der Anfrage an die Quelle landet - bestimmt den Cache-Schlüssel.
        Quellen ohne Zeitfilter in der Anfrage (time_windows None) teilen sich den Eintrag ohne days;
        Quellen mit festen Fenstern (Tag/Woche/Monat/Jahr) teilen ihn je Fenster."""
        if not days or p.time_windows is None or not self._accepts_days(p):
            return None
        if not p.time_windows:
            return days
        return min((w for w in p.time_windows if w >= days), default=None)

    def _filters_exactly(self, p: Provider, days: int) -> bool:
        """Filtert die Quelle selbst genau auf `days` Tage (dann sind auch undatierte Treffer verlässlich)?"""
        request_days = self._request_days(p, days)
        return (request_days is not None and p.max_native_days >= days
                and (not p.time_windows or request_days == days))

    def _accepts_days(self, p: Provider) -> bool:
        """Eigene Quellen aus der Zeit vor dem Zeitfilter haben search(...) ohne days: dann nachfiltern."""
        cached = self._days_ok.get(type(p))
        if cached is None:
            try:
                params = inspect.signature(p.search).parameters
                cached = "days" in params or any(x.kind is inspect.Parameter.VAR_KEYWORD for x in params.values())
            except (TypeError, ValueError):
                cached = False
            self._days_ok[type(p)] = cached
        return cached

    @staticmethod
    def _within(results: list[Result], days: int, native: bool) -> tuple[list[Result], bool]:
        """Nur Treffer der letzten `days` Tage. Undatierte bleiben nur, wenn die Quelle selbst gefiltert
        hat. Liefert (Treffer, ob etwas verworfen wurde)."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
        kept = [r for r in results if (r.published[:19] >= cutoff if r.published else native)]
        return kept, len(kept) < len(results)

    async def _query_source_inner(self, name: str, query: str, n: int, lang: str, kind: str,
                                  max_age: float, budget: float, allow_paid: bool, days: int | None = None
                                  ) -> tuple[list[Result], SourceStatus]:
        p = self.providers[name]
        page = _page_size(n)
        try:
            request_days = self._request_days(p, days)
            # undatierte Treffer nur behalten, wenn die Quelle genau auf diese Spanne gefiltert hat
            # (SearXNG mit days=2 fragt "week" -> undatierte könnten bis zu 7 Tage alt sein)
            # Bilder/Videos sind fast nie datiert: dort zählt das Fenster der Quelle (z.B. "Woche" für days=3),
            # sonst bliebe bei days=3 nichts übrig
            native = days is not None and (
                self._filters_exactly(p, days)
                # Bilder/Videos: Fenster der Quelle genügt; jenseits des größten Fensters (z.B. 400 Tage) ist
                # die Antwort ohne Filter eine Obermenge - undatierte Treffer sind dann ebenfalls ok
                or (kind in MEDIA_KINDS and (request_days is not None
                                             or bool(p.time_windows) and days > max(p.time_windows or (0,)))))
            key = StateStore.key(name, p.cache_scope, query, page, lang, kind,
                                 *([request_days] if request_days else []))
            if (hit := self._cache_get(name, key, max_age)) is not None:
                cached, meta = hit
                if days:     # Cache enthält die ungefilterte Antwort der Quelle -> hier filtern
                    cached, dropped = self._within(cached, days, native)
                    if dropped:
                        meta["filtered"] = True
                return cached[:n], make_status("cached", len(cached[:n]), meta=meta)
            if not p.available:
                return [], make_status("not_configured", message=t("API-Key fehlt", "API key missing"))
            if kind not in p.kinds:
                return [], make_status("unsupported", message=t(f"kann nur {', '.join(p.kinds)}",
                                                                f"supports only {', '.join(p.kinds)}"))
            if p.paid and not allow_paid:
                return [], make_status("not_configured", message=t("Bezahlquelle - für diese Suche nicht erlaubt",
                                                                   "paid source - not allowed for this search"))
            if p.breaker.is_open():
                return [], self._paused(p)
            p.precheck(query, kind)          # SourceSkipped hier kostet weder Tempo-Slot noch Request
            # etwas unter dem Rundenbudget: wer nur wartet, soll als "rate_limited" enden, nicht als "deadline"
            async with p.limiter.slot(budget=min(budget * 0.9, self.max_wait)):
                if p.breaker.is_open():   # während des Wartens hat eine andere Anfrage eine Sperre gemeldet
                    return [], self._paused(p)
                if p.paid:   # erst mit sicherem Slot zählen (sonst kostet ein "rate_limited" Budget)
                    try:
                        self.store.reserve_paid_call(self.paid_daily_limit, self.paid_monthly_limit)
                    except sqlite3.Error:      # Zähler nicht prüfbar -> nicht bezahlen (fail closed)
                        return [], make_status("unavailable", message=t("Bezahl-Zähler gerade nicht prüfbar "
                                                                      "(Zustandsdatei gesperrt) - übersprungen",
                                                                        "paid-query counter not checkable right now (state file locked) - skipped"))
                self._require_open()
                client = self._proxied if p.via_proxy and self._proxied is not None else self._client
                assert client is not None
                # days nur übergeben, wenn die Anfrage davon abhängt: eigene Provider ohne days-Parameter bleiben
                # nutzbar (sie werden nur nachgefiltert)
                # die Quelle bekommt den Zeitraum, der den Cache-Eintrag bestimmt (bei festen Fenstern das ganze
                # Fenster): der Eintrag enthält dann alles aus diesem Fenster, die Engine filtert genau nach
                extra = {"days": request_days} if request_days else {}
                raw = await asyncio.wait_for(p.search(client, query, page, lang, kind, **extra),
                                             p.timeout + 5)
            meta = {k: v for k, v in (getattr(raw, "meta", None) or {}).items() if v}
            results = [r for r in raw if isinstance(r, Result) and r.url]
            for r in results:
                r.published = normalize_date(r.published)
            if results or p.paid:   # leer nur bei Bezahlquellen cachen (sonst evtl. stille Sperre)
                try:
                    p.breaker.success()
                    # ungefiltert speichern: andere days-Werte mit derselben Anfrage nutzen denselben Eintrag
                    self.store.set(key, {"results": [r.to_dict() for r in results], "meta": meta})
                except Exception:   # Ergebnisse trotzdem liefern
                    log.warning(t("Cache für %s nicht schreibbar", "cache for %s not writable"), name, exc_info=True)
            if days:
                results, dropped = self._within(results, days, native)
                if dropped:
                    meta = {**meta, "filtered": True}      # 0 Treffer nach dem Filter ist keine stille Sperre
            return results[:n], make_status("ok", len(results[:n]), meta=meta)
        except BlockedError as e:
            try:
                p.breaker.trip(str(e))
            except Exception:
                log.exception(t("Sperre für %s konnte nicht gespeichert werden",
                                "block for %s could not be stored"), name)
            return [], make_status("blocked", message=t(f"gesperrt, Pause {p.breaker.retry_in()}s: {e}",
                                                        f"blocked, pause {p.breaker.retry_in()}s: {e}"))
        except RateLimited as e:
            return [], make_status("rate_limited", message=str(e))
        except BudgetExceeded as e:
            return [], make_status("budget", message=str(e))
        except SourceSkipped as e:
            return [], make_status("skipped", message=str(e))
        except SourceUnavailable as e:
            log.info(t("Quelle %s nicht verfügbar: %s", "source %s unavailable: %s"), name, e)
            return [], make_status("unavailable", message=str(e)[:300], meta=e.meta)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.info(t("Quelle %s nicht verfügbar: %s: %s", "source %s unavailable: %s: %s"), name, type(e).__name__, e)
            return [], make_status("unavailable", message=f"{type(e).__name__}: {e}"[:300])
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            # meist ein unerwartetes Antwortformat der Quelle (z.B. null statt Objekt) - kein Ausfall unseres Codes,
            # aber sichtbar loggen, falls es doch ein Bug ist
            log.warning(t("Quelle %s: unerwartetes Antwortformat (%s: %s)",
                          "source %s: unexpected response format (%s: %s)"), name, type(e).__name__, e, exc_info=True)
            return [], make_status("unavailable", message=t(f"unerwartetes Antwortformat ({type(e).__name__}: {e})",
                                                            f"unexpected response format ({type(e).__name__}: {e})")[:300])
        except asyncio.CancelledError:
            raise
        except Exception as e:   # Bug in einer Quelle (oder DB-Fehler): laut loggen, Suche läuft weiter
            log.exception(t("Unerwarteter Fehler in Quelle %s", "unexpected error in source %s"), name)
            return [], make_status("error", message=f"{type(e).__name__}: {e}"[:300])

    @staticmethod
    def _paused(p: Provider) -> SourceStatus:
        return make_status("paused", message=t(f"pausiert ({p.breaker.retry_in()}s): {p.breaker.last_error}",
                                               f"paused ({p.breaker.retry_in()}s): {p.breaker.last_error}"))

    async def _round(self, names: Sequence[str], query: str, n: int, lang: str, kind: str,
                     max_age: float, timeout: float, allow_paid: bool = True, patience: float | None = None,
                     days: int | None = None) -> list[tuple[str, list[Result], SourceStatus]]:
        """Fragt `names` parallel. Endet nach `timeout` - oder früher, sobald mindestens n Treffer da sind
        und Nachzügler länger als `patience` Sekunden auf sich warten lassen (z.B. GDELT mit 10+ s)."""
        if not names:
            return []
        timeout = max(timeout, 0.5)
        t_end = time.monotonic() + timeout
        tasks = [asyncio.create_task(self._query_source(s, query, n, lang, kind, max_age, timeout, allow_paid,
                                                        days)) for s in names]
        done: set[asyncio.Task] = set()
        enough_since: float | None = None
        aborted = False
        try:
            pending = set(tasks)
            while pending:
                now = time.monotonic()
                wait_for = t_end - now
                if enough_since is not None and patience is not None:
                    wait_for = min(wait_for, enough_since + patience - now)
                if wait_for <= 0:
                    break
                just_done, pending = await asyncio.wait(pending, timeout=wait_for,
                                                        return_when=asyncio.FIRST_COMPLETED)
                done |= just_done
                if enough_since is None and sum(t.result()[1]["count"] for t in done if not t.cancelled()) >= n:
                    enough_since = time.monotonic()
        except asyncio.CancelledError:
            aborted = True
            raise
        finally:
            await _cancel_all(tasks)   # Deadline ODER Abbruch des Aufrufers: keine verwaisten Anfragen
            # Abgebrochene Quellen zählen sich nicht selbst - gerade hängende Quellen sollen in /metrics auffallen
            spent = int((time.monotonic() - (t_end - timeout)) * 1000)
            # "early" = nicht abgewartet, weil schnellere Quellen schon genug hatten (kein Hänger)
            cut_state = ("cancelled" if aborted else
                         "early" if enough_since is not None and time.monotonic() < t_end - 0.05 else "deadline")
            for name, task in zip(names, tasks):
                if task.cancelled():
                    self._count(name, cut_state, spent)
        waited = int((time.monotonic() - (t_end - timeout)) * 1000)
        early = enough_since is not None and time.monotonic() < t_end - 0.05
        cut = make_status("deadline", ms=waited, meta={"early": True} if early else None,
                          message=(t(f"nicht abgewartet ({waited / 1000:.1f}s): schnellere Quellen hatten schon "
                                   "genug Treffer",
                                     f"not awaited ({waited / 1000:.1f}s): faster sources already had enough results")) if early else t(f"Zeitbudget abgelaufen ({waited / 1000:.1f}s)",
                                                                    f"time budget exhausted ({waited / 1000:.1f}s)"))
        return [(name, *(t.result() if t in done and not t.cancelled() else ([], dict(cut, meta=dict(cut["meta"])))))
                for name, t in zip(names, tasks)]

    def _usable(self, names: Iterable[str], kind: str, skip_engines: Collection[str] = ()) -> list[str]:
        out = []
        for s in names:
            p = self.providers.get(s)
            try:
                if p and p.available and kind in p.kinds and p.same_engine not in skip_engines:
                    out.append(s)
            except Exception:   # kaputte Quelle (z.B. available wirft): weglassen statt Suche abbrechen
                log.exception(t("Quelle %s nicht prüfbar - übersprungen",
                                "source %s cannot be checked - skipped"), s)
        return out

    # ------------------------------------------------------------------ öffentliche API

    def _validate(self, query: str, n: int, kind: str) -> tuple[str, int]:
        if not isinstance(query, str) or not query.strip():
            raise InputError(t("leere Suchanfrage", "empty query"))
        query = query.strip()
        try:
            query.encode("utf-8")
        except UnicodeEncodeError:
            raise InputError(t("Suchanfrage enthält ungültige Zeichen (keine gültige UTF-8-Kodierung)",
                               "query contains invalid characters (not valid UTF-8)")) from None
        if len(query) > 2000:
            raise InputError(t("Suchanfrage zu lang (max. 2000 Zeichen)",
                               "query too long (max. 2000 characters)"))
        if kind not in KINDS:
            raise InputError(t(f"kind muss eines von {list(KINDS)} sein",
                               f"kind must be one of {list(KINDS)}"))
        try:
            n = int(n)
        except (TypeError, ValueError, OverflowError):
            raise InputError(t("n muss eine ganze Zahl sein", "n must be a whole number")) from None
        return query, max(1, min(n, 100))

    async def search(self, query: str, n: int = 10, kind: str = "web", lang: str = "auto",
                     sources: list[str] | None = None, max_age: float | None = None,
                     deadline: float = 20.0, paid: bool = True, days: int | None = None) -> SearchResponse:
        """Parallele Suche.

        Args:
            query: Suchbegriff (nicht leer, max. 2000 Zeichen)
            n: gewünschte Trefferzahl (1-100)
            kind: "web", "news" (neueste zuerst), "ref" (Nachschlagewerke), "images" oder "videos"
            lang: Sprachcode wie "de"/"en" oder "auto"
            sources: feste Quellenliste statt der Stufen (Bezahlquellen darin nur mit paid=True)
            max_age: maximales Cache-Alter in s (0 = immer frisch; Standard je Suchart)
            deadline: Gesamtbudget in s für alle Stufen (> 0)
            paid: Bezahlquellen erlauben (greifen ohnehin nur mit API-Key und innerhalb der Limits)
            days: nur Treffer der letzten N Tage (1-3650). Quellen filtern selbst, wo sie das können;
                sonst werden Treffer nachträglich nach Datum gefiltert (undatierte fallen dann weg).
        Returns:
            {"query", "kind", "results": [Result-Dicts], "sources": {name: SourceStatus}}
        Raises:
            InputError: ungültige Eingabe; RuntimeError: Engine nicht (mehr) geöffnet
        """
        self._require_open()
        query, n = self._validate(query, n, kind)
        if not isinstance(lang, str) or len(lang) > 20:
            raise InputError(t("lang muss ein Sprachcode wie 'de' oder 'auto' sein",
                               "lang must be a language code like 'de' or 'auto'"))
        lang = lang.strip() or "auto"
        try:
            deadline = float(deadline)
            max_age = DEFAULT_MAX_AGE[kind] if max_age is None else max(0.0, float(max_age))
        except (TypeError, ValueError):
            raise InputError(t("deadline und max_age müssen Zahlen sein",
                               "deadline and max_age must be numbers")) from None
        if not math.isfinite(deadline) or deadline <= 0:
            raise InputError(t("deadline muss eine endliche Zahl größer als 0 sein",
                               "deadline must be a finite number greater than 0"))
        if sources is not None and (isinstance(sources, str) or not all(isinstance(x, str) for x in sources)):
            raise InputError(t("sources muss eine Liste von Quellnamen (Texte) sein",
                               "sources must be a list of source names (strings)"))
        days = self._validate_days(days, kind)
        t_end = time.monotonic() + max(float(deadline), 0.5)
        outcomes: list[tuple[str, list[Result], SourceStatus]] = []

        def total() -> int:
            return sum(st["count"] for _, _, st in outcomes)

        def remaining() -> float:
            return t_end - time.monotonic()

        if sources:
            unknown = [s for s in sources if s not in self.providers]
            if unknown:
                raise _unknown_sources_error(unknown, self.providers)
            outcomes += await self._round(list(dict.fromkeys(sources)), query, n, lang, kind, max_age,
                                          remaining(), allow_paid=paid, days=days)
            if outcomes and all(st["state"] == "unsupported" for _, _, st in outcomes):
                raise InputError(t(f"keine der Quellen {', '.join(n_ for n_, _, _ in outcomes)} kann die Suchart {kind!r}",
                                   f"none of the sources {', '.join(n_ for n_, _, _ in outcomes)} supports kind {kind!r}"))
        else:
            primary, fallback, paid_names = self.policy.for_kind(kind)
            usable_primary = self._usable(primary, kind)
            usable_fallback = self._usable(fallback, kind)
            if not usable_primary and not usable_fallback and not (paid and self._usable(paid_names, kind)):
                raise InputError(t(f"keine nutzbare Quelle für Suchart {kind!r} konfiguriert",
                                   f"no usable source configured for kind {kind!r}"))
            share = _PRIMARY_SHARE if usable_fallback else 1.0
            # Sind schon n Treffer da, wartet Stufe 1 nur noch kurz auf langsame Quellen
            patience = max(2.0, 0.15 * float(deadline))
            outcomes += await self._round(usable_primary, query, n, lang, kind, max_age, remaining() * share,
                                          allow_paid=paid, patience=patience, days=days)
            primary_outcomes = list(outcomes)
            # Stufe 2 nur, wenn wirklich zu wenig kam (bei kleinem n reicht n)
            if total() < min(n, max(3, n // 2)) and remaining() >= 0.3:
                failed = {str(e[0]) for _, _, st in outcomes for e in st["meta"].get("engines_failed", [])
                          if isinstance(e, (list, tuple)) and e}
                # Restzeit für die Bezahlstufe freihalten, falls sie in Frage kommt (hängender Fallback)
                reserve = 0.25 if paid and self._usable(paid_names, kind) else 0.0
                outcomes += await self._round(self._usable(fallback, kind, failed), query, n, lang, kind, max_age,
                                              remaining() * (1 - reserve), allow_paid=paid, days=days)
            # Stufe 3: Bezahlen hilft nur, wenn Sperren/Fehler schuld sind - nicht bei seltenen Suchen
            # und nicht, wenn Quellen nur wegen des Tempos übersprungen wurden.
            # Maßgeblich sind die Hauptquellen: liefert SearXNG normal (nur wenige Treffer), ist die Suche selten -
            # eine pausierte Nebenquelle (z.B. Marginalia) rechtfertigt keine Bezahlabfrage
            # ohne nutzbare freie Quelle (z.B. nur ein API-Key konfiguriert) ist die Bezahlstufe der einzige Weg
            ran_free = bool(usable_primary or usable_fallback)
            had_failures = not ran_free or any(not is_benign(st) for _, _, st in (primary_outcomes or outcomes))
            if paid and total() < min(n, 3) and had_failures:
                # mit Zeitfilter nur Bezahlquellen, die selbst filtern: sonst kostet die Abfrage Geld, und der
                # Nachfilter verwirft die (meist undatierten) Treffer
                paid_usable = [s for s in self._usable(paid_names, kind)
                               if not days or self._filters_exactly(self.providers[s], days)]
                for name in paid_usable:
                    if remaining() < 1:
                        break
                    # Bezahlquellen nie mit großem Pool (z.B. Monitor n=60) fragen: max. 10 Treffer
                    outcomes += await self._round([name], query, min(n, 10), lang, kind, max_age, remaining(),
                                                  days=days)
                    # Stopp bei JEDER normalen Antwort (auch 0 Treffer): eine zweite Bezahlquelle findet für
                    # eine seltene Suche auch nichts - sonst zahlt man pro Lauf für jede konfigurierte API
                    last = outcomes[-1][2]
                    if last["state"] in ("ok", "cached", "budget") and not (last["count"] == 0
                                                                           and last["meta"].get("filtered")):
                        break

        # Stille Sperren: gescrapte Quelle liefert 0, während andere Treffer haben
        # (nicht, wenn die Quelle selbst Treffer als themenfremd verworfen hat)
        found = total()
        for name, _, st in outcomes:
            p = self.providers[name]
            try:
                # mit Zeitfilter ist "0 Treffer" ein normales Ergebnis, kein Sperrverdacht
                if st["state"] == "ok" and st["count"] == 0 and p.scraped and not days and not st["meta"].get("filtered"):
                    p.breaker.note_empty(others_found=found > 0)
            except Exception:
                log.exception(t("Sperrzustand für %s konnte nicht aktualisiert werden",
                                "block state for %s could not be updated"), name)

        self._count("_search:" + kind, "ok" if found else "empty", 0)
        merged = merge_rrf([res for _, res, _ in outcomes], dedupe_titles=(kind == "news"))
        if kind == "news":
            # Neueste zuerst (tagesgenau); innerhalb eines Tages entscheidet die Relevanz
            merged.sort(key=lambda r: ((r.published or "")[:10], r.score), reverse=True)
        return {"query": query, "kind": kind, "results": [r.to_dict() for r in merged[:n]],
                "sources": {name: st for name, _, st in outcomes}}

    @staticmethod
    def _validate_days(days: object, kind: str) -> int | None:
        if days is None:
            return None
        try:
            if isinstance(days, bool) or not isinstance(days, (int, float, str)):
                raise ValueError
            as_float = float(days)
            if not as_float.is_integer():
                raise ValueError
            days = int(as_float)
        except (TypeError, ValueError, OverflowError):
            raise InputError(t("days muss eine ganze Zahl sein", "days must be a whole number")) from None
        if not 1 <= days <= MAX_DAYS:
            raise InputError(t(f"days muss zwischen 1 und {MAX_DAYS} liegen",
                               f"days must be between 1 and {MAX_DAYS}"))
        if kind == "ref":
            raise InputError(t("days gilt nicht für Nachschlagewerke (kind 'ref')",
                               "days does not apply to reference search (kind 'ref')"))
        return days

    async def search_many(self, queries: Sequence[str], concurrency: int = 4,
                          total_deadline: float | None = None, **kw) -> list[SearchResponse]:
        """Viele Anfragen parallel (max. `concurrency` gleichzeitig). Reihenfolge bleibt erhalten,
        identische Anfragen werden nur einmal ausgeführt; ungültige Einzelanfragen liefern
        {"error": ...} statt die ganze Liste abzubrechen. `total_deadline`: Gesamtbudget in s; späte
        Anfragen bekommen nur die Restzeit (mind. 1 s)."""
        try:
            sem = asyncio.Semaphore(max(1, min(int(concurrency), 64)))
            t_end = time.monotonic() + float(total_deadline) if total_deadline else None
        except (TypeError, ValueError, OverflowError):
            raise InputError(t("concurrency und total_deadline müssen Zahlen sein",
                               "concurrency and total_deadline must be numbers")) from None
        if isinstance(queries, str) or not isinstance(queries, (list, tuple)) or \
                any(not isinstance(q, str) for q in queries):
            raise InputError(t("queries muss eine Liste von Texten sein",
                               "queries must be a list of strings"))
        try:
            per_query = float(kw.pop("deadline", 20.0))
        except (TypeError, ValueError):
            raise InputError(t("deadline muss eine Zahl sein", "deadline must be a number")) from None
        if kw.get("days") is not None:     # einmal für alle prüfen statt N gleicher Fehler je Anfrage
            kw["days"] = self._validate_days(kw["days"], kw.get("kind", "web"))

        async def one(q: str) -> SearchResponse:
            async with sem:
                budget = per_query if t_end is None else max(1.0, min(per_query, t_end - time.monotonic()))
                try:
                    return await self.search(q, deadline=budget, **kw)
                except InputError as e:
                    return {"query": q, "kind": kw.get("kind", "web"), "results": [], "sources": {},
                            "error": str(e)}
        unique = list(dict.fromkeys(queries))
        tasks = [asyncio.create_task(one(q)) for q in unique]
        try:
            results = await asyncio.gather(*tasks)
        finally:   # ein unerwarteter Fehler/Abbruch: die übrigen Suchen nicht weiterlaufen lassen
            await _cancel_all(tasks)
        by_query = dict(zip(unique, results))
        return [by_query[q] for q in queries]

    async def fetch(self, url: str, max_chars: int = 20_000) -> PageResponse:
        """Seite abrufen und als Markdown liefern (SSRF-Schutz, robots.txt, Tempo pro Host,
        Gesamtzeitlimit). Wirft fetch.FetchError mit lesbarer Meldung."""
        from .fetch import Fetcher
        self._require_open()
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 100 <= max_chars <= 1_000_000:
            raise InputError(t("max_chars muss eine ganze Zahl zwischen 100 und 1000000 sein",
                               "max_chars must be a whole number between 100 and 1000000"))
        client = self._proxied or self._client
        assert client is not None
        if self._fetcher is None:
            self._fetcher = Fetcher(self.store)
        return await self._fetcher.fetch(client, url, max_chars=max_chars)

    def status(self) -> dict[str, SourceInfo]:
        """Zustand aller Quellen (hostweit): verfügbar, Sucharten, Tempo, Pause."""
        self._require_open()
        out: dict[str, SourceInfo] = {}
        for name, p in self.providers.items():
            br = p.breaker.status()
            try:
                available = bool(p.available)
            except Exception:
                available = False
            out[name] = {"available": available, "kinds": list(p.kinds), "min_interval_s": p.min_interval,
                         "scraped": p.scraped, "blocked": br["blocked"], "retry_in_s": br["retry_in_s"],
                         "strikes": br["strikes"], "last_error": br["last_error"]}
        return out

    def paid_usage(self) -> dict[str, int]:
        """Bezahl-Abfragen heute/diesen Monat (hostweit, UTC) und die Limits; -1 = nicht lesbar."""
        self._require_open()
        try:
            used = self.store.usage()
        except Exception:
            used = {"day": -1, "month": -1}
        return {"day": used["day"], "month": used["month"], "daily_limit": self.paid_daily_limit,
                "monthly_limit": self.paid_monthly_limit}
