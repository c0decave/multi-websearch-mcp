"""Benachrichtigungskanäle (websearch.notify): Formate, Kürzung, Escaping, Fehlerfälle, Konfiguration."""
import ast
import asyncio
import json
import logging
import re
import smtplib
from email import message_from_bytes
from email.policy import default as email_policy
from pathlib import Path

import httpx
import pytest

from websearch import notify
from websearch.core import InputError

TOKEN = "123456789:AAH-geheimesBotToken_xyz"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

HITS = {"id": "abc123def456", "type": "hits", "watch": "marke", "query": "Grüne <Energie> & mehr",
        "new": [{"id": "h1", "title": "A *fett* <b> & [x]", "url": "https://a.example/p?(1)&x=\"2\"",
                 "snippet": "Kurzinfo"},
                {"id": "h2", "title": "", "url": "https://b.example/"}]}
BLIND = {"id": "h-1", "type": "health", "watch": "marke", "status": "blind", "since": "2026-01-01T00:00:00Z",
         "errors": {"searxng": "HTTP 429 <rate>", "ddg": "captcha"}}
RECOVERED = {"id": "h-2", "type": "health", "watch": "marke", "status": "recovered",
             "since": "2026-01-01T00:00:00Z", "errors": {}}


def many(n, title_len=60):
    return dict(HITS, new=[{"title": f"Treffer {i} " + "x" * title_len, "url": f"https://e.example/{i}"}
                           for i in range(n)])


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("WEBSEARCH_WEBHOOK", "WEBSEARCH_WEBHOOK_FORMAT", "WEBSEARCH_TELEGRAM_CHAT_ID",
                "WEBSEARCH_SMTP_HOST", "WEBSEARCH_SMTP_PORT", "WEBSEARCH_SMTP_USER", "WEBSEARCH_SMTP_PASSWORD",
                "WEBSEARCH_SMTP_FROM", "WEBSEARCH_SMTP_TO", "WEBSEARCH_SMTP_TLS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
    notify.reset_backoff()
    yield
    notify.reset_backoff()


def configure(monkeypatch, fmt, url="https://hook.example/x", **extra):
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", fmt)
    if url:
        monkeypatch.setenv("WEBSEARCH_WEBHOOK", url)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def deliver(payload, handler=lambda r: httpx.Response(200)):
    """notify.send über einen MockTransport; gibt (Ergebnis, Requests) zurück."""
    sent = []

    def h(req):
        sent.append(req)
        return handler(req)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await notify.send(c, payload)
    return asyncio.run(go()), sent


# --------------------------------------------------------------------------- Rückwärtskompatibilität

def test_json_text_slack_unchanged(monkeypatch):
    configure(monkeypatch, "json")
    ok, sent = deliver(HITS)
    assert ok and json.loads(sent[0].content) == HITS

    configure(monkeypatch, "text", "https://ntfy.example/topic?auth=geheim")
    ok, sent = deliver(HITS)
    assert ok and sent[0].url.params["title"] == "2 neu: marke" and sent[0].url.params["auth"] == "geheim"
    assert sent[0].content.decode().startswith("A *fett* <b> & [x]\nhttps://a.example/")

    configure(monkeypatch, "slack")
    ok, sent = deliver(HITS)
    body = json.loads(sent[0].content)
    assert set(body) == {"text"} and body["text"].startswith("*2 neu: marke* (Grüne &lt;Energie&gt; &amp; mehr)")


def test_health_text_german_is_unchanged():
    assert notify.as_text(BLIND) == ("Watch 'marke' liefert seit 2026-01-01T00:00:00Z keine Ergebnisse, weil "
                                     "Quellen ausfallen:\n- searxng: HTTP 429 <rate>\n- ddg: captcha")
    assert notify.as_text(RECOVERED) == "Watch 'marke' liefert wieder Ergebnisse (blind seit 2026-01-01T00:00:00Z)."
    assert notify.title(BLIND) == "websearch: marke BLIND" and notify.title(RECOVERED) == "websearch: marke wieder ok"


def test_english_texts(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    assert notify.title(HITS) == "2 new: marke" and notify.title(RECOVERED) == "websearch: marke recovered"
    assert "has returned no results since" in notify.as_text(BLIND)
    assert notify.email_subject(HITS) == "[websearch] marke: 2 new results"
    assert notify.email_subject(dict(HITS, new=HITS["new"][:1])) == "[websearch] marke: 1 new result"
    assert re.search(r"\n… \+\d+ more$", notify.as_discord(many(200))["content"])


def test_enabled_and_unknown_format_is_not_sent(monkeypatch):
    assert not notify.enabled()
    configure(monkeypatch, "discord")
    assert notify.enabled()
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "xml")
    assert deliver(HITS)[0] is False


# --------------------------------------------------------------------------- Discord

def test_discord_hits(monkeypatch):
    configure(monkeypatch, "discord", "https://discord.com/api/webhooks/1/geheimtoken")
    ok, sent = deliver(dict(HITS, watch="@everyone"))
    body = json.loads(sent[0].content)
    assert ok and body["allowed_mentions"] == {"parse": []} and body["flags"] == 4
    lines = body["content"].split("\n")
    assert lines[0] == r"**2 neu: @everyone** (Grüne \<Energie\> & mehr)"
    assert lines[1] == r"• [A \*fett\* \<b\> & \[x\]](https://a.example/p?%281%29&x=" + '"2")'
    assert lines[2] == "• [https://b.example/](https://b.example/)"     # leerer Titel -> URL als Text


def test_discord_health():
    blind = notify.as_discord(BLIND)["content"]
    assert blind.startswith(":warning: **websearch: marke BLIND**\nWatch 'marke' liefert seit")
    assert r"- searxng: HTTP 429 \<rate\>" in blind
    assert notify.as_discord(RECOVERED)["content"].startswith(":white_check_mark: **websearch: marke wieder ok**")


def test_discord_truncates_gracefully():
    content = notify.as_discord(many(200))["content"]
    assert len(content) <= 2000 and len(content) > 1800                  # Platz wird genutzt
    shown = content.count("\n• ")
    assert content.endswith(f"… +{200 - shown} weitere") and shown > 10
    # Emoji zählen bei Discord/Telegram doppelt (UTF-16)
    emoji = notify.as_discord(dict(HITS, new=[{"title": "😀" * 200, "url": f"https://e/{i}"} for i in range(30)]))
    assert notify._u16(emoji["content"]) <= 2000


# --------------------------------------------------------------------------- Teams

def test_teams_adaptive_card(monkeypatch):
    configure(monkeypatch, "teams", "https://prod-01.westeurope.logic.azure.com/workflows/abc/triggers/manual"
                                    "/paths/invoke?api-version=2016-06-01&sig=geheim")
    ok, sent = deliver(HITS, lambda r: httpx.Response(202))          # Workflows antworten 202 Accepted
    body = json.loads(sent[0].content)
    assert ok and body["type"] == "message" and len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert all(b["type"] == "TextBlock" for b in card["body"])
    texts = [b["text"] for b in card["body"]]
    assert texts[0] == "2 neu: marke" and texts[1] == "Grüne <Energie> & mehr"
    assert texts[2] == r"[A \*fett\* <b> & \[x\]](https://a.example/p?%281%29&x=" + '"2")'


def test_teams_health_and_truncation():
    card = notify.as_teams(BLIND)["attachments"][0]["content"]
    assert card["body"][0]["color"] == "Attention" and card["body"][2]["text"] == "• searxng: HTTP 429 <rate>"
    assert notify.as_teams(RECOVERED)["attachments"][0]["content"]["body"][0]["color"] == "Good"
    big = notify.as_teams(many(2000, title_len=200))
    assert len(json.dumps(big).encode()) <= notify.TEAMS_LIMIT
    assert big["attachments"][0]["content"]["body"][-1]["text"].startswith("… +")


# --------------------------------------------------------------------------- Telegram

def test_telegram_payload_and_escaping(monkeypatch):
    configure(monkeypatch, "telegram", TG_URL, WEBSEARCH_TELEGRAM_CHAT_ID="-100123")
    ok, sent = deliver(HITS, lambda r: httpx.Response(200, json={"ok": True, "result": {}}))
    body = json.loads(sent[0].content)
    assert ok and body["chat_id"] == "-100123" and body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    lines = body["text"].split("\n")
    assert lines[0] == "<b>2 neu: marke</b> (Grüne &lt;Energie&gt; &amp; mehr)"
    assert lines[1] == ('• <a href="https://a.example/p?(1)&amp;x=&quot;2&quot;">'
                        "A *fett* &lt;b&gt; &amp; [x]</a>")
    health = notify.as_telegram(BLIND, "1")["text"]
    assert health.startswith("⚠️ <b>websearch: marke BLIND</b>") and "HTTP 429 &lt;rate&gt;" in health


def test_telegram_truncates():
    text = notify.as_telegram(many(500), "1")["text"]
    assert notify._u16(text) <= 4096 and re.search(r"… \+\d+ weitere$", text)
    assert text.count("<a ") == text.count("</a>")                       # keine halben Tags


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}),
    httpx.Response(200, text="<html>Proxy</html>"),
    httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}),
])
def test_telegram_errors_are_failures(monkeypatch, caplog, response):
    configure(monkeypatch, "telegram", TG_URL, WEBSEARCH_TELEGRAM_CHAT_ID="42")
    with caplog.at_level(logging.WARNING, logger="websearch.notify"):
        ok, sent = deliver(HITS, lambda r: response)
    assert ok is False and len(sent) == 1
    assert "Webhook fehlgeschlagen (https://api.telegram.org)" in caplog.text


