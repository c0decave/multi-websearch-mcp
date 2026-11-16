"""'websearch init': Einrichtung in einem Schritt.

Legt .env aus .env.example.<sprache> bzw. .env.example an (sonst aus einer eingebauten Minimal-Vorlage):
zufälliges SEARXNG_SECRET, Kontakt im User-Agent, Sprache. Bereitet watches/watches.json vor und prüft,
ob Docker Compose bzw. Podman Compose passt (sonst Hinweise auf Quadlet, bubblewrap, pip).

Eine vorhandene .env wird nie still überschrieben:
  - mit --contact/--lang (ohne --force) werden nur WEBSEARCH_UA/WEBSEARCH_LANG darin geändert,
  - mit --force wird sie aus der Vorlage neu erzeugt,
in beiden Fällen nach einer Sicherung als .env.bak-JJJJMMTT-HHMMSS (wird nie überschrieben).
Geschrieben wird atomar (temporäre Datei im selben Verzeichnis + os.replace) mit Rechten 0600.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from . import __version__
from .i18n import lang as current_lang
from .i18n import t

# Wird genutzt, wenn keine .env.example daneben liegt (z.B. Installation per pip)
_MINIMAL_ENV = """\
SEARXNG_SECRET=
WEBSEARCH_UA=
WEBSEARCH_LANG=
# SEARXNG_URL=http://127.0.0.1:8888
WEBSEARCH_WEBHOOK=
WEBSEARCH_WEBHOOK_FORMAT=json
"""

# Der Kontakt landet in .env, die per "set -a; . ./.env" von der Shell gelesen wird: nur harmlose
# Zeichen zulassen (keine Backticks, $, Anführungszeichen, Leerzeichen, Klammern ...)
_SAFE = r"[A-Za-z0-9._%+@:/-]"
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
_URL = rf"https?://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::\d{{1,5}})?(?:/{_SAFE}*)?"
# Variablen von websearch selbst (stehen in der Vorlage oder werden angehängt); alles andere sind eigene Hilfswerte
_OWN_KEY = re.compile(r"(WEBSEARCH|MCP|REDDIT|SEARXNG|DDGS|EXA|PERPLEXITY|BRAVE|SERPER|TAVILY|LINKUP|MARGINALIA)_")
_CONTACT = re.compile(rf"(?:{_EMAIL}|{_URL})")
# Mindestversionen: docker compose wegen env_file mit "required: false"; Podman ab 5.0 mit Provider
_MIN_COMPOSE = {"docker": (2, 24), "podman": (5, 0), "podman-compose": (1, 1)}

EXIT_OK, EXIT_FAILED = 0, 1


def valid_contact(value: str) -> bool:
    """E-Mail-Adresse oder http(s)-URL aus [A-Za-z0-9._%+@:/-] (landet im User-Agent und in .env)."""
    return bool(_CONTACT.fullmatch(value.strip()))


def set_env_line(text: str, key: str, value: str) -> str:
    """KEY=... setzen: ALLE aktiven Zeilen ersetzen (sonst gewänne eine spätere doppelte Zeile), sonst die erste
    auskommentierte, sonst anhängen."""
    line = f"{key}={value}"
    new, count = re.subn(rf"^(?:export\s+)?{re.escape(key)}=.*$", lambda _m: line, text, flags=re.M)
    if count:
        return new
    new, count = re.subn(rf"^#\s*{re.escape(key)}=.*$", lambda _m: line, text, count=1, flags=re.M)
    if count:
        return new
    return text.rstrip("\n") + f"\n{line}\n"


def env_value(text: str, key: str) -> str | None:
    """Wert von KEY in einem .env-Text (gleicher Parser wie überall: websearch.envfile) oder None."""
    from .envfile import parse_env_text
    return parse_env_text(text).get(key)


def ua_for(contact: str) -> str:
    return f'"websearch/{__version__} ({contact.strip()})"'


class Compose(NamedTuple):
    """Gefundenes Compose-Werkzeug: tool = "docker" | "podman" | "podman-compose"."""
    tool: str
    version: tuple[int, int]

    @property
    def cmd(self) -> str:
        return "podman-compose" if self.tool == "podman-compose" else f"{self.tool} compose"

    @property
    def minimum(self) -> tuple[int, int]:
        return _MIN_COMPOSE[self.tool]

    @property
    def usable(self) -> bool:
        return self.version >= self.minimum


def _run(*cmd: str) -> str | None:
    """Ausgabe eines Befehls (stdout+stderr) oder None, wenn er fehlt oder scheitert."""
    if not shutil.which(cmd[0]):
        return None
    try:
        res = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (res.stdout + res.stderr).strip() if res.returncode == 0 else None


def _version(text: str | None, prefix: str = "") -> tuple[int, int] | None:
    m = re.search(rf"{prefix}v?(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def compose_version() -> Compose | None:
    """Docker Compose (>= 2.24), podman compose (Podman >= 5.0 mit Provider) oder podman-compose (>= 1.1).
    Erstes brauchbares Werkzeug in dieser Reihenfolge; sonst das erste gefundene (zu alte); sonst None."""
    found: list[Compose] = []
    if (v := _version(_run("docker", "compose", "version", "--short"))) is not None:
        found.append(Compose("docker", v))
    # podman compose reicht an einen Provider (docker-compose/podman-compose) weiter - nur wenn es den gibt
    podman = _version(_run("podman", "--version"), r"version\s+")
    if podman is not None and _run("podman", "compose", "version") is not None:
        found.append(Compose("podman", podman))
    if (v := _version(_run("podman-compose", "version"), r"podman-compose version\s+")) is not None:
        found.append(Compose("podman-compose", v))
    return next((c for c in found if c.usable), found[0] if found else None)


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    except EOFError:
        return default
    return answer or default


def _create_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Neue Datei exklusiv anlegen (scheitert, falls es sie gibt) - nie mit lesbaren Zwischenrechten."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.fchmod(fd, mode)                       # umask darf nichts ändern, Modus gilt genau
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    finally:
        if fd != -1:
            os.close(fd)


def write_private(path: Path, text: str) -> None:
    """Atomar schreiben: temporäre Datei (0600) im selben Verzeichnis, dann os.replace. Rechte einer
    vorhandenen Datei bleiben erhalten, aber nie für "andere" lesbar (enthält Secret und API-Keys)."""
    mode = 0o600
    if path.exists():
        mode = (path.stat().st_mode & 0o777 & ~0o007) | 0o600
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        _create_private(tmp, text.encode("utf-8"), mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def backup(path: Path) -> Path:
    """Sicherung als .env.bak-JJJJMMTT-HHMMSS (0600); gibt es den Namen schon, mit -1, -2 ... dahinter."""
    data = path.read_bytes()
    base = f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    for i in range(1000):
        target = path.with_name(base if i == 0 else f"{base}-{i}")
        try:
            _create_private(target, data)
            return target
        except FileExistsError:
            continue
    raise FileExistsError(target)


# Dateinamen, nach denen docker/podman compose suchen
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def compose_file_in(directory: Path) -> Path | None:
    return next((directory / n for n in COMPOSE_FILES if (directory / n).is_file()), None)


def find_project(start: Path) -> Path | None:
    """Nächstes Verzeichnis ab `start` aufwärts mit einer Compose-Datei - so sucht auch compose selbst."""
    start = start.resolve()
    return next((d for d in (start, *start.parents) if compose_file_in(d)), None)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _project_dir(directory: str | None, force: bool = False) -> Path:
    """Zielverzeichnis. Ohne --dir (oder "."): das nächste Verzeichnis mit Compose-Datei ab dem aktuellen
    aufwärts - sonst landet .env in einem Unterordner, und 'docker compose up' liest die (fehlende) .env
    im übergeordneten Projekt. Mit --dir: warnen, wenn compose dort eine übergeordnete Datei nähme."""
    from .core import InputError
    cwd = Path.cwd()
    if directory is None or directory.strip() in ("", "."):
        found = find_project(cwd)
        if found is None:
            return Path(".")
        if not _same(found, cwd):
            name = (compose_file_in(found) or found).name
            print(t(f"• Projektverzeichnis {found} (enthält {name})",
                    f"• using project directory {found} (contains {name})"))
        return found
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise InputError(t(f"Verzeichnis {root} gibt es nicht", f"directory {root} does not exist"))
    if compose_file_in(root) is None and (parent := find_project(root.resolve().parent)) is not None:
        msg = t(f"{root} enthält keine Compose-Datei, aber {parent} schon: 'docker compose' würde von hier aus "
                f"{parent} nutzen und dessen .env lesen, nicht diese. Gemeint war wohl: websearch init --dir "
                f"{shlex.quote(str(parent))}",
                f"{root} contains no compose file, but {parent} does: 'docker compose' run from here would use "
                f"{parent} and read its .env, not this one. You probably meant: websearch init --dir "
                f"{shlex.quote(str(parent))}")
        if not force:                # z.B. --dir searxng: dort würde .env sogar in SearXNG eingebunden
            raise InputError(msg + t(" (trotzdem hier anlegen: --force)", " (create it here anyway: --force)"))
        print("! " + msg)
    return root


def _pick_template(root: Path, lang: str) -> str:
    """Vorlage .env.example.<lang>, sonst .env.example, sonst die eingebaute Minimal-Vorlage."""
    for template in (root / f".env.example.{lang}", root / ".env.example"):
        if template.is_file():
            return template.read_text(encoding="utf-8")
    return _MINIMAL_ENV


def run_init(directory: str | None = None, contact: str | None = None, lang: str | None = None, force: bool = False,
             interactive: bool = False) -> int:
    """Exit-Code: 0 = fertig, 1 = Datei nicht schreibbar; Eingabefehler -> InputError (Exit 2)."""
    previous_lang = os.environ.get("WEBSEARCH_LANG")
    try:
        return _run_init(directory, contact, lang, force, interactive)
    finally:
        # Sprachwechsel gilt nur für diesen Lauf
        if previous_lang is None:
            os.environ.pop("WEBSEARCH_LANG", None)
        else:
            os.environ["WEBSEARCH_LANG"] = previous_lang


def _run_init(directory: str | None, contact: str | None, lang: str | None, force: bool, interactive: bool) -> int:
    from .core import InputError
    if lang is not None:                            # --lang gilt sofort für alle Meldungen
        os.environ["WEBSEARCH_LANG"] = "de" if lang.strip().lower().startswith("de") else "en"
    root = _project_dir(directory, force)
    env_file = root / ".env"
    if contact is not None and not valid_contact(contact):
        raise InputError(t(f"--contact muss eine E-Mail-Adresse oder http(s)-URL aus Buchstaben, Ziffern und "
                           f"._%+@:/- sein (ist: {contact!r})",
                           f"--contact must be an e-mail address or http(s) URL made of letters, digits and "
                           f"._%+@:/- (got: {contact!r})"))
    # utf-8-sig: eine .env aus dem Windows-Editor (mit BOM) verliert sonst ihre erste Zeile
    old_text = env_file.read_text(encoding="utf-8-sig") if env_file.is_file() else None
    if lang is None and "WEBSEARCH_LANG" not in os.environ and old_text is not None:
        if saved_lang := env_value(old_text, "WEBSEARCH_LANG"):   # Meldungen gleich in der Sprache der .env
            os.environ["WEBSEARCH_LANG"] = "de" if saved_lang.lower().startswith("de") else "en"

    # --- Fragen (nur interaktiv und nur, was nicht als Option kam)
    while lang is None and interactive:
        default = (env_value(old_text, "WEBSEARCH_LANG") if old_text is not None else None) or current_lang()
        answer = _ask(t("Sprache der Meldungen (de/en)", "Language of messages (de/en)"), default).strip().lower()
        if answer in ("de", "deutsch", "german", "en", "english", "englisch"):
            lang = answer
        else:
            print(t("  Bitte 'de' oder 'en' eingeben.", "  Please enter 'de' or 'en'."))
    if lang is not None:
        lang = "de" if lang.strip().lower().startswith("de") else "en"
        os.environ["WEBSEARCH_LANG"] = lang         # alle weiteren Meldungen in der gewählten Sprache
    while contact is None and interactive:
        answer = _ask(t("Kontakt-E-Mail oder -URL für den User-Agent (Wikipedia/GDELT verlangen das; "
                        "leer = später)",
                        "Contact e-mail or URL for the User-Agent (required by Wikipedia/GDELT; "
                        "empty = later)"))
        if not answer:
            break
        if valid_contact(answer):
            contact = answer
            break
        print(t("  Bitte eine E-Mail-Adresse oder http(s)-URL angeben (nur Buchstaben, Ziffern und ._%+@:/-).",
                "  Please enter an e-mail address or http(s) URL (only letters, digits and ._%+@:/-)."))

    if contact is not None:
        from .doctor import ua_problem
        if problem := ua_problem(ua_for(contact)):
            print(t(f"! {contact}: {problem} - bitte eine echte, erreichbare Adresse angeben",
                    f"! {contact}: {problem} - please give a real, reachable address"))
    try:
        _setup_env(env_file, root, old_text, contact, lang, force)
        watches_hint = _setup_watches(root)
    except OSError as e:
        print(t(f"✗ Konnte nicht schreiben: {e} - Rechte des Verzeichnisses {root} prüfen",
                f"✗ could not write: {e} - check the permissions of directory {root}"), file=sys.stderr)
        return EXIT_FAILED
    _next_steps(root, watches_hint)
    sys.stdout.flush()
    return EXIT_OK


def _setup_env(env_file: Path, root: Path, old_text: str | None, contact: str | None, lang: str | None,
               force: bool) -> None:
    if old_text is not None and not force:
        if contact is None and lang is None:
            print(t(f"• {env_file} gibt es schon - unverändert (Kontakt/Sprache ändern: websearch init "
                    "--contact ... --lang ...; aus der Vorlage neu erzeugen: --force)",
                    f"• {env_file} already exists - left unchanged (change contact/language: websearch init "
                    "--contact ... --lang ...; regenerate from the template: --force)"))
            return
        # Nur die angegebenen Zeilen ändern, alles andere (API-Keys, Tokens) bleibt
        text = old_text
        if contact is not None:
            text = set_env_line(text, "WEBSEARCH_UA", ua_for(contact))
        if lang is not None:
            text = set_env_line(text, "WEBSEARCH_LANG", lang)
        if text == old_text:
            print(t(f"• {env_file} ist schon aktuell - unverändert", f"• {env_file} is up to date - left unchanged"))
            return
        saved = backup(env_file)
        write_private(env_file, text)
        changed = ", ".join(k for k in ("WEBSEARCH_UA", "WEBSEARCH_LANG")
                            if env_value(text, k) != env_value(old_text, k))
        print(t(f"✓ {env_file} aktualisiert ({changed}); Sicherung: {saved.name}",
                f"✓ updated {env_file} ({changed}); backup: {saved.name}"))
        return

    # --force: neue Vorlage, aber alle bisherigen Werte (Kontakt, Sprache, API-Keys, Tokens) bleiben erhalten.
    # Werte werden wörtlich (Original-Schreibweise: Anführungszeichen, Escapes, $) übernommen - neu quotieren
    # könnte Sonderzeichen verändern. Danach wird geprüft, dass jeder Wert genauso aufgelöst wird wie vorher.
    from .envfile import _parse, parse_env_text
    old_values = parse_env_text(old_text) if old_text is not None else {}
    raw: dict[str, str] = {}               # KEY -> Rohtext nach "=" (nur Zeilen mit nicht leerem Rohwert)
    old_lines: list[str] = []
    for line in (old_text or "").split("\n"):
        parsed = _parse(line)
        if parsed is not None and parsed[1]:
            body = line.strip()
            body = body[7:].lstrip() if body.startswith("export ") else body
            raw[parsed[0]] = body.partition("=")[2].strip()
            old_lines.append(f"{parsed[0]}={raw[parsed[0]]}")
    lang = lang or old_values.get("WEBSEARCH_LANG") or current_lang()
    template = _pick_template(root, lang)
    no_auth = (old_values.get("MCP_ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes", "on", "ja"))

    def finish(text: str) -> str:
        if "SEARXNG_SECRET" not in raw:
            text = set_env_line(text, "SEARXNG_SECRET", secrets.token_hex(32))
        if "MCP_AUTH_TOKEN" not in raw and not no_auth:
            # Auch auf 127.0.0.1 gilt: jeder lokale Nutzer erreicht den Port - ohne Token könnte er Suchen (und
            # Bezahl-APIs) über diesen Dienst nutzen. Bewusst ohne Token (MCP_ALLOW_NO_AUTH=1): so lassen.
            text = set_env_line(text, "MCP_AUTH_TOKEN", secrets.token_hex(32))
        text = set_env_line(text, "WEBSEARCH_LANG", lang)
        if contact:
            text = set_env_line(text, "WEBSEARCH_UA", ua_for(contact))
        return text

    # Vergleich OHNE die Variablen der alten Datei in der Umgebung: wer `set -a; . ./.env` gemacht hat, hätte
    # sonst jeden Verweis "aufgelöst", auch wenn er in der neuen Datei vor seiner Definition steht
    clean_env = {k: v for k, v in os.environ.items() if k not in old_values and k not in raw}
    old_values = parse_env_text(old_text, clean_env) if old_text is not None else {}

    def same(text: str) -> bool:
        """Lösen alle bisherigen Variablen noch genauso auf (außer den bewusst geänderten)?"""
        new_values = parse_env_text(text, clean_env)
        changed = {"WEBSEARCH_LANG"} | ({"WEBSEARCH_UA"} if contact else set())
        return all(new_values.get(k) == v for k, v in old_values.items() if k not in changed)

    # 1. Versuch: Werte an ihre Stelle in der Vorlage, Unbekanntes nach oben (Verweise gelten nur nach oben)
    extra = [k for k in raw if not re.search(rf"^#?\s*{re.escape(k)}=", template, re.M) and not _OWN_KEY.match(k)]
    text = template
    if extra:
        text = (t("# --- aus der bisherigen .env übernommen ---\n", "# --- kept from the previous .env ---\n")
                + "".join(f"{k}={raw[k]}\n" for k in extra) + "\n" + text)
    for key, value in raw.items():
        if key not in extra:
            text = set_env_line(text, key, value)
    text = finish(text)
    if not same(text):
        # 2. Versuch: alle bisherigen Zeilen in ihrer Reihenfolge oben, die Vorlage nur noch als Kommentar-Referenz
        body = template
        for key in raw:
            body = re.sub(rf"^{re.escape(key)}=", f"# {key}=", body, flags=re.M)     # KEY: nur [A-Za-z0-9_]
        text = finish(t("# --- aus der bisherigen .env übernommen (Reihenfolge wegen ${...}-Verweisen) ---\n",
                        "# --- kept from the previous .env (order kept because of ${...} references) ---\n")
                      + "\n".join(old_lines) + "\n\n" + body)
        if not same(text):
            from .core import InputError
            raise InputError(t("--force würde Werte mit ${...}-Verweisen verändern - .env bitte von Hand an die neue "
                               "Vorlage anpassen (nichts geschrieben)",
                               "--force would change values with ${...} references - please adapt .env to the new "
                               "template by hand (nothing written)"))
    keep = {k: old_values.get(k, raw[k]) for k in raw}
    if old_text is not None:
        saved = backup(env_file)
        print(t(f"• alte .env gesichert als {saved}", f"• old .env backed up as {saved}"))
    write_private(env_file, text)
    if keep:
        print(t(f"✓ {env_file} aus der aktuellen Vorlage neu erzeugt; bisherige Werte übernommen ({len(keep)})",
                f"✓ regenerated {env_file} from the current template; previous values kept ({len(keep)})"))
    else:
        print(t(f"✓ {env_file} angelegt (zufälliges SEARXNG_SECRET und MCP_AUTH_TOKEN, WEBSEARCH_LANG={lang})",
                f"✓ created {env_file} (random SEARXNG_SECRET and MCP_AUTH_TOKEN, WEBSEARCH_LANG={lang})"))
    if not contact and not keep.get("WEBSEARCH_UA"):
        print(t("! Kein Kontakt angegeben: nachholen mit  websearch init --contact du@eure-firma.de",
                "! No contact given: add it with  websearch init --contact you@your-company.com"))


def _setup_watches(root: Path) -> bool:
    """watches/watches.json aus dem Beispiel anlegen. True, wenn sie noch die Beispiel-Suchen enthält."""
    watches, example = root / "watches" / "watches.json", root / "watches.example.json"
    if watches.exists():
        print(t(f"• {watches} gibt es schon - unverändert", f"• {watches} already exists - left unchanged"))
    elif example.exists():
        watches.parent.mkdir(exist_ok=True)
        shutil.copy2(example, watches)
        # Container lesen als eigene UID (Monitor 10001): auch bei umask 077 lesbar machen
        watches.parent.chmod(0o755)
        watches.chmod(0o644)
        print(t(f"✓ {watches} aus watches.example.json angelegt",
                f"✓ created {watches} from watches.example.json"))
    try:
        return example.is_file() and watches.is_file() and watches.read_bytes() == example.read_bytes()
    except OSError:
        return False


def _http_add(root: Path) -> str:
    """claude-mcp-add-Befehl für den HTTP-Dienst - mit Token-Header, ohne das Token anzuzeigen. Das Token liest
    derselbe Parser wie überall (Anführungszeichen, export, ${VAR}) statt eines zerbrechlichen sed."""
    try:
        from .envfile import parse_env_file
        env = parse_env_file(root / ".env")
    except OSError:
        env = {}
    # nur für compose: die Datei veröffentlicht immer 127.0.0.1:8765 (MCP_PORT gilt dort im Container)
    cmd = "claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp"
    if not env.get("MCP_AUTH_TOKEN"):
        return cmd
    env_path = shlex.quote(str((root / ".env").resolve()))
    getter = f"{shlex.quote(sys.executable)} -m websearch.envfile --get MCP_AUTH_TOKEN {env_path}"
    # T=$(...) && ...: bricht ab, wenn das Token fehlt, statt einen leeren Header zu speichern
    return f"T=$({getter}) && \\\n      {cmd} --header \"Authorization: Bearer $T\""


def _next_steps(root: Path, watches_hint: bool) -> None:
    compose_file = compose_file_in(root)
    settings = root / "searxng" / "settings.yml"
    try:
        if compose_file and settings.is_file() and not settings.stat().st_mode & 0o004:
            print(t(f"! {settings} ist für andere nicht lesbar - SearXNG läuft im Container als UID 977 und "
                    f"bräuchte: chmod a+r {shlex.quote(str(settings))}; chmod a+rx {shlex.quote(str(settings.parent))}",
                    f"! {settings} is not world-readable - SearXNG runs as UID 977 in the container and needs: "
                    f"chmod a+r {shlex.quote(str(settings))}; chmod a+rx {shlex.quote(str(settings.parent))}"))
    except OSError:
        pass
    cd = "" if _same(root, Path.cwd()) else f"    cd {shlex.quote(str(root))}\n"
    found = compose_version()
    print()
    if watches_hint:
        print(t("! watches/watches.json enthält noch die Beispiel-Suchen: vor dem Start eigene eintragen.",
                "! watches/watches.json still contains the demo searches: put your own there before starting."))
    if compose_file and found and found.usable:
        name, ver, cmd = found.tool, f"{found.version[0]}.{found.version[1]}", found.cmd
        print(t(f"✓ {cmd} gefunden ({name} {ver}). Nächste Schritte:",
                f"✓ {cmd} found ({name} {ver}). Next steps:"))
        if cd:
            print(cd, end="")
        if watches_hint:
            print(t("    $EDITOR watches/watches.json      (Beispiel-Suchen ersetzen)",
                    "    $EDITOR watches/watches.json      (replace the demo searches)"))
        print(f"    {cmd} up -d\n    {cmd} ps")
        print(t(f"    {cmd} exec websearch-mcp websearch doctor      (Einrichtung prüfen)",
                f"    {cmd} exec websearch-mcp websearch doctor      (check the setup)"))
        print(t(f"\nFür Claude Code:\n    {_http_add(root)}", f"\nFor Claude Code:\n    {_http_add(root)}"))
        return
    if compose_file and found:
        cmd, ver = found.cmd, f"{found.version[0]}.{found.version[1]}"
        need = f"{found.minimum[0]}.{found.minimum[1]}"
        print(t(f"! {cmd} ({found.tool} {ver}) ist zu alt (mindestens {need} nötig). Andere Wege:",
                f"! {cmd} ({found.tool} {ver}) is too old ({need} or newer needed). Other ways:"))
    elif compose_file:
        print(t("! Weder docker compose noch podman compose gefunden - es geht auch ohne:",
                "! Neither docker compose nor podman compose found - it also works without:"))
    elif find_project(root) is None:
        print(t(f"! Keine Compose-Datei (docker-compose.yml) in {root.resolve()} oder darüber gefunden - "
                "Container-Start per compose geht von hier nicht. Andere Wege:",
                f"! No compose file (docker-compose.yml) found in {root.resolve()} or its parents - "
                "starting containers via compose does not work from here. Other ways:"))
    deploy = root / "deploy"
    where = "" if deploy.is_dir() else t("   (Dateien im Quell-Repository)", "   (files in the source repository)")
    print(t(f"  a) Podman Quadlet (systemd, rootless):  deploy/podman/install.sh{where}\n"
            "     installiert die Units nach ~/.config/containers/systemd und baut das Image",
            f"  a) Podman Quadlet (systemd, rootless):  deploy/podman/install.sh{where}\n"
            "     installs the units into ~/.config/containers/systemd and builds the image"))
    print(t("  b) bubblewrap-Sandbox ohne Container:  deploy/bwrap/websearch-bwrap <befehl>  (jeder websearch-Befehl\n"
            "     in bwrap), deploy/bwrap/searxng-bwrap (lokaler SearXNG-Checkout in bwrap); systemd-User-Units\n"
            "     in deploy/systemd/",
            "  b) bubblewrap sandbox without containers:  deploy/bwrap/websearch-bwrap <command>  (any websearch\n"
            "     command inside bwrap), deploy/bwrap/searxng-bwrap (local SearXNG checkout inside bwrap); systemd\n"
            "     user units in deploy/systemd/"))
    if shutil.which("bwrap"):
        print(t("     ✓ bubblewrap gefunden", "     ✓ bubblewrap found"))
    print(t("  c) Direkt (pip): SearXNG separat betreiben und SEARXNG_URL setzen; ohne SearXNG laufen die übrigen\n"
            "     Quellen als Fallback. Dann:",
            "  c) Directly (pip): run SearXNG separately and set SEARXNG_URL; without SearXNG the other sources\n"
            "     run as a fallback. Then:"))
    print(f"{cd}    set -a; . ./.env; set +a\n    websearch doctor\n"
          "    websearch search \"open source\" -n 5")
    env_path = shlex.quote(str((root / ".env").resolve()))
    stdio = f"claude mcp add -s user websearch -- {shlex.quote(sys.executable)} -m websearch --env-file {env_path} mcp"
    print(t(f"\nFür Claude Code (stdio, liest die .env selbst):\n    {stdio}",
            f"\nFor Claude Code (stdio, reads the .env itself):\n    {stdio}"))
