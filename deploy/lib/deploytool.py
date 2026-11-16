"""Hilfsfunktionen für die Deploy-Skripte (deploy/bwrap, deploy/systemd, deploy/podman).

Läuft mit dem System-Python (python3 -I, ab 3.8, ohne websearch-Paket):
  env FILE [KEY...]          .env lesen (ohne sie auszuführen) -> KEY\\0WERT\\0... (nur KEYs, falls angegeben)
  podman-env FILE [KEY...]   dasselbe als KEY=WERT-Zeilen für Podmans EnvironmentFile= (Werte wörtlich)
  render VORLAGE NAME=WERT.. @NAME@-Platzhalter einer systemd-/Quadlet-Vorlage ersetzen (mit Escaping)
  seccomp                    seccomp-BPF-Programm für bwrap --seccomp (blockiert AF_UNIX-Sockets, io_uring)
  mcp-add ENVFILE PORT       Befehl zum Eintragen des HTTP-MCP-Servers in Claude Code (mit Token-Header, ohne
                             das Token anzuzeigen - wie 'websearch init')

Der .env-Parser ist eine wörtliche Kopie von websearch/envfile.py (Regeln von docker compose); ein Test in
tests/test_deploy.py prüft, dass beide dasselbe liefern. Bei Änderungen beide anpassen.
"""

import os
import platform
import re
import shlex
import struct
import sys

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r", "$": "\0"}

_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
_LITERAL_DOLLAR = "\0"          # Platzhalter für ein mit \\$ geschütztes $ (NUL kommt in Umgebungen nie vor)


def _interpolate(value, known):
    """$VAR, ${VAR}, ${VAR:-vorgabe}, ${VAR-vorgabe} wie docker compose / die Shell ersetzen; Quelle: zuvor in
    der Datei gesetzte Variablen, dann die Umgebung. Unbekannte Variablen werden leer."""
    def sub(m):
        name = m.group(1) or m.group(4)
        op, default = m.group(2), m.group(3) or ""
        cur = known.get(name, os.environ.get(name))
        if op == ":-":
            return cur if cur else default
        if op == "-":
            return default if cur is None else cur
        return cur or ""
    return _VAR.sub(sub, value).replace(_LITERAL_DOLLAR, "$")


# --------------------------------------------------------------------------- .env (wie websearch/envfile.py)

def _parse(line):
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

def parse_line(line, known=None):
    """Eine Zeile -> (KEY, Wert) oder None. 'einfach' quotierte Werte bleiben wörtlich, sonst werden $VAR,
    ${VAR}, ${VAR:-x} ersetzt (aus `known`, dann der Umgebung)."""
    parsed = _parse(line)
    if parsed is None:
        return None
    key, value, quote = parsed
    if quote == "'":
        return key, value.replace(_LITERAL_DOLLAR, "$")
    return key, _interpolate(value, known if known is not None else {})


def parse_env_file(path):
    """Alle gesetzten (nicht leeren) Variablen einer .env-Datei. Die Datei wird nie ausgeführt."""
    env = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f.read().split("\n"):
            pair = parse_line(line, env)
            if pair is not None and pair[1]:
                env[pair[0]] = pair[1]
    return env


def _selected(path, keys):
    env = parse_env_file(path)
    return {k: v for k, v in env.items() if not keys or k in keys}


def cmd_env(args):
    for key, value in _selected(args[0], args[1:]).items():
        if "\0" not in value:
            sys.stdout.write(key + "\0" + value + "\0")
    return 0


def cmd_podman_env(args):
    # Podman übernimmt Werte wörtlich (keine Anführungszeichen/Kommentare) - mehrzeilige gehen nicht
    out = []
    for key, value in _selected(args[0], args[1:]).items():
        if "\n" in value or "\r" in value or "\0" in value:
            sys.stderr.write("deploytool: %s ist mehrzeilig - für Podman ausgelassen / multi-line value "
                             "skipped for Podman\n" % key)
            continue
        out.append(key + "=" + value + "\n")
    sys.stdout.write("".join(out))
    return 0


# --------------------------------------------------------------------------- Unit-Vorlagen

def _systemd_escape(value, quoted, exec_line):
    """Wert für eine systemd-Unit: % ist ein Specifier (%% = %); in "..." zusätzlich \\ und " escapen;
    in Argumenten von Exec*= außerdem $ verdoppeln (dort wird $VAR ersetzt, im Programmpfad und in
    Environment= nicht)."""
    value = value.replace("%", "%%")
    if quoted:
        value = value.replace("\\", "\\\\").replace('"', '\\"')
    if exec_line:
        value = value.replace("$", "$$")
    return value