def test_token_never_logged(monkeypatch, caplog):
    configure(monkeypatch, "telegram", TG_URL, WEBSEARCH_TELEGRAM_CHAT_ID="42")

    def echo(req):                       # Server/Proxy wiederholt die URL in seiner Fehlerseite
        return httpx.Response(404, text=f"Cannot POST {req.url} ({req.url.path})")

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    def redirect(req):
        return httpx.Response(302, headers={"location": f"https://api.telegram.org/bot{TOKEN}/sendMessage/"})
    with caplog.at_level(logging.DEBUG, logger="websearch.notify"):
        for handler in (echo, boom, redirect):
            notify.reset_backoff()
            assert deliver(HITS, handler)[0] is False
    assert caplog.records and TOKEN not in caplog.text and "geheimes" not in caplog.text
    assert "https://api.telegram.org" in caplog.text


@pytest.mark.parametrize("fmt", ["json", "text", "slack", "discord", "teams"])
def test_webhook_secrets_redacted_for_all_formats(monkeypatch, caplog, fmt):
    configure(monkeypatch, fmt, "https://hooks.example/services/T000/B000/supersecret123?auth=geheimnis")
    with caplog.at_level(logging.WARNING, logger="websearch.notify"):
        deliver(HITS, lambda r: httpx.Response(500, text=f"bad {r.url}"))
    assert "supersecret123" not in caplog.text and "geheimnis" not in caplog.text
    assert "(https://hooks.example)" in caplog.text


