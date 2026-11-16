"""Benachrichtigungen für das Monitoring: genau EIN Ziel (Webhook oder E-Mail) und JSONL-Export.

Umgebungsvariablen:
  WEBSEARCH_WEBHOOK         Ziel-URL (leer = kein Webhook; bei Format email nicht nötig)
  WEBSEARCH_WEBHOOK_FORMAT  json (Standard)  - Payload unverändert als JSON (n8n, eigene Dienste)
                            text             - Klartext für ntfy; Titel als ?title=... (URL-kodiert,
                                               Umlaute sicher; vorhandene Query-Parameter wie ?auth= bleiben)
                            slack            - {"text": ...} mit Slack-mrkdwn-Links <url|titel>
                            discord          - Discord-Webhook {"content": ...}: Markdown-Links [titel](url),
                                               max. 2000 Zeichen, keine @everyone-/@here-Erwähnungen,
                                               keine Link-Vorschauen
                            teams            - Microsoft Teams über einen Workflows-Webhook (Power Automate,
                                               "Wenn eine Teams-Webhookanforderung empfangen wird"):
                                               Nachricht mit Adaptive Card 1.4 (TextBlocks), max. ~28 KB
                            telegram         - Bot-API sendMessage: WEBSEARCH_WEBHOOK =
                                               https://api.telegram.org/bot<TOKEN>/sendMessage,
                                               WEBSEARCH_TELEGRAM_CHAT_ID = Chat-ID (Pflicht) oder @kanalname;
                                               parse_mode HTML, max. 4096 Zeichen, keine Link-Vorschau.
                                               Telegram meldet Fehler auch mit HTTP 200 + {"ok": false}
                            email            - SMTP (stdlib smtplib im Thread), Klartext + HTML-Alternative:
      WEBSEARCH_SMTP_HOST       Server (Pflicht)
      WEBSEARCH_SMTP_PORT       Standard 587 (bei WEBSEARCH_SMTP_TLS=ssl: 465)
      WEBSEARCH_SMTP_TLS        starttls (Standard) | ssl | none
      WEBSEARCH_SMTP_USER       Login (optional; dann auch WEBSEARCH_SMTP_PASSWORD)
      WEBSEARCH_SMTP_PASSWORD
      WEBSEARCH_SMTP_FROM       Absender (Standard: WEBSEARCH_SMTP_USER, falls das eine Adresse ist)
      WEBSEARCH_SMTP_TO         Empfänger, kommagetrennt (Pflicht)
  WEBSEARCH_MONITOR_OUT     Pfad einer JSONL-Datei; jede Meldung (Treffer UND Health) wird als eine Zeile
                            angehängt. Schlägt das Schreiben fehl, wird der Fehler weitergereicht: die
                            Treffer bleiben dann ungesehen und werden beim nächsten Lauf erneut gemeldet.

Payloads (jede mit stabiler "id" zur Deduplizierung - Zustellung ist at-least-once):
  {"id", "type": "hits",   "watch", "query", "new": [Result-Dicts]}
  {"id", "type": "health", "watch", "status": "blind" | "recovered", "since": ISO, "errors": {quelle: meldung}}
Ein fehlschlagender Versand wird geloggt und bricht nie den Monitor-Lauf ab; der Monitor legt die Meldung
in seine Outbox und versucht es später erneut (siehe monitor.flush_outbox). send() gibt nur bei echter
Zustellung True zurück (2xx, bei Telegram zusätzlich "ok": true, bei E-Mail vom Server angenommen).

Geheimnisse: Webhook-URLs enthalten oft Tokens (Telegram-Bot, Slack, Discord, ?auth=...). Logs und
Fehlermeldungen zeigen deshalb nur Schema + Host; Pfad, Query und Zugangsdaten werden auch aus Server-
und Ausnahmetexten entfernt.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import stat
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx

from .core import InputError
from .i18n import t

log = logging.getLogger("websearch.notify")

FORMATS = ("json", "text", "slack", "discord", "teams", "telegram", "email")
TLS_MODES = ("starttls", "ssl", "none")

DISCORD_LIMIT = 2000       # Zeichen in "content"
TELEGRAM_LIMIT = 4096      # Zeichen nach dem Parsen der Entities - wir messen konservativ das rohe HTML
TEAMS_LIMIT = 26_000       # Bytes JSON (Teams-Grenze 28 KB, etwas Luft für Umschlag/Kodierung)
EMAIL_MAX_ITEMS = 100      # Treffer pro E-Mail
TITLE_MAX = 256            # einzelne Felder kürzen, damit Kopf + ein Treffer immer passen
QUERY_MAX = 200
WATCH_MAX = 100
LINE_MAX = 300


def webhook_format() -> str:
    return os.environ.get("WEBSEARCH_WEBHOOK_FORMAT", "json").strip().lower() or "json"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _webhook_url() -> str:
    return _env("WEBSEARCH_WEBHOOK")


def enabled() -> bool:
    """True, wenn ein Ziel konfiguriert ist: WEBSEARCH_WEBHOOK, bei Format email SMTP-Host + Empfänger."""
    if webhook_format() == "email":
        return bool(_env("WEBSEARCH_SMTP_HOST") and _env("WEBSEARCH_SMTP_TO"))
    return bool(_webhook_url())


# --------------------------------------------------------------------------- Geheimnisse schwärzen

def _target(url: str) -> str:
    """Nur Schema + Host (+ Port) einer URL - für Logs und Fehlermeldungen."""
    try:
        u = httpx.URL(url)
    except Exception:
        return t("<ungültige URL>", "<invalid URL>")
    if not u.host:
        return f"{u.scheme}:" if u.scheme else t("<ungültige URL>", "<invalid URL>")
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{u.host}{port}"


def _secrets(url: str) -> list[str]:
    """Teile der URL, die nie in Logs auftauchen dürfen (längste zuerst)."""
    found = {url}
    try:
        u = httpx.URL(url)
        found |= {str(u), u.path, u.raw_path.decode("ascii", "replace"), u.query.decode("ascii", "replace"),
                  u.userinfo.decode("ascii", "replace")}
        found |= {seg for seg in u.path.split("/")}
        found |= {v for _, v in u.params.multi_items()}
    except Exception:
        pass
    # kurze Stücke (z.B. "/x") nicht ersetzen - das würde beliebigen Text zerstückeln
    return sorted((s for s in found if len(s) >= 6 or s == url), key=len, reverse=True)


def _scrub(text: str, url: str | None = None) -> str:
    """Entfernt Pfad, Query und Zugangsdaten der Webhook-URL aus beliebigem Text."""
    url = _webhook_url() if url is None else url
    if not url:
        return text
    for s in _secrets(url):
        if s:
            text = text.replace(s, "***")
    return text


# --------------------------------------------------------------------------- Konfiguration

@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    tls: str
    user: str
    password: str
    sender: str
    to: tuple[str, ...]


def smtp_config() -> SmtpConfig:
    """Liest und prüft die SMTP-Einstellungen; InputError mit verständlicher Meldung bei Fehlern."""
    host = _env("WEBSEARCH_SMTP_HOST")
    to_raw = _env("WEBSEARCH_SMTP_TO")
    missing = [n for n, v in (("WEBSEARCH_SMTP_HOST", host), ("WEBSEARCH_SMTP_TO", to_raw)) if not v]
    if missing:
        names = ", ".join(missing)
        raise InputError(t(f"WEBSEARCH_WEBHOOK_FORMAT=email braucht {names}",
                           f"WEBSEARCH_WEBHOOK_FORMAT=email requires {names}"))
    tls = _env("WEBSEARCH_SMTP_TLS").lower() or "starttls"
    if tls not in TLS_MODES:
        allowed = ", ".join(TLS_MODES)
        raise InputError(t(f"WEBSEARCH_SMTP_TLS={tls!r} ist ungültig - erlaubt: {allowed}",
                           f"WEBSEARCH_SMTP_TLS={tls!r} is invalid - allowed: {allowed}"))
    port_raw = _env("WEBSEARCH_SMTP_PORT")
    port = 465 if tls == "ssl" else 587
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            raise InputError(t(f"WEBSEARCH_SMTP_PORT={port_raw!r} ist kein gültiger Port (1-65535)",
                               f"WEBSEARCH_SMTP_PORT={port_raw!r} is not a valid port (1-65535)"))
    user, password = _env("WEBSEARCH_SMTP_USER"), os.environ.get("WEBSEARCH_SMTP_PASSWORD", "")
    if bool(user) != bool(password):
        raise InputError(t("WEBSEARCH_SMTP_USER und WEBSEARCH_SMTP_PASSWORD nur zusammen setzen",
                           "set WEBSEARCH_SMTP_USER and WEBSEARCH_SMTP_PASSWORD together"))
    if tls == "none" and user and not _local_host(host):
        # Passwort im Klartext übers Netz: nur bewusst und nur zu einem lokalen Relay
        raise InputError(t("WEBSEARCH_SMTP_TLS=none mit Login würde das Passwort unverschlüsselt senden - starttls "
                           "oder ssl nutzen (none nur für ein lokales Relay wie localhost)",
                           "WEBSEARCH_SMTP_TLS=none with a login would send the password unencrypted - use "
                           "starttls or ssl (none only for a local relay such as localhost)"))
    sender = _env("WEBSEARCH_SMTP_FROM") or (user if "@" in user else "")
    if not sender:
        raise InputError(t("WEBSEARCH_SMTP_FROM fehlt (Absenderadresse)",
                           "WEBSEARCH_SMTP_FROM is missing (sender address)"))
    if "@" not in parseaddr(sender)[1]:
        raise InputError(t(f"WEBSEARCH_SMTP_FROM ist keine E-Mail-Adresse: {sender!r}",
                           f"WEBSEARCH_SMTP_FROM is not an email address: {sender!r}"))
    to = tuple(a.strip() for a in to_raw.split(",") if a.strip())
    bad = [a for a in to if "@" not in parseaddr(a)[1]]
    if bad or not to:
        shown = ", ".join(bad) or repr(to_raw)
        raise InputError(t(f"WEBSEARCH_SMTP_TO enthält ungültige Adressen: {shown}",
                           f"WEBSEARCH_SMTP_TO contains invalid addresses: {shown}"))
    return SmtpConfig(host, port, tls, user, password, sender, to)


_CHAT_ID = re.compile(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,}")


def _local_host(host: str) -> bool:
    import ipaddress
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check_config() -> None:
    """Beim Start aufrufen: ungültige Einstellungen sofort und verständlich melden."""
    fmt = webhook_format()
    if fmt not in FORMATS:
        allowed = ", ".join(FORMATS)
        raise InputError(t(f"WEBSEARCH_WEBHOOK_FORMAT={fmt!r} ist ungültig - erlaubt: {allowed}",
                           f"WEBSEARCH_WEBHOOK_FORMAT={fmt!r} is invalid - allowed: {allowed}"))
    url = _webhook_url()
    if fmt == "email":
        smtp_config()
        if url:
            log.warning(t("WEBSEARCH_WEBHOOK wird ignoriert: WEBSEARCH_WEBHOOK_FORMAT=email versendet per SMTP",
                          "WEBSEARCH_WEBHOOK is ignored: WEBSEARCH_WEBHOOK_FORMAT=email sends via SMTP"))
        return
    if _env("WEBSEARCH_SMTP_HOST"):
        log.warning(t("WEBSEARCH_SMTP_HOST ist gesetzt, wird aber nur mit WEBSEARCH_WEBHOOK_FORMAT=email genutzt",
                      "WEBSEARCH_SMTP_HOST is set but only used with WEBSEARCH_WEBHOOK_FORMAT=email"))
    if not url:
        if os.environ.get("WEBSEARCH_WEBHOOK_FORMAT", "").strip() and fmt != "json":
            # ausdrücklich ein Ziel-Format gewählt, aber kein Ziel: lieber beim Start abbrechen als still nichts senden
            raise InputError(t(f"WEBSEARCH_WEBHOOK_FORMAT={fmt}, aber WEBSEARCH_WEBHOOK ist leer - es würde nichts "
                               "verschickt. WEBSEARCH_WEBHOOK setzen oder WEBSEARCH_WEBHOOK_FORMAT leeren.",
                               f"WEBSEARCH_WEBHOOK_FORMAT={fmt}, but WEBSEARCH_WEBHOOK is empty - nothing would be "
                               "sent. Set WEBSEARCH_WEBHOOK or clear WEBSEARCH_WEBHOOK_FORMAT."))
        return
    try:
        u = httpx.URL(url)
    except Exception as e:
        err = _scrub(str(e), url)
        raise InputError(t(f"WEBSEARCH_WEBHOOK ist keine gültige URL ({err})",
                           f"WEBSEARCH_WEBHOOK is not a valid URL ({err})")) from None
    shown = _target(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise InputError(t(f"WEBSEARCH_WEBHOOK muss eine http(s)-URL sein (mit https://...): {shown!r}",
                           f"WEBSEARCH_WEBHOOK must be an http(s) URL (starting with https://...): {shown!r}"))
    if fmt == "telegram":
        if not u.path.endswith("/sendMessage"):
            raise InputError(t("WEBSEARCH_WEBHOOK für Telegram muss auf /bot<TOKEN>/sendMessage enden",
                               "WEBSEARCH_WEBHOOK for Telegram must end with /bot<TOKEN>/sendMessage"))
        chat = _env("WEBSEARCH_TELEGRAM_CHAT_ID")
        if not chat:
            raise InputError(t("WEBSEARCH_WEBHOOK_FORMAT=telegram braucht WEBSEARCH_TELEGRAM_CHAT_ID",
                               "WEBSEARCH_WEBHOOK_FORMAT=telegram requires WEBSEARCH_TELEGRAM_CHAT_ID"))
        if not _CHAT_ID.fullmatch(chat):
            raise InputError(t(f"WEBSEARCH_TELEGRAM_CHAT_ID={chat!r} ist ungültig - erwartet Zahl oder @kanalname",
                               f"WEBSEARCH_TELEGRAM_CHAT_ID={chat!r} is invalid - expected a number or @channel"))


# --------------------------------------------------------------------------- Formatierung

def title(payload: dict) -> str:
    watch = payload["watch"]
    if payload.get("type") == "health":
        state = "BLIND" if payload["status"] == "blind" else t("wieder ok", "recovered")
        return f"websearch: {watch} {state}"
    n = len(payload["new"])
    return t(f"{n} neu: {watch}", f"{n} new: {watch}")


def _health_parts(payload: dict) -> tuple[str, list[str]]:
    """(Einleitung, Zeilen) einer Health-Meldung - die Zeilen werden je Format als Liste gerendert."""
    watch, since = payload["watch"], payload.get("since", "?")
    if payload["status"] == "blind":
        lines = [f"{k}: {v}" for k, v in payload.get("errors", {}).items()] or [t("(keine Details)",
                                                                                  "(no details)")]
        return (t(f"Watch '{watch}' liefert seit {since} keine Ergebnisse, weil Quellen ausfallen:",
                  f"Watch '{watch}' has returned no results since {since} because sources are failing:"),
                lines)
    return (t(f"Watch '{watch}' liefert wieder Ergebnisse (blind seit {since}).",
              f"Watch '{watch}' returns results again (blind since {since})."), [])


def _health_text(payload: dict) -> str:
    intro, lines = _health_parts(payload)
    return "\n".join([intro, *(f"- {line}" for line in lines)])


def as_text(payload: dict) -> str:
    if payload.get("type") == "health":
        return _health_text(payload)
    return "\n".join(f"{r['title']}\n{r['url']}" for r in payload["new"])


def _slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def as_slack(payload: dict) -> dict:
    if payload.get("type") == "health":
        return {"text": ":warning: " + _slack_escape(_health_text(payload))
                if payload["status"] == "blind" else ":white_check_mark: " + _slack_escape(_health_text(payload))}
    lines = [f"*{_slack_escape(title(payload))}* ({_slack_escape(payload['query'])})"]
    for r in payload["new"]:
        url = r['url'].replace('|', '%7C').replace('<', '%3C').replace('>', '%3E')
        lines.append(f"• <{url}|{_slack_escape(r['title'] or r['url'])}>")
    return {"text": "\n".join(lines)}


# --- gemeinsame Bausteine der neuen Formate

T = TypeVar("T")


def _clip(s: Any, limit: int) -> str:
    s = " ".join(str(s or "").split())          # Zeilenumbrüche/Steuerzeichen aus Einzelfeldern entfernen
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _u16(s: str) -> int:
    """Länge in UTF-16-Einheiten (so zählen Telegram und Discord; Emoji zählen doppelt)."""
    return len(s.encode("utf-16-le")) // 2


def _more(n: int) -> str:
    return t(f"… +{n} weitere", f"… +{n} more")


def _fit(render: Callable[[int], T], n: int, fits: Callable[[T], bool]) -> T:
    """Größtes k <= n, dessen Darstellung render(k) (k Einträge + ggf. "… +N weitere") ins Limit passt."""
    full = render(n)
    if fits(full):
        return full
    best, lo, hi = render(0), 0, n - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        r = render(mid)
        if fits(r):
            best, lo = r, mid + 1
        else:
            hi = mid - 1
    return best


def _parts(payload: dict) -> tuple[str, str, list[Any]]:
    """(Überschrift, Unterzeile, Einträge): Treffer-Dicts bzw. Fehlerzeilen der Health-Meldung."""
    watch = _clip(payload.get("watch", ""), WATCH_MAX)
    p = dict(payload, watch=watch)
    if payload.get("type") == "health":
        intro, lines = _health_parts(p)
        return title(p), intro, [_clip(line, LINE_MAX) for line in lines]
    p.setdefault("new", [])
    return title(p), _clip(payload.get("query", ""), QUERY_MAX), list(p["new"])


def _is_blind(payload: dict) -> bool:
    return payload.get("type") == "health" and payload.get("status") == "blind"


def _url_in_parens(url: str) -> str:
    """URL für Markdown-Links [text](url): Klammern, spitze Klammern und Leerraum kodieren."""
    for a, b in (("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E"), (" ", "%20"), ("\n", "")):
        url = url.replace(a, b)
    return url


# --- Discord

_DISCORD_SPECIAL = re.compile(r"([\\*_~`|\[\]()<>#])")


def _discord_escape(s: str) -> str:
    return _DISCORD_SPECIAL.sub(r"\\\1", s)


def as_discord(payload: dict) -> dict:
    head, sub, items = _parts(payload)
    if payload.get("type") == "health":
        icon = ":warning:" if _is_blind(payload) else ":white_check_mark:"
        top = [f"{icon} **{_discord_escape(head)}**", _discord_escape(sub)]
        rows = [f"- {_discord_escape(line)}" for line in items]
    else:
        top = [f"**{_discord_escape(head)}**" + (f" ({_discord_escape(sub)})" if sub else "")]
        rows = [f"• [{_discord_escape(_clip(r.get('title') or r['url'], TITLE_MAX))}]({_url_in_parens(r['url'])})"
                for r in items]

    def render(k: int) -> str:
        return "\n".join(top + rows[:k] + ([_more(len(rows) - k)] if k < len(rows) else []))
    content = _fit(render, len(rows), lambda s: _u16(s) <= DISCORD_LIMIT)
    # allowed_mentions: keine Pings (@everyone, @here, Rollen, Nutzer); flags 4 = SUPPRESS_EMBEDS
    return {"content": content, "allowed_mentions": {"parse": []}, "flags": 4}


# --- Telegram

def _h(s: str) -> str:
    return html.escape(s, quote=False)


def as_telegram(payload: dict, chat_id: str) -> dict:
    head, sub, items = _parts(payload)
    if payload.get("type") == "health":
        icon = "⚠️" if _is_blind(payload) else "✅"
        top = [f"{icon} <b>{_h(head)}</b>", _h(sub)]
        rows = [f"• {_h(line)}" for line in items]
    else:
        top = [f"<b>{_h(head)}</b>" + (f" ({_h(sub)})" if sub else "")]
        rows = [f'• <a href="{html.escape(r["url"], quote=True)}">{_h(_clip(r.get("title") or r["url"], TITLE_MAX))}</a>'
                for r in items]

    def render(k: int) -> str:
        return "\n".join(top + rows[:k] + ([_h(_more(len(rows) - k))] if k < len(rows) else []))
    text = _fit(render, len(rows), lambda s: _u16(s) <= TELEGRAM_LIMIT)
    return {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}


# --- Microsoft Teams (Workflows-Webhook mit Adaptive Card)

_TEAMS_SPECIAL = re.compile(r"([\\*_~`\[\]])")


def _teams_escape(s: str) -> str:
    return _TEAMS_SPECIAL.sub(r"\\\1", s)


def as_teams(payload: dict) -> dict:
    head, sub, items = _parts(payload)
    color = "Attention" if _is_blind(payload) else ("Good" if payload.get("type") == "health" else "Default")
    top: list[dict] = [{"type": "TextBlock", "text": _teams_escape(head), "weight": "Bolder", "size": "Medium",
                        "wrap": True, "color": color}]
    if sub:
        top.append({"type": "TextBlock", "text": _teams_escape(sub), "isSubtle": True, "wrap": True,
                    "spacing": "None"})
    if payload.get("type") == "health":
        rows = [f"• {_teams_escape(line)}" for line in items]
    else:
        rows = [f"[{_teams_escape(_clip(r.get('title') or r['url'], TITLE_MAX))}]({_url_in_parens(r['url'])})"
                for r in items]

    def render(k: int) -> dict:
        body = top + [{"type": "TextBlock", "text": row, "wrap": True, "spacing": "Small"} for row in rows[:k]]
        if k < len(rows):
            body.append({"type": "TextBlock", "text": _more(len(rows) - k), "isSubtle": True, "wrap": True})
        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                "version": "1.4", "body": body, "msteams": {"width": "Full"}}
        return {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    return _fit(render, len(rows), lambda m: len(json.dumps(m).encode("utf-8")) <= TEAMS_LIMIT)


# --- E-Mail

def email_subject(payload: dict) -> str:
    watch = _clip(payload.get("watch", ""), WATCH_MAX)
    if payload.get("type") == "health":
        state = "BLIND" if _is_blind(payload) else t("wieder ok", "recovered")
        return f"[websearch] {watch}: {state}"
    n = len(payload.get("new", []))
    if n == 1:
        return t(f"[websearch] {watch}: {n} neuer Treffer", f"[websearch] {watch}: {n} new result")
    return t(f"[websearch] {watch}: {n} neue Treffer", f"[websearch] {watch}: {n} new results")


def _message_id(payload: dict, sender: str) -> str:
    """Message-ID aus der stabilen Payload-ID: erneute Zustellung (at-least-once) = gleiche ID ->
    viele Mailprogramme/Server zeigen die Nachricht nur einmal."""
    domain = re.sub(r"[^A-Za-z0-9.-]", "", parseaddr(sender)[1].rpartition("@")[2]) or "websearch.invalid"
    pid = re.sub(r"[^A-Za-z0-9._-]", "", str(payload.get("id") or ""))[:64]
    return f"<{pid}.websearch@{domain}>" if pid else make_msgid("websearch", domain=domain)


def as_email(payload: dict, cfg: SmtpConfig) -> EmailMessage:
    head, sub, items = _parts(payload)
    if payload.get("type") == "health":
        plain = "\n".join([sub, *(f"- {line}" for line in items)])
        body_html = (f"<p>{html.escape(sub)}</p>"
                     + ("<ul>" + "".join(f"<li>{html.escape(line)}</li>" for line in items) + "</ul>" if items else ""))
    else:
        shown, rest = items[:EMAIL_MAX_ITEMS], len(items) - EMAIL_MAX_ITEMS
        query_label = t("Suche", "Query")
        blocks = [f"{query_label}: {sub}", ""]
        lis = []
        for r in shown:
            name = _clip(r.get("title") or r["url"], TITLE_MAX)
            snippet = _clip(r.get("snippet", ""), LINE_MAX)
            blocks += [name, r["url"], *([snippet] if snippet else []), ""]
            lis.append(f'<li><a href="{html.escape(r["url"], quote=True)}">{html.escape(name)}</a>'
                       + (f"<br><small>{html.escape(snippet)}</small>" if snippet else "") + "</li>")
        if rest > 0:
            blocks.append(_more(rest))
            lis.append(f"<li>{html.escape(_more(rest))}</li>")
        plain = "\n".join(blocks).rstrip()
        body_html = f"<p>{html.escape(query_label)}: <b>{html.escape(sub)}</b></p><ul>{''.join(lis)}</ul>"
    msg = EmailMessage()
    msg["Subject"] = email_subject(payload)
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = _message_id(payload, cfg.sender)
    msg["Auto-Submitted"] = "auto-generated"           # keine Abwesenheitsnotizen zurück
    msg.set_content(f"{head}\n\n{plain}\n")
    msg.add_alternative(f"<html><body><h3>{html.escape(head)}</h3>{body_html}</body></html>", subtype="html")
    return msg


# --------------------------------------------------------------------------- Versand

SEND_TIMEOUT = 15.0        # gesamter Versand (httpx-timeout gilt nur pro Lesevorgang - "Tröpfel"-Server!)
SMTP_TIMEOUT = 10.0        # Socket-Timeout je SMTP-Schritt (zusätzlich zu SEND_TIMEOUT insgesamt)
BACKOFF_S = 60.0           # spätestens dann wieder versuchen (auch ohne neuen Durchlauf, z.B. bei API-Nutzung)
_down_until = 0.0          # Endpunkt gestört -> restliche Meldungen direkt in die Outbox


def reset_backoff() -> None:
    """Zu Beginn jedes Monitor-Durchlaufs: das Ziel wieder versuchen."""
    global _down_until
    _down_until = 0.0


async def send(client: httpx.AsyncClient, payload: dict) -> bool:
    """Schickt die Meldung an das konfigurierte Ziel. Gibt True bei Erfolg zurück, loggt Fehler.
    Ist das Ziel in diesem Durchlauf schon einmal gescheitert, wird nicht erneut gewartet: so hält ein
    hängender Endpunkt bei vielen Meldungen nicht den ganzen Durchlauf auf (alles landet in der Outbox)."""
    global _down_until
    if not enabled() or time.monotonic() < _down_until:
        return False
    is_mail = webhook_format() == "email"
    try:
        job = _send_mail(payload) if is_mail else _send(client, _webhook_url(), payload)
        ok = await asyncio.wait_for(job, SEND_TIMEOUT)
    except asyncio.TimeoutError:
        if is_mail:
            log.warning(t("E-Mail-Versand fehlgeschlagen: keine vollständige Antwort in %.0f s",
                          "email delivery failed: no complete response within %.0f s"), SEND_TIMEOUT)
        else:
            log.warning(t("Webhook fehlgeschlagen (%s): keine vollständige Antwort in %.0f s",
                          "Webhook failed (%s): no complete response within %.0f s"),
                        _target(_webhook_url()), SEND_TIMEOUT)
        ok = False
    _down_until = 0.0 if ok else time.monotonic() + BACKOFF_S
    return ok


async def _send(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    fmt = webhook_format()
    target = _target(url)
    try:
        if fmt == "text":
            # Titel als Query-Parameter statt Header: HTTP-Header dürfen kein UTF-8 (Umlaute!) enthalten.
            # copy_merge_params behält vorhandene Parameter wie ?auth=... (httpx' params= würde sie ersetzen).
            dest = httpx.URL(url).copy_merge_params({"title": title(payload)})
            resp = await client.post(dest, content=as_text(payload).encode("utf-8"), timeout=10,
                                     headers={"Content-Type": "text/plain; charset=utf-8"})
        elif fmt == "slack":
            resp = await client.post(url, json=as_slack(payload), timeout=10)
        elif fmt == "json":
            resp = await client.post(url, json=payload, timeout=10)
        elif fmt == "discord":
            resp = await client.post(url, json=as_discord(payload), timeout=10)
        elif fmt == "teams":
            resp = await client.post(url, json=as_teams(payload), timeout=10)
        elif fmt == "telegram":
            resp = await client.post(url, json=as_telegram(payload, _env("WEBSEARCH_TELEGRAM_CHAT_ID")), timeout=10)
        else:
            log.error(t("Webhook nicht gesendet: WEBSEARCH_WEBHOOK_FORMAT=%r ist ungültig",
                        "Webhook not sent: WEBSEARCH_WEBHOOK_FORMAT=%r is invalid"), fmt)
            return False
        if not 200 <= resp.status_code < 300:
            # auch 3xx: eine Weiterleitung (z.B. http->https, fehlender Slash) hat NICHT zugestellt
            loc = _scrub(resp.headers.get("location") or "", url)
            hint = t(f" -> {loc} (Webhook-URL anpassen)", f" -> {loc} (adjust the webhook URL)") if loc else ""
            log.warning(t("Webhook fehlgeschlagen (%s): HTTP %s%s: %s", "Webhook failed (%s): HTTP %s%s: %s"),
                        target, resp.status_code, hint, _scrub(resp.text[:200], url))
            return False
        if fmt == "telegram":
            # Telegram meldet manche Fehler mit HTTP 200 und {"ok": false, "description": ...}
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or data.get("ok") is not True:
                desc = data.get("description", "") if isinstance(data, dict) else resp.text[:200]
                log.warning(t("Webhook fehlgeschlagen (%s): Telegram meldet ok=false: %s",
                              "Webhook failed (%s): Telegram returned ok=false: %s"), target, _scrub(str(desc), url))
                return False
        return True
    except Exception as e:   # auch InvalidURL, ValueError ... - die Meldung steht ohnehin auf stdout
        log.warning(t("Webhook fehlgeschlagen (%s): %s: %s", "Webhook failed (%s): %s: %s"),
                    target, type(e).__name__, _scrub(str(e), url))
        return False


def _smtp_deliver(cfg: SmtpConfig, msg: EmailMessage) -> dict:
    """Blockierender SMTP-Versand (läuft in einem Thread). Gibt die abgelehnten Empfänger zurück."""
    ctx = ssl.create_default_context()
    smtp: smtplib.SMTP
    if cfg.tls == "ssl":
        smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT, context=ctx)
    else:
        smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT)
    with smtp:
        if cfg.tls == "starttls":
            smtp.starttls(context=ctx)      # nie stillschweigend unverschlüsselt weitermachen
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        return dict(smtp.send_message(msg, to_addrs=list(cfg.to)))


async def _send_mail(payload: dict) -> bool:
    try:
        cfg = smtp_config()
    except InputError as e:
        log.error(t("E-Mail nicht gesendet: %s", "email not sent: %s"), e)
        return False
    server = f"{cfg.host}:{cfg.port}"
    try:
        refused = await asyncio.to_thread(_smtp_deliver, cfg, as_email(payload, cfg))
    except Exception as e:   # SMTPException, OSError, ssl.SSLError ...
        msg = str(e).replace(cfg.password, "***") if len(cfg.password) >= 4 else str(e)
        log.warning(t("E-Mail-Versand fehlgeschlagen (%s): %s: %s", "email delivery failed (%s): %s: %s"),
                    server, type(e).__name__, msg)
        return False
    if refused:
        # teilweise zugestellt: NICHT erneut senden (sonst doppelt bei den anderen Empfängern)
        log.warning(t("E-Mail an %s abgelehnt (%s), an die übrigen zugestellt",
                      "email to %s refused (%s), delivered to the others"), ", ".join(refused), server)
    return True


def export(payload: dict) -> None:
    """Hängt die Meldung als JSON-Zeile an WEBSEARCH_MONITOR_OUT an (falls gesetzt).

    Fehler (OSError) werden geloggt UND weitergereicht: der Monitor markiert die Treffer dann nicht als
    gesehen, sie werden beim nächsten Lauf erneut gemeldet (Zustellgarantie)."""
    path = os.environ.get("WEBSEARCH_MONITOR_OUT", "").strip()
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # neu angelegt nur für den eigenen Nutzer lesbar (enthält gefundene Treffer und Fehlermeldungen)
        # O_NOFOLLOW: kein Schreiben über einen untergeschobenen Symlink; vorhandene Datei auf 0600 ziehen
        # O_NONBLOCK: eine FIFO ohne Leser blockiert sonst die Ereignisschleife (sie wird unten abgelehnt)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(t(f"{path} ist keine normale Datei (Symlinks, FIFOs, Geräte werden abgelehnt)",
                                f"{path} is not a regular file (symlinks, FIFOs, devices are refused)"))
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error(t("Export nach %s fehlgeschlagen: %s", "export to %s failed: %s"), path, e)
        raise
