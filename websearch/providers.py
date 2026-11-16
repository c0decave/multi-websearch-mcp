"""Suchquellen. Jede Quelle kennt ihr eigenes Tempo und erkennt Sperren selbst.

Sucharten (kind):
  web  - allgemeine Websuche
  news - aktuelle Nachrichten (für Monitoring)
  ref  - Nachschlagewerke / Fachquellen

Stufen (tier):
  primary  - kostenlos, wird immer gefragt
  fallback - kostenlos, nur wenn primary zu wenig liefert
  paid     - optional mit API-Key (Prepaid/Freikontingent), nur als letzte Reserve
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .i18n import lang, t
from .core import (BlockedError, CircuitBreaker, InputError, RateLimiter, Result, SourceSkipped, SourceUnavailable,
                   log)

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


def bot_ua() -> str:
    """Wikimedia/GDELT/HN wollen einen ehrlichen User-Agent mit Kontakt (WEBSEARCH_UA)."""
    from . import __version__
    return os.environ.get("WEBSEARCH_UA", f"websearch/{__version__} (+https://example.invalid/contact)")


# Nur in HTML-Antworten suchen: In JSON/RSS können diese Wörter ganz normal in Treffern stehen
_BLOCK_MARKERS = ("captcha", "unusual traffic", "/sorry/index", "anomaly-modal", "are you a robot",
                  "javascript is required to complete this challenge", "cf-chl-", "challenge-platform")


class ResultList(list):
    """Liste von Results plus Metadaten der Quelle (z.B. ausgefallene SearXNG-Engines)."""
    meta: dict

    def __init__(self, items=(), **meta):
        super().__init__(items)
        self.meta = meta


def _check_http(resp: httpx.Response, name: str) -> None:
    code = resp.status_code
    if code in (403, 429):
        raise BlockedError(f"{name}: HTTP {code}")
    if code == 402:
        raise BlockedError(t(f"{name}: HTTP 402 - Guthaben/Kontingent aufgebraucht", f"{name}: HTTP 402 - credit/quota used up"))
    if code == 401:
        # als Sperre behandeln: sonst würde eine Quelle mit falschem Key bei jeder Suche erneut
        # gefragt (und z.B. die nächste Bezahlquelle nie erreicht)
        raise BlockedError(t(f"{name}: HTTP 401 - API-Key ungültig",
                             f"{name}: HTTP 401 - invalid API key"))
    if code == 400 and "credit" in resp.text[:500].lower():     # Serper: "Not enough credits"
        raise BlockedError(t(f"{name}: HTTP 400 - Guthaben aufgebraucht", f"{name}: HTTP 400 - credits used up"))
    if code >= 500:
        raise SourceUnavailable(f"{name}: HTTP {code}")
    if code >= 400:
        raise SourceUnavailable(f"{name}: HTTP {code}: {resp.text[:120]}")
    if "html" in resp.headers.get("content-type", "").lower():
        head = resp.text[:6000].lower()
        if any(m in head for m in _BLOCK_MARKERS):
            raise BlockedError(t(f"{name}: Sperrseite/Captcha erkannt", f"{name}: block page/captcha detected"))


def _json(resp: httpx.Response, name: str):
    try:
        return resp.json()
    except (ValueError, RecursionError) as e:      # RecursionError: absurd tief verschachteltes JSON
        raise SourceUnavailable(t(f"{name}: keine gültige JSON-Antwort: {resp.text[:120]!r}",
                                  f"{name}: no valid JSON response: {resp.text[:120]!r}")) from e


def _strip_tags(s: str) -> str:
    """HTML-Tags entfernen und Entities auflösen (&nbsp; &amp; ...)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _lang_code(lang: str | None, default: str = "en") -> str:
    """'de', 'de-DE', 'en_US' -> 'de'/'en'. Alles andere -> default (schützt auch Hostnamen)."""
    m = re.match(r"^([a-z]{2,3})(?:[-_][A-Za-z]{2,4})?$", (lang or "").strip().lower())
    return m.group(1) if m else default


_WORD = re.compile(r"[\w]+", re.UNICODE)


