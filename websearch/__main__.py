"""Kommandozeile (Deutsch/Englisch je nach WEBSEARCH_LANG bzw. Systemsprache, siehe i18n.py).

Exit-Codes: 0 = Erfolg (mind. ein Treffer bzw. Befehl ok), 1 = keine Treffer und alle Quellen
fehlgeschlagen (bzw. 'doctor' hat Fehler gefunden), 2 = ungültige Eingabe/Konfiguration,
130 = abgebrochen (Strg+C).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import textwrap
from typing import Any, Mapping

from . import __version__
from .core import NON_FAILURE_STATES, SUCCESS_STATES, InputError
from .engine import KINDS, MAX_DAYS, SearchEngine
from .i18n import check_lang, lang, t

EXIT_OK, EXIT_NO_RESULTS, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


class UsageError(Exception):
    """Ungültige Eingabe oder Konfiguration -> Exit-Code 2."""


def _err(msg: object) -> None:
    print(t(f"Fehler: {msg}", f"Error: {msg}"), file=sys.stderr)


def _setup_logging(verbose: int, quiet: bool) -> None:
    # Reihenfolge: -v/-q (Kommandozeile) vor WEBSEARCH_LOG_LEVEL vor Standard WARNING
    flag = "DEBUG" if verbose >= 2 else "INFO" if verbose == 1 else "WARNING" if quiet else ""
    level = flag or os.environ.get("WEBSEARCH_LOG_LEVEL", "").strip().upper() or "WARNING"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        print(t(f"Warnung: WEBSEARCH_LOG_LEVEL={level!r} unbekannt - nutze WARNING",
                f"Warning: WEBSEARCH_LOG_LEVEL={level!r} unknown - using WARNING"), file=sys.stderr)
        level = "WARNING"
    logging.basicConfig(level=level, stream=sys.stderr, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for lg in ("httpx", "httpcore", "primp", "ddgs", "trafilatura", "charset_normalizer"):
        logging.getLogger(lg).setLevel(logging.WARNING)
    # Monitor-Meldungen ("Basislinie", "3 neu") sind der eigentliche Output auf stderr
    if not quiet:
        logging.getLogger("websearch.monitor").setLevel(min(logging.INFO, logging.getLogger().level))


def _engine_ok(res: Mapping[str, Any]) -> bool:
    """Exit 1 nur, wenn wirklich alles ausgefallen ist - nicht bei bloß gedrosselten/übersprungenen Quellen."""
    from .core import is_benign
    return bool(res["results"]) or any(is_benign(st) for st in res["sources"].values())


# --------------------------------------------------------------------------- Ausgabe

def _print_result(res: Mapping[str, Any], snippets: bool = True) -> None:
    n = len(res["results"])
    print(t(f"\n== {res['query']}  ({res['kind']}, {n} Treffer)", f"\n== {res['query']}  ({res['kind']}, {n} results)"))
    if res.get("error"):
        print(t(f"   Fehler: {res['error']}", f"   Error: {res['error']}"))
    width = 100
    for i, r in enumerate(res["results"], 1):
        date = f" [{r['published'][:10]}]" if r.get("published") else ""
        duration = f" ({r['duration']})" if r.get("duration") else ""
        print(f"{i:2}. {r['title'][:width]}{duration}{date}\n    {r['url']}")
        if r.get("image"):
            print(t(f"    Bild: {r['image']}", f"    image: {r['image']}"))
        if snippets and r.get("snippet"):
            for line in textwrap.wrap(r["snippet"], width, max_lines=2, placeholder=" …"):
                print(f"    {line}")
        print(f"    via {', '.join(r['engines'][:5])}")
    print(t("-- Quellen:", "-- sources:"))
    for name, st in res["sources"].items():
        benign = st["state"] in NON_FAILURE_STATES or st["meta"].get("early")
        mark = "✓" if st["state"] in SUCCESS_STATES else "–" if benign else "✗"
        info = st["message"] or ""
        hits = t("Treffer", "results")
        print(f"   {mark} {name:16} {st['state']:14} {st['count']:3} {hits:7} {st['ms']:6} ms  {info}")
        failed = st["meta"].get("engines_failed")
        if isinstance(failed, list) and failed:
            items = [f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) > 1 else str(e) for e in failed]
            print(t(f"       SearXNG-Engines ausgefallen: {', '.join(items)}",
                    f"       SearXNG engines failed: {', '.join(items)}"))


def _print_status(status: dict, searxng: dict | None = None, usage: dict | None = None) -> None:
    head = (t("Quelle", "source"), t("verfügbar", "available"), t("Sucharten", "kinds"), t("Takt", "interval"),
            t("Zustand", "state"))
    print(f"{head[0]:18} {head[1]:10} {head[2]:28} {head[3]:>8}  {head[4]}")
    for name, s in status.items():
        if not s["available"]:
            state = t("nicht konfiguriert (API-Key/Zugangsdaten fehlen)", "not configured (API key/credentials missing)")
        elif s["blocked"]:
            state = t(f"PAUSIERT noch {s['retry_in_s']}s - {s['last_error']}",
                      f"PAUSED for {s['retry_in_s']}s - {s['last_error']}")
        elif name == "searxng" and searxng and not searxng["reachable"]:
            why = searxng.get("error") or t("kein /healthz", "no /healthz")
            state = t(f"NICHT erreichbar ({why})", f"NOT reachable ({why})")
        else:
            state = t("bereit", "ready")
        yes = t("ja", "yes") if s["available"] else t("nein", "no")
        print(f"{name:18} {yes:10} {','.join(s['kinds']):28} {s['min_interval_s']:7.2f}s  {state}")
    if searxng:
        state = t("erreichbar", "reachable") if searxng["reachable"] else t("NICHT erreichbar", "NOT reachable")
        print(t(f"\nSearXNG unter {searxng['url']}: {state}", f"\nSearXNG at {searxng['url']}: {state}"))
        if searxng.get("engines_with_errors"):
            engines = ", ".join(searxng["engines_with_errors"])
            print(t(f"SearXNG-Engines mit Fehlern (Details: {searxng['url']}/stats): {engines}",
                    f"SearXNG engines with errors (details: {searxng['url']}/stats): {engines}"))
    if usage:
        day = "?" if usage["day"] < 0 else usage["day"]
        month = "?" if usage["month"] < 0 else usage["month"]
        print(t(f"Bezahl-Abfragen: heute {day}/{usage['daily_limit']}, Monat {month}/{usage['monthly_limit']} "
                "(WEBSEARCH_PAID_DAILY_LIMIT / _MONTHLY_LIMIT)",
                f"Paid queries: today {day}/{usage['daily_limit']}, month {month}/{usage['monthly_limit']} "
                "(WEBSEARCH_PAID_DAILY_LIMIT / _MONTHLY_LIMIT)"))
    print(t("\nHinweis: 'status --reset' setzt nur die Pausen dieses Tools zurück; SearXNG-interne Sperren "
            "hebt nur ein Neustart von SearXNG auf.",
            "\nNote: 'status --reset' only clears this tool's pauses; SearXNG's own engine suspensions are "
            "only lifted by restarting SearXNG."))


# --------------------------------------------------------------------------- Befehle

async def _cmd_search(a) -> int:
    kw = _search_kwargs(a)
    async with SearchEngine() as eng:
        res = await eng.search(a.query, **kw)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_result(res, snippets=not a.no_snippets)
    return EXIT_OK if _engine_ok(res) else EXIT_NO_RESULTS


async def _cmd_many(a) -> int:
    try:
        fh = sys.stdin if a.file == "-" else open(a.file, encoding="utf-8-sig")
        with fh:
            queries = [q.strip() for q in fh if q.strip() and not q.lstrip().startswith("#")]
    except OSError as e:
        raise UsageError(t(f"Datei nicht lesbar: {e}", f"cannot read file: {e}")) from None
    except UnicodeDecodeError:
        raise UsageError(t("Anfragen-Datei ist nicht UTF-8-kodiert - bitte als UTF-8 speichern",
                           "query file is not UTF-8 encoded - please save it as UTF-8")) from None
    if not queries:
        raise UsageError(t("keine Suchanfragen gefunden (eine pro Zeile, # = Kommentar)",
                           "no queries found (one per line, # = comment)"))
    if not 1 <= a.concurrency <= 32:
        raise UsageError(t("--concurrency muss zwischen 1 und 32 liegen", "--concurrency must be between 1 and 32"))
    async with SearchEngine() as eng:
        results = await eng.search_many(queries, concurrency=a.concurrency, **_search_kwargs(a))
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))   # ein gültiges JSON-Array
    else:
        for res in results:
            _print_result(res, snippets=not a.no_snippets)
    return EXIT_OK if any(_engine_ok(r) for r in results) else EXIT_NO_RESULTS


async def _cmd_fetch(a) -> int:
    from .fetch import FetchError, FetchForbidden
    async with SearchEngine() as eng:
        try:
            page = await eng.fetch(a.url, max_chars=a.max_chars)
        except FetchForbidden as e:      # verbotenes Ziel = ungültige Eingabe
            _err(e)
            return EXIT_USAGE
        except FetchError as e:
            _err(e)
            return EXIT_NO_RESULTS
    if a.json:
        print(json.dumps(page, ensure_ascii=False, indent=2))
    else:
        print(f"# {page['title'] or page['final_url']}\n<{page['final_url']}>\n")
        print(page["content"])
        if page["truncated"]:
            print(t(f"\n[... gekürzt auf {a.max_chars} Zeichen]", f"\n[... truncated to {a.max_chars} characters]"))
    return EXIT_OK


async def _cmd_monitor(a) -> int:
    from .monitor import run_monitor
    if not (1 <= a.interval <= 86400):
        raise UsageError(t("--interval muss zwischen 1 und 86400 Sekunden liegen",
                           "--interval must be between 1 and 86400 seconds"))
    await run_monitor(a.file, loop=a.loop, alert_first=a.alert_first, interval=a.interval)
    if not a.loop:              # einzelner Lauf (Cron): Exit 1, wenn jeder Watch gerade blind ist
        from .monitor import blind_watches
        blind, total = blind_watches(a.file)
        if total and blind == total:
            print(t(f"Alle {total} Watches sind blind (Quellen fallen aus) - websearch status / doctor prüfen",
                    f"all {total} watches are blind (sources failing) - check websearch status / doctor"),
                  file=sys.stderr)
            return EXIT_NO_RESULTS
    return EXIT_OK


from .doctor import probe_searxng  # noqa: E402  (Erreichbarkeit + gesperrte Engines, mit Zeitlimit)


async def _cmd_status(a) -> int:
    async with SearchEngine() as eng:
        if a.reset:
            eng.store.reset_state()
            print(t("Alle Pausen und Tempo-Slots dieses Tools zurückgesetzt.",
                    "All pauses and rate slots of this tool were reset."), file=sys.stderr)
        st = eng.status()
        usage = eng.paid_usage()
    sx = await probe_searxng() if "searxng" in st else None
    if a.json:
        out: dict = {"sources": st, "paid_usage": usage}
        if sx:
            out["searxng"] = sx
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _print_status(st, sx, usage)
    return EXIT_OK


async def _cmd_sources(a) -> int:
    from .engine import DEFAULT_POLICY
    from .providers import default_providers
    tiers: dict[str, list[str]] = {}
    for tier in ("primary", "fallback", "paid"):
        for kind, names in getattr(DEFAULT_POLICY, tier).items():
            for n in names:
                tiers.setdefault(n, []).append(f"{kind}:{tier}")
    for name, p in default_providers().items():
        doc = p.describe()
        missing = "" if p.available else t("(nicht konfiguriert) ", "(not configured) ")
        print(f"{name:18} {missing}{doc}")
        used = ", ".join(tiers.get(name, [t("nur explizit per -s", "only explicitly via -s")]))
        print(f"{'':18} {t('Sucharten', 'kinds')}: {', '.join(p.kinds)} · {t('Stufen', 'tiers')}: {used}")
    return EXIT_OK


async def _cmd_metrics(a) -> int:
    from .metrics import render
    async with SearchEngine() as eng:
        sys.stdout.write(render(eng))
    return EXIT_OK


async def _cmd_doctor(a) -> int:
    from .doctor import run_doctor
    return await run_doctor(live=not a.offline, as_json=a.json)


async def _cmd_init(a) -> int:
    from .wizard import run_init
    return run_init(directory=a.dir, contact=a.contact, lang=a.lang, force=a.force,
                    interactive=not a.yes and sys.stdin.isatty())


def _search_kwargs(a) -> dict:
    sources = [s.strip() for s in a.sources.split(",") if s.strip()] if a.sources else None
    if a.n < 1 or a.n > 100:
        raise UsageError(t("-n muss zwischen 1 und 100 liegen", "-n must be between 1 and 100"))
    if not (0 < a.deadline <= 3600):
        raise UsageError(t("--deadline muss größer als 0 und höchstens 3600 Sekunden sein",
                           "--deadline must be greater than 0 and at most 3600 seconds"))
    kw = dict(n=a.n, kind=a.kind, lang=a.lang, sources=sources, max_age=0 if a.fresh else None,
              paid=not a.no_paid, deadline=a.deadline)
    if a.days is not None:
        kw["days"] = a.days
    return kw


# --------------------------------------------------------------------------- Parser

# argparse holt seine eigenen Texte ("usage:", "options", Fehlermeldungen) über gettext - auf Deutsch
# ohne installierten Katalog. Deshalb hier die wichtigsten Texte selbst übersetzen.
_ARGPARSE_DE = {
    "usage: ": "Aufruf: ",
    "positional arguments": "Argumente",
    "options": "Optionen",
    "show this help message and exit": "diese Hilfe anzeigen und beenden",
    "%(prog)s: error: %(message)s\n": "%(prog)s: Fehler: %(message)s\n",
    "argument %(argument_name)s: %(message)s": "Argument %(argument_name)s: %(message)s",
    "invalid %(type)s value: %(value)r": "ungültiger Wert (%(type)s): %(value)r",
    "invalid choice: %(value)r (choose from %(choices)s)": "ungültige Auswahl: %(value)r (möglich: %(choices)s)",
    "the following arguments are required: %s": "diese Argumente fehlen: %s",
    "unrecognized arguments: %s": "unbekannte Argumente: %s",
    "expected one argument": "erwartet einen Wert",
    "ambiguous option: %(option)s could match %(matches)s": "mehrdeutige Option: %(option)s passt zu %(matches)s",
    "not allowed with argument %s": "nicht zusammen mit %s erlaubt",
}


def _translate_argparse() -> None:
    import gettext
    argparse._ = (lambda s: _ARGPARSE_DE.get(s, s)) if lang() == "de" else gettext.gettext  # type: ignore[attr-defined]


def build_parser() -> argparse.ArgumentParser:
    _translate_argparse()
    ap = argparse.ArgumentParser(
        prog="websearch", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=t("Parallele, captcha-arme Websuche über freie Quellen (SearXNG, Bing-RSS, Google News, ...).",
                      "Parallel, captcha-avoiding web search over free sources (SearXNG, Bing RSS, Google News, ...)."),
        epilog=t(textwrap.dedent("""\
            Erste Schritte:
              websearch init           (.env mit Secret und Kontakt anlegen)
              websearch doctor         (Einrichtung prüfen, mit Lösungshinweisen)

            Beispiele:
              websearch search "Wärmepumpe Förderung" -l de
              websearch search "EU AI Act" -k news --days 7 -n 5 --json
              websearch search "Nordlicht" -k images
              websearch fetch https://example.org/artikel
              websearch monitor watches/watches.json --loop
              websearch mcp            (MCP-Server für KI-Agents)

            Sprache: WEBSEARCH_LANG=de|en (Standard: Systemsprache).
            Exit-Codes: 0 ok, 1 keine Treffer/alle Quellen ausgefallen, 2 ungültige Eingabe, 130 abgebrochen."""),
            textwrap.dedent("""\
            Getting started:
              websearch init           (create .env with secret and contact)
              websearch doctor         (check the setup, with hints how to fix problems)

            Examples:
              websearch search "heat pump subsidies" -l en
              websearch search "EU AI Act" -k news --days 7 -n 5 --json
              websearch search "northern lights" -k images
              websearch fetch https://example.org/article
              websearch monitor watches/watches.json --loop
              websearch mcp            (MCP server for AI agents)

            Language: WEBSEARCH_LANG=de|en (default: system language).
            Exit codes: 0 ok, 1 no results/all sources failed, 2 invalid input, 130 interrupted.""")))
    ap.add_argument("--version", action="version", version=f"websearch {__version__}",
                    help=t("Version anzeigen und beenden", "show the version and exit"))
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help=t("mehr Log-Ausgaben (-vv = Debug)", "more log output (-vv = debug)"))
    ap.add_argument("-q", "--quiet", action="store_true",
                    help=t("nur Warnungen/Fehler loggen", "log warnings/errors only"))
    ap.add_argument("--env-file", metavar=t("DATEI", "FILE"),
                    help=t(".env-Datei laden (ohne sie auszuführen; gesetzte Umgebungsvariablen gewinnen) - z.B. für "
                           "den stdio-MCP-Server: websearch --env-file .env mcp",
                           "load a .env file (never executed; variables already set win) - e.g. for the stdio "
                           "MCP server: websearch --env-file .env mcp"))
    sub = ap.add_subparsers(dest="cmd", metavar=t("BEFEHL", "COMMAND"))

    def search_opts(p):
        p.add_argument("-k", "--kind", default="web", choices=list(KINDS),
                       help=t("web = Websuche, news = Nachrichten (neueste zuerst), ref = Nachschlagewerke, "
                              "images = Bilder, videos = Videos",
                              "web = web search, news = news (newest first), ref = reference works, "
                              "images, videos"))
        p.add_argument("-n", type=int, default=10, metavar=t("ANZAHL", "COUNT"),
                       help=t("Trefferzahl 1-100 (Standard 10)", "number of results 1-100 (default 10)"))
        p.add_argument("-l", "--lang", default="auto", metavar=t("SPRACHE", "LANG"),
                       help=t("z.B. de, en (Standard auto)", "e.g. de, en (default auto)"))
        p.add_argument("-d", "--days", type=int, metavar=t("TAGE", "DAYS"),
                       help=t(f"nur Treffer der letzten N Tage (1-{MAX_DAYS}; nicht für -k ref)",
                              f"only results from the last N days (1-{MAX_DAYS}; not for -k ref)"))
        p.add_argument("-s", "--sources", metavar=t("LISTE", "LIST"),
                       help=t("feste Quellen, kommagetrennt (siehe 'websearch sources'); ohne Stufen",
                              "fixed sources, comma-separated (see 'websearch sources'); no tiers"))
        p.add_argument("--json", action="store_true", help=t("maschinenlesbare Ausgabe (JSON)", "machine-readable output (JSON)"))
        p.add_argument("--fresh", action="store_true", help=t("Cache ignorieren", "ignore the cache"))
        p.add_argument("--no-paid", action="store_true", help=t("Bezahl-APIs nie verwenden", "never use paid APIs"))
        p.add_argument("--no-snippets", action="store_true",
                       help=t("Textausschnitte nicht anzeigen", "do not show snippets"))
        p.add_argument("--deadline", type=float, default=20.0, metavar=t("SEK", "SEC"),
                       help=t("Zeitbudget (Standard 20 s)", "time budget (default 20 s)"))

    p = sub.add_parser("search", help=t("eine Suche ausführen", "run one search"))
    p.add_argument("query", help=t("Suchbegriff", "search query"))
    search_opts(p)
    p = sub.add_parser("many", help=t("viele Suchen parallel (eine pro Zeile, '-' = stdin)",
                                      "many searches in parallel (one per line, '-' = stdin)"))
    p.add_argument("file", help=t("Datei mit Suchanfragen oder '-'", "file with queries or '-'"))
    p.add_argument("--concurrency", type=int, default=4,
                   help=t("gleichzeitige Suchen (Standard 4)", "concurrent searches (default 4)"))
    search_opts(p)
    p = sub.add_parser("fetch", help=t("Seite abrufen und als Markdown ausgeben", "fetch a page and print it as Markdown"))
    p.add_argument("url", help=t("http(s)-Adresse der Seite (auch Google-News-Links)",
                                 "http(s) address of the page (Google News links too)"))
    p.add_argument("--max-chars", type=int, default=20_000,
                   help=t("Inhalt kürzen (Standard 20000)", "truncate content (default 20000)"))
    p.add_argument("--json", action="store_true", help=t("Ergebnis als JSON", "result as JSON"))
    p = sub.add_parser("monitor", help=t("Watches prüfen und nur neue Treffer melden",
                                         "check watches and report only new results"))
    p.add_argument("file", help="watches.json")
    p.add_argument("--loop", action="store_true",
                   help=t("dauerhaft laufen (Datei wird bei Änderung neu geladen)",
                          "keep running (the file is reloaded when it changes)"))
    p.add_argument("--interval", type=float, default=30.0,
                   help=t("Prüfabstand der Schleife in s (Standard 30)", "loop tick in seconds (default 30)"))
    p.add_argument("--alert-first", action="store_true",
                   help=t("auch beim ersten Lauf melden", "also report on the first run"))
    p = sub.add_parser("status", help=t("Quellen und aktuelle Pausen anzeigen (hostweit)",
                                        "show sources and current pauses (host-wide)"))
    p.add_argument("--reset", action="store_true",
                   help=t("alle Pausen/Tempo-Slots dieses Tools zurücksetzen", "reset all pauses/rate slots of this tool"))
    p.add_argument("--json", action="store_true", help=t("Ergebnis als JSON", "result as JSON"))
    sub.add_parser("sources", help=t("alle Quellen mit Beschreibung und Stufen auflisten",
                                     "list all sources with description and tiers"))
    p = sub.add_parser("doctor", help=t("Einrichtung prüfen und Lösungen vorschlagen",
                                        "check the setup and suggest fixes"))
    p.add_argument("--offline", action="store_true",
                   help=t("keine Suchen; nur lokale Prüfungen und Erreichbarkeit von SearXNG",
                          "no searches; only local checks and SearXNG reachability"))
    p.add_argument("--json", action="store_true", help=t("Ergebnis als JSON", "result as JSON"))
    p = sub.add_parser("init", help=t(".env anlegen (Secret, Kontakt, Sprache) und Watches vorbereiten",
                                      "create .env (secret, contact, language) and prepare watches"))
    p.add_argument("--dir", default=None,
                   help=t("Projektverzeichnis (Standard: nächstes Verzeichnis mit docker-compose.yml, sonst das aktuelle)",
                          "project directory (default: nearest directory with docker-compose.yml, else the current one)"))
    p.add_argument("--contact", metavar=t("KONTAKT", "CONTACT"), help=t("Kontakt-E-Mail oder -URL für den User-Agent (ändert in einer vorhandenen "
                                       ".env nur WEBSEARCH_UA)",
                                       "contact e-mail or URL for the User-Agent (updates only WEBSEARCH_UA in an "
                                       "existing .env)"))
    p.add_argument("--lang", choices=["de", "en"], help=t("Sprache der Meldungen", "language of messages"))
    p.add_argument("--force", action="store_true",
                   help=t("aus der aktuellen Vorlage neu erzeugen, bisherige Werte bleiben (Sicherung als "
                          ".env.bak-<Zeitstempel>)",
                          "regenerate from the current template, keeping all previous values (backup as "
                          ".env.bak-<timestamp>)"))
    p.add_argument("-y", "--yes", action="store_true", help=t("nichts fragen", "do not ask questions"))
    sub.add_parser("metrics", help=t("Prometheus-Metriken ausgeben", "print Prometheus metrics"))
    p = sub.add_parser("mcp", help=t("MCP-Server starten (stdio oder --http)", "start the MCP server (stdio or --http)"))
    p.add_argument("--http", action="store_true",
                   help=t("streamable-http statt stdio (MCP_HOST/MCP_PORT)", "streamable-http instead of stdio (MCP_HOST/MCP_PORT)"))
    return ap


COMMANDS = {"search": _cmd_search, "many": _cmd_many, "fetch": _cmd_fetch, "monitor": _cmd_monitor,
            "status": _cmd_status, "sources": _cmd_sources, "metrics": _cmd_metrics, "doctor": _cmd_doctor,
            "init": _cmd_init}


def _load_env_file_arg(args: list[str]) -> int | None:
    """--env-file DATEI bzw. --env-file=DATEI (vor dem Befehl) laden. Fehlt die Datei, nur warnen (die
    mitgelieferte .mcp.json nutzt .env, die es vor 'websearch init' noch nicht gibt)."""
    path = None
    for i, arg in enumerate(args):
        if arg == "--":
            break
        if arg == "--env-file" and i + 1 < len(args):
            path = args[i + 1]
        elif arg.startswith("--env-file="):
            path = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            break                              # ab dem Befehl gehören Argumente dem Befehl
    if not path:
        return None
    from .envfile import load_env_file
    try:
        load_env_file(path)
    except FileNotFoundError:
        print(t(f"Warnung: --env-file {path} gibt es nicht - weiter ohne (websearch init legt sie an)",
                f"Warning: --env-file {path} does not exist - continuing without it (websearch init creates it)"),
              file=sys.stderr)
    except (OSError, UnicodeDecodeError) as e:
        _err(t(f"--env-file nicht lesbar: {e}", f"--env-file not readable: {e}"))
        return EXIT_USAGE
    return None


def main(argv: list[str] | None = None) -> int:
    # --env-file vor dem Parser laden: WEBSEARCH_LANG daraus soll schon für Hilfe und Fehlermeldungen gelten
    args = sys.argv[1:] if argv is None else list(argv)
    if (code := _load_env_file_arg(args)) is not None:
        return code
    parser = build_parser()
    a = parser.parse_args(args)
    if a.cmd is None:                       # nur "websearch": Hilfe statt einer knappen Fehlermeldung
        parser.print_help()
        return EXIT_USAGE
    _setup_logging(a.verbose, a.quiet)
    if a.cmd not in ("doctor", "init"):     # doctor meldet eine falsche Sprache selbst, init setzt sie
        try:
            check_lang()
        except InputError as e:
            _err(e)
            return EXIT_USAGE
    if a.cmd == "mcp":
        from .mcp_server import serve
        try:
            serve(http=a.http)
        except InputError as e:
            _err(e)
            return EXIT_USAGE
        return EXIT_OK
    try:
        return asyncio.run(COMMANDS[a.cmd](a))
    except (UsageError, InputError) as e:
        _err(e)
        return EXIT_USAGE
    except sqlite3.Error as e:
        files = [f"WEBSEARCH_CACHE={os.environ.get('WEBSEARCH_CACHE', '~/.cache/websearch/cache.db')}"]
        if a.cmd == "monitor":
            files.append(f"WEBSEARCH_MONITOR_DB={os.environ.get('WEBSEARCH_MONITOR_DB', '~/.cache/websearch/monitor.db')}")
        _err(t(f"SQLite-Datei nicht nutzbar ({e}). Betroffen ist eine von: {', '.join(files)}. "
               "Pfad und Rechte prüfen oder die Variable auf einen schreibbaren Pfad setzen.",
               f"SQLite file not usable ({e}). Affected is one of: {', '.join(files)}. "
               "Check path and permissions or point the variable to a writable path."))
        return EXIT_USAGE
    except OSError as e:
        hint = ""
        if getattr(e, "filename", None):
            hint = t(" - Datenpfad nicht anlegbar? WEBSEARCH_CACHE / WEBSEARCH_MONITOR_DB auf ein schreibbares "
                     "Verzeichnis setzen",
                     " - data path not creatable? Point WEBSEARCH_CACHE / WEBSEARCH_MONITOR_DB to a writable "
                     "directory")
        _err(f"{e}{hint}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        print(t("abgebrochen", "interrupted"), file=sys.stderr)
        return EXIT_INTERRUPTED


def run() -> None:
    """Einstiegspunkt der Konsole. Ein hängender ddgs-Request läuft in einem Thread-Pool der Bibliothek,
    auf den Python beim Beenden warten würde (bis zu dessen Timeout) - die Ausgabe ist aber fertig.
    Deshalb nach dem Flushen hart beenden, wenn solche Threads übrig sind."""
    import threading
    code = main()
    # auch Daemon-Threads: concurrent.futures wartet beim Beenden auf seine Worker (ddgs, trafilatura)
    if any(t_.is_alive() and t_ is not threading.main_thread() for t_ in threading.enumerate()):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    run()


def run_mcp() -> None:
    """Einstiegspunkt ``multi-websearch-mcp`` = ``websearch [globale Optionen] mcp [--http]``.

    Damit startet ``uvx multi-websearch-mcp`` (so rufen MCP-Registries und Clients ein PyPI-Paket auf) direkt den
    MCP-Server; ``--env-file``, ``-v`` und ``-q`` gelten wie bei ``websearch``."""
    args = sys.argv[1:]
    sys.argv = [sys.argv[0], *[a for a in args if a != "--http"], "mcp", *[a for a in args if a == "--http"]]
    run()
