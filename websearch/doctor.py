"""'websearch doctor': prüft die Einrichtung und sagt bei jedem Problem, wie man es behebt.

Jede Prüfung liefert ok / warn / fail mit Erklärung und (bei Problemen) einem konkreten Lösungsschritt.
Exit-Code 1, sobald eine Prüfung "fail" ist; Warnungen allein ergeben 0.

--offline: nur lokale Prüfungen plus Erreichbarkeit der eigenen SearXNG-Instanz (/healthz, /stats/errors) -
keine Suche, keine Anfrage an Suchdienste im Internet.

Hängt nie: jede Netzprüfung hat ein eigenes Zeitbudget (asyncio.wait_for), dazu ein Gesamtbudget.
Zugangsdaten in URLs (SEARXNG_URL=http://user:pass@...) werden nie ausgegeben.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Literal

from .i18n import t

Level = Literal["ok", "warn", "fail"]
_MARK = {"ok": "✓", "warn": "!", "fail": "✗"}

CHECK_TIMEOUT = 10.0        # Sekunden je Netzprüfung (SearXNG-Probe, Testanfrage)
SEARCH_TIMEOUT = 25.0       # Sekunden je Testsuche über alle Quellen
TOTAL_TIMEOUT = 75.0        # Sekunden für alle Netzprüfungen zusammen
from .core import MIN_TOKEN_LEN  # noqa: E402  (eine Quelle für die Mindestlänge)

# Platzhalter aus Vorlagen und Doku: reservierte Domains (RFC 2606/6761), Beispiel-Firmen, Beispiel-Postfächer
_PLACEHOLDER_TLDS = (".example", ".invalid", ".test", ".localhost", ".local")
_PLACEHOLDER_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost)$")
_PLACEHOLDER_WORDS = ("your-company", "yourcompany", "eure-firma", "euerfirma", "your-domain", "eure-domain",
                      "kontakt@eure", "contact@your")
_PLACEHOLDER_LOCALS = ("you", "du", "your.name", "dein.name", "first.last", "vorname.nachname")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)")
_URL_HOST = re.compile(r"https?://(?:[^/\s@()]*@)?([^/\s:()]+)", re.I)


@dataclass
class Check:
    level: Level
    title: str
    detail: str = ""
    fix: str = ""


# --------------------------------------------------------------------------- Hilfen

def redact_url(url: str) -> str:
    """Nur Schema://Host[:Port] - Zugangsdaten, Pfad und Query werden nie angezeigt."""
    import httpx
    try:
        u = httpx.URL(url)
    except Exception:
        return t("<ungültige URL>", "<invalid URL>")
    if not u.host:
        return t("<ungültige URL>", "<invalid URL>")
    host = f"[{u.host}]" if ":" in u.host else u.host
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


