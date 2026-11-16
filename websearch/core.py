"""Kernbausteine: Datenmodell, Rate-Limiter, Circuit-Breaker, Zustandsspeicher, Normalisierung, Ranking.

Grundprinzip: Captchas werden nicht gelöst, sondern vermieden.
  * jede Quelle hat ein eigenes Tempo (Mindestabstand) und eine Parallelitätsgrenze - hostweit,
    d.h. über alle Prozesse (CLI, Monitor, MCP), weil die Slots in SQLite vergeben werden
  * meldet eine Quelle eine Sperre (Captcha, 429, 403 ...), wird sie mit wachsender Pause
    abgeschaltet statt weiter angefragt (sonst verlängert sich die Sperre); auch dieser Zustand
    liegt in SQLite und gilt für alle Prozesse
  * identische Anfragen werden aus dem Cache beantwortet
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Literal, NamedTuple, TypeVar

from typing_extensions import Concatenate, NotRequired, ParamSpec, TypedDict
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .i18n import t

log = logging.getLogger("websearch")

# Bei jeder Änderung am Result-Format erhöhen -> alte Cache-Einträge werden ignoriert
CACHE_VERSION = 4


# =========================================================================== Datenmodell

@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    source: str = ""                 # liefernde Quelle, z.B. "searxng", "google_news"
    rank: int = 0                    # Position in der Quelle (1-basiert)
    published: str | None = None     # ISO-8601 in UTC, falls bekannt (News)
    engines: list[str] = field(default_factory=list)  # alle Quellen/Engines, die den Treffer lieferten
    score: float = 0.0               # nach dem Mergen gesetzt (Reciprocal Rank Fusion)
    redirect: bool = False           # URL ist eine Weiterleitung (z.B. Google News) -> direkte URL bevorzugen
    # nur Bild-/Videosuche (kind "images" / "videos"); url ist dort die Seite, auf der das Medium steht
    image: str = ""                  # direkte Bild-URL (images)
    thumbnail: str = ""              # Vorschaubild (images, videos)
    duration: str = ""               # Laufzeit wie "12:34" (videos)

    def __post_init__(self) -> None:
        # Fremde APIs liefern gelegentlich null/Zahlen/Objekte - hier vereinheitlichen, damit ein
        # einzelner kaputter Eintrag nie Merge, Ausgabe oder MCP-Schema sprengt
        self.title = _as_text(self.title)
        self.snippet = _as_text(self.snippet)
        self.url = self.url.strip() if isinstance(self.url, str) else ""
        if not self.url.lower().startswith(("http://", "https://")):   # javascript:, ftp:, Müll -> verwerfen
            self.url = ""
        self.source = _as_text(self.source)
        for attr in ("image", "thumbnail"):          # Medien-URLs: nur http(s), sonst leer
            v = getattr(self, attr)
            setattr(self, attr, v.strip() if isinstance(v, str) and v.strip().lower().startswith(
                ("http://", "https://")) else "")
        self.duration = _as_text(self.duration)
        self.engines = [e for e in self.engines if isinstance(e, str)] if isinstance(self.engines, list) else []
        if isinstance(self.published, bool) or (self.published is not None
                                                and not isinstance(self.published, (str, int, float))):
            self.published = None
        if not isinstance(self.rank, int) or isinstance(self.rank, bool):
            self.rank = 0

    def to_dict(self) -> ResultDict:
        return asdict(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v) if isinstance(v, (int, float)) else ""


SourceState = Literal[
    "ok",              # Antwort erhalten (count kann 0 sein)
    "cached",          # aus dem Cache
    "blocked",         # Sperre erkannt -> Quelle jetzt pausiert
    "paused",          # war bereits pausiert, nicht angefragt
    "rate_limited",    # nächster freier Slot liegt hinter dem Zeitbudget, nicht angefragt
    "unavailable",     # Timeout, 5xx, ungültige Antwort, nicht erreichbar
    "unsupported",     # Quelle kann diese Suchart nicht
    "not_configured",  # API-Key fehlt bzw. Bezahlquellen für diese Suche nicht erlaubt
    "budget",          # Tages-/Monatslimit für Bezahl-APIs erreicht
    "skipped",         # bewusst nicht gefragt (z.B. Anfrage passt nicht zur Quelle)
    "deadline",        # Zeitbudget der Suche abgelaufen
    "error",           # unerwarteter Fehler (Bug) -> wird mit Traceback geloggt
]
SUCCESS_STATES = ("ok", "cached")
# Zustände, die KEIN Ausfall der Quelle sind: rechtfertigen weder Bezahlabfragen noch "blind"-Warnungen
NON_FAILURE_STATES = (*SUCCESS_STATES, "rate_limited", "unsupported", "not_configured", "budget", "skipped")


def env_flag(name: str, default: bool = False) -> bool:
    """Ja/Nein-Umgebungsvariable einheitlich lesen: 1/true/yes/on bzw. 0/false/no/off (sonst default)."""
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on", "ja"):
        return True
    if v in ("0", "false", "no", "off", "nein"):
        return False
    return default


@contextlib.contextmanager
def _exclusive(lock_path: Path):
    """Prozessübergreifende Sperre über eine Sperrdatei (fcntl.flock; ohne fcntl, z.B. Windows: keine Sperre)."""
    try:
        import fcntl
    except ImportError:                       # pragma: no cover
        yield
        return
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def private_db_file(path: Path) -> None:
    """Verzeichnis (0700, wenn neu angelegt) und SQLite-Datei samt -wal/-shm (0600) nur für den eigenen Nutzer:
    Cache und Monitoring-DB enthalten Suchverlauf und abgerufene Seiten."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if not path.exists():    # SQLite legt -wal/-shm später mit denselben Rechten wie die Datei an
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        for suffix in ("", "-wal", "-shm"):
            p = path.with_name(path.name + suffix)
            if p.exists() and p.stat().st_mode & 0o077:
                p.chmod(0o600)
    except OSError:              # z.B. fremde Datei im geteilten Volume - SQLite meldet echte Probleme selbst
        pass


