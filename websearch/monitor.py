"""Monitoring: führt definierte Suchen regelmäßig aus und meldet nur NEUE Treffer.

watches.json:
[
  {"name": "marke", "query": "\"ACME GmbH\"", "kind": "news", "lang": "de", "every_min": 60},
  {"name": "cve",   "query": "openssl vulnerability", "kind": "news", "every_min": 180, "max_age_days": 2}
]

Felder (siehe `Watch`): name, query (Pflicht) · kind (news) · lang (auto) · every_min (60, min. 5) ·
        n (20, Top-n die gemeldet werden, 1-100) · sources (Liste oder weglassen) · deadline (60 s, 5-600) ·
        max_age_days (3; ältere datierte Treffer gelten nicht als "neu"). Unbekannte Felder sind ein Fehler.

Regeln gegen Fehlalarme:
  * Erster Lauf je Watch = Basislinie (nichts melden), sobald er mindestens einen Treffer hatte.
  * Kandidaten-Pool: Abgefragt werden min(100, max(3n, 30)) Treffer; ALLE werden als gesehen markiert,
    gemeldet werden aber nur neue Treffer innerhalb der Top-n. So gilt ein alter Treffer, der von Rang 25
    auf Rang 15 rutscht, nicht als neu. Kompromiss: Ein wirklich neuer Treffer, der zuerst ausserhalb der
    Top-n auftaucht, wird still übernommen und später nicht mehr gemeldet.
  * Erstmals liefernde Quellen: War eine Quelle bisher nie erfolgreich (z.B. SearXNG beim Start noch nicht
    bereit), werden Treffer, die NUR von solchen Quellen kommen, beim ersten Mal still übernommen.
  * Datierte Treffer älter als max_age_days werden nie gemeldet.

Blind-Erkennung: Liefert ein Watch WEBSEARCH_BLIND_AFTER (Standard 3) Läufe in Folge keine Treffer, weil
Quellen ausfallen, geht EINE Health-Meldung raus ({"type": "health", "status": "blind"}); sobald er wieder
liefert, eine Entwarnung ("recovered"). So ist "nichts Neues" von "wir sind blind" unterscheidbar.

Ausgabe: jede Meldung als JSON-Zeile auf stdout (Logs auf stderr), Webhook und JSONL-Export: siehe notify.py.

Zustellgarantie: Neue Treffer werden ZUERST ausgegeben (stdout + JSONL), erst danach in EINER Transaktion pro
Watch als gesehen markiert. Scheitert die Ausgabe (z.B. BrokenPipeError), bleiben die Treffer ungesehen und
werden beim nächsten Lauf erneut gemeldet. Ein fehlgeschlagener Webhook landet in einer Outbox und wird zu
Beginn jedes Durchlaufs erneut versucht (Backoff 1 min, verdoppelt bis 1 h; nach 24 h verworfen + ERROR-Log).
Im Dauerbetrieb (--loop) wird die Watch-Datei bei Änderung neu eingelesen; eine fehlerhafte neue Version
wird geloggt und die alte weiterverwendet. Fehler im eigenen Zustand (SQLite gesperrt, kaputte Outbox-Zeile,
nicht lesbare Watch-Datei) werden geloggt; die Schleife läuft weiter.

Zustellung ist "at-least-once": Fällt der Prozess z.B. zwischen Webhook und Festschreiben aus, kann eine
Meldung ein zweites Mal kommen. Jede Meldung trägt deshalb eine stabile `id` (sha256 über Typ, Watch und die
normalisierten URLs bzw. Status + Beginn bei Health) - Empfänger deduplizieren über dieses Feld; bei
Wiederholungen aus der Outbox bleibt die id gleich.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import functools
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import Any, Collection, Mapping, Sequence

import httpx

from . import notify
from .i18n import t
from .core import SUCCESS_STATES, InputError, env_int, is_benign, normalize_url, private_db_file
from .engine import KINDS, SearchEngine
from .providers import disabled_sources

log = logging.getLogger("websearch.monitor")

SEEN_RETENTION_DAYS = 180
STARTUP_TIMEOUT_S = 10.0            # nur für Schema/Aufräumen beim Öffnen
BUSY_TIMEOUT_MS = 250               # Normalbetrieb: kurz, damit die Schleife nie lange hängt
OUTBOX_MAX_AGE = 24 * 3600         # fehlgeschlagene Webhooks so lange erneut versuchen
OUTBOX_BACKOFF = (60.0, 3600.0)    # erster Abstand, maximaler Abstand (verdoppelt sich)


# --------------------------------------------------------------------------- Watches

@dataclass(frozen=True)
class Watch:
    name: str
    query: str
    kind: str = "news"
    lang: str = "auto"
    every_min: float = 60
    n: int = 20
    sources: tuple[str, ...] | None = None
    deadline: float = 60
    max_age_days: float = 3

    @property
    def every_s(self) -> float:
        return self.every_min * 60

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = list(self.sources) if self.sources else None
        return d


_FIELDS = [f.name for f in fields(Watch)]


def _reject_constant(name: str):
    raise InputError(t(f"Watch-Datei: '{name}' ist kein gültiger Zahlenwert",
                       f"watch file: '{name}' is not a valid number"))


def _num(label: str, w: dict, key: str, default, lo=None, hi=None, integer=False):
    v = w.get(key, default)
    if isinstance(v, float) and not math.isfinite(v):
        raise InputError(t(f"{label}: '{key}' muss eine endliche Zahl sein (ist: {v!r})",
                           f"{label}: '{key}' must be a finite number (got: {v!r})"))
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InputError(t(f"{label}: '{key}' muss eine {'ganze ' if integer else ''}Zahl sein (ist: {v!r})",
                           f"{label}: '{key}' must be a {'whole ' if integer else ''}number (got: {v!r})"))
    if integer and (isinstance(v, float) and not v.is_integer()):
        raise InputError(t(f"{label}: '{key}' muss eine ganze Zahl sein (ist: {v!r})",
                           f"{label}: '{key}' must be a whole number (got: {v!r})"))
    if lo is not None and v < lo or hi is not None and v > hi:
        if hi is not None:
            raise InputError(t(f"{label}: '{key}' muss zwischen {lo} und {hi} liegen (ist: {v!r})",
                               f"{label}: '{key}' must be between {lo} and {hi} (got: {v!r})"))
        raise InputError(t(f"{label}: '{key}' muss mindestens {lo} sein (ist: {v!r})",
                           f"{label}: '{key}' must be at least {lo} (got: {v!r})"))
    return int(v) if integer else float(v)


def parse_watch(w, index: int = 0) -> Watch:
    """Prüft einen Watch-Eintrag streng und liefert ein `Watch`. Fehlermeldungen in der aktiven Sprache."""
    if isinstance(w, Watch):
        return w
    label = f"Watch #{index + 1}"
    if not isinstance(w, dict):
        raise InputError(t(f"{label}: muss ein JSON-Objekt sein (ist: {type(w).__name__})",
                           f"{label}: must be a JSON object (got: {type(w).__name__})"))
    name = w.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InputError(t(f"{label}: 'name' fehlt oder ist kein Text",
                           f"{label}: 'name' is missing or not a string"))
    label = f"Watch {name!r}"
    unknown = [k for k in w if k not in _FIELDS]
    if unknown:
        hints = []
        for k in unknown:
            close = difflib.get_close_matches(k, _FIELDS, n=1)
            hints.append(f"'{k}'" + (t(f" (gemeint: '{close[0]}'?)", f" (did you mean '{close[0]}'?)")
                                     if close else ""))
        raise InputError(t(f"{label}: unbekanntes Feld {', '.join(hints)} - erlaubt: {', '.join(_FIELDS)}",
                           f"{label}: unknown field {', '.join(hints)} - allowed: {', '.join(_FIELDS)}"))
    query = w.get("query")
    if not isinstance(query, str) or not query.strip():
        raise InputError(t(f"{label}: 'query' fehlt oder ist leer",
                           f"{label}: 'query' is missing or empty"))
    kind = w.get("kind", "news")
    if kind not in KINDS:
        raise InputError(t(f"{label}: 'kind' muss eines von {', '.join(KINDS)} sein (ist: {kind!r})",
                           f"{label}: 'kind' must be one of {', '.join(KINDS)} (got: {kind!r})"))
    lang = w.get("lang", "auto")
    if not isinstance(lang, str) or not lang.strip():
        raise InputError(t(f"{label}: 'lang' muss ein Text wie 'de' oder 'auto' sein (ist: {lang!r})",
                           f"{label}: 'lang' must be a string like 'de' or 'auto' (got: {lang!r})"))
    sources = w.get("sources")
    if sources is not None:
        if (not isinstance(sources, list) or not sources
                or not all(isinstance(s, str) and s.strip() for s in sources)):
            raise InputError(t(f"{label}: 'sources' muss eine nicht-leere Liste von Quellennamen sein "
                             f"(oder weglassen) (ist: {sources!r})",
                               f"{label}: 'sources' must be a non-empty list of source names (or omitted) (got: {sources!r})"))
        sources = tuple(s.strip() for s in sources)
    return Watch(
        name=name.strip(), query=query.strip(), kind=kind, lang=lang.strip(), sources=sources,
        every_min=_num(label, w, "every_min", 60, lo=5),
        n=_num(label, w, "n", 20, lo=1, hi=100, integer=True),
        deadline=_num(label, w, "deadline", 60, lo=5, hi=600),
        max_age_days=_num(label, w, "max_age_days", 3, lo=0.01),
    )


SourceKinds = Mapping[str, tuple[str, ...]]


def check_sources(watches: list[Watch], known: SourceKinds | set[str] | None) -> None:
    """Quellennamen in Watches gegen die verfügbaren Quellen prüfen (Tippfehler sofort beim Start melden,
    nicht erst bei jedem Lauf). Ist `known` ein Mapping Name -> Sucharten, wird auch geprüft, ob jede Quelle
    die Suchart des Watches kann. `known=None` überspringt die Prüfung."""
    if known is None:
        return
    names = set(known)
    disabled = disabled_sources()
    for w in watches:
        for src in w.sources or ():
            if src not in names:
                if src in disabled:
                    raise InputError(t(f"Watch {w.name!r}: Quelle {src!r} ist per WEBSEARCH_DISABLE abgeschaltet",
                                       f"watch {w.name!r}: source {src!r} is disabled via WEBSEARCH_DISABLE"))
                close = difflib.get_close_matches(src, sorted(names), n=1)
                hint = t(f" (gemeint: '{close[0]}'?)", f" (did you mean '{close[0]}'?)") if close else ""
                raise InputError(t(f"Watch {w.name!r}: unbekannte Quelle {src!r}{hint} - verfügbar: "
                                 f"{', '.join(sorted(names))}",
                                   f"watch {w.name!r}: unknown source {src!r}{hint} - available: {', '.join(sorted(names))}"))
            if isinstance(known, Mapping):
                kinds = tuple(known[src])
                if w.kind not in kinds:
                    raise InputError(t(f"Watch {w.name!r}: Quelle {src!r} kann die Suchart {w.kind!r} nicht "
                                     f"(nur: {', '.join(kinds)}) - 'kind' oder 'sources' anpassen",
                                       f"watch {w.name!r}: source {src!r} does not support kind {w.kind!r} (only: {', '.join(kinds)}) - adjust 'kind' or 'sources'"))
        if w.sources:
            _check_never_skipped(w)


def _check_never_skipped(w: Watch) -> None:
    """Ein Watch, dessen feste Quellen seine Anfrage ALLE grundsätzlich überspringen (z.B. nur GDELT mit
    "EU AI"), würde nie etwas finden und nie warnen - deshalb schon beim Laden ablehnen."""
    from .core import SourceSkipped
    from .providers import default_providers
    provs = default_providers()
    reasons = []
    for src in w.sources or ():
        p = provs.get(src)
        if p is None:
            return
        try:
            p.precheck(w.query, w.kind)
            return                      # mindestens eine Quelle fragt -> ok
        except SourceSkipped as e:
            reasons.append(str(e))
    if reasons:
        raise InputError(t(f"Watch {w.name!r}: keine der Quellen kann diese Anfrage suchen ({'; '.join(reasons)}) "
                         "- andere Quellen oder längere Suchwörter verwenden",
                           f"watch {w.name!r}: none of the sources can search this query ({'; '.join(reasons)}) - use other sources or longer search words"))


def default_source_kinds() -> dict[str, tuple[str, ...]]:
    """Name -> Sucharten aller eingebauten Quellen (ohne die per WEBSEARCH_DISABLE abgeschalteten)."""
    from .providers import default_providers
    return {name: tuple(p.kinds) for name, p in default_providers().items()}


def default_source_names() -> set[str]:
    """Namen aller eingebauten Quellen (ohne die per WEBSEARCH_DISABLE abgeschalteten)."""
    return set(default_source_kinds())


def load_watches(path: str | Path, known_sources: SourceKinds | set[str] | None = None) -> list[Watch]:
    """Watch-Datei laden und streng prüfen. Mit `known_sources` werden auch die Quellennamen (und bei einem
    Mapping Name -> Sucharten die Verträglichkeit mit 'kind') geprüft."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")       # -sig: Datei aus Windows-Editor mit BOM ist ok
    except UnicodeDecodeError:
        raise InputError(t(f"Watch-Datei {p.name} ist nicht UTF-8-kodiert - bitte als UTF-8 speichern",
                           f"watch file {p.name} is not UTF-8 encoded - please save it as UTF-8")) from None
    except FileNotFoundError:
        raise InputError(t(f"Watch-Datei nicht gefunden: {p}", f"watch file not found: {p}")) from None
    except OSError as e:
        raise InputError(t(f"Watch-Datei nicht lesbar: {p} ({e.strerror})",
                           f"watch file not readable: {p} ({e.strerror})")) from None
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise InputError(t(f"Watch-Datei {p.name}: zu tief verschachtelt",
                           f"watch file {p.name}: nested too deeply")) from None
    except json.JSONDecodeError as e:
        raise InputError(t(f"Watch-Datei {p.name}: kein gültiges JSON (Zeile {e.lineno}, Spalte {e.colno}: "
                         f"{e.msg})",
                           f"watch file {p.name}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})")) from None
    if not isinstance(data, list):
        raise InputError(t(f"Watch-Datei {p.name}: muss eine JSON-Liste von Watches sein",
                           f"watch file {p.name}: must be a JSON list of watches"))
    watches, names = [], set()
    for i, raw in enumerate(data):
        w = parse_watch(raw, i)
        if w.name in names:
            raise InputError(t(f"Watch-Name doppelt: {w.name!r}", f"duplicate watch name: {w.name!r}"))
        names.add(w.name)
        watches.append(w)
    check_sources(watches, known_sources)
    return watches


