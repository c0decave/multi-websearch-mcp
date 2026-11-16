""".env-Dateien lesen - ohne sie auszuführen, nach den Regeln von docker compose (dotenv).

Genutzt von 'websearch mcp --env-file' (stdio-Server für Claude Code & Co.) und den bwrap-Skripten in
deploy/bwrap/. Regeln:
  * Leerzeilen und Zeilen mit # am Anfang werden ignoriert, ein führendes "export " ebenfalls
  * KEY muss [A-Za-z_][A-Za-z0-9_]* sein, sonst wird die Zeile übersprungen
  * 'einfach' quotiert: wörtlich; "doppelt" quotiert: \\n, \\t, \\", \\\\ werden ersetzt
  * nach einem quotierten Wert darf ein Kommentar folgen (KEY="v" # Kommentar)
  * unquotiert: Kommentar nur nach Leerzeichen (KEY=a#b bleibt "a#b", KEY=a #b wird "a")
  * leere Werte werden nicht übernommen (wie "nicht gesetzt")
  * unquotiert und "doppelt" quotiert werden $VAR, ${VAR}, ${VAR:-vorgabe}, ${VAR-vorgabe} ersetzt (zuvor in der
    Datei gesetzte Variablen, dann die Umgebung) - wie docker compose und `set -a; . ./.env`; \\$ bzw.
    'einfache' Anführungszeichen schützen ein $

Als Skript: python -m websearch.envfile DATEI  -> KEY\\0WERT\\0... auf stdout (für Shell-Skripte),
            python -m websearch.envfile --get KEY DATEI -> nur der Wert (z.B. für den Token-Header).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r", "$": "\0"}

_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
_LITERAL_DOLLAR = "\0"          # Platzhalter für ein mit \\$ geschütztes $ (NUL kommt in Umgebungen nie vor)


def _interpolate(value: str, known: dict[str, str], environ: Mapping[str, str] | None = None) -> str:
    """$VAR, ${VAR}, ${VAR:-vorgabe}, ${VAR-vorgabe} wie docker compose / die Shell ersetzen; Quelle: zuvor in
    der Datei gesetzte Variablen, dann die Umgebung. Unbekannte Variablen werden leer."""
    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(4)
        op, default = m.group(2), m.group(3) or ""
        cur = known.get(name, (os.environ if environ is None else environ).get(name))
        if op == ":-":
            return cur if cur else default
        if op == "-":
            return default if cur is None else cur
        return cur or ""
    return _VAR.sub(sub, value).replace(_LITERAL_DOLLAR, "$")


def _parse(line: str) -> tuple[str, str, str] | None:
    """Eine Zeile -> (KEY, roher Wert, Anführungszeichen) oder None (Kommentar, leer, ungültig)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export ") or line.startswith("export\t"):
        line = line[7:].lstrip()
    key, sep, rest = line.partition("=")
    key = key.strip()
    if not sep or not _KEY.fullmatch(key):
        return None
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        quote, i, out = rest[0], 1, []
        while i < len(rest):
            ch = rest[i]
            if quote == '"' and ch == "\\" and i + 1 < len(rest):
                out.append(_ESCAPES.get(rest[i + 1], "\\" + rest[i + 1]))
                i += 2
                continue
            if ch == quote:
                tail = rest[i + 1:].strip()
                if tail and not tail.startswith("#"):
                    return None                     # Müll nach dem schließenden Anführungszeichen
                return key, "".join(out), quote
            out.append(ch)
            i += 1
        return None                                 # Anführungszeichen nicht geschlossen
    value = re.split(r"\s+#", rest, maxsplit=1)[0].rstrip()
    return key, value, ""


def parse_line(line: str, known: dict[str, str] | None = None,
               environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """Eine Zeile -> (KEY, Wert) oder None. 'einfach' quotierte Werte bleiben wörtlich, sonst werden $VAR,
    ${VAR}, ${VAR:-x} ersetzt (aus `known`, dann der Umgebung)."""
    parsed = _parse(line)
    if parsed is None:
        return None
    key, value, quote = parsed
    if quote == "'":
        return key, value.replace(_LITERAL_DOLLAR, "$")
    return key, _interpolate(value, known if known is not None else {}, environ)


def parse_env_text(text: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Alle gesetzten (nicht leeren) Variablen eines .env-Textes (Zeilen nur an \\n getrennt, wie die Shell).
    `environ`: Umgebung für ${VAR} (Standard os.environ)."""
    env: dict[str, str] = {}
    for line in text.lstrip("\ufeff").split("\n"):
        if (pair := parse_line(line, env, environ)) is not None and pair[1]:
            env[pair[0]] = pair[1]
    return env


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Alle gesetzten (nicht leeren) Variablen einer .env-Datei. Die Datei wird nie ausgeführt."""
    return parse_env_text(Path(path).read_text(encoding="utf-8-sig"))


def load_env_file(path: str | os.PathLike[str], override: bool = False) -> list[str]:
    """Variablen in os.environ übernehmen (bereits gesetzte gewinnen, außer override). Liefert die Namen."""
    loaded = []
    for key, value in parse_env_file(path).items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 3 and args[0] == "--get":          # --get KEY FILE -> nur der Wert (für $(...) in Befehlen)
        from .i18n import t
        try:
            value = parse_env_file(args[2]).get(args[1])
        except OSError as e:
            print(t(f"{args[2]} nicht lesbar ({e.strerror}) - erst 'websearch init' im Projektverzeichnis",
                    f"{args[2]} not readable ({e.strerror}) - run 'websearch init' in the project directory first"),
                  file=sys.stderr)
            return 1
        if not value:
            print(t(f"{args[1]} ist in {args[2]} nicht gesetzt - 'websearch init' erzeugt ein Token",
                    f"{args[1]} is not set in {args[2]} - 'websearch init' creates a token"), file=sys.stderr)
            return 1
        sys.stdout.write(value)
        return 0
    if len(args) != 1:
        print("usage: python -m websearch.envfile FILE | --get KEY FILE", file=sys.stderr)
        return 2
    for key, value in parse_env_file(args[0]).items():
        if "\0" not in value:
            sys.stdout.write(f"{key}\0{value}\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