# Mindestlänge für MCP_AUTH_TOKEN (Server und doctor warnen darunter)
MIN_TOKEN_LEN = 32


def in_container() -> bool:
    """Läuft der Prozess in einem Container? Docker legt /.dockerenv an, Podman /run/.containerenv;
    WEBSEARCH_IN_CONTAINER=1/0 erzwingt das Ergebnis (z.B. für bwrap-Sandboxen oder andere Laufzeiten)."""
    forced = os.environ.get("WEBSEARCH_IN_CONTAINER", "").strip()
    if forced:
        return forced.lower() in ("1", "true", "yes", "on", "ja")
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def is_benign(status: "SourceStatus") -> bool:
    """Kein Ausfall: Erfolg, bewusst ausgelassen (NON_FAILURE_STATES) oder nur nicht abgewartet, weil
    schnellere Quellen schon genug Treffer hatten (deadline mit meta.early)."""
    return status["state"] in NON_FAILURE_STATES or bool(status.get("meta", {}).get("early"))


def env_int(name: str, default: int, minimum: int = 0) -> int:
    """Ganzzahl aus der Umgebung; leer = Standard; Unsinn -> InputError mit Variablennamen."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        raise InputError(t(f"{name} muss eine ganze Zahl sein (ist: {raw!r})",
                           f"{name} must be a whole number (got: {raw!r})")) from None


def env_path(name: str, default: str) -> str:
    """Pfad aus der Umgebung; leere Werte gelten als nicht gesetzt (z.B. `WEBSEARCH_CACHE=` in .env)."""
    return os.environ.get(name, "").strip() or default


class SourceStatus(TypedDict):
    state: SourceState
    count: int
    ms: int
    message: str | None
    meta: dict[str, Any]           # quellenspezifisch, z.B. {"engines_failed": [["google", "CAPTCHA"]]}


class ResultDict(TypedDict):
    """Ein Treffer in der Antwort (Result.to_dict())."""
    title: str
    url: str
    snippet: str
    source: str
    rank: int
    published: str | None
    engines: list[str]
    score: float
    redirect: bool
    image: str
    thumbnail: str
    duration: str


class SearchResponse(TypedDict):
    query: str
    kind: str
    results: list[ResultDict]
    sources: dict[str, SourceStatus]
    error: NotRequired[str]        # nur bei search_many: ungültige Einzelanfrage


class PageResponse(TypedDict):
    """Ergebnis von SearchEngine.fetch()."""
    url: str
    final_url: str
    title: str | None
    content: str                   # Markdown (bzw. Text)
    truncated: bool
    content_type: str
    cached: bool


class SourceInfo(TypedDict):
    """Eintrag von SearchEngine.status()."""
    available: bool
    kinds: list[str]
    min_interval_s: float
    scraped: bool
    blocked: bool
    retry_in_s: int
    strikes: int
    last_error: str | None


def make_status(state: SourceState, count: int = 0, message: str | None = None,
                meta: dict | None = None, ms: int = 0) -> SourceStatus:
    return {"state": state, "count": count, "ms": ms, "message": message, "meta": meta or {}}


class WebsearchError(Exception):
    """Basisklasse aller erwarteten Fehler dieses Pakets (alles andere ist ein Bug)."""


class InputError(WebsearchError, ValueError):
    """Ungültige Eingabe des Aufrufers (leere Anfrage, unbekannte Quelle ...). CLI: Exit-Code 2,
    MCP: lesbarer Tool-Fehler."""


class BlockedError(WebsearchError):
    """Quelle hat uns gesperrt (Captcha, 429, 403, Guthaben leer ...). Löst eine Pause aus."""


class SourceUnavailable(WebsearchError):
    """Quelle vorübergehend nicht nutzbar (Timeout, 5xx, ungültige Antwort) - ohne Sperrverdacht.
    `meta` wird in den SourceStatus übernommen (z.B. welche SearXNG-Engines ausgefallen sind)."""

    def __init__(self, message: str, meta: dict | None = None):
        super().__init__(message)
        self.meta = meta or {}


class RateLimited(WebsearchError):
    """Der nächste freie Slot liegt hinter dem Zeitbudget - Anfrage wird gar nicht erst gestellt."""

    def __init__(self, wait: float, queued: bool = False):
        super().__init__(t(f"wartet {wait:.1f}s hinter laufenden Anfragen derselben Quelle",
                           f"waits {wait:.1f}s behind running requests to the same source") if queued
                         else t(f"nächster Slot erst in {wait:.1f}s", f"next slot only in {wait:.1f}s"))
        self.wait = wait


class SourceSkipped(WebsearchError):
    """Quelle wird für diese Anfrage bewusst nicht gefragt (z.B. GDELT bei zu kurzen Wörtern) - kein Ausfall."""


class BudgetExceeded(WebsearchError):
    """Tages-/Monatslimit für Bezahl-APIs erreicht."""


# =========================================================================== Zustandsspeicher

class BreakerState(NamedTuple):
    strikes: int = 0
    open_until: float = 0.0
    last_error: str | None = None
    empty_streak: int = 0


_P = ParamSpec("_P")
_T = TypeVar("_T")


def _guarded(fn: Callable[Concatenate["StateStore", _P], _T]) -> Callable[Concatenate["StateStore", _P], _T]:
    """Für StateStore-Methoden: ist die Datei gesperrt, für einige Sekunden gar nicht mehr fragen
    ("degradiert"), statt bei jeder Quelle und jedem Aufruf erneut busy_timeout abzuwarten."""
    @functools.wraps(fn)
    def wrapper(self: "StateStore", *a: _P.args, **k: _P.kwargs) -> _T:
        if time.monotonic() < self._degraded_until:
            raise sqlite3.OperationalError(t("Zustandsdatei vorübergehend gesperrt (degradiert)",
                                             "state file temporarily unavailable (degraded)"))
        try:
            return fn(self, *a, **k)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                self._degraded_until = time.monotonic() + self.DEGRADED_FOR_S
                log.warning(t("Zustandsdatei %s gesperrt - arbeite %ss ohne sie weiter",
                              "state file %s locked - continuing %ss without it"), self.path,
                            self.DEGRADED_FOR_S)
            raise
    return wrapper


class StateStore:
    """Eine SQLite-Datei für Cache, Sperrzustand, Tempo-Slots und Bezahl-Zähler.

    WAL + busy_timeout + BEGIN IMMEDIATE -> mehrere Prozesse eines Hosts können sie gleichzeitig
    nutzen. Die Zugriffe sind kurz (ms) und laufen synchron in der Ereignisschleife; deshalb ist der
    busy_timeout im laufenden Betrieb kurz (250 ms), und nach einer Sperre wird die Datei einige
    Sekunden lang gar nicht gefragt (Tempo/Sperren gelten dann nur prozessintern)."""

    BUSY_TIMEOUT_MS = 250          # laufender Betrieb
    STARTUP_TIMEOUT_S = 10.0       # Anlegen des Schemas (einmalig, darf warten)
    DEGRADED_FOR_S = 5.0
    PURGE_EVERY = 500

    def __init__(self, path: str | Path, ttl: float = 6 * 3600, max_age_purge: float = 7 * 86400):
        self.path = Path(path)
        self.ttl = ttl
        self.max_age_purge = max_age_purge
        self._writes = 0
        self._degraded_until = 0.0
        private_db_file(self.path)
        self._lock = threading.Lock()
        try:
            self._open()
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e)):
                raise
            # Kaputte Datei: Cache, Pausen, Tempo sind entbehrlich -> beiseitelegen und neu anlegen. Die
            # Bezahl-Zähler aber nicht: sie werden gerettet. Mehrere Prozesse (MCP + Monitor im selben Volume)
            # dürfen das nicht gleichzeitig tun: Sperrdatei, und wer zu spät kommt, nutzt die neue Datei.
            with contextlib.suppress(sqlite3.Error):
                self._db.close()
            with _exclusive(self.path.with_name(self.path.name + ".lock")):
                try:
                    self._open()             # hat ein anderer Prozess schon repariert?
                    return
                except sqlite3.DatabaseError:
                    with contextlib.suppress(sqlite3.Error):
                        self._db.close()
                broken = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}-{os.getpid()}")
                log.warning(t("Zustandsdatei %s ist beschädigt (%s) - verschoben nach %s, lege neu an",
                              "state file %s is corrupt (%s) - moved to %s, creating a new one"), self.path, e, broken)
                for suffix in ("", "-wal", "-shm"):
                    src = self.path.with_name(self.path.name + suffix)
                    if src.exists():
                        src.replace(broken.with_name(broken.name + suffix))
                private_db_file(self.path)
                self._open()
                self._restore_usage(broken)

    def _restore_usage(self, broken: Path) -> None:
        """Bezahl-Zähler aus der beschädigten Datei übernehmen. Gelingt das nicht, Bezahl-APIs für den Rest des
        Tages sperren (fail closed); der Monatszähler beginnt dann bei 0 (Warnung im Log)."""
        rows: list[tuple[str, int]] = []
        try:
            with contextlib.closing(sqlite3.connect(f"file:{broken}?mode=ro", uri=True)) as old:
                rows = [(str(p), int(n)) for p, n in old.execute("SELECT period, n FROM usage")]
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.warning(t("Bezahl-Zähler nicht lesbar (%s) - Bezahl-APIs für heute gesperrt, Monatszähler beginnt "
                          "neu (Ausgabelimit im Anbieter-Dashboard schützt zusätzlich)",
                          "paid-query counters unreadable (%s) - paid APIs blocked for today, the monthly counter "
                          "restarts (the spending limit in the provider dashboard still applies)"), e)
            rows = [(self._periods()[0], 10**9)]
        with self._tx() as db:
            db.executemany("INSERT INTO usage VALUES (?, ?) ON CONFLICT(period) DO UPDATE SET n = MAX(n, excluded.n)",
                           rows)

    def _open(self) -> None:
        # isolation_level=None: Transaktionen explizit steuern (BEGIN IMMEDIATE)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   timeout=self.STARTUP_TIMEOUT_S, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=%d" % int(self.STARTUP_TIMEOUT_S * 1000))
        self._db.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: Commits ohne fsync (nach einem Stromausfall fehlen höchstens die letzten Cache-/
        # Zähler-Einträge, die Datei bleibt konsistent) - hält die kurzen Schreibzugriffe in der Ereignisschleife billig
        self._db.execute("PRAGMA synchronous=NORMAL")
        for ddl in ("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, t REAL, v TEXT)",
                    "CREATE TABLE IF NOT EXISTS breaker (name TEXT PRIMARY KEY, strikes INT, "
                    "open_until REAL, last_error TEXT, empty_streak INT)",
                    "CREATE TABLE IF NOT EXISTS slots (name TEXT PRIMARY KEY, next REAL)",
                    "CREATE TABLE IF NOT EXISTS usage (period TEXT PRIMARY KEY, n INT)",
                    # Zähler für Prometheus (hostweit, seit Anlegen der Datei): Anfragen je Quelle und Ergebnis
                    "CREATE TABLE IF NOT EXISTS stats (name TEXT, state TEXT, n INT, ms INT, "
                    "PRIMARY KEY (name, state))"):
            self._db.execute(ddl)
        self._db.execute("PRAGMA busy_timeout=%d" % self.BUSY_TIMEOUT_MS)
        try:   # Aufräumen ist optional - nie deswegen den Start scheitern lassen
            self._db.execute("DELETE FROM cache WHERE t < ?", (time.time() - self.max_age_purge,))
        except sqlite3.OperationalError as e:
            log.info(t("Cache-Aufräumen beim Start übersprungen: %s",
                       "cache cleanup at startup skipped: %s"), e)

    @property
    def degraded(self) -> bool:
        """True, solange die Datei nach einer Sperre bewusst nicht gefragt wird."""
        return time.monotonic() < self._degraded_until

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._db.close()

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # -- Cache
    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps([CACHE_VERSION, *parts], sort_keys=True).encode()).hexdigest()

    @_guarded
    def get(self, key: str, ttl: float | None = None):
        row = self._db.execute("SELECT t, v FROM cache WHERE k=?", (key,)).fetchone()
        if row:
            age = time.time() - row[0]
            if 0 <= age < (self.ttl if ttl is None else ttl):    # age < 0: Uhr wurde zurückgestellt
                return json.loads(row[1])
        return None

    @_guarded
    def set(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("REPLACE INTO cache VALUES (?,?,?)", (key, time.time(), json.dumps(value)))
            self._writes += 1
            if self._writes % self.PURGE_EVERY == 0:      # lang laufende Prozesse: regelmäßig aufräumen
                self._db.execute("DELETE FROM cache WHERE t < ?", (time.time() - self.max_age_purge,))
                self._db.execute("DELETE FROM slots WHERE next < ?", (time.time() - 3600,))

    # -- Circuit-Breaker
    @_guarded
    def load_breaker(self, name: str) -> BreakerState | None:
        row = self._db.execute("SELECT strikes, open_until, last_error, empty_streak FROM breaker "
                               "WHERE name=?", (name,)).fetchone()
        return BreakerState(*row) if row else None

    @_guarded
    def update_breaker(self, name: str, fn: Callable[[BreakerState], BreakerState]) -> BreakerState:
        """Lesen, fn anwenden, schreiben - atomar, damit parallele Prozesse sich nicht überschreiben."""
        with self._tx() as db:
            row = db.execute("SELECT strikes, open_until, last_error, empty_streak FROM breaker "
                             "WHERE name=?", (name,)).fetchone()
            new = fn(BreakerState(*row) if row else BreakerState())
            db.execute("REPLACE INTO breaker VALUES (?,?,?,?,?)", (name, *new))
        return new

    # -- Tempo-Slots
    @_guarded
    def claim_slot(self, name: str, interval: float, max_wait: float = float("inf")) -> float | None:
        """Reserviert hostweit den nächsten freien Zeitpunkt. Liefert dessen Unix-Zeit, oder None,
        wenn man dafür länger als max_wait warten müsste (dann wird nichts reserviert)."""
        with self._tx() as db:
            row = db.execute("SELECT next FROM slots WHERE name=?", (name,)).fetchone()
            now = time.time()
            nxt = row[0] if row else 0.0
            if nxt - now > max(3600.0, interval * 100):   # Uhr zurückgestellt / VM-Restore: Unsinn verwerfen
                nxt = now
            slot = max(now, nxt)
            if slot - time.time() > max_wait:
                return None
            db.execute("REPLACE INTO slots VALUES (?,?)", (name, slot + interval))
        return slot

    @_guarded
    def peek_slot(self, name: str) -> float:
        """Unix-Zeit des nächsten freien Slots (0 = sofort)."""
        row = self._db.execute("SELECT next FROM slots WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0.0

    @_guarded
    def release_slot(self, name: str, slot: float, interval: float) -> None:
        """Nicht genutzten Slot zurückgeben - nur wenn seither niemand einen späteren reserviert hat."""
        with self._tx() as db:
            db.execute("UPDATE slots SET next=? WHERE name=? AND next=?", (slot, name, slot + interval))

    # -- Bezahl-Zähler (hostweit)
    @staticmethod
    def _periods(now: float | None = None) -> tuple[str, str]:
        t = time.gmtime(now or time.time())
        return time.strftime("day:%Y-%m-%d", t), time.strftime("month:%Y-%m", t)

    @_guarded
    def usage(self) -> dict[str, int]:
        day, month = self._periods()
        rows = dict(self._db.execute("SELECT period, n FROM usage WHERE period IN (?,?)", (day, month)).fetchall())
        return {"day": rows.get(day, 0), "month": rows.get(month, 0)}

    @_guarded
    def reserve_paid_call(self, daily_limit: int, monthly_limit: int) -> None:
        """Zählt eine Bezahl-Abfrage - oder wirft BudgetExceeded, wenn ein Limit erreicht ist (atomar)."""
        day, month = self._periods()
        with self._tx() as db:
            rows = dict(db.execute("SELECT period, n FROM usage WHERE period IN (?,?)", (day, month)).fetchall())
            if rows.get(day, 0) >= daily_limit:
                raise BudgetExceeded(t(f"Tageslimit für Bezahl-APIs erreicht ({daily_limit}, WEBSEARCH_PAID_DAILY_LIMIT)",
                                       f"daily limit for paid APIs reached ({daily_limit}, WEBSEARCH_PAID_DAILY_LIMIT)"))
            if rows.get(month, 0) >= monthly_limit:
                raise BudgetExceeded(t(f"Monatslimit für Bezahl-APIs erreicht ({monthly_limit}, "
                                     "WEBSEARCH_PAID_MONTHLY_LIMIT)",
                                       f"monthly limit for paid APIs reached ({monthly_limit}, WEBSEARCH_PAID_MONTHLY_LIMIT)"))
            for period in (day, month):
                db.execute("INSERT INTO usage VALUES (?, 1) ON CONFLICT(period) DO UPDATE SET n = n + 1", (period,))
            db.execute("DELETE FROM usage WHERE period < ? AND period LIKE 'day:%'",
                       (time.strftime("day:%Y-%m-%d", time.gmtime(time.time() - 40 * 86400)),))

    # -- Zähler (Metriken)
    @_guarded
    def record_stat(self, name: str, state: str, ms: int) -> None:
        """Eine Anfrage zählen (eine Anweisung, ohne eigene Transaktion: billig und atomar)."""
        with self._lock:
            self._db.execute("INSERT INTO stats VALUES (?,?,1,?) ON CONFLICT(name, state) "
                             "DO UPDATE SET n = n + 1, ms = ms + excluded.ms", (name, state, max(0, int(ms))))

    def record_stats(self, rows: list[tuple[str, str, int, int]]) -> None:
        """Mehrere Zähler (name, state, anzahl, summe_ms) in einer Transaktion addieren. Bewusst ohne
        @_guarded: eine gesperrte Datei beim Zählen darf Tempo und Sperren nicht abschalten."""
        if time.monotonic() < self._degraded_until:
            raise sqlite3.OperationalError("degraded")
        with self._tx() as db:
            db.executemany("INSERT INTO stats VALUES (?,?,?,?) ON CONFLICT(name, state) "
                           "DO UPDATE SET n = n + excluded.n, ms = ms + excluded.ms", rows)

    @_guarded
    def stats(self) -> list[tuple[str, str, int, int]]:
        """Alle Zähler als (name, state, anzahl, summe_ms)."""
        return [tuple(r) for r in self._db.execute("SELECT name, state, n, ms FROM stats ORDER BY name, state")]

    @_guarded
    def reset_state(self) -> None:
        """Alle Pausen und Tempo-Slots löschen (Cache und Bezahl-Zähler bleiben)."""
        with self._tx() as db:
            db.execute("DELETE FROM breaker")
            db.execute("DELETE FROM slots")


# =========================================================================== Tempo & Sperren

class RateLimiter:
    """Mindestabstand zwischen zwei Requests + max. gleichzeitige Requests pro Quelle.

    Nach `attach(store, name)` werden die Slots hostweit über SQLite vergeben (Monitor + MCP + CLI
    teilen sich z.B. GDELTs 1 Anfrage / 5 s). Ohne Store gilt das Tempo nur im eigenen Prozess."""

    def __init__(self, min_interval: float, concurrency: int = 1):
        self.min_interval = min_interval
        self._sem = asyncio.Semaphore(concurrency)
        self._next_local = 0.0
        self._store: StateStore | None = None
        self._name = ""

    def attach(self, store: StateStore, name: str) -> None:
        self._store, self._name = store, name

    def _claim(self, max_wait: float) -> float | None:
        if self.min_interval <= 0:
            return time.time()
        if self._store is not None:
            try:
                return self._store.claim_slot(self._name, self.min_interval, max_wait)
            except sqlite3.Error as e:   # DB gesperrt/kaputt: Tempo wenigstens prozessintern halten
                log.debug(t("Tempo-Slot für %s nicht hostweit vergebbar (%s) - nutze prozessinternes Tempo",
                            "rate slot for %s not assignable host-wide (%s) - using in-process rate limit"),
                          self._name, e)
        slot = max(time.time(), self._next_local)
        if slot - time.time() > max_wait:
            return None
        self._next_local = slot + self.min_interval
        return slot

    def _release(self, slot: float) -> None:
        if self.min_interval <= 0:
            return
        if self._store is not None:
            with contextlib.suppress(sqlite3.Error):
                self._store.release_slot(self._name, slot, self.min_interval)
        if self._next_local == slot + self.min_interval:
            self._next_local = slot

    @contextlib.asynccontextmanager
    async def slot(self, budget: float = float("inf")):
        """Wartet auf den nächsten Slot. Liegt er hinter `budget` Sekunden -> RateLimited (ohne
        Reservierung). Wird der Aufrufer beim Warten abgebrochen, wird der Slot zurückgegeben."""
        if math.isfinite(budget):
            # auch das Warten auf die EIGENE Parallelitätsgrenze zählt ins Budget: wer hinter einer
            # langsamen Anfrage derselben Quelle steht, ist "rate_limited" - kein Ausfall der Quelle
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=max(budget, 0.0))
            except asyncio.TimeoutError:
                raise RateLimited(budget, queued=True) from None
            budget -= time.monotonic() - t0          # Wartezeit in der Warteschlange zählt mit
        else:
            await self._sem.acquire()
        try:
            slot = self._claim(max(budget, 0.0))
            if slot is None:
                raise RateLimited(self._wait_estimate())
            wait = slot - time.time()
            if wait > 0:
                try:
                    await asyncio.sleep(wait)
                except BaseException:
                    self._release(slot)          # Slot verfallen lassen wäre verschenktes Tempo
                    raise
            yield
        finally:
            self._sem.release()

    def _wait_estimate(self) -> float:
        nxt = self._next_local
        if self._store is not None:
            with contextlib.suppress(sqlite3.Error):
                nxt = max(nxt, self._store.peek_slot(self._name))
        return max(0.0, nxt - time.time())


class CircuitBreaker:
    """Pausiert eine Quelle nach einer Sperre: base, 2x, 4x ... bis max (z.B. 5 min ... 6 h).

    * Ist die Quelle schon pausiert, eskalieren weitere Sperrmeldungen (von bereits laufenden
      Anfragen) NICHT - sonst würde aus einer Sperre schnell eine 6-h-Pause.
    * Erfolgsmeldungen von Anfragen, die vor der Sperre gestartet sind, heben sie nicht auf.
    * Mit Store gilt der Zustand für alle Prozesse."""

    def __init__(self, name: str = "", base_cooldown: float = 300, max_cooldown: float = 6 * 3600,
                 store: StateStore | None = None):
        self.name = name
        self.base = base_cooldown
        self.max = max_cooldown
        self.store = store
        self.state = BreakerState()
        self._local_until = 0.0      # Sperre, die nur prozessintern gespeichert werden konnte
        self._load()

    def attach(self, store: StateStore, name: str | None = None) -> None:
        if self.store is not None and self.store is not store and self.store.path != store.path:
            raise ValueError(t(f"Quelle {name or self.name} gehört schon zu einer anderen Engine "
                             f"({self.store.path}) - pro Engine eigene Provider-Instanzen verwenden",
                               f"source {name or self.name} already belongs to another engine ({self.store.path}) - use separate provider instances per engine"))
        self.store = store
        if name:
            self.name = name
        self._load()

    def _load(self) -> BreakerState:
        if self.store:   # Store ist die Wahrheit (auch nach reset_state durch einen anderen Prozess)
            try:
                loaded = self.store.load_breaker(self.name) or BreakerState()
            except sqlite3.Error as e:   # DB nicht lesbar: mit dem zuletzt bekannten Zustand weiter
                log.debug(t("Sperrzustand für %s nicht lesbar: %s",
                            "block state for %s not readable: %s"), self.name, e)
                return self.state
            if self._local_until > loaded.open_until:
                # Sperre wurde gemeldet, während die Datei gesperrt war: jetzt nachtragen statt vergessen
                local, self._local_until = self.state, 0.0
                with contextlib.suppress(sqlite3.Error):
                    loaded = self.store.update_breaker(self.name, lambda s: local if local.open_until > s.open_until else s)
            self.state = loaded
        return self.state

    def _update(self, fn: Callable[[BreakerState], BreakerState]) -> None:
        if self.store:
            try:
                self.state = self.store.update_breaker(self.name, fn)
                return
            except sqlite3.Error as e:   # nicht hostweit speicherbar -> wenigstens in diesem Prozess merken
                log.debug(t("Sperrzustand für %s nicht speicherbar: %s",
                            "block state for %s not storable: %s"), self.name, e)
        self.state = fn(self.state)
        self._local_until = max(self._local_until, self.state.open_until)

    # bequemer Lesezugriff
    strikes = property(lambda self: self.state.strikes)
    open_until = property(lambda self: self.state.open_until)
    last_error = property(lambda self: self.state.last_error)
    empty_streak = property(lambda self: self.state.empty_streak)

    def is_open(self) -> bool:
        """True, solange die Quelle pausiert ist (liest den aktuellen, hostweiten Zustand)."""
        return time.time() < self._load().open_until

    def retry_in(self) -> int:
        return max(0, int(self.state.open_until - time.time()))

    def trip(self, reason: str) -> None:
        base, mx = self.base, self.max

        def apply(s: BreakerState) -> BreakerState:
            now = time.time()
            if s.open_until > now:                       # schon offen -> nur Grund aktualisieren
                return s._replace(last_error=reason, empty_streak=0)
            strikes = s.strikes + 1
            return BreakerState(strikes, now + min(base * 2 ** (strikes - 1), mx), reason, 0)
        self._update(apply)
        log.warning(t("Quelle %s pausiert für %ss (Sperre #%s): %s",
                      "source %s paused for %ss (block #%s): %s"),
                    self.name, self.retry_in(), self.strikes, reason)

    def success(self) -> None:
        def apply(s: BreakerState) -> BreakerState:
            if s.open_until > time.time():               # Antwort von vor der Sperre -> ignorieren
                return s
            return BreakerState(0, s.open_until, None, 0)
        if self._load() != BreakerState(0, self.state.open_until, None, 0):
            self._update(apply)

    def note_empty(self, others_found: bool, limit: int = 3) -> None:
        """Stille Sperren erkennen: gescrapte Engines liefern bei Blockade oft einfach 0 Treffer."""
        self._update(lambda s: s._replace(empty_streak=s.empty_streak + 1 if others_found else 0))
        if self.empty_streak >= limit:
            self.trip(t(f"{limit}x leer, während andere Quellen Treffer hatten (Verdacht: stille Sperre)",
                        f"{limit}x empty while other sources had results (suspected silent block)"))

    def status(self) -> dict:
        blocked = self.is_open()
        return {"blocked": blocked, "retry_in_s": self.retry_in() if blocked else 0,
                "strikes": self.strikes, "last_error": self.last_error}


# =========================================================================== Normalisierung

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
             "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref_src", "oc", "srsltid"}
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_DEFAULT_PORTS = {80, 443, None}


def normalize_url(url: str) -> str:
    """Schlüssel für die Duplikaterkennung: Schema, www, Slash am Ende, Tracking-Parameter und
    Prozent-Kodierung vereinheitlichen. Nicht-Standard-Ports bleiben erhalten."""
    try:
        p = urlsplit(url.strip())
        host = (p.hostname or "").lower().removeprefix("www.")
        if host.startswith("m.") and host.count(".") >= 2:     # m.example.com -> example.com
            host = host[2:]
        port = p.port
    except ValueError:
        return url
    netloc = host if port in _DEFAULT_PORTS else f"{host}:{port}"
    path = quote(unquote(p.path), safe=_PATH_SAFE).rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if k.lower() not in _TRACKING))
    return urlunsplit(("https", netloc, path, query, ""))


def normalize_date(value) -> str | None:
    """Datumsangaben (ISO, RFC 822, GDELT, Unix-Zeit) -> ISO-8601 in UTC; Unbekanntes -> None."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            s = str(value).strip()
            if len(s) == 16 and s[8] == "T" and s.endswith("Z") and s[:8].isdigit():   # GDELT 20261103T120000Z
                s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}+00:00"
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        if dt.timestamp() > time.time() + 2 * 86400:          # "2099-01-01": würde News dauerhaft oben halten
            return None
        return dt.isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError, AttributeError):   # z.B. 0001-01-01T00:00+05:00
        return None