def relevant(query: str, text: str, strict: bool = False, minimum: int | None = None) -> bool:
    """Enthält text genügend Suchwörter (ab 3 Zeichen, ohne Operatoren wie site:) als ganze Wörter?

    Normal: min(2, Anzahl) bzw. `minimum`. strict (für sehr unscharf suchende Quellen wie Wikipedia):
    bei drei Suchwörtern alle, ab vier mind. 75 %."""
    terms = {w.lower() for w in _WORD.findall(re.sub(r"\b\w+:\S+", " ", query)) if len(w) >= 3}
    if not terms:
        return True
    words = {w.lower() for w in _WORD.findall(text)}
    found = {t for t in terms if _term_in(t, words)}
    ordered = [w.lower() for w in _WORD.findall(re.sub(r"\b\w+:\S+", " ", query)) if len(w) >= 3]
    for a, b in zip(ordered, ordered[1:]):          # zwei Suchwörter als ein Wort geschrieben (Kompositum)
        if (a not in found or b not in found) and _compound_in(a, b, words):
            found |= {a, b}
    need = min(2, len(terms)) if minimum is None else min(minimum, len(terms))
    if strict and len(terms) >= 3:
        need = len(terms) if len(terms) == 3 else -(-len(terms) * 3 // 4)   # aufgerundet 75 %
    return len(found) >= need


_LINKS = ("", "s", "n", "en", "e", "es")


def _compound_in(a: str, b: str, words: set[str]) -> bool:
    """Steht "a" + Fugenelement + "b" (oder umgekehrt) als ein Wort im Text? Bahn+Streik -> Bahnstreik,
    Rente+n+Erhöhung -> Rentenerhöhung, Gas+Preis -> Gaspreise. Endungen hinten bis 3 Zeichen erlaubt."""
    for x, y in ((a, b), (b, a)):
        stems = {x, x[:-1]} if x.endswith("e") else {x}           # Rente -> Renten..., Schule -> Schul...
        for stem in stems:
            for link in _LINKS:
                comp = f"{stem}{link}{y}"
                if any(w.startswith(comp) and len(w) - len(comp) <= 3 for w in words):
                    return True
    return False


def _term_in(term: str, words: set[str]) -> bool:
    """Wort-Treffer mit etwas Toleranz: exakt; Wortanfang ab 4 Zeichen ("pump" -> "pumps", "streik" ->
    "streiken"); Teilwort ab 5 Zeichen für Komposita ("förderung" in "wärmepumpenförderung");
    Plural in der Anfrage, Singular im Text ("streiks" -> "streik", höchstens 2 Zeichen Unterschied)."""
    if term in words:
        return True
    return any((len(term) >= 4 and w.startswith(term) and (len(w) - len(term) <= 3 or len(term) >= 6))
               # pump -> pumps; ab 6 Zeichen auch Bestimmungswort: wärmepumpe -> wärmepumpenförderung
               or (len(term) >= 6 and w.endswith(term))       # förderung -> wärmepumpenförderung (Grundwort am Ende)
               or (len(w) >= 3 and term.startswith(w) and len(term) - len(w) <= 2)   # streiks -> streik, cars -> car
               for w in words)


def short_terms(query: str) -> set[str]:
    """Kurze Suchwörter (< 3 Zeichen, z.B. 'EU', 'AI', 'M4'), die relevant() ignoriert."""
    return {w.lower() for w in _WORD.findall(re.sub(r"\b\w+:\S+", " ", query)) if len(w) < 3}


def _unwrap_redirect(url: str) -> str:
    """Bing-News verpackt Ziel-URLs in apiclick.aspx?url=... -> echte URL für Deduplizierung."""
    if "bing.com/news/apiclick" in url:
        target = parse_qs(urlsplit(url).query).get("url")
        if target:
            return target[0]
    return url


def _parse_rss(xml_text: str, source: str, n: int) -> ResultList:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise SourceUnavailable(t(f"{source}: kein gültiges RSS ({e})", f"{source}: no valid RSS ({e})")) from e
    out = ResultList()
    for item in root.iter("item"):
        if len(out) >= n:
            break
        url = _unwrap_redirect((item.findtext("link") or "").strip())
        if not url:
            continue
        title = (item.findtext("title") or "").strip()
        desc = _strip_tags(item.findtext("description") or "").replace("\u00ad", "")   # weiche Trennstriche weg
        snippet = desc[:400]
        pub = item.find("source")
        if pub is not None and (pub.text or pub.get("url")):     # Google News: Herausgeber mitliefern
            publisher = (pub.text or "").strip() or pub.get("url", "")
            snippet = f"{publisher} ({pub.get('url')})" if pub.get("url") else publisher
            # Googles Beschreibung ist meist nur "Titel + Herausgeber" -> dann nicht wiederholen
            head = title.rsplit(" - ", 1)[0][:40]
            if desc and not (len(head) >= 10 and desc.startswith(head)):
                snippet += f" - {desc[:300]}"
        out.append(Result(
            title=title, url=url, snippet=snippet,
            source=source, rank=len(out) + 1, published=item.findtext("pubDate"),
        ))
    return out


# Englische Beschreibungen für 'websearch sources' (Deutsch = Docstring der Klasse)
_ABOUT_EN = {
    "searxng": "Self-hosted SearXNG meta search (Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia and more), "
               "also images and videos. SearXNG pauses blocked engines itself, so it may be queried faster.",
    "bing_rss": "Bing delivers search results as RSS (format=rss). According to the feed for private, non-commercial "
                "use only - for commercial use set WEBSEARCH_DISABLE=bing_rss.",
    "google_news": "Google News search as RSS - official feed, no scraping, ideal for monitoring.",
    "hackernews": "Algolia HN API: official, free, no key. Most relevant stories of the last 30 days.",
    "gdelt": "GDELT DOC 2.0: worldwide news, free, no key. Max. 1 request / 5 s per IP.",
    "wikipedia": "Wikipedia full-text search (official MediaWiki API, no key) in the chosen language.",
    "marginalia": "Independent index (small/non-commercial web). The key 'public' is shared and often exhausted; "
                  "get your own free key by mail to contact@marginalia-search.com.",
    "exa": "Exa: own (neural) index. 10 $ free credit per month without a payment method.",
    "linkup": "Linkup: search API for agents. Monthly free quota (per provider), then 5 $/1,000.",
    "tavily": "Tavily: search prepared for LLMs. 1,000 credits/month free (no card), then 0.008 $/credit.",
    "brave_api": "Brave Search API (own index). 5 $/1,000 queries, 5 $ free credit per month, credit card needed.",
    "perplexity": "Perplexity Search API: raw search results (no LLM answer). Fast mode 1 $/1,000 (default).",
    "reddit": "Reddit search via the official API (OAuth, free \"script\" app, ~100 requests/min). Active only with "
              "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET.",
    "serper": "Serper.dev: Google results via API. 2,500 free queries, then prepaid from about 1 $/1,000.",
}


def time_window(days: int | None, windows: dict[str, int]) -> str | None:
    """Kleinstes Zeitfenster einer Quelle, das `days` Tage abdeckt (z.B. {"day": 1, "week": 7, ...});
    None, wenn days leer ist oder kein Fenster groß genug ist."""
    if not days:
        return None
    fitting = [(span, name) for name, span in windows.items() if span >= days]
    return min(fitting)[1] if fitting else None


def _date_ago(days: int) -> str:
    """Datum vor `days` Tagen (UTC) als JJJJ-MM-TT."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


_DMWY = {"d": 1, "w": 7, "m": 31, "y": 366}
_SX_RANGES = {"day": 1, "week": 7, "month": 31, "year": 366}


class Provider:
    name: str = "base"
    kinds: tuple[str, ...] = ("web",)
    min_interval: float = 1.0      # Sekunden zwischen zwei Requests an diese Quelle (hostweit)
    concurrency: int = 1
    timeout: float = 15.0
    base_cooldown: float = 300
    # True = Suchseite wird gescrapt: Sperren kommen oft als "0 Treffer" -> Stille-Sperre-Erkennung.
    # Offizielle APIs/Feeds (HN, GDELT, Wikipedia ...) sind dagegen oft legitim leer.
    scraped: bool = False
    # Gleiche Suchmaschine wie eine SearXNG-Engine -> überspringen, wenn SearXNG sie gerade gesperrt meldet
    same_engine: str | None = None
    # Kostenpflichtig (API-Key mit Guthaben): zählt gegen WEBSEARCH_PAID_DAILY/MONTHLY_LIMIT
    paid: bool = False
    # Über WEBSEARCH_PROXY anfragen (öffentliche Quellen). Nie: eigene SearXNG-Instanz, Bezahl-APIs.
    via_proxy: bool = True
    # Spezialquelle, die für die meisten Anfragen legitim leer ist (Hacker News, GDELT): ihr "0 Treffer"
    # beweist nicht, dass die Suche funktioniert hat
    sparse: bool = False
    # Zeitfilter (days): bis zu so vielen Tagen filtert die Quelle selbst. Dann bleiben auch Treffer ohne
    # Datum erhalten; sonst filtert die Engine nachträglich und verwirft undatierte Treffer. 0 = nie nativ.
    max_native_days: int = 0
    # Wie der Zeitfilter in der Anfrage landet (bestimmt den Cache-Schlüssel): None = gar nicht (die Engine
    # filtert nur nach), () = genau in Tagen, (1, 7, 31, 366) = in festen Fenstern Tag/Woche/Monat/Jahr.
    # Undatierte Treffer bleiben nur, wenn das Fenster genau den gewünschten Tagen entspricht.
    time_windows: tuple[int, ...] | None = None

    def __init__(self) -> None:
        # Name wird beim Anhängen an die Engine (attach) noch einmal gebunden -> auch Unterklassen,
        # die self.name erst nach super().__init__() setzen, bekommen eigenen Sperrzustand.
        self.limiter = RateLimiter(self.min_interval, self.concurrency)
        self.breaker = CircuitBreaker(self.name, self.base_cooldown)

    @property
    def available(self) -> bool:
        """False, wenn z.B. der API-Key fehlt (Quelle wird dann übersprungen)."""
        return True

    def describe(self) -> str:
        """Kurzbeschreibung für 'websearch sources': erster Absatz des Docstrings bzw. englischer Text."""
        doc = " ".join((type(self).__doc__ or "").strip().split("\n\n")[0].split())
        return (doc if lang() == "de" else _ABOUT_EN.get(self.name, doc)) or self.name

    @property
    def cache_scope(self) -> str:
        """Fließt in den Cache-Schlüssel ein: andere Konfiguration (z.B. andere SearXNG-Instanz)
        = anderer Cache. Standard: nichts."""
        return ""

    def precheck(self, query: str, kind: str) -> None:
        """Vor dem Tempo-Slot: SourceSkipped werfen, wenn die Anfrage für diese Quelle sinnlos ist
        (kostet dann weder Slot noch Request)."""

    def check(self, resp: httpx.Response) -> None:
        """HTTP-Antwort klassifizieren: BlockedError (Pause), SourceUnavailable (kein Sperrverdacht).
        Quellen mit Sonderfällen überschreiben das und rufen danach super().check() auf."""
        _check_http(resp, self.name)

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        """Bis zu n Treffer liefern. Darf BlockedError / SourceUnavailable werfen; optional eine
        ResultList mit .meta zurückgeben (landet in SourceStatus['meta'])."""
        raise NotImplementedError


# --------------------------------------------------------------------------- Metasuche (Rückgrat)

class SearXNG(Provider):
    """Selbst gehostete SearXNG-Metasuche (Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia u.a.), auch
    Bilder und Videos. SearXNG pausiert gesperrte Engines intern selbst, daher darf man hier schneller fragen."""
    name = "searxng"
    kinds = ("web", "news", "ref", "images", "videos")
    max_native_days = 366
    time_windows = (1, 7, 31, 366)          # time_range=day|week|month|year
    via_proxy = False          # eigene Instanz: SearXNG hat seine eigenen Proxy-Einstellungen
    min_interval = 0.25
    concurrency = 6
    timeout = 20.0

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("SEARXNG_URL", "").strip() or "http://127.0.0.1:8888").rstrip("/")
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise InputError(t(f"SEARXNG_URL braucht http:// oder https:// (ist: {self.base_url!r})",
                               f"SEARXNG_URL needs http:// or https:// (got: {self.base_url!r})"))
        super().__init__()
        self._engines: dict[str, set[str]] | None = None     # aktive Engines je Kategorie (aus /config)
        self._engines_at = 0.0

    async def _active_engines(self, client: httpx.AsyncClient, category: str) -> set[str] | None:
        """Aktive Engines einer Kategorie laut /config (1 h gemerkt). None = unbekannt."""
        if self._engines is None or time.monotonic() - self._engines_at > 3600:
            try:
                r = await client.get(f"{self.base_url}/config", timeout=5)
                cats: dict[str, set[str]] = {}
                for e in r.json().get("engines", []):
                    if isinstance(e, dict) and e.get("enabled"):
                        for c in e.get("categories") or []:
                            cats.setdefault(str(c), set()).add(str(e.get("name")))
                self._engines, self._engines_at = cats, time.monotonic()
            except Exception as e:      # ohne /config wird vorsichtig "alles ausgefallen" angenommen
                log.debug(t("SearXNG /config nicht lesbar: %s", "SearXNG /config not readable: %s"), e)
                self._engines, self._engines_at = {}, time.monotonic() - 3600 + 60   # 1 min nicht erneut fragen
                return None
        return self._engines.get(category)

    @property
    def cache_scope(self) -> str:
        return self.base_url

    def check(self, resp: httpx.Response) -> None:
        if resp.status_code == 403 and "format" in resp.text.lower():
            raise SourceUnavailable(t("searxng: JSON-Format nicht freigeschaltet (search.formats: json)",
                                      "searxng: JSON format not enabled (search.formats: json)"))
        super().check(resp)

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        sx_lang = lang if lang and lang != "auto" and _lang_code(lang, "") else "auto"
        params = {"q": query, "format": "json", "language": sx_lang,
                  "categories": kind if kind in ("news", "images", "videos") else "general"}
        if time_range := time_window(days, _SX_RANGES):
            params["time_range"] = time_range
        try:
            r = await client.get(f"{self.base_url}/search", params=params, timeout=self.timeout)
        except httpx.ConnectError as e:
            raise SourceUnavailable(t(f"searxng nicht erreichbar unter {self.base_url}",
                                      f"searxng not reachable at {self.base_url}")) from e
        self.check(r)
        data = _json(r, self.name)
        if not isinstance(data, dict):
            raise SourceUnavailable(t("searxng: unerwartetes Antwortformat", "searxng: unexpected response format"))
        failed = _engine_failures(data.get("unresponsive_engines"))
        rows = [x for x in (data.get("results") or []) if isinstance(x, dict) and isinstance(x.get("url"), str)]
        # Bings Web-Index liefert bei mehrteiligen Anfragen gern Themenfremdes: Treffer, die NUR Bing
        # gefunden hat, müssen genügend Suchwörter enthalten (Konsens-Treffer bleiben immer)
        def text_of(x: dict) -> str:
            return f"{x.get('title') or ''} {x.get('content') or ''} {x['url']}"
        if kind in ("images", "videos"):
            pass   # Medien-Engines liefern kaum Text zum Prüfen; Titel sind oft Dateinamen
        elif kind == "news":
            # News-Engines (DDG/Qwant/Brave News) suchen unscharf, und "neueste zuerst" würde Themenfremdes
            # nach oben spülen -> alle News-Treffer prüfen
            rows = [x for x in rows if relevant(query, text_of(x))]
        else:
            rows = [x for x in rows if x.get("engines") != ["bing"] or relevant(query, text_of(x))]
        rows = rows[:n]
        if not rows and failed:
            # Haben andere Engines normal (leer) geantwortet, ist die Anfrage einfach selten -> ok mit 0.
            # Nur wenn ALLE aktiven Engines ausgefallen sind, ist das kein legitimes "nichts gefunden"
            # (typischer Rechenzentrums-Fall) - sonst würde jede seltene Suche Bezahlquellen auslösen.
            # "general" enthält auch Wikipedia/Wikidata, die praktisch nie ausfallen -> gegen die eigentlichen
            # Web-Indizes ("web") prüfen, sonst wäre ein Totalausfall von Google/Bing/DDG/Brave/Qwant unsichtbar
            cat = "web" if params["categories"] == "general" else params["categories"]
            active = await self._active_engines(client, cat)
            if active and cat != params["categories"]:
                # "web" enthält auch Bild-/Video-Engines (z.B. brave.images), die bei "general" gar nicht
                # gefragt wurden - nur die Schnittmenge zählt
                active = active & (await self._active_engines(client, params["categories"]) or active)
            if not active and cat != params["categories"]:
                active = await self._active_engines(client, params["categories"])
            if active and not active <= {e for e, _ in failed}:
                return ResultList([], engines_failed=failed)
            raise SourceUnavailable(t("searxng: keine Treffer, Engines ausgefallen: ",
                                      "searxng: no results, engines failed: ")
                                    + ", ".join(f"{e} ({why})" if why else e for e, why in failed),
                                    meta={"engines_failed": failed})   # -> Fallback überspringt genau diese
        def engines_of(x: dict) -> list[str]:
            raw = x.get("engines")
            return [f"searxng:{e}" for e in raw if isinstance(e, str)] if isinstance(raw, list) else []
        return ResultList(
            [Result(title=x.get("title") or "", url=x["url"], snippet=x.get("content") or "",
                    source=self.name, rank=i, published=x.get("publishedDate"), engines=engines_of(x),
                    image=(x.get("img_src") or "") if kind == "images" else "",
                    thumbnail=(x.get("thumbnail_src") or x.get("thumbnail") or "")
                    if kind in ("images", "videos") else "",
                    duration=_duration(x.get("length")) if kind == "videos" else "")
             for i, x in enumerate(rows, 1)],
            engines_failed=failed,
        )


def _duration(value) -> str:
    """Videolänge vereinheitlichen: Sekunden (Zahl) -> "m:ss" bzw. "h:mm:ss"; Text bleibt, wie er ist."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if not 0 <= value < 10**7:
            return ""
        total = int(value)
        h, rest = divmod(total, 3600)
        m, sec = divmod(rest, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    return str(value).strip()[:20] if isinstance(value, str) else ""


def _engine_failures(raw) -> list[list[str]]:
    """SearXNGs unresponsive_engines robust lesen -> [[engine, grund], ...]."""
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, (list, tuple)) and e and isinstance(e[0], str):
            out.append([e[0], str(e[1]) if len(e) > 1 and e[1] is not None else ""])
        elif isinstance(e, str):
            out.append([e, ""])
    return out


# --------------------------------------------------------------------------- ddgs-Bibliothek

async def _run_in_daemon_thread(fn):
    """Wie asyncio.to_thread, aber in einem Daemon-Thread: ein hängender ddgs-Request (Timeout erst
    nach 15 s) hält so weder das Programmende noch Strg+C auf."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def runner():
        def deliver(exc: BaseException | None, res=None) -> None:
            if not fut.done():
                fut.set_exception(exc) if exc is not None else fut.set_result(res)
        try:
            res = fn()
        except BaseException as e:   # noqa: BLE001 - wird im Event-Loop weitergereicht
            # e als Argument binden: Python löscht die except-Variable am Blockende
            with contextlib.suppress(RuntimeError):          # Loop schon geschlossen
                loop.call_soon_threadsafe(deliver, e)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, None, res)
    threading.Thread(target=runner, name="ddgs", daemon=True).start()
    return await fut


def _ddgs_info(backend: str) -> str:
    return {
        "yahoo": t("Yahoo-Websuche (Bing-Index) über die ddgs-Bibliothek.",
                   "Yahoo web search (Bing index) via the ddgs library."),
        "duckduckgo": t("DuckDuckGo (HTML-Version) über die ddgs-Bibliothek, auch Bilder/Videos; blockt "
                        "Rechenzentrums-IPs oft mit HTTP 202.",
                        "DuckDuckGo (HTML version) via the ddgs library, also images/videos; often blocks "
                        "data-center IPs with HTTP 202."),
        "brave": t("Brave-Websuche (eigener Index) über die ddgs-Bibliothek; drosselt schnell mit HTTP 429.",
                   "Brave web search (own index) via the ddgs library; throttles quickly with HTTP 429."),
        "startpage": t("Startpage (Google-Ergebnisse) über ddgs - nur explizit: Proof-of-Work-Captcha seit 2026.",
                       "Startpage (Google results) via ddgs - explicit only: proof-of-work captcha since 2026."),
        "mojeek": t("Mojeek (eigener Index) über ddgs - nur explizit: Proof-of-Work-Captcha seit 2026.",
                    "Mojeek (own index) via ddgs - explicit only: proof-of-work captcha since 2026."),
    }.get(backend, t(f"{backend} über die ddgs-Bibliothek.", f"{backend} via the ddgs library."))


class DDGS(Provider):
    """Python-Bibliothek `ddgs`: fragt einzelne Engines direkt ab (mit Browser-TLS-Fingerprint
    via primp). Zweites Standbein neben SearXNG; jede Backend-Engine sparsam nutzen.
    Proxy optional über DDGS_PROXY (wertet ddgs selbst aus)."""
    kinds: tuple[str, ...] = ("web", "news", "ref")
    min_interval = 3.0
    concurrency = 1
    base_cooldown = 600
    max_native_days = 366       # timelimit d / w / m / y
    time_windows: tuple[int, ...] | None = (1, 7, 31, 366)
    scraped = True
    _SAME = {"duckduckgo": "duckduckgo", "brave": "brave", "yahoo": "yahoo"}
    request_timeout = 6

    def __init__(self, backend: str = "duckduckgo"):
        self.backend = backend
        self.name = f"ddgs:{backend}"
        self.same_engine = self._SAME.get(backend)
        if backend == "duckduckgo":          # nur DuckDuckGo kann in ddgs Bilder UND Videos
            self.kinds = (*self.kinds, "images", "videos")
        if backend == "mojeek":              # ddgs ignoriert timelimit bei Mojeek -> nur nachfiltern
            self.max_native_days, self.time_windows = 0, None
        super().__init__()
        # Threads lassen sich nicht abbrechen: Slot erst freigeben, wenn der Thread WIRKLICH fertig ist,
        # sonst laufen nach einer Deadline mehrere Requests parallel gegen dieselbe Engine.
        self._inflight = asyncio.Semaphore(self.concurrency)

    def describe(self) -> str:
        return _ddgs_info(self.backend)

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        from ddgs import DDGS as _DDGS
        from ddgs.engines import ENGINES
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        cat = kind if kind in ("news", "images", "videos") else "text"
        if self.backend not in ENGINES.get(cat, {}):
            # sonst fällt ddgs still auf backend="auto" zurück und fragt ALLE Engines ab
            raise SourceUnavailable(t(f"{self.name}: Backend für '{cat}' in ddgs deaktiviert/unbekannt "
                                    f"(verfügbar: {sorted(ENGINES.get(cat, {}))})",
                                      f"{self.name}: backend for '{cat}' disabled/unknown in ddgs (available: {sorted(ENGINES.get(cat, {}))})"))
        # "wt-wt" bricht in ddgs 9.16 (fragt wt.wikipedia.org ab) -> us-en als Default
        region = {"de": "de-de", "en": "us-en", "fr": "fr-fr", "es": "es-es", "it": "it-it"}.get(
            _lang_code(lang, ""), "us-en")

        statuses: list[tuple[int, str]] = []

        def run():
            # kurzer Timeout: ddgs' eigener Thread-Pool hält sonst bei hängenden Engines das Programmende auf
            d = _DDGS(timeout=self.request_timeout)
            # ddgs verschluckt Sperren (gibt bei != 200 einfach None zurück und meldet "No results").
            # Deshalb HTTP-Status der Engine mitschneiden. Nutzt ddgs-Interna -> Version in pyproject gepinnt.
            try:
                for eng in d._get_engines(cat, self.backend):
                    orig = eng.http_client.request

                    def wrapped(*a, _orig=orig, **k):
                        resp = _orig(*a, **k)
                        statuses.append((resp.status_code, (resp.text or "")[:6000].lower()))
                        return resp
                    eng.http_client.request = wrapped  # type: ignore[method-assign]
            except Exception as e:   # Interna geändert -> ohne Mitschnitt weiter (Sperren dann nur heuristisch)
                log.debug(t("ddgs-Statusmitschnitt nicht möglich: %s",
                            "ddgs status capture not possible: %s"), e)
            fn = getattr(d, cat)
            extra = {"timelimit": tl} if (tl := time_window(days, _DMWY)) else {}
            return fn(query, region=region, backend=self.backend, max_results=n, **extra)

        def blocked_reason() -> str | None:
            for code, body in statuses:
                if code in (403, 429) or (code == 202 and self.backend == "duckduckgo"):
                    return f"HTTP {code}"
                if any(m in body for m in _BLOCK_MARKERS):
                    return t("Sperrseite/Captcha erkannt", "block page/captcha detected")
            return None

        def server_error() -> str | None:
            codes = sorted({c for c, _ in statuses if c >= 400})
            return f"HTTP {', '.join(map(str, codes))}" if codes else None

        await self._inflight.acquire()
        fut = asyncio.ensure_future(_run_in_daemon_thread(run))
        def _done(f: asyncio.Future) -> None:
            self._inflight.release()
            if not f.cancelled():
                f.exception()          # abholen, sonst meldet asyncio "exception was never retrieved"
        fut.add_done_callback(_done)
        try:
            rows = await asyncio.shield(fut)
        except RatelimitException as e:
            raise BlockedError(f"{self.name}: {e}") from e
        except TimeoutException as e:
            raise SourceUnavailable(f"{self.name}: Timeout") from e
        except DDGSException as e:
            if reason := blocked_reason():
                raise BlockedError(f"{self.name}: {reason}") from e
            if reason := server_error():          # z.B. Yahoo HTTP 500 -> nicht als "0 Treffer" tarnen
                raise SourceUnavailable(f"{self.name}: {reason}") from e
            if "no results" in str(e).lower():
                return []   # möglicherweise stille Sperre -> Erkennung in der Engine
            raise SourceUnavailable(f"{self.name}: {e}") from e
        rows = [x for x in rows if isinstance(x, dict)]
        if kind == "images":       # url = Seite mit dem Bild, image = das Bild selbst
            return [Result(title=x.get("title", ""), url=x.get("url") or "", source=self.name, rank=i,
                           image=x.get("image") or "", thumbnail=x.get("thumbnail") or "",
                           snippet=x.get("source") or "")
                    for i, x in enumerate(rows, 1)]
        if kind == "videos":
            def thumb(x: dict) -> str:
                imgs = x.get("images")
                return (imgs.get("medium") or imgs.get("large") or imgs.get("small") or "") \
                    if isinstance(imgs, dict) else ""
            return [Result(title=x.get("title", ""), url=x.get("content") or "", source=self.name, rank=i,
                           snippet=x.get("description") or x.get("publisher") or "",
                           published=x.get("published"), thumbnail=thumb(x),
                           duration=_duration(x.get("duration")))
                    for i, x in enumerate(rows, 1)]
        rows = [x for x in rows if x.get("href") or x.get("url")]
        return [
            Result(title=x.get("title", ""), url=x.get("href") or x.get("url"),
                   snippet=x.get("body", ""), source=self.name, rank=i, published=x.get("date"))
            for i, x in enumerate(rows, 1)
        ]


# --------------------------------------------------------------------------- Offizielle, freie Schnittstellen

class BingRSS(Provider):
    """Bing liefert Suchergebnisse als RSS (format=rss). Laut Feed nur für privaten,
    nicht-kommerziellen Gebrauch - für kommerzielle Nutzung WEBSEARCH_DISABLE=bing_rss setzen."""
    name = "bing_rss"
    kinds = ("web", "news", "ref")
    min_interval = 2.0
    scraped = True
    same_engine = "bing"

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        path = "news/search" if kind == "news" else "search"
        params: dict[str, str | int] = {"q": query, "format": "rss", "count": min(n, 50)}
        if code := _lang_code(lang, ""):
            params["setlang"] = code
        r = await client.get(f"https://www.bing.com/{path}", params=params,
                             headers={"User-Agent": BROWSER_UA}, timeout=self.timeout)
        self.check(r)
        results = _parse_rss(r.text, self.name, n)
        if kind == "news":
            return results
        # Bings RSS-Endpunkt liefert bei mehrteiligen Anfragen gern Themenfremdes ("open source ..." ->
        # OpenAI, Openbank). Behalten wird nur, was genügend Suchwörter enthält.
        kept = [r for r in results if relevant(query, f"{r.title} {r.snippet} {r.url}")]
        return ResultList(kept, filtered=len(results) - len(kept))


class Wikipedia(Provider):
    """Wikipedia-Volltextsuche (offizielle MediaWiki-API, ohne Key) in der gewählten Sprache."""
    name = "wikipedia"
    kinds = ("ref",)
    min_interval = 0.2
    concurrency = 4

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        wl = _lang_code(lang, "en")
        r = await client.get(f"https://{wl}.wikipedia.org/w/api.php", timeout=self.timeout,
                             params={"action": "query", "list": "search", "srsearch": query,
                                     "srlimit": min(n, 50), "format": "json", "utf8": 1},
                             headers={"User-Agent": bot_ua()})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("query", {}).get("search", [])
                if relevant(query, f"{x.get('title', '')} {_strip_tags(x.get('snippet', ''))}", strict=True)]
        return [
            Result(title=x["title"], source=self.name, rank=i, snippet=_strip_tags(x.get("snippet", "")),
                   url=f"https://{wl}.wikipedia.org/wiki/{quote(x['title'].replace(' ', '_'), safe=':/()_,-.!~')}")
            for i, x in enumerate(rows, 1)
        ]


class Marginalia(Provider):
    """Unabhängiger Index (kleines/nicht-kommerzielles Web). Key 'public' ist geteilt und oft
    ausgeschöpft; eigener kostenloser Key per Mail an contact@marginalia-search.com."""
    name = "marginalia"
    kinds = ("web",)
    min_interval = 2.0
    base_cooldown = 120

    def check(self, resp: httpx.Response) -> None:
        if resp.status_code == 503 or "qpm limit" in resp.text[:200].lower():   # geteilter Key ausgeschöpft
            raise BlockedError(f"{self.name}: Rate-Limit ({resp.status_code})")
        super().check(resp)

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        key = os.environ.get("MARGINALIA_KEY", "public")
        r = await client.get("https://api2.marginalia-search.com/search", timeout=self.timeout,
                             params={"query": query, "count": min(n, 100)},
                             headers={"API-Key": key, "User-Agent": bot_ua()})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("results", []) if x.get("url")]
        return [Result(title=x.get("title", ""), url=x["url"], snippet=x.get("description", ""),
                       source=self.name, rank=i) for i, x in enumerate(rows, 1)]


class GoogleNewsRSS(Provider):
    """Google News Suche als RSS - offizieller Feed, kein Scraping, ideal für Monitoring."""
    name = "google_news"
    kinds = ("news",)
    min_interval = 3.0
    time_windows = ()           # when:Nd

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        hl, gl = ("de", "DE") if _lang_code(lang) == "de" else ("en-US", "US")
        # Suchoperator when:7d schränkt Google selbst ein (Treffer sind ohnehin datiert -> Engine filtert genau)
        q = f"{query} when:{days}d" if days else query
        r = await client.get("https://news.google.com/rss/search", timeout=self.timeout,
                             params={"q": q, "hl": hl, "gl": gl, "ceid": f"{gl}:{hl.split('-')[0]}"},
                             headers={"User-Agent": BROWSER_UA})
        self.check(r)
        results = _parse_rss(r.text, self.name, n)
        for res in results:
            res.redirect = urlsplit(res.url).hostname == "news.google.com"   # beim Mergen direkte URL bevorzugen
        return results


class HackerNews(Provider):
    """Algolia-HN-API: offiziell, frei, ohne Key. Relevanteste Stories der letzten 30 Tage."""
    name = "hackernews"
    kinds = ("news",)
    sparse = True
    max_native_days = 3650
    time_windows = ()           # numericFilters=created_at_i>...
    min_interval = 0.5
    concurrency = 2

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        since = int(time.time()) - (days or 30) * 86400
        r = await client.get("https://hn.algolia.com/api/v1/search", timeout=self.timeout,
                             params={"query": query, "tags": "story", "hitsPerPage": min(n, 50),
                                     "typoTolerance": "false",
                                     "numericFilters": f"created_at_i>{since}"},
                             headers={"User-Agent": bot_ua()})
        self.check(r)
        # Algolia sucht unscharf (Treffer im Text eines langen Beitrags genügen) - deshalb selbst prüfen
        short = short_terms(query)

        def on_topic(x: dict) -> bool:
            title, body = f"{x.get('title') or ''} {x.get('url') or ''}", x.get("story_text") or ""
            words = {w.lower() for w in _WORD.findall(f"{title} {body}")}
            return (relevant(query, f"{title} {body}") and relevant(query, title, minimum=1)
                    and short <= words)
        hits = [x for x in _json(r, self.name).get("hits", []) if isinstance(x, dict) and on_topic(x)]
        return [
            Result(title=x.get("title") or "", source=self.name, rank=i, published=x.get("created_at"),
                   url=x.get("url") or f"https://news.ycombinator.com/item?id={x['objectID']}",
                   snippet=f"{x.get('points', 0)} points, {x.get('num_comments', 0)} comments")
            for i, x in enumerate(hits, 1)
        ]


class GDELT(Provider):
    """GDELT DOC 2.0: weltweite Nachrichten, frei, ohne Key. Max. 1 Request / 5 s pro IP."""
    name = "gdelt"
    kinds = ("news",)
    sparse = True
    min_interval = 6.0
    timeout = 40.0
    max_native_days = 90        # GDELT DOC durchsucht höchstens die letzten 3 Monate
    time_windows = ()           # timespan=Nd
    base_cooldown = 900

    @staticmethod
    def build_query(query: str) -> str:
        """GDELT lehnt Wörter < 3 Zeichen ab ('keyword too short'), auch in Phrasen.
        Kurze Wörter (z.B. 'EU', 'AI') entfernen; Operatoren wie sourcelang: bleiben erhalten."""
        parts = re.findall(r'"[^"]*"|\S+', query)
        out = []
        for p in parts:
            if p.startswith('"'):
                words = [w for w in p.strip('"').split() if len(w) >= 3]
                if len(words) > 1:
                    out.append('"' + " ".join(words) + '"')
                elif words:
                    out.append(words[0])
            elif ":" in p or p.startswith("-") or len(p) >= 3:
                out.append(p)
        return " ".join(out)

    def precheck(self, query: str, kind: str) -> None:
        q = self.build_query(query)
        terms = re.findall(r"\w+", re.sub(r"\b\w+:\S+", " ", query))
        kept = re.findall(r"\w+", re.sub(r"\b\w+:\S+", " ", q))
        if not q or len(kept) * 2 <= len(terms):
            # z.B. "EU AI Act" -> nur "Act", "AI Act" -> nur "Act": wäre eine ganz andere Suche
            raise SourceSkipped(t("gdelt: mindestens die Hälfte der Suchwörter ist kürzer als 3 Zeichen - GDELT "
                                "kann die nicht suchen, übersprungen",
                                  "gdelt: at least half of the search words are shorter than 3 characters - GDELT cannot search them, skipped"))

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        self.precheck(query, kind)
        q = self.build_query(query)
        if _lang_code(lang, "") == "de":
            q += " sourcelang:german"
        r = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", timeout=self.timeout,
                             params={"query": q, "mode": "artlist", "format": "json",
                                     "maxrecords": min(max(n, 1), 250), "sort": "datedesc",
                                     "timespan": f"{min(days, 90)}d" if days else "7d"},
                             headers={"User-Agent": bot_ua()})
        self.check(r)          # 429 -> BlockedError
        try:
            arts = r.json().get("articles", [])
        except (ValueError, RecursionError):      # GDELT liefert Fehler (Rate-Limit, "phrase too short" ...) als Klartext
            if "limit requests" in r.text[:300].lower():
                raise BlockedError("gdelt: Rate-Limit") from None
            raise SourceUnavailable(f"gdelt: {r.text[:120]}") from None
        # Was GDELT nicht suchen konnte ("AI" in "AI Act"), muss wenigstens im Titel stehen
        short = short_terms(query)
        def on_topic(title: str) -> bool:
            words = {w.lower() for w in _WORD.findall(title)}
            return relevant(query, title) and short <= words
        arts = [a for a in arts if isinstance(a, dict) and a.get("url") and on_topic(a.get("title") or "")]
        return [Result(title=a.get("title", ""), url=a["url"], snippet=a.get("domain", ""),
                       source=self.name, rank=i, published=a.get("seendate"))
                for i, a in enumerate(arts, 1)]


# --------------------------------------------------------------------------- Optionale Bezahl-/Freikontingent-APIs

class _KeyProvider(Provider):
    """Quelle mit API-Key aus der Umgebung. Sperren (Guthaben leer, Key ungültig) ändern sich nicht
    in Minuten -> lange Grundpause (1 h, verdoppelt bis 6 h)."""
    env_key = ""
    base_cooldown = 3600
    paid = True
    via_proxy = False          # offizielle APIs mit Key: kein Grund, Key und Anfragen über einen Proxy zu leiten

    @property
    def available(self) -> bool:
        return bool(os.environ.get(self.env_key))

    @property
    def key(self) -> str:
        return os.environ[self.env_key]


class BraveAPI(_KeyProvider):
    """Brave Search API (eigener Index). 5 $/1.000 Anfragen, 5 $ Freiguthaben pro Monat, Kreditkarte nötig."""
    name = "brave_api"
    env_key = "BRAVE_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 1.1
    max_native_days = 3650      # freshness=JJJJ-MM-TTtoJJJJ-MM-TT
    time_windows = ()

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        ep = "news" if kind == "news" else "web"
        params: dict[str, str | int] = {"q": query, "count": min(n, 20)}
        if code := _lang_code(lang, ""):
            params["search_lang"] = code
        if days:
            params["freshness"] = f"{_date_ago(days)}to{_date_ago(0)}"
        r = await client.get(f"https://api.search.brave.com/res/v1/{ep}/search", timeout=self.timeout,
                             params=params,
                             headers={"X-Subscription-Token": self.key, "Accept": "application/json"})
        self.check(r)
        data = _json(r, self.name)
        rows = data.get("results", []) if ep == "news" else data.get("web", {}).get("results", [])
        return [Result(title=_strip_tags(x.get("title", "")), url=x["url"],
                       snippet=_strip_tags(x.get("description", "")),
                       source=self.name, rank=i, published=x.get("page_age"))
                for i, x in enumerate([x for x in rows if x.get("url")], 1)]


class SerperAPI(_KeyProvider):
    """Serper.dev: Google-Ergebnisse per API. 2.500 Gratisanfragen, danach Prepaid ab ca. 1 $/1.000."""
    name = "serper"
    env_key = "SERPER_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 0.2
    max_native_days = 366       # tbs=qdr:d|w|m|y
    time_windows = (1, 7, 31, 366)
    concurrency = 3

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        body = {"q": query, "num": 10 if n <= 10 else min(n, 100)}   # >10 Treffer kostet 2 Credits
        if (code := _lang_code(lang, "")):
            body["hl"] = code
            if code == "de":
                body["gl"] = "de"
        if tl := time_window(days, _DMWY):
            body["tbs"] = f"qdr:{tl}"
        ep = "news" if kind == "news" else "search"
        r = await client.post(f"https://google.serper.dev/{ep}", json=body, timeout=self.timeout,
                              headers={"X-API-KEY": self.key})
        self.check(r)
        rows = _json(r, self.name).get("news" if kind == "news" else "organic", [])
        return [Result(title=x.get("title", ""), url=x["link"], snippet=x.get("snippet", ""),
                       source=self.name, rank=i, published=x.get("date") if kind == "news" else None)
                for i, x in enumerate([x for x in rows if x.get("link")][:n], 1)]


class TavilyAPI(_KeyProvider):
    """Tavily: für LLMs aufbereitete Suche. 1.000 Credits/Monat gratis (ohne Karte), danach 0,008 $/Credit."""
    name = "tavily"
    env_key = "TAVILY_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 0.5
    max_native_days = 366       # time_range=day|week|month|year
    time_windows = (1, 7, 31, 366)

    def check(self, resp: httpx.Response) -> None:
        if resp.status_code in (432, 433):          # laut Tavily-Doku: Plan-/PAYG-Limit erreicht
            raise BlockedError(t(f"{self.name}: HTTP {resp.status_code} - Kontingent erreicht",
                                 f"{self.name}: HTTP {resp.status_code} - quota reached"))
        super().check(resp)

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        body = {"query": query, "max_results": min(n, 20), "search_depth": "basic",
                "topic": "news" if kind == "news" else "general"}
        if time_range := time_window(days, _SX_RANGES):
            body["time_range"] = time_range
        r = await client.post("https://api.tavily.com/search", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.key}"})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("results", []) if x.get("url")]
        return [Result(title=x.get("title", ""), url=x["url"], snippet=(x.get("content") or "")[:500],
                       source=self.name, rank=i, published=x.get("published_date"))
                for i, x in enumerate(rows, 1)]


class ExaAPI(_KeyProvider):
    """Exa: eigener (neuronaler) Index. 10 $ Freiguthaben pro Monat ohne Zahlungsmittel."""
    name = "exa"
    env_key = "EXA_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 0.3
    max_native_days = 3650      # startPublishedDate
    time_windows = ()

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        # "fast"/Lite-Tarif (4 $/1k laut Exa) und max. 10 Treffer: jeder weitere Treffer kostet extra
        body = {"query": query, "numResults": min(n, 10), "type": os.environ.get("EXA_SEARCH_TYPE", "fast")}
        if kind == "news":
            body["category"] = "news"
        if days:
            body["startPublishedDate"] = f"{_date_ago(days)}T00:00:00.000Z"
        r = await client.post("https://api.exa.ai/search", json=body, timeout=self.timeout,
                              headers={"x-api-key": self.key})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("results", []) if x.get("url")]
        return [Result(title=x.get("title") or "", url=x["url"],
                       snippet=" ".join(x.get("highlights") or [])[:500] or (x.get("text") or "")[:500],
                       source=self.name, rank=i, published=x.get("publishedDate"))
                for i, x in enumerate(rows, 1)]


class PerplexityAPI(_KeyProvider):
    """Perplexity Search API: rohe Suchtreffer (kein LLM-Antworttext). Fast-Modus 1 $/1.000 (Standard)."""
    name = "perplexity"
    env_key = "PERPLEXITY_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 0.3

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        # "fast" = 1 $/1k statt 5 $/1k (laut Perplexity-Preisseite); PERPLEXITY_SEARCH_TYPE=pro für den Standardmodus
        body = {"query": query, "max_results": min(n, 10)}
        if (mode := os.environ.get("PERPLEXITY_SEARCH_TYPE", "fast")) != "pro":
            body["search_type"] = mode
        r = await client.post("https://api.perplexity.ai/search", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.key}"})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("results", []) if x.get("url")]
        return [Result(title=x.get("title") or "", url=x["url"], snippet=(x.get("snippet") or "")[:500],
                       source=self.name, rank=i, published=x.get("date") or x.get("last_updated"))
                for i, x in enumerate(rows, 1)]


class LinkupAPI(_KeyProvider):
    """Linkup: Such-API für Agents. Monatliches Gratiskontingent (laut Anbieter), danach 5 $/1.000."""
    name = "linkup"
    env_key = "LINKUP_API_KEY"
    kinds = ("web", "news", "ref")
    min_interval = 0.3
    max_native_days = 3650      # fromDate
    time_windows = ()

    async def search(self, client: httpx.AsyncClient, query: str, n: int, lang: str,
                     kind: str, days: int | None = None) -> list[Result]:
        body: dict[str, str | int] = {"q": query, "depth": "standard", "outputType": "searchResults",
                                      "maxResults": min(n, 10)}
        if days:
            body["fromDate"] = _date_ago(days)
        r = await client.post("https://api.linkup.so/v1/search", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.key}"})
        self.check(r)
        rows = [x for x in _json(r, self.name).get("results", []) if x.get("url")]
        return [Result(title=x.get("name") or x.get("title") or "", url=x["url"],
                       snippet=(x.get("content") or "")[:500], source=self.name, rank=i)
                for i, x in enumerate(rows, 1)]


def disabled_sources() -> set[str]:
    """Per WEBSEARCH_DISABLE (kommagetrennt) abgeschaltete Quellen."""
    return {s.strip() for s in os.environ.get("WEBSEARCH_DISABLE", "").split(",") if s.strip()}


def default_providers() -> dict[str, Provider]:
    from .reddit import Reddit          # eigenes Modul, importiert selbst aus providers
    ps: list[Provider] = [
        SearXNG(),
        BingRSS(), GoogleNewsRSS(), HackerNews(), GDELT(), Wikipedia(), Marginalia(),
        Reddit(),                       # nur mit REDDIT_CLIENT_ID/_SECRET (kostenlose OAuth-App)
        # ddgs 9.16: google/bing/yandex sind für Text deaktiviert; Yahoo liefert Bing-Ergebnisse
        DDGS("yahoo"), DDGS("duckduckgo"), DDGS("brave"),
        # nur explizit per sources=[...]: Proof-of-Work-Captcha seit 2026
        DDGS("startpage"), DDGS("mojeek"),
        # Reihenfolge wie in der Bezahlstufe (DEFAULT_POLICY.paid)
        ExaAPI(), LinkupAPI(), TavilyAPI(), BraveAPI(), PerplexityAPI(), SerperAPI(),
    ]
    disabled = disabled_sources()
    if unknown := disabled - {p.name for p in ps}:
        log.warning(t("WEBSEARCH_DISABLE nennt unbekannte Quellen (Tippfehler?): %s",
                      "WEBSEARCH_DISABLE names unknown sources (typo?): %s"), ", ".join(sorted(unknown)))
    return {p.name: p for p in ps if p.name not in disabled}