# --------------------------------------------------------------------------- E-Mail

class FakeSMTP:
    instances: list = []
    fail: Exception | None = None
    refused: dict = {}

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.calls, self.sent = host, port, timeout, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg, to_addrs=None):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.sent.append((msg, to_addrs))
        return FakeSMTP.refused


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail, FakeSMTP.refused = [], None, {}
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    configure(monkeypatch, "email", url=None, WEBSEARCH_SMTP_HOST="mail.example", WEBSEARCH_SMTP_USER="bot@example.org",
              WEBSEARCH_SMTP_PASSWORD="pw-geheim", WEBSEARCH_SMTP_TO="a@example.org, B <b@example.org>")
    return FakeSMTP


def test_email_hits(smtp):
    assert notify.enabled()                                       # ohne WEBSEARCH_WEBHOOK
    notify.check_config()
    ok, sent = deliver(HITS)
    assert ok and sent == []                                      # kein HTTP
    s = smtp.instances[0]
    assert (s.host, s.port, s.timeout) == ("mail.example", 587, notify.SMTP_TIMEOUT)
    assert s.calls == ["starttls", ("login", "bot@example.org", "pw-geheim"), "quit"]
    msg, to = s.sent[0]
    assert to == ["a@example.org", "B <b@example.org>"]
    msg = message_from_bytes(msg.as_bytes(), policy=email_policy)
    assert msg["Subject"] == "[websearch] marke: 2 neue Treffer"
    assert msg["From"] == "bot@example.org" and msg["Message-ID"] == "<abc123def456.websearch@example.org>"
    assert msg["Auto-Submitted"] == "auto-generated"
    plain = msg.get_body(("plain",)).get_content()
    page = msg.get_body(("html",)).get_content()
    assert "Suche: Grüne <Energie> & mehr" in plain and "https://a.example/p?(1)&x=\"2\"" in plain
    assert "Kurzinfo" in plain
    assert "Grüne &lt;Energie&gt; &amp; mehr" in page and "A *fett* &lt;b&gt; &amp; [x]</a>" in page
    assert 'href="https://a.example/p?(1)&amp;x=&quot;2&quot;"' in page