def _scrub(text: str, url: str) -> str:
    """Fehlertexte von Zugangsdaten der URL befreien."""
    import httpx
    text = text.replace(url, redact_url(url))
    try:
        userinfo = httpx.URL(url).userinfo.decode("ascii", "replace")
    except Exception:
        userinfo = ""
    for secret in sorted({userinfo, *userinfo.split(":", 1)} - {""}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def searxng_url() -> str:
    return os.environ.get("SEARXNG_URL", "").strip().rstrip("/") or "http://127.0.0.1:8888"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def ua_contacts(ua: str) -> list[tuple[str, str]]:
    """(lokaler Teil, Domain) aller E-Mail-Adressen und URL-Hosts im User-Agent."""
    found = [(m.group(1).lower(), m.group(2).lower().rstrip(".")) for m in _EMAIL.finditer(ua)]
    found += [("", m.group(1).lower().rstrip(".")) for m in _URL_HOST.finditer(ua)]
    return found


def is_placeholder_contact(local: str, domain: str) -> bool:
    whole = f"{local}@{domain}"
    return ("." not in domain or domain.endswith(_PLACEHOLDER_TLDS) or bool(_PLACEHOLDER_DOMAINS.search(domain))
            or any(w in whole for w in _PLACEHOLDER_WORDS) or local in _PLACEHOLDER_LOCALS)


def ua_problem(ua: str) -> str | None:
    """None, wenn der User-Agent einen echten Kontakt enthält; sonst der Grund."""
    ua = ua.strip()
    if not ua:
        return t("WEBSEARCH_UA ist nicht gesetzt (Standard-User-Agent ohne Kontakt)",
                 "WEBSEARCH_UA is not set (default User-Agent without contact)")
    contacts = ua_contacts(ua)
    if not contacts:
        return t("keine E-Mail-Adresse oder URL im User-Agent", "no e-mail address or URL in the User-Agent")
    if all(is_placeholder_contact(local, domain) for local, domain in contacts):
        return t("Kontakt ist ein Platzhalter aus Vorlage/Doku", "contact is a placeholder from a template/docs")
    return None


def read_env_file(path: Path) -> dict[str, str]:
    """Gesetzte Variablen einer .env - derselbe Parser wie für --env-file und die Deploy-Skripte."""
    from .envfile import parse_env_file
    return parse_env_file(path)


def runtime() -> str:
    """Kurzbeschreibung der Laufzeit: bwrap-Sandbox (vom Wrapper gesetzt), Podman, Docker, Container, keine."""
    from .core import in_container
    if os.environ.get("WEBSEARCH_SANDBOX", "").strip().lower() == "bwrap":
        return t("bubblewrap-Sandbox", "bubblewrap sandbox")
    if not in_container():
        return t("kein Container (direkt auf dem Rechner)", "no container (directly on the host)")
    if os.path.exists("/run/.containerenv"):
        return "Podman"
    if os.path.exists("/.dockerenv"):
        return "Docker"
    return t("Container (WEBSEARCH_IN_CONTAINER=1)", "container (WEBSEARCH_IN_CONTAINER=1)")


# --------------------------------------------------------------------------- lokale Prüfungen

def check_config() -> list[Check]:
    """Prüfungen ohne Netz: Sprache, Kontakt, .env geladen, Benachrichtigung, Proxy, MCP-Absicherung, Bezahl-APIs."""
    from .core import InputError
    out: list[Check] = []
    title = t("Konfiguration", "configuration")
    fix_env = t("Wert in .env korrigieren (siehe .env.example.de)", "fix the value in .env (see .env.example)")
    from .core import in_container
    in_docker = in_container()
    out.append(Check("ok", t("Laufzeitumgebung", "runtime"), runtime()))

    from .i18n import check_lang
    try:
        check_lang()
    except InputError as e:
        out.append(Check("fail", f"{title}: WEBSEARCH_LANG", str(e), fix_env))

    try:
        from .engine import _proxy_from_env
        _proxy_from_env()
    except InputError as e:
        out.append(Check("fail", f"{title}: WEBSEARCH_PROXY", str(e),
                         t("Proxy-URL korrigieren oder leeren", "fix or clear the proxy URL")))

    # --- Kontakt im User-Agent
    ua = os.environ.get("WEBSEARCH_UA", "").strip()
    problem = ua_problem(ua)
    if problem:
        docker = restart_hint() if in_docker else ""
        out.append(Check("warn", t("Kontakt im User-Agent fehlt", "no contact in the User-Agent"),
                         t(f"{problem}. Wikipedia und GDELT verlangen einen erreichbaren Kontakt; ohne ihn "
                           "drohen Sperren.",
                           f"{problem}. Wikipedia and GDELT require a reachable contact; without it they may "
                           "block you."),
                         t(f"websearch init --contact <deine-adresse>   (ändert nur WEBSEARCH_UA in .env){docker}",
                           f"websearch init --contact <your-address>   (changes only WEBSEARCH_UA in .env){docker}")))
    else:
        out.append(Check("ok", t("Kontakt im User-Agent", "contact in the User-Agent"), ua))

    # --- .env geladen? (nur außerhalb von Docker: compose liest sie selbst)
    env_file = Path(".env")
    if not in_docker and env_file.is_file():
        try:
            wanted = read_env_file(env_file)
        except (OSError, UnicodeDecodeError) as e:
            out.append(Check("warn", t(".env nicht lesbar", ".env not readable"), f"{type(e).__name__}: {e}",
                             t("Rechte von .env prüfen", "check the permissions of .env")))
        else:
            missing = sorted(k for k in wanted if k not in os.environ)
            differ = sorted(k for k, v in wanted.items() if k in os.environ and os.environ[k].strip() != v)
            if missing or differ:
                parts = []
                if missing:
                    parts.append(t(f"fehlt in der Umgebung: {', '.join(missing)}",
                                   f"missing from the environment: {', '.join(missing)}"))
                if differ:
                    parts.append(t(f"anders als in .env: {', '.join(differ)}",
                                   f"different from .env: {', '.join(differ)}"))
                out.append(Check("warn", t(".env ist nicht (vollständig) geladen", ".env is not (fully) loaded"),
                                 "; ".join(parts) + t(". Docker liest .env selbst; für lokale Läufe muss sie in "
                                                      "die Umgebung.",
                                                      ". Docker reads .env itself; for local runs it must be "
                                                      "loaded into the environment."),
                                 "set -a; . ./.env; set +a"))

    out += _check_notify(title, fix_env)
    out += check_mcp()
    out += _check_limits(fix_env)
    out += _check_fetch_deps()

    from .providers import default_providers
    paid = [name for name, p in default_providers().items() if p.paid and p.available]
    if paid:
        out.append(Check("ok", t("Bezahl-APIs (nur Reserve)", "paid APIs (fallback only)"),
                         t(f"{', '.join(paid)}; Limits {os.environ.get('WEBSEARCH_PAID_DAILY_LIMIT', '100')}/Tag, "
                           f"{os.environ.get('WEBSEARCH_PAID_MONTHLY_LIMIT', '1000')}/Monat",
                           f"{', '.join(paid)}; limits {os.environ.get('WEBSEARCH_PAID_DAILY_LIMIT', '100')}/day, "
                           f"{os.environ.get('WEBSEARCH_PAID_MONTHLY_LIMIT', '1000')}/month")))
    return out


def restart_hint() -> str:
    """Wie man Änderungen an .env in die laufenden Dienste bekommt - je nach Laufzeitumgebung."""
    if os.path.exists("/run/.containerenv"):          # Podman: compose oder Quadlet
        return t(" - auf dem Host ausführen, danach Dienste neu starten ('podman compose up -d' bzw. bei Quadlet "
                 "'deploy/podman/install.sh' erneut)",
                 " - run it on the host, then restart the services ('podman compose up -d', or re-run "
                 "'deploy/podman/install.sh' for Quadlet)")
    return t(" - auf dem Host ausführen, danach Container neu starten ('docker compose up -d')",
             " - run it on the host, then restart the containers ('docker compose up -d')")


def _check_limits(fix_env: str) -> list[Check]:
    """Ungültige Zahlen in den Bezahl-Limits würden sonst erst beim Öffnen der Engine scheitern."""
    from .core import InputError, env_int
    out = []
    for name, default in (("WEBSEARCH_PAID_DAILY_LIMIT", 100), ("WEBSEARCH_PAID_MONTHLY_LIMIT", 1000)):
        try:
            env_int(name, default)
        except InputError as e:
            out.append(Check("fail", f"{t('Konfiguration', 'configuration')}: {name}", str(e), fix_env))
    return out


def _check_fetch_deps() -> list[Check]:
    """fetch_url braucht trafilatura samt lxml_html_clean (fehlt bei alten pip-Versionen gern)."""
    try:
        import lxml_html_clean  # noqa: F401
        import trafilatura  # noqa: F401
    except ImportError as e:
        return [Check("fail", t("Seitenabruf (fetch_url) nicht nutzbar", "page fetching (fetch_url) unusable"),
                      f"{type(e).__name__}: {e}",
                      t("pip install -U pip && pip install -e .   (bzw. pip install lxml_html_clean)",
                        "pip install -U pip && pip install -e .   (or pip install lxml_html_clean)"))]
    return []


def _check_notify(title: str, fix_env: str) -> list[Check]:
    from . import notify
    from .core import InputError
    name = t("Benachrichtigung", "notification")
    try:
        notify.check_config()
    except InputError as e:
        return [Check("fail", f"{title}: {name}", str(e), fix_env)]
    fmt = notify.webhook_format()
    if not notify.enabled():
        if os.environ.get("WEBSEARCH_WEBHOOK_FORMAT", "").strip() and fmt != "json":
            target = "WEBSEARCH_SMTP_HOST + WEBSEARCH_SMTP_TO" if fmt == "email" else "WEBSEARCH_WEBHOOK"
            return [Check("warn", t("Benachrichtigung ohne Ziel", "notification without target"),
                          t(f"WEBSEARCH_WEBHOOK_FORMAT={fmt}, aber {target} ist leer - es wird nichts verschickt.",
                            f"WEBSEARCH_WEBHOOK_FORMAT={fmt}, but {target} is empty - nothing will be sent."),
                          t(f"{target} in .env setzen oder WEBSEARCH_WEBHOOK_FORMAT leeren",
                            f"set {target} in .env or clear WEBSEARCH_WEBHOOK_FORMAT"))]
        return []
    return [Check("ok", name, t(f"Format {fmt}", f"format {fmt}"))]


def _token_fix() -> str:
    return t('sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=$(openssl rand -hex 32)/" .env '
             "(in .env selbst wird $(...) nicht ausgeführt)",
             'sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=$(openssl rand -hex 32)/" .env '
             "($(...) is not executed inside .env)")


def check_mcp() -> list[Check]:
    """Absicherung des MCP-HTTP-Servers: ruft genau die Prüfung auf, mit der der Server beim Start
    entscheidet (mcp_server.check_exposure) - doctor und Server können sich so nicht widersprechen."""
    import logging
    from .core import InputError
    from .mcp_server import allowed_hosts, check_exposure, exposure
    host = os.environ.get("MCP_HOST", "").strip() or "127.0.0.1"
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip() or None
    out: list[Check] = []
    title = t("MCP-Server (HTTP)", "MCP server (HTTP)")
    reasons = exposure(host, allowed_hosts())
    where = "; ".join(reasons)
    srv_log = logging.getLogger("websearch")
    level = srv_log.level
    srv_log.setLevel(logging.ERROR)              # die Warnungen der Prüfung erscheinen hier als Check
    try:
        check_exposure(host, token)
    except InputError as e:
        if token:                                # z.B. Token mit Nicht-ASCII-Zeichen
            out.append(Check("fail", t(f"{title} startet nicht", f"{title} will not start"), str(e), _token_fix()))
            return out
        out.append(Check("fail", t(f"{title} startet nicht: kein Token", f"{title} will not start: no token"),
                         t(f"Über 127.0.0.1 hinaus erreichbar ({where}), aber MCP_AUTH_TOKEN ist leer.",
                           f"Reachable beyond 127.0.0.1 ({where}), but MCP_AUTH_TOKEN is empty."),
                         t(f"{_token_fix()} - oder MCP_HOST=127.0.0.1 und nur lokale MCP_ALLOWED_HOSTS",
                           f"{_token_fix()} - or MCP_HOST=127.0.0.1 and only local MCP_ALLOWED_HOSTS")))
        return out
    finally:
        srv_log.setLevel(level)
    if reasons and not token:
        out.append(Check("warn", t(f"{title} ohne Anmeldung", f"{title} without auth"),
                         t(f"MCP_ALLOW_NO_AUTH=1 bei {where}: nur in Ordnung, solange der Port ausschließlich "
                           "auf 127.0.0.1 veröffentlicht ist (docker-compose: \"127.0.0.1:8765:8765\").",
                           f"MCP_ALLOW_NO_AUTH=1 with {where}: ok only while the port is published on "
                           "127.0.0.1 only (docker-compose: \"127.0.0.1:8765:8765\")."),
                         t(f"für Zugriff von anderen Rechnern: {_token_fix()}",
                           f"for access from other machines: {_token_fix()}")))
    in_dotenv = False
    if not token and Path(".env").is_file():
        try:
            in_dotenv = bool(read_env_file(Path(".env")).get("MCP_AUTH_TOKEN"))
        except (OSError, UnicodeDecodeError):
            pass
    if not token and in_dotenv:
        # nicht neu erzeugen (bräche verbundene Clients) - die .env ist nur nicht geladen
        out.append(Check("warn", t(f"{title}: Token in .env, aber nicht geladen", f"{title}: token in .env but not loaded"),
                         t("MCP_AUTH_TOKEN steht in .env, fehlt aber in dieser Umgebung.",
                           "MCP_AUTH_TOKEN is in .env but missing from this environment."),
                         t("set -a; . ./.env; set +a   (oder: websearch --env-file .env doctor)",
                           "set -a; . ./.env; set +a   (or: websearch --env-file .env doctor)")))
    elif not token and not reasons:
        out.append(Check("warn", t(f"{title} ohne Token", f"{title} without token"),
                         t("Nur auf 127.0.0.1 erreichbar, aber jeder lokale Nutzer dieses Rechners könnte den Dienst "
                           "(und konfigurierte Bezahl-APIs) verwenden.",
                           "Only reachable on 127.0.0.1, but every local user of this machine could use the service "
                           "(and configured paid APIs)."),
                         t(f"{_token_fix()} - dann den Client mit 'Authorization: Bearer <token>' einbinden",
                           f"{_token_fix()} - then add the client with 'Authorization: Bearer <token>'")))
    if token and len(token) < MIN_TOKEN_LEN:
        out.append(Check("warn", t("MCP_AUTH_TOKEN ist kurz", "MCP_AUTH_TOKEN is short"),
                         t(f"{len(token)} Zeichen - mindestens {MIN_TOKEN_LEN} empfohlen.",
                           f"{len(token)} characters - at least {MIN_TOKEN_LEN} recommended."),
                         _token_fix()))
    return out


# --------------------------------------------------------------------------- Netzprüfungen

async def probe_searxng(timeout: float = CHECK_TIMEOUT) -> dict[str, Any]:
    """Ist SearXNG erreichbar (/healthz), und welche seiner Engines melden Fehler (/stats/errors)?
    Keine Suche. Das Ergebnis enthält die URL nur geschwärzt (Schema://Host[:Port]); dauert nie länger
    als `timeout` Sekunden (auch nicht bei einem Server, der die Antwort tröpfchenweise schickt)."""
    import httpx
    url = searxng_url()
    out: dict[str, Any] = {"url": redact_url(url), "reachable": False}

    async def go() -> None:
        async with httpx.AsyncClient(timeout=min(4.0, timeout), trust_env=False) as c:
            r = await c.get(f"{url}/healthz")
            out["reachable"] = r.status_code == 200
            if not out["reachable"]:
                out["error"] = f"HTTP {r.status_code}"
                return
            stats = await c.get(f"{url}/stats/errors")
            if stats.status_code == 200 and "json" in stats.headers.get("content-type", ""):
                data = stats.json()
                if isinstance(data, dict):
                    out["engines_with_errors"] = sorted(str(k) for k in data)

    try:
        await asyncio.wait_for(go(), timeout)
    except asyncio.TimeoutError:
        out["error"] = t(f"keine vollständige Antwort in {timeout:.0f}s", f"no complete answer within {timeout:.0f}s")
        out["timeout"] = True
    except Exception as e:      # Diagnose darf nie selbst scheitern
        out["error"] = _scrub(f"{type(e).__name__}: {e}", url)
    return out


async def check_searxng(live: bool = True, timeout: float = CHECK_TIMEOUT) -> list[Check]:
    """SearXNG erreichbar? Engines mit Fehlern? Live zusätzlich: JSON-API freigeschaltet, Testsuche."""
    import httpx
    url = searxng_url()
    shown = redact_url(url)
    sx = await probe_searxng(timeout)
    if not sx["reachable"]:
        return [Check("fail", t("SearXNG nicht erreichbar", "SearXNG not reachable"),
                      f"{shown} ({sx.get('error') or 'HTTP != 200'})",
                      t("Docker: 'docker compose up -d searxng' und 'docker compose ps' (Podman: 'podman compose ...'); "
                        "lokal/bwrap: SEARXNG_URL prüfen. "
                        "Ohne SearXNG laufen nur die schwächeren Ausweichquellen.",
                        "Docker: 'docker compose up -d searxng' and 'docker compose ps' (Podman: 'podman compose ...'); "
                        "locally/bwrap: check SEARXNG_URL. "
                        "Without SearXNG only the weaker fallback sources run."))]
    out = [Check("ok", t("SearXNG erreichbar", "SearXNG reachable"), shown)]
    if errors := sx.get("engines_with_errors"):
        out.append(Check("warn", t("SearXNG-Engines mit Fehlern", "SearXNG engines with errors"), ", ".join(errors),
                         t(f"Details unter {shown}/stats. Häufen sich Captchas bei Google/DuckDuckGo, läuft der Dienst "
                           "vermutlich auf einer Rechenzentrums-IP - besser im Büro-/Heimnetz betreiben "
                           "(ANALYSE.md, Abschnitt 3).",
                           f"details at {shown}/stats. Frequent captchas at Google/DuckDuckGo usually mean a data-center "
                           "IP - better run it on an office/home connection (ANALYSE.md, section 3).")))
    if not live:
        return out

    async def search() -> httpx.Response:
        async with httpx.AsyncClient(timeout=min(8.0, timeout), trust_env=False) as c:
            return await c.get(f"{url}/search", params={"q": "test", "format": "json"})

    title = t("SearXNG-Testsuche", "SearXNG test search")
    try:
        r = await asyncio.wait_for(search(), timeout)
        if r.status_code == 403:
            out.append(Check("fail", t("SearXNG-JSON-API gesperrt", "SearXNG JSON API disabled"), "HTTP 403",
                             t("in searxng/settings.yml unter search.formats 'json' eintragen und SearXNG neu starten",
                               "add 'json' under search.formats in searxng/settings.yml and restart SearXNG")))
        elif r.status_code == 429:
            out.append(Check("fail", t("SearXNG-Limiter blockiert uns", "SearXNG limiter blocks us"), "HTTP 429",
                             t("in searxng/settings.yml server.limiter: false setzen (interner Dienst)",
                               "set server.limiter: false in searxng/settings.yml (internal service)")))
        elif r.status_code != 200:
            out.append(Check("warn", title, f"HTTP {r.status_code}"))
        else:
            data = r.json()
            n = len(data.get("results") or [])
            dead = [e[0] for e in data.get("unresponsive_engines") or [] if isinstance(e, list) and e]
            level: Level = "ok" if n else "warn"
            out.append(Check(level, title, t(f"{n} Treffer", f"{n} results")
                             + (t(f"; ausgefallen: {', '.join(dead)}", f"; failed: {', '.join(dead)}") if dead else "")))
    except asyncio.TimeoutError:
        out.append(Check("warn", title, t(f"keine Antwort in {timeout:.0f}s", f"no answer within {timeout:.0f}s"),
                         t(f"SearXNG überlastet? Details unter {shown}/stats", f"SearXNG overloaded? details at {shown}/stats")))
    except Exception as e:
        out.append(Check("warn", t("SearXNG-Testsuche fehlgeschlagen", "SearXNG test search failed"),
                         _scrub(f"{type(e).__name__}: {e}", url)))
    return out


class _Budget:
    """Gesamtbudget aller Netzprüfungen; jede Prüfung bekommt min(eigenes Budget, Rest)."""

    def __init__(self, total: float) -> None:
        self.end = time.monotonic() + total

    def left(self, per_check: float) -> float:
        return max(0.0, min(per_check, self.end - time.monotonic()))


def _skipped(title: str) -> Check:
    return Check("warn", title, t("übersprungen - Zeitbudget von doctor aufgebraucht",
                                  "skipped - doctor's time budget used up"))


async def _bounded(coro: Awaitable[list[Check]], seconds: float, title: str, fix: str = "") -> list[Check]:
    """Prüfung mit Zeitlimit; bei Überschreitung ein fail-Ergebnis statt Hängen."""
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        return [Check("fail", title, t(f"keine Antwort in {seconds:.0f}s", f"no answer within {seconds:.0f}s"), fix)]


async def check_engine(live: bool, budget: _Budget | None = None) -> list[Check]:
    """Zustandsdatei, pausierte Quellen, SearXNG und (live) je eine Web- und News-Testsuche."""
    from .engine import SearchEngine
    budget = budget or _Budget(TOTAL_TIMEOUT)
    out: list[Check] = []
    async with SearchEngine() as eng:
        out.append(Check("ok", t("Zustandsdatei", "state file"), str(eng.cache_path)))
        status = eng.status()
        for name, s in status.items():
            if s["available"] and s["blocked"]:
                out.append(Check("warn", t(f"Quelle {name} pausiert", f"source {name} paused"),
                                 t(f"noch {s['retry_in_s']}s: {s['last_error']}",
                                   f"for {s['retry_in_s']}s more: {s['last_error']}"),
                                 t("abwarten (empfohlen) oder 'websearch status --reset'",
                                   "wait (recommended) or 'websearch status --reset'")))
        if "searxng" in status:
            secs = budget.left(2 * CHECK_TIMEOUT)
            # check_searxng begrenzt jede Anfrage selbst; das äußere Limit fängt den Rest ab
            out += await _bounded(check_searxng(live, timeout=min(CHECK_TIMEOUT, secs)), secs,
                                  "SearXNG") if secs > 0 else [_skipped("SearXNG")]
        if live:
            for kind, query in (("web", "open source software"), ("news", "technology")):
                title = t(f"Testsuche ({kind})", f"test search ({kind})")
                secs = budget.left(SEARCH_TIMEOUT)
                if secs <= 0:
                    out.append(_skipped(title))
                    continue
                out += await _bounded(_test_search(eng, kind, query, title, secs), secs, title,
                                      t("Details: websearch -v search \"open source\" --fresh",
                                        "details: websearch -v search \"open source\" --fresh"))
    return out


async def _test_search(eng: Any, kind: str, query: str, title: str, secs: float) -> list[Check]:
    t0 = time.monotonic()
    res = await eng.search(query, n=5, kind=kind, max_age=0, deadline=max(1.0, secs - 3), paid=False)
    hits, took = len(res["results"]), time.monotonic() - t0
    bad = sorted(n for n, st in res["sources"].items() if st["state"] in ("blocked", "unavailable", "error", "paused"))
    level: Level = "fail" if not hits else "warn" if bad else "ok"
    detail = t(f"{hits} Treffer in {took:.1f}s", f"{hits} results in {took:.1f}s")
    if bad:
        detail += t(f"; Probleme: {', '.join(bad)}", f"; problems: {', '.join(bad)}")
    return [Check(level, title, detail, "" if level == "ok" else t("Details: websearch -v search \"open source\" --fresh",
                                                                   "details: websearch -v search \"open source\" --fresh"))]


# --------------------------------------------------------------------------- Ausgabe

def summary(checks: list[Check]) -> str:
    n = len(checks)
    fails = sum(c.level == "fail" for c in checks)
    warns = sum(c.level == "warn" for c in checks)
    total = _plural(n, t("1 Prüfung", "1 check"), t(f"{n} Prüfungen", f"{n} checks"))
    errors = _plural(fails, t("1 Fehler", "1 error"), t(f"{fails} Fehler", f"{fails} errors"))
    warnings = _plural(warns, t("1 Warnung", "1 warning"), t(f"{warns} Warnungen", f"{warns} warnings"))
    return f"{total}: {errors}, {warnings}"


async def run_doctor(live: bool = True, as_json: bool = False, total_timeout: float | None = None) -> int:
    from .core import InputError
    try:
        checks = check_config()
    except Exception as e:      # Diagnose darf nie mit Traceback enden
        checks = [Check("fail", t("Konfiguration", "configuration"), f"{type(e).__name__}: {e}")]
    total = TOTAL_TIMEOUT if total_timeout is None else total_timeout
    try:
        # Sicherheitsnetz über alles (Öffnen/Schließen der Zustandsdatei eingeschlossen)
        checks += await asyncio.wait_for(check_engine(live, _Budget(total)), total + 5)
    except asyncio.TimeoutError:
        checks.append(Check("fail", t("Zeitbudget überschritten", "time budget exceeded"),
                            t(f"Netzprüfungen nach {total:.0f}s abgebrochen", f"network checks aborted after {total:.0f}s"),
                            t("Netz/SearXNG prüfen oder 'websearch doctor --offline'",
                              "check the network/SearXNG or run 'websearch doctor --offline'")))
    except InputError as e:
        if any(c.detail == str(e) for c in checks):      # schon gemeldet (z.B. ungültiges Bezahl-Limit)
            checks.append(Check("warn", t("Netzprüfungen übersprungen", "network checks skipped"),
                                t("erst den Konfigurationsfehler oben beheben", "fix the configuration error above first")))
        else:
            checks.append(Check("fail", t("Konfiguration", "configuration"), str(e),
                                t("erst diesen Fehler beheben - die Netzprüfungen wurden übersprungen",
                                  "fix this first - the network checks were skipped")))
    except Exception as e:      # Diagnose darf nie mit Traceback enden
        checks.append(Check("fail", t("Unerwarteter Fehler", "unexpected error"), f"{type(e).__name__}: {e}"))
    failed = any(c.level == "fail" for c in checks)
    if as_json:
        print(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"{_MARK[c.level]} {c.title}" + (f" - {c.detail}" if c.detail else ""))
            if c.fix:
                print(f"    → {c.fix}")
        print("\n" + summary(checks))
    return 1 if failed else 0
