"""Offline-Tests für 'websearch init' (wizard.py) und 'websearch doctor' (doctor.py)."""
import asyncio
import http.server
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from websearch import __main__ as cli
from websearch import doctor as D
from websearch import providers as P
from websearch import wizard as W
from websearch.core import Result
from websearch.engine import TierPolicy
from websearch.providers import Provider, ResultList

ROOT = Path(__file__).parent.parent
DOCKER = W.Compose("docker", (2, 30))


def env_of(path: Path) -> str:
    return (path / ".env").read_text(encoding="utf-8")


def backups(path: Path) -> list[Path]:
    return sorted(path.glob(".env.bak-*"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "compose_version", lambda: DOCKER)
    (tmp_path / ".env.example").write_text('SEARXNG_SECRET=\nWEBSEARCH_UA="websearch/0.2 (kontakt@eure-firma.de)"\n'
                                           "# WEBSEARCH_LANG=\nOTHER=1\n", encoding="utf-8")
    (tmp_path / "watches.example.json").write_text("[]", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


def secret_of(text: str) -> str:
    m = re.search(r"^SEARXNG_SECRET=(.*)$", text, re.M)
    assert m
    return m.group(1)


# =========================================================================== init: .env anlegen

def test_init_creates_env_and_watches(project, capsys):
    assert cli.main(["init", "--dir", str(project), "--contact", "ops@firma.example", "--lang", "en", "-y"]) == 0
    env = env_of(project)
    assert re.search(r"^SEARXNG_SECRET=[0-9a-f]{64}$", env, re.M)
    assert re.search(r'^WEBSEARCH_UA="websearch/[\w.]+ \(ops@firma.example\)"$', env, re.M)
    assert "\nWEBSEARCH_LANG=en\n" in env and "OTHER=1" in env
    assert oct((project / ".env").stat().st_mode & 0o777) == "0o600"
    assert (project / "watches" / "watches.json").read_text() == "[]"
    out = capsys.readouterr().out
    assert "docker compose up -d" in out and "docker compose exec websearch-mcp websearch doctor" in out
    assert "claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp" in out
    assert "watches/watches.json" in out and "demo searches" in out      # --lang en gilt für die Ausgabe
    assert out.index("watches/watches.json") < out.index("docker compose up -d")
    assert not list(project.glob(".env.tmp*")) and not list(project.glob("..env.tmp*"))


def test_init_never_overwrites_backups(project, capsys):
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    first = secret_of(env_of(project))
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0           # zweiter Lauf: nichts ändern
    assert secret_of(env_of(project)) == first and backups(project) == []
    assert "--force" in capsys.readouterr().out
    (project / ".env").write_text(env_of(project) + "BRAVE_API_KEY=key123\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    # --force: neue Vorlage, bisherige Werte (Secret, Token, API-Keys) bleiben
    assert secret_of(env_of(project)) == first and "BRAVE_API_KEY=key123" in env_of(project)
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0   # gleiche Sekunde möglich
    saved = backups(project)
    assert len(saved) == 2 and all(re.fullmatch(r"\.env\.bak-\d{8}-\d{6}(-\d+)?", p.name) for p in saved)
    assert all("BRAVE_API_KEY=key123" in p.read_text(encoding="utf-8") for p in saved)   # nie überschrieben
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in saved)


def test_init_updates_contact_and_lang_in_place(project, capsys):
    (project / ".env").write_text('SEARXNG_SECRET=geheim\nBRAVE_API_KEY=key123\nWEBSEARCH_UA="x (a@b.example)"\n'
                                  "WEBSEARCH_LANG=de\n", encoding="utf-8")
    (project / ".env").chmod(0o644)
    assert cli.main(["init", "--dir", str(project), "--contact", "https://firma.de/kontakt", "--lang", "en",
                     "-y"]) == 0
    env = env_of(project)
    assert "SEARXNG_SECRET=geheim\nBRAVE_API_KEY=key123\n" in env               # Rest bleibt unangetastet
    assert re.search(r'^WEBSEARCH_UA="websearch/[\w.]+ \(https://firma.de/kontakt\)"$', env, re.M)
    assert "\nWEBSEARCH_LANG=en\n" in env
    assert (project / ".env").stat().st_mode & 0o777 == 0o640                 # Rechte erhalten, ohne "andere"
    [saved] = backups(project)
    assert "BRAVE_API_KEY=key123" in saved.read_text(encoding="utf-8") and "WEBSEARCH_LANG=de" in saved.read_text()
    assert "updated" in capsys.readouterr().out
    # gleiche Werte noch einmal: keine Änderung, keine weitere Sicherung
    assert cli.main(["init", "--dir", str(project), "--contact", "https://firma.de/kontakt", "--lang", "en",
                     "-y"]) == 0
    assert len(backups(project)) == 1


@pytest.mark.parametrize("bad", ["kein kontakt", "a`id`@firma.de", "$(id)@firma.de", 'a"@firma.de',
                                 "ops@firma.de;rm", "https://firma.de/$HOME", "https://firma.de/a b", "ftp://x.de",
                                 "ops@firma", "a'b@firma.de", "https://firma.de/?a=1&b=2", "(ops@firma.de)"])
def test_contact_validation_rejects_shell_characters(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "compose_version", lambda: None)
    assert not W.valid_contact(bad)
    assert cli.main(["init", "--dir", str(tmp_path), "--contact", bad, "-y"]) == 2
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("good", ["ops@firma.de", "first.last+web@sub.firma.co.uk", "https://firma.de/kontakt",
                                  "http://firma.de:8080/a/b-c_d.html", "https://firma.de"])
def test_contact_validation_accepts_mail_and_url(good):
    assert W.valid_contact(good)


def test_init_without_example_and_without_docker(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(W, "compose_version", lambda: None)
    monkeypatch.setattr(W.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    assert cli.main(["init", "--dir", str(tmp_path), "--contact", "https://firma.example/kontakt", "-y"]) == 0
    env = env_of(tmp_path)
    assert "(https://firma.example/kontakt)" in env and re.search(r"^SEARXNG_SECRET=[0-9a-f]{64}$", env, re.M)
    out = capsys.readouterr().out
    assert "set -a" in out                                         # ohne Docker: Anleitung für lokal
    assert f"claude mcp add -s user websearch -- {sys.executable} -m websearch --env-file" in out   # stdio statt http
    assert "--transport http" not in out and "liest die .env selbst" in out
    assert "deploy/podman/install.sh" in out and "deploy/bwrap/websearch-bwrap" in out
    assert "deploy/bwrap/searxng-bwrap" in out and "deploy/systemd/" in out and "bubblewrap gefunden" in out
    assert cli.main(["init", "--dir", str(tmp_path / "fehlt"), "-y"]) == 2


def test_init_template_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "compose_version", lambda: None)
    (tmp_path / ".env.example").write_text("# english\nSEARXNG_SECRET=\nWEBSEARCH_LANG=en\n", encoding="utf-8")
    (tmp_path / ".env.example.de").write_text("# deutsch\nSEARXNG_SECRET=\nWEBSEARCH_LANG=de\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(tmp_path), "--lang", "de", "-y"]) == 0
    assert env_of(tmp_path).startswith("# deutsch")
    assert cli.main(["init", "--dir", str(tmp_path), "--lang", "en", "-y", "--force"]) == 0
    env = env_of(tmp_path)
    assert env.startswith("# english") and "\nWEBSEARCH_LANG=en\n" in env
    (tmp_path / ".env.example.en").write_text("# en-spezifisch\nSEARXNG_SECRET=\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(tmp_path), "--lang", "en", "-y", "--force"]) == 0
    assert env_of(tmp_path).startswith("# en-spezifisch")
    for name in (".env.example", ".env.example.de", ".env.example.en"):
        (tmp_path / name).unlink()
    assert cli.main(["init", "--dir", str(tmp_path), "--lang", "en", "-y", "--force"]) == 0
    assert env_of(tmp_path).startswith("SEARXNG_SECRET=")                     # eingebaute Minimal-Vorlage


def test_init_interactive_switches_language(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W, "compose_version", lambda: None)
    prompts, answers = [], iter(["en", "keine adresse", "ops@firma.de"])

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", fake_input)
    assert W.run_init(str(tmp_path), interactive=True) == 0
    assert prompts[0].startswith("Sprache") and prompts[1].startswith("Contact e-mail")
    out = capsys.readouterr().out
    assert "Please enter an e-mail address" in out and "created" in out
    assert "\nWEBSEARCH_LANG=en\n" in env_of(tmp_path) and "(ops@firma.de)" in env_of(tmp_path)
    assert os.environ["WEBSEARCH_LANG"] == "de"                              # nur für diesen Lauf


def test_init_reports_write_errors(project, monkeypatch, capsys):
    def boom(path, text):
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(W, "write_private", boom)
    assert cli.main(["init", "--dir", str(project), "-y"]) == 1
    assert "Rechte" in capsys.readouterr().err


def test_write_private_is_atomic_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    W.write_private(target, "A=1\n")
    assert target.read_text() == "A=1\n" and target.stat().st_mode & 0o777 == 0o600

    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(W.os, "replace", fail)
    with pytest.raises(OSError):
        W.write_private(target, "A=2\n")
    assert target.read_text() == "A=1\n" and [p.name for p in tmp_path.iterdir()] == [".env"]


def test_set_env_line():
    assert W.set_env_line("A=1\nB=2\n", "B", "3") == "A=1\nB=3\n"
    assert W.set_env_line("A=1\n# B=2\n", "B", "3") == "A=1\nB=3\n"
    assert W.set_env_line("A=1", "C", r"x\1") == "A=1\nC=x\\1\n"        # Ersatztext wird nicht interpretiert
    assert W.env_value('A="x y"\nB=z  # kommentar\nexport C=1\n', "B") == "z"
    assert W.env_value('A="x y"\n', "A") == "x y" and W.env_value("export C=1\n", "C") == "1"


# =========================================================================== init: Compose-Werkzeuge

@pytest.mark.parametrize("found, cmd", [(W.Compose("podman", (5, 2)), "podman compose"),
                                        (W.Compose("podman-compose", (1, 2)), "podman-compose")])
def test_init_next_steps_for_podman(project, monkeypatch, capsys, found, cmd):
    monkeypatch.setattr(W, "compose_version", lambda: found)
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    out = capsys.readouterr().out
    assert f"    {cmd} up -d" in out and f"{cmd} exec websearch-mcp websearch doctor" in out
    assert "docker compose" not in out and "--transport http" in out


def test_init_too_old_docker_compose(project, monkeypatch, capsys):
    monkeypatch.setattr(W, "compose_version", lambda: W.Compose("docker", (2, 20)))
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    out = capsys.readouterr().out
    assert "zu alt" in out and "2.24" in out and "deploy/podman/install.sh" in out and "set -a" in out


def test_compose_version_detection(monkeypatch):
    outputs: dict = {}
    monkeypatch.setattr(W, "_run", lambda *cmd: outputs.get(cmd))
    assert W.compose_version() is None
    outputs[("docker", "compose", "version", "--short")] = "2.29.1"
    assert W.compose_version() == W.Compose("docker", (2, 29))
    outputs[("docker", "compose", "version", "--short")] = "v2.20.0"          # zu alt: Podman bevorzugen
    outputs[("podman", "--version")] = "podman version 5.2.3"
    outputs[("podman", "compose", "version")] = "podman-compose version 1.2.0"
    assert W.compose_version() == W.Compose("podman", (5, 2))
    outputs[("podman", "--version")] = "podman version 4.9.4"                # podman compose erst ab 5.0
    assert W.compose_version() == W.Compose("docker", (2, 20))               # nichts brauchbar: erstes
    outputs[("podman-compose", "version")] = "podman-compose version 1.1.0\npodman version 4.9.4"
    assert W.compose_version() == W.Compose("podman-compose", (1, 1))
    del outputs[("podman", "compose", "version")]                           # Podman ohne Provider
    outputs[("podman", "--version")] = "podman version 5.0.0"
    assert W.compose_version() == W.Compose("podman-compose", (1, 1))
    assert not W.Compose("podman-compose", (1, 0)).usable and W.Compose("docker", (2, 24)).usable


# =========================================================================== doctor: Hilfen

class Fake(Provider):
    min_interval = 0
    kinds = ("web", "news")

    def __init__(self, name="a"):
        self.name = name
        super().__init__()

    async def search(self, client, query, n, lang, kind, days=None):
        return ResultList([Result("x", f"https://{self.name}.example/x", source=self.name, rank=1)])


@pytest.fixture
def doc_env(tmp_path, monkeypatch):
    """Doctor in leerem Verzeichnis, ohne Container, mit Fake-Quelle und eigener Zustandsdatei."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "0")
    monkeypatch.delenv("WEBSEARCH_SANDBOX", raising=False)
    for var in ("MCP_HOST", "MCP_AUTH_TOKEN", "MCP_ALLOWED_HOSTS", "MCP_ALLOW_NO_AUTH", "WEBSEARCH_WEBHOOK",
                "WEBSEARCH_WEBHOOK_FORMAT", "WEBSEARCH_SMTP_HOST", "WEBSEARCH_SMTP_TO", "SEARXNG_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WEBSEARCH_UA", "websearch/0.3 (ops@firma.de)")
    monkeypatch.setenv("WEBSEARCH_CACHE", str(tmp_path / "c.db"))
    import websearch.engine as E
    monkeypatch.setattr(E, "default_providers", lambda: {"a": Fake("a")})
    monkeypatch.setattr(E, "DEFAULT_POLICY", TierPolicy(primary={"web": ["a"], "news": ["a"]}))
    return tmp_path


def by_title(checks, part):
    return [c for c in checks if part in c.title]


def template_ua(name: str) -> str:
    m = re.search(r"^WEBSEARCH_UA=(.*)$", (ROOT / name).read_text(encoding="utf-8"), re.M)
    assert m
    return m.group(1).strip('"')


@pytest.mark.parametrize("ua", [template_ua(".env.example"), template_ua(".env.example.de"),
                                "websearch/0.3 (you@your-company.com)", "websearch/0.3 (du@eure-firma.de)",
                                "websearch/0.3 (you@example.org)", "websearch/0.3 (du@example.com)",
                                "websearch/0.3 (+https://example.invalid/contact)", "websearch/0.3",
                                "websearch/0.3 (ops@firma.test)", "websearch/0.3 (ops@intranet.localhost)",
                                "websearch/0.3 (https://www.example.net/kontakt)", "websearch/0.3 (ops@localhost)",
                                "websearch/0.3 (you@realfirma.de)", "websearch/0.3 (kontakt@eure-firma.example)", ""])
def test_placeholder_contacts_are_detected(ua):
    assert D.ua_problem(ua)


@pytest.mark.parametrize("ua", ["websearch/0.3 (ops@firma.de)", "websearch/0.3 (https://firma.de/kontakt)",
                                "websearch/0.3 (+https://wiki.firma.co.uk/bots; bots@firma.co.uk)",
                                "websearch/0.3 (examples@firma.de)"])
def test_real_contacts_pass(ua):
    assert D.ua_problem(ua) is None


def test_readme_examples_are_placeholders():
    for name in ("README.md", "README.de.md", "docs/QUICKSTART.md", "docs/USAGE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        contacts = re.findall(r"--contact (\S+)", text) + re.findall(r"websearch/[\w.]+ \(([^)\s]+)\)", text)
        for contact in contacts:
            assert D.ua_problem(f"websearch/0.3 ({contact})"), (name, contact)


def test_redact_url_and_scrub():
    assert D.redact_url("http://user:pw@searx.lan:8888/x?token=1") == "http://searx.lan:8888"
    assert D.redact_url("https://[::1]/") == "https://[::1]"
    assert D.redact_url("kaputt") == "<ungültige URL>"
    msg = D._scrub("Fehler bei http://user:pw@h.lan/ (user:pw)", "http://user:pw@h.lan/")
    assert "pw" not in msg and "user" not in msg and "http://h.lan" in msg


def test_summary_plurals(monkeypatch):
    one = [D.Check("fail", "a"), D.Check("warn", "b"), D.Check("ok", "c")]
    assert D.summary(one) == "3 Prüfungen: 1 Fehler, 1 Warnung"
    assert D.summary([D.Check("warn", "a"), D.Check("warn", "b")]) == "2 Prüfungen: 0 Fehler, 2 Warnungen"
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    assert D.summary(one[:1]) == "1 check: 1 error, 0 warnings"
    assert D.summary(one + one) == "6 checks: 2 errors, 2 warnings"


# =========================================================================== doctor: Prüfungen ohne Netz

def test_doctor_offline_reports_problems_with_fixes(doc_env, monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_UA", "websearch/0.2 (kontakt@eure-firma.de)")
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "quatsch")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    assert cli.main(["doctor", "--offline"]) == 1                 # ungültiges Webhook-Format = Fehler
    out = capsys.readouterr().out
    assert "✗" in out and "WEBSEARCH_WEBHOOK_FORMAT" in out
    assert "Kontakt im User-Agent fehlt" in out and "websearch init --contact" in out and "--force" not in out
    assert "startet nicht" in out and "MCP_AUTH_TOKEN" in out
    assert re.search(r"\d+ Prüfungen: 2 Fehler, 1 Warnung\n", out)


@pytest.mark.parametrize("env, level", [
    ({}, "warn"),                                    # nur lokal, aber ohne Token: andere lokale Nutzer
    ({"MCP_HOST": ""}, "warn"),
    ({"MCP_HOST": "localhost", "MCP_ALLOWED_HOSTS": "127.0.0.1:*,localhost:*,[::1]:*"}, "warn"),
    ({"MCP_AUTH_TOKEN": "x" * 40}, None),
    ({"MCP_AUTH_TOKEN": "tökén" * 8}, "fail"),      # Nicht-ASCII: Server startet nicht (echter Grund)
    ({"MCP_HOST": "0.0.0.0"}, "fail"),
    ({"MCP_ALLOWED_HOSTS": "127.0.0.1:*,search.firma.de,search.firma.de:*"}, "fail"),
    ({"MCP_HOST": "::"}, "fail"),
    ({"MCP_HOST": "0.0.0.0", "MCP_ALLOW_NO_AUTH": "1", "WEBSEARCH_IN_CONTAINER": "1"}, "warn"),
    ({"MCP_HOST": "0.0.0.0", "MCP_ALLOW_NO_AUTH": "1", "WEBSEARCH_IN_CONTAINER": "0"}, "fail"),  # nur im Container
    ({"MCP_HOST": "0.0.0.0", "MCP_AUTH_TOKEN": "x" * 32}, None),
    ({"MCP_HOST": "0.0.0.0", "MCP_AUTH_TOKEN": "kurz"}, "warn"),
    ({"MCP_AUTH_TOKEN": "kurz"}, "warn"),
])
def test_doctor_mcp_protection(doc_env, monkeypatch, env, level):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    checks = D.check_mcp()
    assert [c.level for c in checks] == ([level] if level else [])


def test_doctor_warns_about_no_auth_also_in_container(doc_env, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "1")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_ALLOW_NO_AUTH", "1")
    [c] = by_title(D.check_config(), "MCP")
    assert c.level == "warn" and "127.0.0.1" in c.detail


def test_doctor_env_not_loaded(doc_env, monkeypatch):
    (doc_env / ".env").write_text('SEARXNG_SECRET=abc\nWEBSEARCH_UA="websearch/0.3 (ops@firma.de)"\n'
                                  "SEARXNG_URL=http://127.0.0.1:8888\nLEER=\n", encoding="utf-8")
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")   # pip-Weg: nur SEARXNG_URL exportiert
    monkeypatch.delenv("SEARXNG_SECRET", raising=False)
    [c] = by_title(D.check_config(), ".env")
    assert c.level == "warn" and "SEARXNG_SECRET" in c.detail and "abc" not in c.detail and "set -a" in c.fix
    monkeypatch.setenv("SEARXNG_SECRET", "anders")
    [c] = by_title(D.check_config(), ".env")
    assert "anders als in .env: SEARXNG_SECRET" in c.detail
    monkeypatch.setenv("SEARXNG_SECRET", "abc")
    assert by_title(D.check_config(), ".env") == []
    monkeypatch.delenv("SEARXNG_SECRET")
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "1")             # Container liest .env selbst
    assert by_title(D.check_config(), ".env") == []


def test_doctor_notification_checks(doc_env, monkeypatch):
    def notify_checks():
        return [c for c in D.check_config() if "Benachrichtigung" in c.title]
    assert notify_checks() == []
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "telegram")       # Format ohne Ziel
    [c] = notify_checks()
    assert c.level in ("warn", "fail") and "WEBSEARCH_WEBHOOK" in c.detail + c.fix
    monkeypatch.setenv("WEBSEARCH_WEBHOOK", "https://api.telegram.org/botX/sendMessage")
    [c] = notify_checks()
    assert c.level == "fail" and "WEBSEARCH_TELEGRAM_CHAT_ID" in c.detail     # unvollständig
    monkeypatch.setenv("WEBSEARCH_TELEGRAM_CHAT_ID", "12345")
    [c] = notify_checks()
    assert c.level == "ok" and "telegram" in c.detail
    monkeypatch.setenv("WEBSEARCH_WEBHOOK_FORMAT", "email")
    monkeypatch.delenv("WEBSEARCH_WEBHOOK")
    [c] = notify_checks()
    assert c.level in ("warn", "fail") and "WEBSEARCH_SMTP" in c.detail + c.fix


def test_doctor_bad_language_is_a_failed_check(doc_env, monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_LANG", "klingon")
    assert asyncio.run(D.run_doctor(live=False)) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "WEBSEARCH_LANG" in out and "checks:" in out


def test_doctor_runtime_line(doc_env, monkeypatch):
    assert "kein Container" in by_title(D.check_config(), "Laufzeit")[0].detail
    monkeypatch.setenv("WEBSEARCH_SANDBOX", "bwrap")
    assert by_title(D.check_config(), "Laufzeit")[0].detail == "bubblewrap-Sandbox"
    monkeypatch.delenv("WEBSEARCH_SANDBOX")
    monkeypatch.setenv("WEBSEARCH_IN_CONTAINER", "1")
    assert D.runtime() in ("Docker", "Podman", "Container (WEBSEARCH_IN_CONTAINER=1)")


# =========================================================================== doctor: Netz

def test_doctor_json_live_with_fake_sources(doc_env, capsys):
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    titles = [c["title"] for c in data["checks"]]
    assert "Testsuche (web)" in titles and "Testsuche (news)" in titles
    assert all(c["level"] != "fail" for c in data["checks"])


def test_doctor_reports_unreachable_searxng(doc_env, monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    monkeypatch.setenv("SEARXNG_URL", "http://admin:geheim@127.0.0.1:9")    # Port 9 (discard): nichts lauscht
    import websearch.engine as E
    monkeypatch.setattr(E, "default_providers", lambda: {"searxng": P.SearXNG()})
    monkeypatch.setattr(E, "DEFAULT_POLICY", TierPolicy(primary={"web": ["searxng"]}))
    assert cli.main(["doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "SearXNG not reachable" in out and "docker compose up -d searxng" in out
    assert "geheim" not in out and "admin" not in out and "http://127.0.0.1:9" in out
    assert re.search(r"\d+ checks: 1 error, \d+ warnings?\n", out)


class _FakeSearx(http.server.BaseHTTPRequestHandler):
    paths: list = []

    def do_GET(self):
        type(self).paths.append(self.path.split("?")[0])
        body = {"/healthz": b"OK", "/stats/errors": b'{"google": []}',
                "/search": b'{"results": [{"url": "https://x"}], "unresponsive_engines": []}'}.get(
            self.path.split("?")[0], b"")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "application/json" if body.startswith(b"{") else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_searx(monkeypatch):
    _FakeSearx.paths = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeSearx)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SEARXNG_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    yield _FakeSearx.paths
    srv.shutdown()


def test_doctor_offline_does_not_search(fake_searx):
    checks = asyncio.run(D.check_searxng(live=False))
    assert [c.level for c in checks] == ["ok", "warn"] and "google" in checks[1].detail
    assert "/search" not in fake_searx and "/healthz" in fake_searx
    checks = asyncio.run(D.check_searxng(live=True))
    assert "/search" in fake_searx and by_title(checks, "Testsuche")[0].detail == "1 Treffer"


@pytest.fixture
def trickle(monkeypatch):
    """Server, der Kopfzeilen schickt und den Rumpf tröpfchenweise (nie fertig) - darf doctor nicht aufhalten."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(10)
    stop = threading.Event()

    def handle(c):
        try:
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                c.sendall(b" ")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            c.close()

    def serve():
        while not stop.is_set():
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setenv("SEARXNG_URL", f"http://127.0.0.1:{s.getsockname()[1]}")
    yield
    stop.set()
    s.close()


def test_doctor_does_not_hang_on_trickling_searxng(doc_env, trickle, monkeypatch, capsys):
    monkeypatch.setattr(D, "CHECK_TIMEOUT", 1.0)
    import websearch.engine as E
    monkeypatch.setattr(E, "default_providers", lambda: {"searxng": P.SearXNG()})
    monkeypatch.setattr(E, "DEFAULT_POLICY", TierPolicy(primary={"web": ["searxng"]}))
    t0 = time.monotonic()
    assert asyncio.run(D.run_doctor(live=False)) == 1
    assert time.monotonic() - t0 < 6
    out = capsys.readouterr().out
    assert "SearXNG nicht erreichbar" in out and "keine vollständige Antwort" in out


def test_doctor_overall_budget(doc_env, monkeypatch, capsys):
    async def slow(*a, **k):
        await asyncio.sleep(30)
    monkeypatch.setattr(D, "_test_search", lambda *a, **k: slow())
    t0 = time.monotonic()
    assert asyncio.run(D.run_doctor(live=True, total_timeout=1.0)) == 1
    assert time.monotonic() - t0 < 5
    out = capsys.readouterr().out
    assert "keine Antwort in 1s" in out and "übersprungen" in out


def test_probe_searxng_redacts_url(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://u:pw@127.0.0.1:9/")
    sx = asyncio.run(D.probe_searxng(timeout=2))
    assert sx["url"] == "http://127.0.0.1:9" and sx["reachable"] is False and "pw" not in json.dumps(sx)


# =========================================================================== init: Projektverzeichnis

def test_init_from_subdirectory_targets_compose_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W, "compose_version", lambda: DOCKER)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (repo / ".env.example").write_text("# vorlage\nSEARXNG_SECRET=\n", encoding="utf-8")
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["init", "--contact", "no@terra.de", "--lang", "en", "-y"]) == 0
    assert not (repo / "sub" / ".env").exists()
    assert env_of(repo).startswith("# vorlage")
    out = capsys.readouterr().out
    assert f"using project directory {repo.resolve()} (contains docker-compose.yml)" in out
    assert f"cd {repo.resolve()}" in out and "docker compose up -d" in out
    assert W.run_init(None, contact="no@terra.de") == 0                  # Python-API: None = automatisch
    assert not (repo / "sub" / ".env").exists()


def test_init_explicit_dir_below_compose_project_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W, "compose_version", lambda: DOCKER)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(repo / "sub"), "-y"]) == 2          # abbrechen statt falsche .env
    assert not (repo / "sub" / ".env").exists()
    err = capsys.readouterr().err
    assert "contains no compose file" not in err                            # Deutsch (Standard)
    assert "enthält keine Compose-Datei" in err and f"--dir {repo.resolve()}" in err and "--force" in err
    assert cli.main(["init", "--dir", str(repo / "sub"), "-y", "--force"]) == 0   # ausdrücklich gewünscht
    assert (repo / "sub" / ".env").exists()
    out = capsys.readouterr().out
    assert "Keine Compose-Datei" not in out                                 # es gibt ja eine darüber


def test_init_without_compose_says_so_and_skips_cd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W, "compose_version", lambda: DOCKER)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "-y"]) == 0
    out = capsys.readouterr().out
    assert (tmp_path / ".env").exists() and "Keine Compose-Datei" in out and str(tmp_path.resolve()) in out
    assert "cd " not in out and "set -a" in out