# =========================================================================== Merging

def title_key(title: str) -> str:
    """'Schlagzeile - Quelle' (Google News) und 'Schlagzeile' sollen gleich sein."""
    t = title.rsplit(" - ", 1)[0] if " - " in title else title
    return " ".join("".join(ch for ch in t.lower() if ch.isalnum() or ch.isspace()).split())


def _merge_key(r: Result) -> str:
    """Identität eines Treffers: bei Bildern das Bild selbst (eine Galerie-Seite hat viele Bilder),
    sonst die normalisierte URL."""
    return "img:" + normalize_url(r.image) if r.image else normalize_url(r.url)


def merge_rrf(result_lists: list[list[Result]], k: int = 60, dedupe_titles: bool = False) -> list[Result]:
    """Reciprocal Rank Fusion: Treffer, die viele Quellen weit oben haben, landen oben.

    * ein Treffer zählt pro Quelle nur einmal (bester Rang); Bilder werden am Bild-URL erkannt
    * fehlende Felder (Datum, Bild, Vorschau, Laufzeit) werden aus anderen Quellen ergänzt
    * `dedupe_titles` (News): gleiche Schlagzeile (>= 20 Zeichen) = gleicher Artikel, auch bei
      verschiedenen URLs; die direkte URL gewinnt gegen eine Weiterleitung (Result.redirect)
    * die Eingaben werden nicht verändert"""
    merged: dict[str, Result] = {}
    by_title: dict[str, str] = {}
    for results in result_lists:
        seen_here: set[str] = set()
        for r in results:
            if not r.url:
                continue
            key = _merge_key(r)
            tkey = title_key(r.title) if dedupe_titles else ""
            if len(tkey) >= 20 and key not in merged and tkey in by_title:
                key = by_title[tkey]
            if key in seen_here:
                continue
            seen_here.add(key)
            contrib = 1.0 / (k + max(r.rank, 1))
            if key in merged:
                m = merged[key]
                m.score += contrib
                for e in [r.source, *r.engines]:
                    if e and e not in m.engines:
                        m.engines.append(e)
                if len(r.snippet) > len(m.snippet):
                    m.snippet = r.snippet
                m.published = m.published or r.published
                m.image, m.thumbnail = m.image or r.image, m.thumbnail or r.thumbnail
                m.duration = m.duration or r.duration
                if m.redirect and not r.redirect:
                    m.url, m.redirect = r.url, False
            else:
                merged[key] = replace(r, score=contrib,
                                      engines=[e for e in dict.fromkeys([r.source, *r.engines]) if e])
                if len(tkey) >= 20:
                    by_title.setdefault(tkey, key)
    return sorted(merged.values(), key=lambda r: r.score, reverse=True)