class WatchFile:
    """Watch-Datei mit Neu-Einlesen bei Änderung (Zeit, Größe, Inode). Fehlerhafte neue Version -> alte behalten."""

    def __init__(self, path: str | Path, known_sources: SourceKinds | set[str] | None = None):
        self.path = Path(path)
        self.known_sources = known_sources
        self.watches = load_watches(self.path, known_sources)   # beim Start: Fehler sofort melden
        self._mtime = self._stat()

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
            # Zeit, Größe und Inode: erkennt auch Ersetzen per Rename und Änderungen im selben Zeit-Tick
            return st.st_mtime_ns, st.st_size, st.st_ino
        except OSError:
            return None

    def current(self) -> list[Watch]:
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            self._mtime = mtime
            try:
                self.watches = load_watches(self.path, self.known_sources)
                log.info(t("Watch-Datei neu eingelesen: %d Watches", "watch file reloaded: %d watches"), len(self.watches))
            except InputError as e:
                log.error(t("Watch-Datei geändert, aber fehlerhaft - alte Watches bleiben aktiv: %s",
                            "watch file changed but invalid - old watches stay active: %s"), e)
        return self.watches


# --------------------------------------------------------------------------- Zustand

class SeenStore:
    """Zustand des Monitors (SQLite).

    Lesen ist jederzeit erlaubt; Schreiben nur in `transaction()` (BEGIN IMMEDIATE ... COMMIT). Innerhalb
    einer Transaktion wird nie `await`et - so können parallel laufende Watches ihre Schreibzugriffe nicht
    verschränken, und ein Fehler mitten im Lauf hinterlässt keine halb geschriebenen Zeilen."""

    def __init__(self, path: str | Path, retention_days: float = SEEN_RETENTION_DAYS):
        private_db_file(Path(path))
        # isolation_level=None: keine impliziten Transaktionen - wir steuern sie explizit.
        # Beim Öffnen (Schema, Aufräumen) darf es länger dauern; danach kurzer busy_timeout.
        self.db = sqlite3.connect(str(path), timeout=STARTUP_TIMEOUT_S, isolation_level=None)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as e:      # z.B. gerade gesperrt - WAL ist dann meist schon aktiv
            log.warning(t("Monitor-DB: WAL-Modus nicht setzbar (%s)",
                          "monitor DB: WAL mode not settable (%s)"), e)
        have = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {"seen", "runs", "baseline", "sources", "health", "outbox", "outbox_dead", "watch_query"}
        if not needed <= have:     # nur dann Schreib-Lock nehmen (bestehende DB: kein Warten auf andere)
            with self.transaction() as db:
                db.execute("CREATE TABLE IF NOT EXISTS seen (watch TEXT, url TEXT, t REAL, PRIMARY KEY (watch, url))")
                db.execute("CREATE TABLE IF NOT EXISTS runs (watch TEXT PRIMARY KEY, t REAL)")
                db.execute("CREATE TABLE IF NOT EXISTS baseline (watch TEXT PRIMARY KEY, t REAL)")
                db.execute("CREATE TABLE IF NOT EXISTS sources (watch TEXT, source TEXT, t REAL, "
                           "PRIMARY KEY (watch, source))")
                db.execute("CREATE TABLE IF NOT EXISTS health (watch TEXT PRIMARY KEY, blind_runs INT, "
                           "blind_since REAL, alerted INT, last_success REAL)")
                db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, "
                           "created REAL, attempts INT, next_try REAL, last_error TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS outbox_dead (id INTEGER PRIMARY KEY, payload TEXT, "
                           "created REAL, reason TEXT, t REAL)")
                db.execute("CREATE TABLE IF NOT EXISTS watch_query (watch TEXT PRIMARY KEY, qhash TEXT)")
        # ab hier kurzer busy_timeout - auch fürs optionale Aufräumen (darf nie den Start aufhalten)
        self.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # Alte "gesehen"-Einträge entfernen, damit die Datei nicht unbegrenzt wächst. Nicht kritisch:
        # ist die Datei gerade gesperrt, beim nächsten Start.
        self.retention_days = retention_days
        self.purge()

    def purge(self) -> None:
        """Alte "gesehen"-Einträge löschen (Start und im Dauerbetrieb täglich). Nicht kritisch."""
        try:
            with self.transaction() as db:
                db.execute("DELETE FROM seen WHERE t < ?", (time.time() - self.retention_days * 86400,))
        except sqlite3.OperationalError as e:
            log.warning(t("Monitor-DB: Aufräumen übersprungen (%s)", "monitor DB: cleanup skipped (%s)"), e)

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # -- Lesen
    def known_sources(self, watch: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT source FROM sources WHERE watch=?", (watch,))}

    def query_hash(self, watch: str) -> str | None:
        row = self.db.execute("SELECT qhash FROM watch_query WHERE watch=?", (watch,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_query(db, watch: str, qhash: str) -> None:
        db.execute("REPLACE INTO watch_query VALUES (?,?)", (watch, qhash))

    @staticmethod
    def reset_baseline(db, watch: str) -> None:
        """Abfrage eines Watches wurde geändert: alte Basislinie/Quellen gelten nicht mehr."""
        db.execute("DELETE FROM baseline WHERE watch=?", (watch,))
        db.execute("DELETE FROM sources WHERE watch=?", (watch,))

    def has_baseline(self, watch: str) -> bool:
        return self.db.execute("SELECT 1 FROM baseline WHERE watch=?", (watch,)).fetchone() is not None

    def last_run(self, watch: str) -> float:
        row = self.db.execute("SELECT t FROM runs WHERE watch=?", (watch,)).fetchone()
        return row[0] if row else 0.0

    def unseen(self, watch: str, results: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Treffer, die für diesen Watch noch nicht als gesehen markiert sind - OHNE etwas zu schreiben."""
        out, keys = [], set()
        for r in results:
            key = normalize_url(r["url"])
            if key in keys:
                continue
            keys.add(key)
            if self.db.execute("SELECT 1 FROM seen WHERE watch=? AND url=?", (watch, key)).fetchone() is None:
                out.append(dict(r))
        return out

    def health(self, watch: str) -> dict:
        row = self.db.execute("SELECT blind_runs, blind_since, alerted, last_success FROM health WHERE watch=?",
                              (watch,)).fetchone()
        b, s, a, ls = row or (0, None, 0, None)
        return {"blind_runs": b, "blind_since": s, "alerted": bool(a), "last_success": ls}

    # -- Schreiben (nur innerhalb von transaction())
    @staticmethod
    def mark_seen(db, watch: str, results: Sequence[Mapping[str, Any]]) -> None:
        """Als gesehen markieren - bzw. den Zeitstempel auffrischen: Treffer, die dauerhaft im Pool bleiben,
        dürfen nach der Aufbewahrungsfrist nicht erneut als "neu" erscheinen."""
        now = time.time()
        db.executemany("INSERT INTO seen VALUES (?,?,?) ON CONFLICT(watch, url) DO UPDATE SET t = excluded.t",
                       [(watch, normalize_url(r["url"]), now) for r in results])

    @staticmethod
    def add_sources(db, watch: str, names) -> None:
        db.executemany("INSERT OR IGNORE INTO sources VALUES (?,?,?)", [(watch, n, time.time()) for n in names])

    @staticmethod
    def set_baseline(db, watch: str) -> None:
        db.execute("INSERT OR IGNORE INTO baseline VALUES (?,?)", (watch, time.time()))

    @staticmethod
    def set_health(db, watch: str, blind_runs: int, blind_since, alerted: bool, last_success) -> None:
        db.execute("REPLACE INTO health VALUES (?,?,?,?,?)", (watch, blind_runs, blind_since, int(alerted), last_success))

    @staticmethod
    def set_run(db, watch: str, at: float | None = None) -> None:
        """Laufzeitpunkt speichern - der START des Laufs, sonst verschiebt die Laufdauer jeden Termin (ein Cron-Job
        im Takt von every_min liefe nur jedes zweite Mal)."""
        db.execute("REPLACE INTO runs VALUES (?,?)", (watch, time.time() if at is None else at))

    def mark_run(self, watch: str) -> None:
        """Nur den Laufzeitpunkt setzen (z.B. nach einem Fehler, damit der Watch nicht im Takt der
        Schleife erneut feuert). Gesehen-Markierungen bleiben unberührt."""
        with self.transaction() as db:
            self.set_run(db, watch)

    # -- Outbox für fehlgeschlagene Webhooks
    @staticmethod
    def outbox_add(db, payload: dict, error: str = "") -> None:
        now = time.time()
        db.execute("INSERT INTO outbox (payload, created, attempts, next_try, last_error) VALUES (?,?,?,?,?)",
                   (json.dumps(payload, ensure_ascii=False), now, 1, now + OUTBOX_BACKOFF[0], error))

    def outbox_due(self, now: float | None = None) -> list[tuple[int, dict, float, int]]:
        """Fällige Einträge. Nicht lesbare Zeilen (kaputtes JSON) werden einmalig geloggt und nach
        `outbox_dead` verschoben - sie dürfen den Monitor nie (bei jedem Neustart erneut) abstürzen lassen."""
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT id, payload, created, attempts FROM outbox WHERE next_try <= ? ORDER BY id",
                               (now,)).fetchall()
        out = []
        for i, p, c, a in rows:
            try:
                payload = json.loads(p)
                if not isinstance(payload, dict):
                    raise ValueError(t(f"kein Objekt, sondern {type(payload).__name__}",
                                       f"not an object but {type(payload).__name__}"))
            except (ValueError, TypeError) as e:
                log.error(t("Outbox-Eintrag %s ist unlesbar und wird aussortiert (outbox_dead): %s",
                            "outbox entry %s is unreadable and moved to outbox_dead: %s"), i, e)
                with self.transaction() as db:
                    db.execute("INSERT OR REPLACE INTO outbox_dead VALUES (?,?,?,?,?)", (i, p, c, str(e), now))
                    db.execute("DELETE FROM outbox WHERE id=?", (i,))
                continue
            out.append((i, payload, c, a))
        return out

    def outbox_size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def outbox_done(self, entry_id: int) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM outbox WHERE id=?", (entry_id,))

    def outbox_retry_later(self, entry_id: int, attempts: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        delay = min(OUTBOX_BACKOFF[0] * 2 ** attempts, OUTBOX_BACKOFF[1])
        with self.transaction() as db:
            db.execute("UPDATE outbox SET attempts=?, next_try=? WHERE id=?", (attempts + 1, now + delay, entry_id))


# --------------------------------------------------------------------------- Auswertung

def _failed(res: Mapping[str, Any]) -> dict[str, str]:
    """{quelle: meldung} aller Quellen, die wirklich ausgefallen sind. Bewusst übersprungene Quellen
    (skipped, rate_limited, unsupported, not_configured, budget) zählen nicht - sonst gäbe es falsche
    "blind"-Warnungen, z.B. wenn GDELT kurze Suchwörter grundsätzlich auslässt."""
    return {name: st.get("message") or st["state"]
            for name, st in res.get("sources", {}).items() if not is_benign(st)}


def _answered(res: Mapping[str, Any], known: set[str], scraped: set[str], sparse: Collection[str] = ()) -> bool:
    """Hat mindestens eine Quelle belastbar geantwortet?

    Treffer zählen immer. "ok, 0 Treffer" zählt nie von gescrapten Seiten (deren 0 ist oft eine stille
    Sperre) und nie von Spezialquellen wie Hacker News/GDELT, die für fast jeden Markennamen 0 liefern.
    Sonst zählt es, wenn die Quelle für diesen Watch schon einmal geliefert hat - oder, solange der Watch
    noch keine Vorgeschichte hat, von jeder breiten Quelle (SearXNG, Google News, Wikipedia ...)."""
    for name, st in res.get("sources", {}).items():
        if st["state"] not in SUCCESS_STATES:
            continue
        if st["count"] > 0:
            return True
        if name in scraped or name in sparse:
            continue
        if known and name not in known:
            continue
        return True
    return False


def _delivering(res: Mapping[str, Any]) -> set[str]:
    return {name for name, st in res.get("sources", {}).items()
            if st["state"] in SUCCESS_STATES and st.get("count", 0) > 0}


def _too_old(r: dict, max_age_days: float) -> bool:
    if not r.get("published"):
        return False
    try:
        dt = datetime.fromisoformat(r["published"])
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc) - timedelta(days=max_age_days)


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None


def blind_after() -> int:
    return env_int("WEBSEARCH_BLIND_AFTER", 3, minimum=1)


def pool_size(n: int) -> int:
    return min(100, max(3 * n, 30))


def hit_id(watch: str, url: str) -> str:
    """Stabile ID eines Treffers je Watch - daran dedupliziert der Empfänger zuverlässig, auch wenn ein
    Treffer bei einer erneuten Zustellung mit anderen Treffern zusammen kommt."""
    return hashlib.sha256(json.dumps(["hit", watch, normalize_url(url)]).encode("utf-8")).hexdigest()[:32]


def payload_id(payload: Mapping[str, Any]) -> str:
    """ID der ganzen Meldung (at-least-once-Zustellung). Für Treffer bitte zusätzlich je Treffer über
    dessen `id` deduplizieren (siehe hit_id), weil sich die Zusammensetzung einer Meldung ändern kann."""
    if payload.get("type") == "health":
        parts: list[Any] = ["health", payload.get("watch"), payload.get("status"), payload.get("since")]
    else:
        parts = ["hits", payload.get("watch"), sorted(normalize_url(r["url"]) for r in payload.get("new", []))]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _with_id(payload: dict) -> dict:
    return {"id": payload_id(payload), **payload}


def fmt_failed(failed: Mapping[str, str]) -> str:
    """{'a': 'x', 'b': 'y'} -> 'a: x; b: y' (für Logs statt Python-Dict-Darstellung)."""
    return "; ".join(f"{k}: {v.removeprefix(k + ': ')}" for k, v in failed.items()) or t("keine", "none")


def _runs(n: int) -> str:
    return t(f"{n} Lauf", f"{n} run") if n == 1 else t(f"{n} Läufen", f"{n} runs")


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _health_transition(store: SeenStore, w: Watch, res: Mapping[str, Any], failed: dict,
                       scraped: set[str] | None = None, sparse: set[str] | None = None) -> tuple[dict | None, tuple]:
    """Berechnet (Health-Meldung oder None, neuer Health-Zustand) - ohne zu schreiben.

    "blind" heißt: keine Quelle hat belastbar geantwortet (siehe _answered). 0 Treffer, während eine
    bewährte Quelle normal geantwortet hat, ist ein legitim leeres Ergebnis (seltene Begriffe!)."""
    sources = res.get("sources", {})
    answered = _answered(res, store.known_sources(w.name), scraped or set(), sparse or set())
    # blind nur bei echten Ausfällen (oder Absturz des Laufs): sind alle Quellen bloß übersprungen oder
    # gedrosselt, ist das kein Ausfall - dauerhaft sinnlose Kombinationen lehnt schon load_watches ab
    blind = not answered and bool(failed)
    h = store.health(w.name)
    now = time.time()
    if blind:
        runs = h["blind_runs"] + 1
        since = h["blind_since"] or now
        if runs >= blind_after() and not h["alerted"]:
            log.warning(t("%s: seit %s blind (keine Treffer, Quellen fallen aus): %s",
                          "%s: blind since %s (no results, sources failing): %s"), w.name, _runs(runs),
                        fmt_failed(failed))
            payload = _with_id({"type": "health", "watch": w.name, "status": "blind", "since": _iso(since),
                                "errors": failed})
            return payload, (runs, since, True, h["last_success"])
        return None, (runs, since, h["alerted"], h["last_success"])
    if not answered:
        # weder Ausfall noch Antwort (alle Quellen gedrosselt/übersprungen): Zustand unverändert lassen -
        # sonst käme nach einer Blind-Warnung ein falsches "recovered"
        return None, (h["blind_runs"], h["blind_since"], h["alerted"], h["last_success"])
    if h["alerted"]:
        log.warning(t("%s: liefert wieder Ergebnisse", "%s: delivers results again"), w.name)
        payload = _with_id({"type": "health", "watch": w.name, "status": "recovered",
                            "since": _iso(h["blind_since"]), "errors": {}})
        return payload, (0, None, False, now)
    return None, (0, None, False, now)


async def _webhook(hook: httpx.AsyncClient, payload: dict) -> str | None:
    """Webhook sofort versuchen. Gibt None bei Erfolg (oder ohne Webhook) zurück, sonst den Fehler -
    dann kommt die Meldung in die Outbox."""
    if not notify.enabled():
        return None
    return None if await notify.send(hook, payload) else t("Versand fehlgeschlagen", "delivery failed")


async def _deliver(payloads: list[dict | None], hook: httpx.AsyncClient) -> list[tuple[dict, str]]:
    """Meldungen ausgeben: stdout + JSONL-Export (Hits UND Health), dann Webhook versuchen.
    Liefert die Meldungen, deren Webhook fehlschlug (-> Outbox). stdout/Export-Fehler werden geworfen."""
    outbox: list[tuple[dict, str]] = []
    for p in payloads:
        if p is None:
            continue
        _emit(p)
        notify.export(p)
        if (err := await _webhook(hook, p)) is not None:
            outbox.append((p, err))
    return outbox


async def record_failure(store: SeenStore, w: Watch, error: str, hook: httpx.AsyncClient,
                         started: float | None = None) -> None:
    """Ein Lauf, der mit einer Ausnahme endete, zählt als "blind" (wie ein Lauf ohne Treffer wegen
    ausgefallener Quellen) - nach WEBSEARCH_BLIND_AFTER solchen Läufen geht die Health-Meldung raus."""
    failed = {"monitor": error}
    health_payload, health_state = _health_transition(store, w, {"results": [], "sources": {}}, failed)
    outbox: list[tuple[dict, str]] = []
    try:
        outbox = await _deliver([health_payload], hook)
    except Exception as e:     # Ausgabe kaputt: Zustand NICHT fortschreiben, Meldung kommt beim nächsten Mal
        log.error(t("%s: Health-Meldung konnte nicht ausgegeben werden: %s: %s",
                    "%s: health alert could not be delivered: %s: %s"), w.name, type(e).__name__, e)
        store.mark_run(w.name)
        return
    with store.transaction() as db:
        store.set_health(db, w.name, *health_state)
        store.set_run(db, w.name, started)          # Start des Laufs, wie bei check_watch
        for p, err in outbox:
            store.outbox_add(db, p, err)


async def check_watch(eng: SearchEngine, store: SeenStore, w: Watch | dict, hook: httpx.AsyncClient,
                      alert_first: bool = False) -> dict | None:
    """Ein Lauf für einen Watch. Gibt die Treffer-Meldung zurück (oder None, wenn nichts Neues).

    Reihenfolge = Zustellgarantie: suchen -> auswerten (nur lesen) -> ausgeben (stdout, JSONL) ->
    Webhook versuchen -> EINE Transaktion: gesehen, Quellen, Basislinie, Health, Lauf, ggf. Outbox."""
    w = parse_watch(w)
    started = time.time()
    res = await eng.search(w.query, n=pool_size(w.n), kind=w.kind, lang=w.lang,
                           sources=list(w.sources) if w.sources else None,
                           max_age=w.every_s * 0.9,   # knapp unter dem Intervall -> frisch, kein Doppelabruf
                           deadline=w.deadline)
    failed = _failed(res)

    # --- auswerten (nur lesen)
    scraped = {name for name, p in eng.providers.items() if getattr(p, "scraped", False)}
    sparse = {name for name, p in eng.providers.items() if getattr(p, "sparse", False)}
    health_payload, health_state = _health_transition(store, w, res, failed, scraped, sparse)
    qhash = hashlib.sha256(json.dumps([w.query, w.kind, w.lang, sorted(w.sources or [])]).encode()).hexdigest()
    old_qhash = store.query_hash(w.name)
    query_changed = old_qhash is not None and old_qhash != qhash
    if query_changed:
        log.info(t("%s: Suchanfrage geändert - lege neue Basislinie an (keine Meldung)",
                   "%s: query changed - creating a new baseline (no alert)"), w.name)
    first = query_changed or not store.has_baseline(w.name)
    known = store.known_sources(w.name)
    top = {normalize_url(r["url"]) for r in res["results"][:w.n]}
    unseen = store.unseen(w.name, res["results"])

    def from_known_source(r: dict) -> bool:
        item_sources = {e for e in r.get("engines", []) if e in res["sources"]}
        # Ohne gespeicherte Quellen (alte DB) oder ohne Quellenangabe: nicht unterdrücken
        return not known or not item_sources or not item_sources.isdisjoint(known)

    report: list[dict] = []
    if first and not alert_first:
        if res["results"]:
            log.info(t("%s: Basislinie mit %d Treffern angelegt", "%s: baseline created with %d results"), w.name, len(unseen))
        else:
            log.info(t("%s: keine Treffer - Basislinie folgt beim nächsten Lauf (Quellenfehler: %s)",
                       "%s: no results - baseline follows on the next run (source errors: %s)"), w.name,
                     fmt_failed(failed))
    else:
        report = [r for r in unseen
                  if normalize_url(r["url"]) in top and from_known_source(r) and not _too_old(r, w.max_age_days)]
        log.info(t("%s: %d neu (Quellenfehler: %s)", "%s: %d new (source errors: %s)"), w.name, len(report), fmt_failed(failed))
    report = [{"id": hit_id(w.name, r["url"]), **r} for r in report]
    payload = _with_id({"type": "hits", "watch": w.name, "query": w.query, "new": report}) if report else None

    # --- ausgeben: wirft das (z.B. BrokenPipeError), wird NICHTS als gesehen markiert -> nächster Lauf meldet erneut
    outbox = await _deliver([health_payload, payload], hook)

    # --- festschreiben: eine Transaktion, kein await
    with store.transaction() as db:
        if query_changed:
            store.reset_baseline(db, w.name)
        store.set_query(db, w.name, qhash)
        store.mark_seen(db, w.name, res["results"])      # der ganze Pool (frischt auch Bekanntes auf)
        store.add_sources(db, w.name, _delivering(res))
        if res["results"]:
            store.set_baseline(db, w.name)
        store.set_health(db, w.name, *health_state)
        store.set_run(db, w.name, started)
        for p, err in outbox:
            store.outbox_add(db, p, err)
    if outbox:
        log.warning(t("%s: %d Meldung(en) in die Webhook-Outbox gelegt (neuer Versuch später)",
                      "%s: %d alert(s) put into the webhook outbox (will retry later)"), w.name, len(outbox))
    return payload


async def flush_outbox(store: SeenStore, hook: httpx.AsyncClient, now: float | None = None) -> int:
    """Fällige Outbox-Einträge erneut senden. Gibt die Zahl erfolgreich zugestellter Meldungen zurück.
    Läuft zu Beginn jedes Durchlaufs und gibt dem Webhook dabei eine neue Chance."""
    notify.reset_backoff()
    now = time.time() if now is None else now
    delivered = 0
    for entry_id, payload, created, attempts in store.outbox_due(now):
        if now - created > OUTBOX_MAX_AGE:
            log.error(t("Webhook-Meldung nach %d Versuchen verworfen (älter als %d h): %s/%s",
                        "webhook alert dropped after %d attempts (older than %d h): %s/%s"), attempts,
                      OUTBOX_MAX_AGE // 3600, payload.get("type"), payload.get("watch"))
            store.outbox_done(entry_id)
            continue
        if not notify.enabled():
            continue                                   # Webhook abgeschaltet: liegen lassen bis zum Verfall
        if await notify.send(hook, payload):
            store.outbox_done(entry_id)
            delivered += 1
        else:
            store.outbox_retry_later(entry_id, attempts, now)
            break   # Endpunkt gestört: nicht N weitere Timeouts abwarten (hält sonst Durchlauf und Heartbeat auf)
    if delivered:
        log.info(t("Outbox: %d Meldung(en) nachgeliefert", "outbox: %d alert(s) delivered late"), delivered)
    return delivered


JITTER = 0.10
STAGGER_MAX_S = 120.0


def monitor_concurrency() -> int:
    """WEBSEARCH_MONITOR_CONCURRENCY: so viele Watches laufen höchstens gleichzeitig (Standard 2)."""
    return env_int("WEBSEARCH_MONITOR_CONCURRENCY", 2, minimum=1)


def _heartbeat() -> None:
    """WEBSEARCH_HEARTBEAT=<datei>: nach jedem Durchlauf anfassen -> Docker-Healthcheck erkennt Hänger."""
    path = os.environ.get("WEBSEARCH_HEARTBEAT", "").strip()
    if path:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(str(time.time()))
        except OSError as e:
            log.warning(t("Heartbeat-Datei %s nicht schreibbar: %s",
                          "heartbeat file %s not writable: %s"), path, e)


def blind_watches(watch_file: str, db_path: str | None = None) -> tuple[int, int]:
    """(blinde Watches, alle Watches) laut Monitor-DB - für den Exit-Code eines einzelnen Laufs (Cron)."""
    watches = load_watches(watch_file)
    store = SeenStore(db_path or os.environ.get("WEBSEARCH_MONITOR_DB", "").strip()
                      or Path.home() / ".cache/websearch/monitor.db")
    try:
        return sum(1 for w in watches if store.health(w.name)["blind_runs"] > 0), len(watches)
    finally:
        store.close()


async def run_monitor(watch_file: str, loop: bool = False, alert_first: bool = False,
                      db_path: str | None = None, engine: SearchEngine | None = None,
                      interval: float = 30.0, max_cycles: int | None = None) -> list[dict]:
    """Führt alle fälligen Watches aus. `loop`: dauerhaft alle `interval` s prüfen (Watch-Datei wird bei
    Änderung neu eingelesen). `max_cycles` begrenzt die Durchläufe (für Tests).

    Konfigurationsfehler (Watch-Datei, Webhook) brechen beim Start mit InputError ab. Danach gilt: kein
    Fehler eines einzelnen Watches oder des eigenen Zustands beendet die Schleife."""
    notify.check_config()
    monitor_concurrency()
    blind_after()
    # Quellennamen + Sucharten gegen die tatsächlich verfügbaren Quellen prüfen (Fehler -> beim Start)
    kinds = ({name: tuple(p.kinds) for name, p in engine.providers.items()} if engine is not None
             else default_source_kinds())
    wf = WatchFile(watch_file, kinds)
    store = SeenStore(db_path or os.environ.get("WEBSEARCH_MONITOR_DB", "").strip()
                      or Path.home() / ".cache/websearch/monitor.db")
    all_hits: list[dict] = []
    eng = engine or SearchEngine()
    # Eine vom Aufrufer übergebene, bereits geöffnete Engine gehört dem Aufrufer: nicht schließen.
    already_open = engine is not None and engine.is_open
    try:
        async with contextlib.AsyncExitStack() as stack:
            if not already_open:
                await stack.enter_async_context(eng)
            hook = await stack.enter_async_context(httpx.AsyncClient())
            cycles = 0
            sem = asyncio.Semaphore(monitor_concurrency())
            jitter: dict[str, float] = {}          # je Watch +-10 % auf das Intervall -> kein Gleichtakt
            start_offset: dict[str, float] = {}
            started: dict[str, float] = {}         # Startzeit im Speicher: hilft, wenn die DB gesperrt ist
            running: dict[str, asyncio.Task] = {}  # im Dauerbetrieb läuft jeder Watch nach eigenem Takt
            last_purge = time.time()

            async def guarded(w: Watch):
                started = time.time()
                try:
                    return await check_watch(eng, store, w, hook, alert_first)
                except Exception as e:   # ein kaputter Watch darf den Dauerbetrieb nicht beenden
                    log.error(t("%s: FEHLER %s: %s", "%s: ERROR %s: %s"), w.name, type(e).__name__, e)
                    # zählt als blinder Lauf; nur Zeitpunkt + Health, Treffer bleiben ungesehen
                    await _guard(t(f"{w.name}: Fehler festhalten", f"{w.name}: recording failure"),
                                 record_failure(store, w, f"{type(e).__name__}: {e}", hook, started))
                    return None

            async def limited(w: Watch):
                async with sem:
                    try:
                        return await guarded(w)
                    finally:
                        jitter[w.name] = random.uniform(1 - JITTER, 1 + JITTER)
                        _heartbeat()        # auch während langer Durchläufe: Healthcheck sieht Fortschritt

            def collect(task: asyncio.Task) -> None:
                if not task.cancelled() and task.exception() is None and task.result():
                    all_hits.append(task.result())

            try:
                while True:
                    await _guard(t("Outbox nachliefern", "outbox delivery"), asyncio.wait_for(flush_outbox(store, hook), 60))
                    watches = _guard_sync(t("Watch-Datei prüfen", "checking watch file"), wf.current, default=wf.watches)
                    now = time.time()
                    due = []
                    for w in watches:
                        if w.name in running:            # läuft noch: nicht doppelt starten
                            continue
                        last = _guard_sync(t(f"{w.name}: letzter Lauf", f"{w.name}: last run"), lambda w=w: store.last_run(w.name), default=0)
                        last = max(last or 0, started.get(w.name, 0))
                        if loop and not last and w.name not in start_offset:
                            # Erststart im Dauerbetrieb: Watches über die ersten Minuten verteilen statt alle
                            # gleichzeitig (Lastspitzen sind ein Captcha-Auslöser)
                            start_offset[w.name] = now + random.uniform(0, min(STAGGER_MAX_S, w.every_s / 2,
                                                                                interval * 4))
                        if now < start_offset.get(w.name, 0):
                            continue
                        if now - last >= w.every_s * jitter.get(w.name, 1.0):
                            due.append(w)
                    for w in due:
                        started[w.name] = now
                        task = asyncio.create_task(limited(w))
                        task.add_done_callback(collect)
                        running[w.name] = task
                        task.add_done_callback(functools.partial(_forget, running, w.name))
                    if not loop:                          # Einzellauf: auf alle warten
                        await asyncio.gather(*running.values(), return_exceptions=True)
                        _heartbeat()
                        return all_hits
                    _heartbeat()
                    if time.time() - last_purge > 86400:
                        _guard_sync(t("Aufräumen", "cleanup"), store.purge, default=None)
                        last_purge = time.time()
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        await asyncio.gather(*running.values(), return_exceptions=True)
                        return all_hits
                    await asyncio.sleep(interval)
            finally:
                for task in running.values():
                    task.cancel()
                if running:
                    await asyncio.gather(*running.values(), return_exceptions=True)
    finally:
        store.close()


def _forget(running: dict[str, asyncio.Task], name: str, _task: asyncio.Task) -> None:
    running.pop(name, None)


async def _guard(what: str, coro) -> Any:
    """Await mit Fehler-Isolation für die Monitor-Schleife: loggen statt abstürzen."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(t("%s fehlgeschlagen (Schleife läuft weiter): %s: %s",
                    "%s failed (loop keeps running): %s: %s"), what, type(e).__name__, e)
        return None


def _guard_sync(what: str, fn, default=None):
    try:
        return fn()
    except Exception as e:
        log.error(t("%s fehlgeschlagen (Schleife läuft weiter): %s: %s",
                    "%s failed (loop keeps running): %s: %s"), what, type(e).__name__, e)
        return default