def cmd_render(args):
    template, values = args[0], {}
    for a in args[1:]:
        name, _, value = a.partition("=")
        if any(c in value for c in "\n\r\0"):
            sys.stderr.write("deploytool: %s enthält einen Zeilenumbruch / contains a line break\n" % name)
            return 2
        values[name] = value
    with open(template, encoding="utf-8") as f:
        lines = f.read().split("\n")
    out = []
    for line in lines:
        res, quoted, i = [], False, 0
        # Exec*=: $VAR wird nur in den Argumenten ersetzt, nicht im Programmpfad (erstes Wort)
        exec_line = line.lstrip().startswith("Exec")
        word = -1 if exec_line else 0          # -1: noch vor dem "=" der Zuweisung
        while i < len(line):
            m = re.match(r"@([A-Z_]+)@", line[i:])
            if m and m.group(1) in values:
                res.append(_systemd_escape(values[m.group(1)], quoted, exec_line and word > 0))
                i += m.end()
                continue
            ch = line[i]
            if ch == "\\" and quoted:                  # escapetes Zeichen in "..." unverändert übernehmen
                res.append(line[i:i + 2])
                i += 2
                continue
            if ch == '"':
                quoted = not quoted
            elif word < 0 and ch == "=":
                word = 0
            elif word >= 0 and not quoted and ch in " \t" and res and res[-1] not in " \t=":
                word += 1
            res.append(ch)
            i += 1
        out.append("".join(res))
    text = "\n".join(out)
    left = sorted(set(re.findall(r"@([A-Z_]+)@", text)))
    if left:
        sys.stderr.write("deploytool: Platzhalter ohne Wert / placeholders without value: %s\n" % ", ".join(left))
        return 2
    sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------- seccomp

# Klassisches BPF (struct sock_filter) für SECCOMP_MODE_FILTER, Daten: struct seccomp_data
_LD_ABS, _JEQ, _JGE, _RET, _AND = 0x20, 0x15, 0x35, 0x06, 0x54
_ALLOW, _ERRNO, _KILL = 0x7FFF0000, 0x00050000, 0x80000000
_EAFNOSUPPORT, _ENOSYS = 97, 38
_AF_UNIX = 1
_SOCK_STREAM, _SOCK_TYPE_MASK = 1, 0xF         # type & 0xf (ohne SOCK_NONBLOCK/SOCK_CLOEXEC)
# Architektur -> (AUDIT_ARCH_*, Nummer von socket(2), von socketpair(2), x32-Bit prüfen)
_ARCHES = {"x86_64": (0xC000003E, 41, 53, True), "amd64": (0xC000003E, 41, 53, True),
           "aarch64": (0xC00000B7, 198, 199, False), "arm64": (0xC00000B7, 198, 199, False)}
_IO_URING = (425, 426, 427)          # io_uring_setup/enter/register (auf allen Architekturen gleich)