def test_doctor_reports_bad_paid_limits_and_missing_fetch_deps(monkeypatch):
    import builtins

    from websearch import doctor
    monkeypatch.setenv("WEBSEARCH_PAID_DAILY_LIMIT", "abc")
    (check,) = doctor._check_limits("fix")
    assert check.level == "fail" and "WEBSEARCH_PAID_DAILY_LIMIT" in check.title
    assert doctor._check_fetch_deps() == []
    real_import = builtins.__import__

    def no_clean(name, *a, **k):
        if name == "lxml_html_clean":
            raise ImportError("No module named 'lxml_html_clean'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_clean)
    (check,) = doctor._check_fetch_deps()
    assert check.level == "fail" and "lxml_html_clean" in check.fix


def test_restart_hint_depends_on_runtime(monkeypatch):
    from websearch import doctor
    monkeypatch.setattr(doctor.os.path, "exists", lambda p: p == "/run/.containerenv")
    assert "deploy/podman/install.sh" in doctor.restart_hint()
    monkeypatch.setattr(doctor.os.path, "exists", lambda p: False)
    assert "docker compose up -d" in doctor.restart_hint()


def test_init_force_keeps_values_byte_for_byte(project):
    """--force übernimmt die Original-Zeilen: Sonderzeichen ($, Anführungszeichen, &, Escapes) bleiben exakt."""
    from websearch.envfile import parse_env_file
    tricky = ("WEBSEARCH_SMTP_PASSWORD='p\\nq$x\"y'\n"
              "WEBSEARCH_WEBHOOK=\"https://x.logic.azure.com/w?a=1&sig=\\\"q\\\"\"\n"
              "REDDIT_CLIENT_SECRET='ab\"; touch PWNED; echo \"'\n"
              "WEBSEARCH_PROXY='http://u:pa$word@proxy:3128'\n")
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    (project / ".env").write_text(env_of(project) + tricky, encoding="utf-8")
    before = parse_env_file(project / ".env")
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    after = parse_env_file(project / ".env")
    for key in ("WEBSEARCH_SMTP_PASSWORD", "WEBSEARCH_WEBHOOK", "REDDIT_CLIENT_SECRET", "WEBSEARCH_PROXY",
                "SEARXNG_SECRET", "MCP_AUTH_TOKEN"):
        assert after[key] == before[key], key
    import subprocess
    out = subprocess.run(["bash", "-c", "set -a; . ./.env; set +a; printf '%s' \"$REDDIT_CLIENT_SECRET\""],
                         cwd=project, capture_output=True, text=True)
    assert not (project / "PWNED").exists() and out.stdout == before["REDDIT_CLIENT_SECRET"]


def test_init_force_keeps_references_to_other_variables(project, monkeypatch):
    shutil.copy(Path(__file__).parent.parent / ".env.example", project / ".env.example")   # echte Vorlage
    """Werte wie ${VAULT_X} oder Verweise auf andere Zeilen bleiben stehen, auch wenn sie gerade leer sind."""
    from websearch.envfile import parse_env_file
    monkeypatch.delenv("VAULT_REDDIT", raising=False)
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    text = env_of(project)
    text = re.sub(r"^MCP_AUTH_TOKEN=.*$", "MCP_AUTH_TOKEN=${MY_TOK}", text, flags=re.M)
    (project / ".env").write_text(f"MY_TOK={'a' * 40}\n{text}REDDIT_CLIENT_SECRET=${{VAULT_REDDIT}}\n", encoding="utf-8")
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    after = env_of(project)
    assert "MCP_AUTH_TOKEN=${MY_TOK}" in after and "REDDIT_CLIENT_SECRET=${VAULT_REDDIT}" in after
    assert parse_env_file(project / ".env")["MCP_AUTH_TOKEN"] == "a" * 40


@pytest.mark.parametrize("extra", [
    "WEBSEARCH_CONTACT=me@firma.de\nWEBSEARCH_UA=\"websearch/0.3.0 (${WEBSEARCH_CONTACT})\"\n",   # Helfer mit Präfix
    "MY_KEY=${SEARXNG_SECRET}x\n",                                                               # Verweis nach oben
])
def test_init_force_resolves_every_value_exactly_as_before(project, extra):
    from websearch.envfile import parse_env_file
    shutil.copy(Path(__file__).parent.parent / ".env.example", project / ".env.example")
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    (project / ".env").write_text(env_of(project) + extra, encoding="utf-8")
    before = parse_env_file(project / ".env")
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    after = parse_env_file(project / ".env")
    assert {k: after.get(k) for k in before} == before


def test_init_force_keeps_deliberate_no_token_setup(project):
    from websearch.envfile import parse_env_file
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    text = re.sub(r"^MCP_AUTH_TOKEN=.*\n", "", env_of(project), flags=re.M) + "MCP_ALLOW_NO_AUTH=1\n"
    (project / ".env").write_text(text, encoding="utf-8")
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    assert "MCP_AUTH_TOKEN" not in parse_env_file(project / ".env")       # kein Token aufgezwungen


def test_init_force_check_is_not_fooled_by_an_exported_env(project, monkeypatch):
    """Nach `set -a; . ./.env` steht jeder Wert auch in der Umgebung - der Vergleich darf das nicht nutzen."""
    from websearch.envfile import parse_env_text
    shutil.copy(Path(__file__).parent.parent / ".env.example", project / ".env.example")
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    text = env_of(project) + 'WEBSEARCH_CONTACT=me@firma.de\nWEBSEARCH_UA="websearch (${WEBSEARCH_CONTACT})"\n'
    (project / ".env").write_text(text, encoding="utf-8")
    monkeypatch.setenv("WEBSEARCH_CONTACT", "me@firma.de")              # wie nach set -a; . ./.env
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    clean = {k: v for k, v in os.environ.items() if k != "WEBSEARCH_CONTACT"}
    assert parse_env_text(env_of(project), clean)["WEBSEARCH_UA"] == "websearch (me@firma.de)"


def test_init_force_handles_a_byte_order_mark(project):
    from websearch.envfile import parse_env_file
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    (project / ".env").write_text("﻿BRAVE_API_KEY=sk-live-123\n" + env_of(project), encoding="utf-8")
    assert parse_env_file(project / ".env")["BRAVE_API_KEY"] == "sk-live-123"
    assert cli.main(["init", "--dir", str(project), "-y", "--force"]) == 0
    assert parse_env_file(project / ".env")["BRAVE_API_KEY"] == "sk-live-123"


def test_doctor_token_in_dotenv_but_not_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    (tmp_path / ".env").write_text("MCP_AUTH_TOKEN=" + "a" * 64 + "\n")
    (check,) = D.check_mcp()
    assert check.level == "warn" and "nicht geladen" in check.title and "sed -i" not in check.fix


def test_init_contact_replaces_every_active_line(project):
    from websearch.envfile import parse_env_file
    assert cli.main(["init", "--dir", str(project), "-y"]) == 0
    (project / ".env").write_text(env_of(project) + 'WEBSEARCH_UA="mine (old@corp.de)"\n', encoding="utf-8")
    assert cli.main(["init", "--dir", str(project), "--contact", "new@corp.de", "-y"]) == 0
    assert "new@corp.de" in parse_env_file(project / ".env")["WEBSEARCH_UA"]