def test_email_health_ssl_and_singular(smtp, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_SMTP_TLS", "ssl")
    assert deliver(BLIND)[0]
    s = smtp.instances[0]
    assert isinstance(s, FakeSMTPSSL) and s.port == 465 and "starttls" not in s.calls
    msg = s.sent[0][0]
    assert msg["Subject"] == "[websearch] marke: BLIND"
    assert "- searxng: HTTP 429 <rate>" in msg.get_body(("plain",)).get_content()
    assert notify.email_subject(dict(HITS, new=HITS["new"][:1])) == "[websearch] marke: 1 neuer Treffer"
    assert notify.email_subject(RECOVERED) == "[websearch] marke: wieder ok"


def test_email_without_tls_or_login(smtp, monkeypatch):
    for var in ("WEBSEARCH_SMTP_USER", "WEBSEARCH_SMTP_PASSWORD"):
        monkeypatch.delenv(var)
    monkeypatch.setenv("WEBSEARCH_SMTP_FROM", "websearch@example.net")
    monkeypatch.setenv("WEBSEARCH_SMTP_TLS", "none")
    monkeypatch.setenv("WEBSEARCH_SMTP_PORT", "25")
    assert deliver(many(150))[0]
    s = smtp.instances[0]
    assert s.port == 25 and s.calls == ["quit"]
    assert "… +50 weitere" in s.sent[0][0].get_body(("plain",)).get_content()


def test_email_failures(smtp, monkeypatch, caplog):
    smtp.fail = smtplib.SMTPAuthenticationError(535, b"auth failed")
    with caplog.at_level(logging.WARNING, logger="websearch.notify"):
        assert deliver(HITS)[0] is False
        assert deliver(HITS)[0] is False                         # Backoff: kein zweiter Verbindungsversuch
    assert len(smtp.instances) == 1
    assert "E-Mail-Versand fehlgeschlagen (mail.example:587): SMTPAuthenticationError" in caplog.text
    assert "pw-geheim" not in caplog.text
    notify.reset_backoff()
    smtp.fail, smtp.refused = None, {"b@example.org": (550, b"no such user")}
    assert deliver(HITS)[0] is True                               # teilweise zugestellt: nicht wiederholen


def test_email_timeout(smtp, monkeypatch):
    import time as _time
    monkeypatch.setattr(notify, "SEND_TIMEOUT", 0.2)
    monkeypatch.setattr(FakeSMTP, "send_message", lambda self, *a, **k: _time.sleep(1))
    assert deliver(HITS)[0] is False


def test_message_id_without_payload_id(smtp):
    cfg = notify.smtp_config()
    mid = notify.as_email(dict(HITS, id=None), cfg)["Message-ID"]
    assert mid.startswith("<") and mid.endswith("@example.org>")
    assert notify.as_email(dict(HITS, id="a b<c>"), cfg)["Message-ID"] == "<abc.websearch@example.org>"


# --------------------------------------------------------------------------- Konfiguration

@pytest.mark.parametrize("env,msg", [
    ({"WEBSEARCH_WEBHOOK_FORMAT": "xml"}, "WEBSEARCH_WEBHOOK_FORMAT='xml' ist ungültig"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "telegram", "WEBSEARCH_WEBHOOK": TG_URL}, "braucht WEBSEARCH_TELEGRAM_CHAT_ID"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "telegram", "WEBSEARCH_WEBHOOK": TG_URL, "WEBSEARCH_TELEGRAM_CHAT_ID": "abc"},
     "WEBSEARCH_TELEGRAM_CHAT_ID='abc' ist ungültig"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "telegram", "WEBSEARCH_TELEGRAM_CHAT_ID": "1",
      "WEBSEARCH_WEBHOOK": f"https://api.telegram.org/bot{TOKEN}/getMe"}, "muss auf /bot<TOKEN>/sendMessage enden"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "discord", "WEBSEARCH_WEBHOOK": "ftp://x/geheim"}, "muss eine http"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email"}, "braucht WEBSEARCH_SMTP_HOST, WEBSEARCH_SMTP_TO"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h"}, "braucht WEBSEARCH_SMTP_TO"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c",
      "WEBSEARCH_SMTP_FROM": "x@y.z", "WEBSEARCH_SMTP_TLS": "tls"}, "WEBSEARCH_SMTP_TLS='tls' ist ungültig"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c",
      "WEBSEARCH_SMTP_FROM": "x@y.z", "WEBSEARCH_SMTP_PORT": "99999"}, "kein gültiger Port"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c",
      "WEBSEARCH_SMTP_USER": "u"}, "nur zusammen"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c"},
     "WEBSEARCH_SMTP_FROM fehlt"),
    ({"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c, kaputt",
      "WEBSEARCH_SMTP_FROM": "x@y.z"}, "ungültige Adressen: kaputt"),
])
def test_check_config_errors(monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(InputError, match=re.escape(msg)):
        notify.check_config()


def test_check_config_errors_in_english_and_redacted(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "telegram")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", TG_URL)
    with pytest.raises(InputError, match="requires WEBSEARCH_TELEGRAM_CHAT_ID"):
        notify.check_config()
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", f"ftp://api.telegram.org/bot{TOKEN}/sendMessage")
    with pytest.raises(InputError) as e:
        notify.check_config()
    assert TOKEN not in str(e.value) and "must be an http(s) URL" in str(e.value)


@pytest.mark.parametrize("env", [
    {},
    {"WEBSEARCH_WEBHOOK_FORMAT": "teams", "WEBSEARCH_WEBHOOK": "https://x.logic.azure.com/w?sig=1"},
    {"WEBSEARCH_WEBHOOK_FORMAT": "telegram", "WEBSEARCH_WEBHOOK": TG_URL, "WEBSEARCH_TELEGRAM_CHAT_ID": "@kanal_1"},
    {"WEBSEARCH_WEBHOOK_FORMAT": "email", "WEBSEARCH_SMTP_HOST": "h", "WEBSEARCH_SMTP_TO": "a@b.c",
     "WEBSEARCH_SMTP_USER": "me@b.c", "WEBSEARCH_SMTP_PASSWORD": "pw", "WEBSEARCH_SMTP_TLS": "SSL"},
])
def test_check_config_valid(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    notify.check_config()


def test_check_config_warns_about_ignored_settings(monkeypatch, caplog):
    monkeypatch.setenv("WEBSEARCH_SMTP_HOST", "mail.example")
    with caplog.at_level(logging.WARNING, logger="websearch.notify"):
        notify.check_config()
    assert "nur mit WEBSEARCH_WEBHOOK_FORMAT=email" in caplog.text


# --------------------------------------------------------------------------- i18n-Vertrag

def _placeholders(node):
    if isinstance(node, ast.JoinedStr):
        return [ast.dump(v.value) for v in node.values if isinstance(v, ast.FormattedValue)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return sorted(re.findall(r"%[-#0 +]*\d*(?:\.\d+)?[sdrfx%]|\{[^{}]*\}", node.value))
    raise AssertionError(f"t() braucht String-Literale: {ast.dump(node)}")


def test_notify_uses_t_with_matching_placeholders():
    src = Path(notify.__file__).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t"]
    assert len(calls) > 30
    for c in calls:
        assert len(c.args) == 2, ast.unparse(c)
        assert _placeholders(c.args[0]) == _placeholders(c.args[1]), ast.unparse(c)
    # keine deutschen Meldungen mehr ohne t(): log.*("...") und InputError("...") nur über t()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], (ast.Constant, ast.JoinedStr)):
            name = ast.unparse(n.func)
            assert not (name.startswith("log.") or name == "InputError"), ast.unparse(n)


def test_format_without_target_fails_at_start(monkeypatch):
    from websearch import notify
    from websearch.core import InputError
    monkeypatch.delenv("WEBSEARCH_WEBHOOK", raising=False)
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "discord")
    with pytest.raises(InputError, match="WEBSEARCH_WEBHOOK"):
        notify.check_config()
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "json")      # Standard aus .env.example: kein Fehler
    notify.check_config()


def test_smtp_login_without_tls_only_to_local_relay(monkeypatch):
    from websearch import notify
    from websearch.core import InputError
    for k, v in (("WEBSEARCH_WEBHOOK_FORMAT", "email"), ("WEBSEARCH_SMTP_HOST", "smtp.example.org"),
                 ("WEBSEARCH_SMTP_TO", "a@b.example"), ("WEBSEARCH_SMTP_USER", "u@b.example"),
                 ("WEBSEARCH_SMTP_PASSWORD", "pw"), ("WEBSEARCH_SMTP_TLS", "none")):
        monkeypatch.setenv(k, v)
    with pytest.raises(InputError, match="starttls"):
        notify.check_config()
    monkeypatch.setenv("WEBSEARCH_SMTP_HOST", "localhost")
    notify.check_config()