def seccomp_program(machine=None):
    """BPF-Programm als bytes oder None (Architektur nicht unterstützt).

    Blockiert socket(AF_UNIX, ...) mit EAFNOSUPPORT: Das Netz der Sandbox ist geteilt, abstrakte
    Unix-Sockets (X11 @/tmp/.X11-unix/X0, alte D-Bus-Session-Busse ...) hängen am Netz-Namespace und wären
    sonst trotz leerem /run, /tmp und $HOME erreichbar. socketpair(2) nur als SOCK_STREAM (asyncio, verbunden,
    connect() scheitert mit EISCONN) - Datagram-/Seqpacket-Paare könnten per sendto()/connect() abstrakte
    Datagram-Sockets erreichen.
    io_uring (umgeht seccomp) -> ENOSYS. Fremde ABIs (i386 auf x86_64, x32) -> Prozess beenden."""
    arch = _ARCHES.get((machine or platform.machine()).lower())
    if arch is None or sys.byteorder != "little":
        return None
    audit, nr_socket, nr_socketpair, x32 = arch
    prog = []                        # (code, jt-Label, jf-Label, k) - Sprungziele als Labels
    labels = {}

    def op(code, k, jt=None, jf=None):
        prog.append((code, jt, jf, k))

    op(_LD_ABS, 4)                                   # seccomp_data.arch
    op(_JEQ, audit, jt=None, jf="kill")
    op(_LD_ABS, 0)                                   # seccomp_data.nr
    if x32:
        op(_JGE, 0x40000000, jt="kill", jf=None)     # __X32_SYSCALL_BIT
    op(_JEQ, nr_socket, jt="socket", jf=None)
    op(_JEQ, nr_socketpair, jt="socketpair", jf=None)
    for nr in _IO_URING:
        op(_JEQ, nr, jt="enosys", jf=None)
    op(_RET, _ALLOW)
    labels["socketpair"] = len(prog)
    op(_LD_ABS, 24)                                  # args[1] (untere 32 Bit): type
    op(_AND, _SOCK_TYPE_MASK)
    op(_JEQ, _SOCK_STREAM, jt=None, jf="eafnosupport")
    op(_RET, _ALLOW)
    labels["socket"] = len(prog)
    op(_LD_ABS, 16)                                  # args[0] (untere 32 Bit, little endian): domain
    op(_JEQ, _AF_UNIX, jt="eafnosupport", jf=None)
    op(_RET, _ALLOW)
    labels["eafnosupport"] = len(prog)
    op(_RET, _ERRNO | _EAFNOSUPPORT)
    labels["enosys"] = len(prog)
    op(_RET, _ERRNO | _ENOSYS)
    labels["kill"] = len(prog)
    op(_RET, _KILL)

    out = b""
    for pc, (code, jt, jf, k) in enumerate(prog):
        def rel(label):
            return 0 if label is None else labels[label] - pc - 1
        out += struct.pack("=HBBI", code, rel(jt), rel(jf), k)
    return out


def cmd_seccomp(args):
    prog = seccomp_program(args[0] if args else None)
    if prog is None:
        sys.stderr.write("deploytool: seccomp für %s nicht unterstützt / not supported\n" % platform.machine())
        return 3
    try:
        os.write(sys.stdout.fileno(), prog)
    except BrokenPipeError:                          # bwrap hat schon aufgegeben - dessen Fehler zählt
        return 1
    return 0


def cmd_mcp_add(args):
    envfile, port = os.path.abspath(args[0]), args[1]
    cmd = "claude mcp add -s user --transport http websearch http://127.0.0.1:%s/mcp" % port
    try:
        token = parse_env_file(envfile).get("MCP_AUTH_TOKEN")
    except OSError:
        token = None
    if token:
        # Token mit demselben Parser auslesen (Anführungszeichen, export, ${VAR}) - nie im Klartext anzeigen
        tool = shlex.quote(os.path.abspath(__file__))
        # T=$(...) && ...: bricht ab, wenn das Token fehlt, statt einen leeren Header zu speichern
        sys.stdout.write("T=$(python3 -I " + tool + " get " + shlex.quote(envfile) + " MCP_AUTH_TOKEN) && \\\n  "
                         + cmd + " --header \"Authorization: Bearer $T\"\n")
    else:
        sys.stdout.write(cmd + "\n# ohne MCP_AUTH_TOKEN in .env (empfohlen: 'websearch init' legt eins an) / without "
                               "MCP_AUTH_TOKEN in .env (recommended: 'websearch init' creates one)\n")
    return 0


def cmd_get(args):
    """get FILE KEY -> Wert (ohne Zeilenumbruch) oder Exit 1 mit Meldung, wenn nicht lesbar/nicht gesetzt."""
    try:
        value = parse_env_file(args[0]).get(args[1])
    except OSError as e:
        sys.stderr.write("%s nicht lesbar / not readable (%s) - websearch init\n" % (args[0], e.strerror))
        return 1
    if not value:
        sys.stderr.write("%s nicht gesetzt in / not set in %s - websearch init\n" % (args[1], args[0]))
        return 1
    sys.stdout.write(value)
    return 0


COMMANDS = {"env": (cmd_env, 1), "podman-env": (cmd_podman_env, 1), "render": (cmd_render, 1),
            "seccomp": (cmd_seccomp, 0), "mcp-add": (cmd_mcp_add, 2), "get": (cmd_get, 2)}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in COMMANDS or len(args) - 1 < COMMANDS[args[0]][1]:
        sys.stderr.write("usage: deploytool.py env|podman-env FILE [KEY...] | render TEMPLATE NAME=VALUE... "
                         "| seccomp [ARCH] | mcp-add ENVFILE PORT | get FILE KEY\n")
        return 2
    return COMMANDS[args[0]][0](args[1:])


if __name__ == "__main__":
    sys.exit(main())
