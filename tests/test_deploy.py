"""Betriebsarten neben Docker: bubblewrap-Sandbox (deploy/bwrap), Podman-Quadlet (deploy/podman),
systemd-User-Dienste (deploy/systemd).

bwrap selbst braucht User-Namespaces; wo die fehlen (Container, CI), führt ein Test-Double die Optionen aus
(Umgebung, Arbeitsverzeichnis, --args-Pipe, seccomp-Programm) und startet den Befehl - geprüft werden dann der
komplette Aufbau der Sandbox und dass websearch darin wirklich läuft. Ein vorhandenes bwrap (oder
WEBSEARCH_TEST_BWRAP=/pfad/zu/bwrap) prüft zusätzlich, dass es alle Optionen akzeptiert; mit User-Namespaces
läuft auch ein echter Sandbox-Test."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
BWRAP_SH = ROOT / "deploy" / "bwrap" / "websearch-bwrap"
SEARXNG_SH = ROOT / "deploy" / "bwrap" / "searxng-bwrap"
PODMAN_DIR = ROOT / "deploy" / "podman"
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
TOOL = ROOT / "deploy" / "lib" / "deploytool.py"
# Die Skripte verweigern root; Tests, die als root laufen (Container/CI), erlauben es ausdrücklich
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
AS_ROOT = {"WEBSEARCH_BWRAP_ALLOW_ROOT": "1"} if IS_ROOT else {}

FAKE_BWRAP = r'''#!PYTHON -I
import ctypes, json, os, sys
ARITY = {"--ro-bind": 2, "--bind": 2, "--dev-bind": 2, "--ro-bind-try": 2, "--bind-try": 2, "--symlink": 2,
         "--setenv": 2, "--unsetenv": 1, "--tmpfs": 1, "--dev": 1, "--proc": 1, "--chdir": 1, "--hostname": 1,
         "--cap-drop": 1, "--dir": 1, "--remount-ro": 1, "--perms": 1, "--chmod": 2, "--file": 2,
         "--bind-data": 2, "--ro-bind-data": 2, "--args": 1, "--seccomp": 1, "--size": 1}
FLAGS = {"--unshare-all", "--share-net", "--unshare-user", "--unshare-user-try", "--disable-userns",
         "--assert-userns-disabled", "--die-with-parent", "--new-session", "--clearenv", "--as-pid-1"}
LOG = os.environ["FAKE_BWRAP_LOG"]
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_BWRAP_VERSION", "bubblewrap 0.8.0"))
    sys.exit(0)
with open(LOG + ".argv", "w") as f:
    json.dump(sys.argv, f)                       # eigene Kommandozeile (= /proc/<pid>/cmdline)
if os.environ.get("FAKE_BWRAP_EARLY_EXIT"):          # bwrap scheitert, bevor es die Pipes liest
    sys.exit("fake-bwrap: early exit")

def read_fd(fd):
    data = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    os.close(fd)
    return data

def parse(a, nested):
    out, i = [], 0
    while i < len(a):
        o = a[i]
        if o == "--":
            return out, a[i + 1:]
        if not o.startswith("--"):
            return out, a[i:]
        if o in FLAGS:
            out.append((o,)); i += 1; continue
        if o not in ARITY:
            sys.exit("fake-bwrap: unknown option " + o)
        vals = a[i + 1:i + 1 + ARITY[o]]
        if len(vals) != ARITY[o]:
            sys.exit("fake-bwrap: missing value for " + o)
        if o == "--args":
            sub = read_fd(int(vals[0])).decode("utf-8").split("\0")
            if sub and sub[-1] == "":
                sub.pop()
            inner, rest = parse(sub, True)
            if rest:
                sys.exit("fake-bwrap: command inside --args")
            out += inner
        else:
            out.append((o, *vals))
        i += 1 + ARITY[o]
    return out, []

opts, cmd = parse(sys.argv[1:], False)
env, cwd, prog = dict(os.environ), None, None
with open(LOG, "w") as log:
    for o in opts:
        if o[0] == "--clearenv":
            env = {}
        elif o[0] == "--setenv":
            env[o[1]] = o[2]
        elif o[0] == "--chdir":
            cwd = o[1]
        elif o[0] == "--seccomp":
            prog = read_fd(int(o[1]))
            o = ("--seccomp", str(len(prog)))
        log.write("\t".join(o) + "\n")
    if os.environ.get("FAKE_BWRAP_NOEXEC"):
        log.write("CMD\t" + json.dumps(cmd) + "\n")
        sys.exit(0)
    if prog is not None:                         # seccomp-Programm wirklich laden (wie bwrap)
        libc = ctypes.CDLL(None, use_errno=True)
        class Prog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_char_p)]
        buf = ctypes.create_string_buffer(prog, len(prog))
        fprog = Prog(len(prog) // 8, ctypes.cast(buf, ctypes.c_char_p))
        ok = libc.prctl(38, 1, 0, 0, 0) == 0 and libc.prctl(22, 2, ctypes.byref(fprog), 0, 0) == 0
        log.write("SECCOMP-LOADED\n" if ok else "SECCOMP-UNAVAILABLE\n")
if cwd:
    os.chdir(cwd)
os.execvpe(cmd[0], cmd, env)
'''


@pytest.fixture
def fake_bwrap(tmp_path):
    path = tmp_path / "fake-bwrap"
    path.write_text(FAKE_BWRAP.replace("PYTHON", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _bash():
    if not shutil.which("bash"):
        pytest.skip("bash fehlt")


def run_wrapper(tmp_path, fake, *args, env=None, cwd=None, script=BWRAP_SH):
    _bash()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    e = {**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(home), "WEBSEARCH_BWRAP": str(fake),
         "WEBSEARCH_PYTHON": sys.executable, "WEBSEARCH_DATA": str(tmp_path / "data"),
         "FAKE_BWRAP_LOG": str(tmp_path / "bwrap.log"), "WEBSEARCH_LANG": "en", **(env or {})}
    p = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=e,
                       cwd=cwd or tmp_path, timeout=120)
    log = (tmp_path / "bwrap.log").read_text() if (tmp_path / "bwrap.log").exists() else ""
    return p, log


def _setenv(log):
    return dict(line.split("\t")[1:3] for line in log.splitlines() if line.startswith("--setenv\t"))


def _argv(tmp_path):
    return json.loads((tmp_path / "bwrap.log.argv").read_text())


def _binds(log, kind="--ro-bind"):
    return [tuple(line.split("\t")[1:]) for line in log.splitlines() if line.startswith(kind + "\t")]


def _cmd(log):
    return json.loads([x for x in log.splitlines() if x.startswith("CMD\t")][0].split("\t", 1)[1])


def _copy_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    (repo / "searxng").mkdir()
    shutil.copy(ROOT / "searxng" / "settings.yml", repo / "searxng" / "settings.yml")
    return repo


# =========================================================================== bubblewrap

def test_websearch_runs_inside_the_sandbox_with_isolated_environment(tmp_path, fake_bwrap):
    envfile = tmp_path / "test.env"
    envfile.write_text('# Kommentar\nWEBSEARCH_UA="websearch/0.3 (ops@firma.de)"  # Kontakt\nREDDIT_CLIENT_ID=\n'
                       "export EXA_API_KEY='k-123'\nNOT_OURS=1\nSEARXNG_SECRET=abc # Kommentar\n", encoding="utf-8")
    p, log = run_wrapper(tmp_path, fake_bwrap, "--env", str(envfile), "sources",
                         env={"MY_SECRET": "leak", "SEARXNG_URL": "http://127.0.0.1:1", "SSL_CERT_DIR": "/etc/ssl"})
    assert p.returncode == 0, p.stderr
    assert "searxng" in p.stdout and "Self-hosted SearXNG" in p.stdout        # websearch lief (englisch)
    env = _setenv(log)
    assert env["WEBSEARCH_UA"] == "websearch/0.3 (ops@firma.de)"              # Anführungszeichen/Kommentar weg
    assert "SEARXNG_SECRET" not in env                                         # liest websearch nie
    assert env["EXA_API_KEY"] == "k-123" and env["SEARXNG_URL"] == "http://127.0.0.1:1"
    assert "MY_SECRET" not in env and "REDDIT_CLIENT_ID" not in env           # fremd bzw. leer
    assert "NOT_OURS" not in env                                              # auch aus .env nur Bekanntes
    assert env["SSL_CERT_DIR"] == "/etc/ssl"                                  # CA-Pfade durchgereicht
    assert "WEBSEARCH_BWRAP" not in env and "WEBSEARCH_PYTHON" not in env     # Steuervariablen nicht
    data = str((tmp_path / "data").resolve())
    assert env["WEBSEARCH_SANDBOX"] == "bwrap" and env["HOME"] == data
    assert env["WEBSEARCH_CACHE"].startswith(data)
    lines = log.splitlines()
    assert lines[:2] == ["--ro-bind\t/\t/", "--dev\t/dev"]                     # alles nur lesbar
    home = (tmp_path / "home").resolve()
    for hidden in (home, "/tmp", "/run"):
        assert f"--tmpfs\t{hidden}" in lines, hidden
    assert lines.index("--tmpfs\t/run") < lines.index(f"--bind\t{data}\t{data}")   # erst leeren, dann einblenden
    for flag in ("--clearenv", "--unshare-all", "--cap-drop\tALL", "--unshare-user", "--disable-userns"):
        assert flag in lines, flag
    assert [x for x in lines if x.startswith("--bind\t")] == [f"--bind\t{data}\t{data}"]   # einzig schreibbar
    assert f"--chdir\t{data}" in lines                                        # nicht das Aufrufer-Verzeichnis
    assert any(x.startswith("--seccomp\t") for x in lines)
    assert oct((tmp_path / "data").stat().st_mode & 0o777) == "0o700"
    argv = _argv(tmp_path)
    assert argv[1:4] == ["--args", "3", "--seccomp"] and argv[argv.index("--") + 1:][1:3] == ["-I", "-B"]


def test_secrets_never_appear_on_bwraps_command_line(tmp_path, fake_bwrap):
    """bwrap lebt so lange wie der Dienst; seine Kommandozeile (/proc/<pid>/cmdline) kann jeder lesen."""
    envfile = tmp_path / "s.env"
    envfile.write_text("WEBSEARCH_SMTP_PASSWORD=smtp-geheim\nREDDIT_CLIENT_SECRET=rd-geheim\n")
    p, log = run_wrapper(tmp_path, fake_bwrap, "--env", str(envfile), "sources",
                         env={"MCP_AUTH_TOKEN": "tok-geheim", "EXA_API_KEY": "exa-geheim"})
    assert p.returncode == 0, p.stderr
    env = _setenv(log)
    argv = " ".join(_argv(tmp_path))
    for secret in ("smtp-geheim", "rd-geheim", "tok-geheim", "exa-geheim"):
        assert secret in env.values()                     # in der Sandbox vorhanden ...
        assert secret not in argv                         # ... aber nicht in bwraps argv
    assert "--setenv" not in argv


def test_seccomp_blocks_unix_sockets_but_websearch_still_works(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources")
    assert p.returncode == 0, p.stderr
    if "SECCOMP-UNAVAILABLE" in log:
        pytest.skip("seccomp hier nicht verfügbar")
    assert "SECCOMP-LOADED" in log and "Self-hosted SearXNG" in p.stdout      # asyncio & Co. laufen
    probe = ("import socket\na, b = socket.socketpair()\n"
             "try:\n    socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)\n    print('DGRAM-OK')\n"
             "except OSError as e:\n    print('DGRAM-BLOCKED', e.errno)\n"
             "try:\n    a.connect(b'\\0websearch-test')\nexcept OSError as e:\n    print('CONNECT', e.errno)\n"
             "try:\n    socket.socket(socket.AF_UNIX)\n    print('UNIX-OK')\nexcept OSError as e:\n"
             "    print('UNIX-BLOCKED', e.errno)\nsocket.socket(socket.AF_INET).close()\nprint('INET-OK')\n")
    prog = subprocess.run([sys.executable, "-I", str(TOOL), "seccomp"], capture_output=True, check=True).stdout
    r, w = os.pipe()
    os.write(w, prog)
    os.close(w)
    try:
        q = subprocess.run([str(fake_bwrap), "--seccomp", str(r), "--", sys.executable, "-I", "-c", probe],
                           capture_output=True, text=True, pass_fds=(r,), timeout=60,
                           env={**os.environ, "FAKE_BWRAP_LOG": str(tmp_path / "p.log")})
    finally:
        os.close(r)
    assert "UNIX-BLOCKED 97" in q.stdout and "INET-OK" in q.stdout, q.stdout + q.stderr
    assert "DGRAM-BLOCKED 97" in q.stdout                 # Datagram-Paare könnten abstrakte Sockets erreichen
    assert "CONNECT" in q.stdout and "DGRAM-OK" not in q.stdout


def test_seccomp_can_be_switched_off(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources",
                         env={"WEBSEARCH_BWRAP_SECCOMP": "0", "WEBSEARCH_BWRAP_DISABLE_USERNS": "0"})
    assert p.returncode == 0, p.stderr
    assert "--seccomp" not in log and "--disable-userns" not in log


def test_old_bwrap_without_disable_userns(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources", env={"FAKE_BWRAP_VERSION": "bubblewrap 0.6.1"})
    assert p.returncode == 0, p.stderr
    assert "--disable-userns" not in log and "0.8" in p.stderr


def test_print_masks_secrets_and_runs_nothing(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "--print", "search", "x",
                         env={"EXA_API_KEY": "geheim", "MCP_AUTH_TOKEN": "tok", "WEBSEARCH_UA": "ua",
                              "WEBSEARCH_WEBHOOK": "https://hooks.example/abc?sig=hooksecret",
                              "DDGS_PROXY": "socks5://proxy:1080/?token=qtok&x=1",
                              "WEBSEARCH_PROXY": "http://user:proxy@pw@proxy:3128", "SEARXNG_URL": "http://u:p@sx:8080/"})
    assert p.returncode == 0 and log == ""
    for secret in ("geheim", "tok ", "hooksecret", "proxy@pw", "@pw", "u:p@", "qtok"):
        assert secret not in p.stdout, secret
    assert "EXA_API_KEY" in p.stdout and r"\*\*\*" in p.stdout
    assert r"http://\*\*\*@proxy:3128" in p.stdout and r"http://\*\*\*@sx:8080/" in p.stdout


def test_home_is_never_exposed_again(tmp_path, fake_bwrap):
    home = tmp_path / "home"
    home.mkdir()
    (home / "watches.json").write_text("[]")
    (home / "link").symlink_to(home)
    for cwd, arg in ((home, str(home / "watches.json")), (home / "link", "watches.json")):   # auch über Symlink
        p, log = run_wrapper(tmp_path, fake_bwrap, "--print", "monitor", arg, cwd=cwd)
        assert p.returncode == 0, p.stderr
        assert f"--ro-bind {home} {home}" not in p.stdout                     # weder als cwd noch als Argument
        assert f"--ro-bind {home}/watches.json {home}/watches.json" in p.stdout   # nur die Datei
        assert "not mounted" in p.stderr


def test_search_queries_are_never_mounted(tmp_path, fake_bwrap):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (tmp_path / "proj").mkdir()
    for cwd, q in ((home, ".ssh"), (tmp_path, "proj")):
        p, _ = run_wrapper(tmp_path, fake_bwrap, "--print", "search", q, cwd=cwd)
        assert p.returncode == 0, p.stderr
        assert ".ssh " not in p.stdout.split(" -- ")[0] and f"{tmp_path}/proj" not in p.stdout


def test_argument_paths_are_mounted_read_only_and_made_absolute(tmp_path, fake_bwrap):
    watches = tmp_path / "w"
    watches.mkdir()
    (watches / "watches.json").write_text("[]")
    (watches / ".watches.json.swp").write_text("")
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--print", "monitor", "w/watches.json", "--interval", "5")
    assert f"--ro-bind {watches} {watches}" in p.stdout                       # nur Watch-Dateien: Hot-Reload
    assert p.stdout.rstrip().endswith(f"monitor {watches}/watches.json --interval 5")   # absolut (cwd = Daten)
    # weitere Einträge (Geschwister-Repos, .env-Sicherungen) -> nur die Datei
    for extra in (".env.bak-20261106", "other-repo"):
        (watches / extra).mkdir() if extra == "other-repo" else (watches / extra).write_text("X=geheim")
        p, _ = run_wrapper(tmp_path, fake_bwrap, "--print", "monitor", "w/watches.json")
        assert f"--ro-bind {watches} {watches}" not in p.stdout, extra
        assert f"--ro-bind {watches}/watches.json {watches}/watches.json" in p.stdout and "restart" in p.stderr
    # eine Datei direkt in /tmp blendet nicht ganz /tmp ein
    fd, name = tempfile.mkstemp(suffix=".json", dir="/tmp")
    os.close(fd)
    t = Path(name)
    try:
        t.write_text("[]")
        p, _ = run_wrapper(tmp_path, fake_bwrap, "--print", "monitor", str(t))
        assert "--ro-bind /tmp /tmp" not in p.stdout and f"--ro-bind {t} {t}" in p.stdout
    finally:
        t.unlink()
    (tmp_path / "q.txt").write_text("a\n")
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--print", "many", "-n", "3", "q.txt")
    assert f"--ro-bind {tmp_path}/q.txt {tmp_path}/q.txt" in p.stdout and f"many -n 3 {tmp_path}/q.txt" in p.stdout


def test_code_in_the_working_directory_is_not_imported(tmp_path, fake_bwrap):
    """python -m würde cwd an den Anfang von sys.path setzen - eine json.py dort liefe in der Sandbox."""
    data = tmp_path / "data"
    data.mkdir()
    for d in (tmp_path, data):                     # Aufrufer-Verzeichnis und (schreibbares) Datenverzeichnis
        (d / "json.py").write_text("import os; print('PWNED', os.environ.get('MCP_AUTH_TOKEN'))\n")
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"MCP_AUTH_TOKEN": "t"})
    assert p.returncode == 0, p.stderr
    assert "PWNED" not in p.stdout + p.stderr


def test_writable_paths_must_stay_in_the_data_dir(tmp_path, fake_bwrap):
    data = (tmp_path / "data").resolve()
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources",
                         env={"WEBSEARCH_MONITOR_OUT": "hits.jsonl", "WEBSEARCH_HEARTBEAT": "sub/hb"})
    assert p.returncode == 0, p.stderr
    env = _setenv(log)
    assert env["WEBSEARCH_MONITOR_OUT"] == f"{data}/hits.jsonl" and env["WEBSEARCH_HEARTBEAT"] == f"{data}/sub/hb"
    assert [x for x in log.splitlines() if x.startswith("--bind\t")] == [f"--bind\t{data}\t{data}"]
    home = tmp_path / "home"
    for bad in (str(home / "hits.jsonl"), str(ROOT / "x.jsonl"), f"{data}/../escape.jsonl", "/data/hits.jsonl",
                "../escape.jsonl"):
        p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"WEBSEARCH_MONITOR_OUT": bad})
        assert p.returncode == 2 and "WEBSEARCH_MONITOR_OUT" in p.stderr and "--rw" in p.stderr, bad
    out = tmp_path / "out"
    out.mkdir()
    p, log = run_wrapper(tmp_path, fake_bwrap, "--rw", str(out), "sources",
                         env={"WEBSEARCH_MONITOR_OUT": str(out / "hits.jsonl")})
    assert p.returncode == 0, p.stderr
    assert f"--bind\t{out}\t{out}" in log


def test_dangerous_writable_or_visible_dirs_are_refused(tmp_path, fake_bwrap):
    home = tmp_path / "home"
    (home / ".local/share/websearch-searxng").mkdir(parents=True)
    for args in (["--rw", str(home)], ["--rw", str(tmp_path)], ["--rw", str(ROOT)], ["--rw", str(ROOT / "deploy")],
                 ["--data", str(home)], ["--data", str(ROOT / "data")], ["--ro", str(tmp_path)],
                 ["--rw", str(home / ".local/share/websearch-searxng")]):
        p, _ = run_wrapper(tmp_path, fake_bwrap, *args, "sources")
        assert p.returncode == 2 and "does not exist" not in p.stderr, (args, p.stderr)
    # in ~ nur ~/.local/share/websearch* und ~/.cache/websearch* (Allow-List); fast alles andere wird von
    # irgendeinem Programm außerhalb der Sandbox ausgeführt oder gelesen
    for rel in (".config/websearch", ".ssh", ".local/bin", "bin", ".cache", ".local/share/systemd/user",
                ".local/share/applications", ".cache/pip", ".config/systemd/user", ".bashrc.d", ".local/share/pipx",
                ".local/share/uv/tools", ".local/share/nvim", ".local/share/fish/vendor_conf.d", ".config/direnv",
                ".config/containers", "go/bin", "Dokumente/out"):
        (home / rel).mkdir(parents=True, exist_ok=True)
        for opt in ("--rw", "--data"):
            p, _ = run_wrapper(tmp_path, fake_bwrap, opt, str(home / rel), "sources")
            assert p.returncode == 2 and "--rw-unsafe" in p.stderr, (opt, rel, p.stderr)
    for rel in (".local/share/websearch-out", ".cache/websearch", ".local/share/websearch2/x"):
        (home / rel).mkdir(parents=True, exist_ok=True)
        p, log = run_wrapper(tmp_path, fake_bwrap, "--rw", str(home / rel), "sources")
        assert p.returncode == 0, (rel, p.stderr)
    # bewusst: --rw-unsafe (Warnung), auch als Freigabe für --data darin
    p, log = run_wrapper(tmp_path, fake_bwrap, "--rw-unsafe", str(home / "Dokumente/out"), "--data",
                         str(home / "Dokumente/out/data"), "sources")
    assert p.returncode == 0 and "--rw-unsafe" in p.stderr, p.stderr
    assert f"--bind\t{home}/Dokumente/out\t{home}/Dokumente/out" in log
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--rw-unsafe", str(home), "sources")      # $HOME selbst nie
    assert p.returncode == 2
    # venv direkt im Home-Verzeichnis: Einblenden würde ~ sichtbar machen
    (home / "bin").mkdir(exist_ok=True)
    (home / "bin" / "python").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (home / "bin" / "python").chmod(0o755)
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"WEBSEARCH_PYTHON": str(home / "bin" / "python")})
    assert p.returncode == 2 and "would expose the home directory" in p.stderr, p.stderr


def test_secret_files_are_masked_in_the_repo(tmp_path, fake_bwrap):
    repo = _copy_repo(tmp_path)
    for name in (".env", ".env.bak-20261106-101010", ".env.example"):
        (repo / name).write_text("MCP_AUTH_TOKEN=x\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[remote]\nurl = https://u:token@github.com/x\n")
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources", script=repo / "deploy/bwrap/websearch-bwrap")
    assert p.returncode == 0, p.stderr
    masks = [d for s, d in _binds(log) if s == "/dev/null"]
    assert f"{repo}/.env" in masks and f"{repo}/.env.bak-20261106-101010" in masks
    assert f"{repo}/.env.example" not in masks and f"--tmpfs\t{repo}/.git" in log
    lines = log.splitlines()
    assert lines.index(f"--ro-bind\t{repo}\t{repo}") < lines.index(f"--ro-bind\t/dev/null\t{repo}/.env")
    assert _setenv(log)["MCP_AUTH_TOKEN"] == "x"                      # Standard-.env: <repo>/.env


def test_cert_paths_outside_visible_dirs_are_mounted(tmp_path, fake_bwrap):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources", env={"SSL_CERT_FILE": str(ca), "REQUESTS_CA_BUNDLE": str(ca)})
    assert p.returncode == 0, p.stderr
    assert _binds(log).count((str(ca), str(ca))) == 1 and _setenv(log)["SSL_CERT_FILE"] == str(ca)


def test_data_dir_is_validated_before_it_is_created(tmp_path, fake_bwrap):
    home = tmp_path / "home"
    home.mkdir()
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--data", str(home / ".config/x"), "sources")
    assert p.returncode == 2 and not (home / ".config").exists()                     # nichts angelegt
    existing = tmp_path / "shared"
    existing.mkdir(mode=0o750)
    existing.chmod(0o750)
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--data", str(existing), "sources")
    assert p.returncode == 0, p.stderr
    assert oct(existing.stat().st_mode & 0o777) == "0o750"                          # vorhandenes nicht geändert


def test_symlinked_searxng_src_in_data_dir_is_refused(tmp_path, fake_bwrap):
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (data / "searxng-src").symlink_to(tmp_path / "elsewhere")
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources")
    assert p.returncode == 2 and "symlink" in p.stderr.lower()
    (data / "searxng-src").unlink()
    (data / "link").symlink_to(tmp_path / "elsewhere")
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"SEARXNG_SRC": str(data / "link" / "src")})
    assert p.returncode == 2 and "symlink" in p.stderr.lower()


def test_legacy_searxng_location_in_data_dir_is_always_read_only(tmp_path, fake_bwrap):
    """Auch wenn es den alten Ort nicht gibt: Die Sandbox darf dort keinen 'SearXNG' anlegen."""
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources")
    assert p.returncode == 0, p.stderr
    lines = log.splitlines()
    data = (tmp_path / "data").resolve()
    assert lines.index(f"--ro-bind\t{data}/searxng-src\t{data}/searxng-src") > lines.index(f"--bind\t{data}\t{data}")
    assert (data / "searxng-src").is_dir() and not any((data / "searxng-src").iterdir())


def test_env_file_option_of_websearch_is_handled_by_the_wrapper(tmp_path, fake_bwrap):
    envfile = tmp_path / "x.env"
    envfile.write_text("WEBSEARCH_UA=from-file\n")
    p, log = run_wrapper(tmp_path, fake_bwrap, "--env-file", str(envfile), "sources")
    assert p.returncode == 0, p.stderr
    assert _setenv(log)["WEBSEARCH_UA"] == "from-file" and "--env-file" not in log
    p, log = run_wrapper(tmp_path, fake_bwrap, "-q", "--env-file", str(envfile), "sources")
    assert p.returncode == 0, p.stderr
    assert _setenv(log)["WEBSEARCH_UA"] == "from-file"
    assert f"--ro-bind\t/dev/null\t{envfile}" in log


def test_wrapper_errors_are_clear(tmp_path, fake_bwrap):
    p, _ = run_wrapper(tmp_path, fake_bwrap, env={"WEBSEARCH_BWRAP": "/nonexistent/bwrap"})
    assert p.returncode == 2 and "Usage" not in p.stdout
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"WEBSEARCH_BWRAP": "/nonexistent/bwrap"})
    assert p.returncode == 2 and "bubblewrap" in p.stderr
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--env", str(tmp_path / "fehlt.env"), "sources")
    assert p.returncode == 2 and "fehlt.env" in p.stderr
    p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"WEBSEARCH_PYTHON": shutil.which("true") or "/bin/true"})
    assert p.returncode == 2 and "pip install -e ." in p.stderr and "WEBSEARCH_PYTHON" in p.stderr
    p, _ = run_wrapper(tmp_path, fake_bwrap, "--help")
    assert p.returncode == 0 and "--print" in p.stdout and "set -euo" not in p.stdout and "english" not in p.stdout


def _test_bwrap():
    return os.environ.get("WEBSEARCH_TEST_BWRAP") or shutil.which("bwrap")


def _real_bwrap_works() -> bool:
    b = _test_bwrap()
    if not b:
        return False
    try:
        return subprocess.run([b, "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                               "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def test_real_bwrap_accepts_the_arguments_if_installed(tmp_path):
    """bwrap prüft alle Optionen (auch die aus --args), bevor es Namespaces anlegt: Ohne Namespaces
    (Container) darf es nur genau daran scheitern, nicht an einer unbekannten Option."""
    b = _test_bwrap()
    if not b:
        pytest.skip("bwrap nicht installiert")
    p, _ = run_wrapper(tmp_path, Path(b), "sources")
    if p.returncode != 0:
        assert "Unknown option" not in p.stderr and ("namespace" in p.stderr or "uid map" in p.stderr), p.stderr
    src = tmp_path / "sx"
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "granian").write_text("#!/bin/sh\n")
    (src / ".venv" / "bin" / "granian").chmod(0o755)
    p = subprocess.run(["bash", str(SEARXNG_SH), "run"], capture_output=True, text=True, timeout=60,
                       env={**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "SEARXNG_SRC": str(src),
                            "WEBSEARCH_BWRAP": b, "SEARXNG_SECRET": "x"})
    if p.returncode != 0:
        assert "Unknown option" not in p.stderr and ("namespace" in p.stderr or "uid map" in p.stderr), p.stderr


@pytest.mark.skipif(not _real_bwrap_works(), reason="kein funktionsfähiges bwrap (User-Namespaces) vorhanden")
def test_real_sandbox_hides_home_and_allows_only_the_data_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.txt").write_text("geheim")
    p, _ = run_wrapper(tmp_path, Path(_test_bwrap()), "sources")
    assert p.returncode == 0, p.stderr
    probe = subprocess.run(["bash", str(BWRAP_SH), "--print", "sources"], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(home), "WEBSEARCH_PYTHON": sys.executable,
                                "WEBSEARCH_DATA": str(tmp_path / "data")})
    assert probe.returncode == 0


# --------------------------------------------------------------------------- searxng-bwrap

def _sx_src(tmp_path):
    src = tmp_path / "sx"
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "granian").write_text("#!/bin/sh\n")
    (src / ".venv" / "bin" / "granian").chmod(0o755)
    return src


def test_searxng_wrapper_print_and_errors(tmp_path):
    _bash()
    src = _sx_src(tmp_path)
    envfile = tmp_path / ".env"
    envfile.write_text('SEARXNG_SECRET="abc123"  # Kommentar\n')
    base = {**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "SEARXNG_SRC": str(src)}
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "run"], capture_output=True, text=True,
                       env={**base, "WEBSEARCH_ENV_FILE": str(envfile)})
    assert p.returncode == 0, p.stderr
    assert "abc123" not in p.stdout and r"SEARXNG_SECRET \*\*\*" in p.stdout
    assert "GRANIAN_HOST 127.0.0.1" in p.stdout and "--unshare-all" in p.stdout and "searx.webapp:app" in p.stdout
    assert f"--tmpfs {ROOT}" in p.stdout and "--tmpfs /run" in p.stdout           # Repo (samt .env) unsichtbar
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "run"], capture_output=True, text=True,
                       env={**base, "WEBSEARCH_ENV_FILE": str(tmp_path / "leer")})
    assert p.returncode == 2 and "SEARXNG_SECRET" in p.stderr
    p = subprocess.run(["bash", str(SEARXNG_SH), "run"], capture_output=True, text=True,
                       env={**base, "SEARXNG_SRC": str(tmp_path / "fehlt")})
    assert p.returncode == 2 and "install" in p.stderr
    p = subprocess.run(["bash", str(SEARXNG_SH), "--help"], capture_output=True, text=True, env=base)
    assert p.returncode == 0 and "SEARXNG_REF" in p.stdout and "set -euo" not in p.stdout


def test_searxng_run_keeps_the_secret_off_the_command_line(tmp_path, fake_bwrap):
    _bash()
    src = _sx_src(tmp_path)
    repo = _copy_repo(tmp_path)
    (repo / ".env").write_text("SEARXNG_SECRET='sx-geheim'\nEXA_API_KEY=k\n")
    (repo / "searxng" / ".env.bak-1").write_text("x")
    p = subprocess.run(["bash", str(repo / "deploy/bwrap/searxng-bwrap"), "run"], capture_output=True, text=True,
                       env={**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "SEARXNG_SRC": str(src),
                            "WEBSEARCH_BWRAP": str(fake_bwrap), "FAKE_BWRAP_LOG": str(tmp_path / "bwrap.log"),
                            "FAKE_BWRAP_NOEXEC": "1"})
    assert p.returncode == 0, p.stderr
    log = (tmp_path / "bwrap.log").read_text()
    env = _setenv(log)
    assert env["SEARXNG_SECRET"] == "sx-geheim" and "EXA_API_KEY" not in env       # nur das Secret
    assert "sx-geheim" not in " ".join(_argv(tmp_path))
    lines = log.splitlines()
    assert lines.index(f"--tmpfs\t{repo}") < lines.index(f"--ro-bind\t{repo}/searxng\t{repo}/searxng")
    assert f"--ro-bind\t/dev/null\t{repo}/searxng/.env.bak-1" in lines and "--disable-userns" in lines


def test_searxng_install_and_update_run_sandboxed_with_pinned_ref(tmp_path, fake_bwrap):
    _bash()
    share = tmp_path / "share"
    base = {**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(share),
            "WEBSEARCH_BWRAP": str(fake_bwrap), "FAKE_BWRAP_LOG": str(tmp_path / "bwrap.log"), "FAKE_BWRAP_NOEXEC": "1"}
    p = subprocess.run(["bash", str(SEARXNG_SH), "install"], capture_output=True, text=True, env=base)
    assert p.returncode == 0, p.stderr
    src = share / "websearch-searxng"                                  # nicht im websearch-Datenverzeichnis
    log = (tmp_path / "bwrap.log").read_text()
    assert _binds(log, "--bind") == [(str(src.resolve()), str(src.resolve()))]  # nur der Quellcode schreibbar
    cmd = _cmd(log)
    assert cmd[0:2] == ["bash", "-euc"] and cmd[-1] == "3e95f6cd2a68add0329cb162907b49289e2bc1fd"
    assert "pip" in cmd[2] and "git -C" in cmd[2] and "fetch" in cmd[2]
    # update: auf SEARXNG_REF wechseln
    (src / ".git").mkdir()
    p = subprocess.run(["bash", str(SEARXNG_SH), "update"], capture_output=True, text=True,
                       env={**base, "SEARXNG_REF": "master"})
    assert p.returncode == 0, p.stderr
    assert _cmd((tmp_path / "bwrap.log").read_text())[-1] == "master"
    # Installation ins Datenverzeichnis von websearch-bwrap wird abgelehnt
    p = subprocess.run(["bash", str(SEARXNG_SH), "install"], capture_output=True, text=True,
                       env={**base, "SEARXNG_SRC": str(share / "websearch" / "searxng-src")})
    assert p.returncode == 2 and "websearch-bwrap" in p.stderr


def test_searxng_legacy_location_is_never_picked_automatically(tmp_path):
    """Der alte Ort liegt im Datenverzeichnis der websearch-Sandbox - dort angelegter 'SearXNG' liefe sonst
    als Dienst außerhalb der Sandbox."""
    _bash()
    share = tmp_path / "share"
    legacy = share / "websearch" / "searxng-src"
    (legacy / ".git").mkdir(parents=True)
    _sx_src(tmp_path)
    shutil.copytree(tmp_path / "sx" / ".venv", legacy / ".venv")
    env = {**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(share),
           "SEARXNG_SECRET": "x"}
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "run"], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and str(legacy) not in p.stdout and "install" in p.stderr and str(legacy) in p.stderr
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "run"], capture_output=True, text=True,
                       env={**env, "SEARXNG_SRC": str(legacy)})                    # nur ausdrücklich
    assert p.returncode == 0 and str(legacy) in p.stdout and "rm -rf" in p.stderr
    # systemd/install.sh ebenso
    repo = _copy_repo(tmp_path)
    (repo / "watches").mkdir()
    (repo / "watches" / "watches.json").write_text("[]")
    (repo / ".env").write_text("SEARXNG_SECRET=abc\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("systemctl", "bwrap"):
        _stub(bindir, name, tmp_path / "calls.log")
    p = subprocess.run(["bash", str(repo / "deploy/systemd/install.sh"), "--no-start"], capture_output=True, text=True,
                       env={**env, "PATH": f"{bindir}:{os.environ['PATH']}", "UNIT_DIR": str(tmp_path / "u")})
    assert p.returncode == 2 and str(legacy) in p.stderr and not (tmp_path / "u").exists()


def test_searxng_print_install_and_update_are_dry_runs(tmp_path):
    _bash()
    share = tmp_path / "share"
    env = {**AS_ROOT, "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(share),
           "WEBSEARCH_BWRAP": "/nonexistent/bwrap"}
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "install"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "git -C" in p.stdout and "3e95f6cd2a68add0329cb162907b49289e2bc1fd" in p.stdout
    assert not (share / "websearch-searxng").exists()                                 # nichts angelegt
    (share / "websearch-searxng" / ".git").mkdir(parents=True)
    p = subprocess.run(["bash", str(SEARXNG_SH), "--print", "update"], capture_output=True, text=True,
                       env={**env, "SEARXNG_REF": "v1"})
    assert p.returncode == 0 and p.stdout.rstrip().endswith(" v1"), p.stdout + p.stderr


@pytest.mark.skipif(not IS_ROOT, reason="nur als root prüfbar")
def test_scripts_refuse_root(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources", env={"WEBSEARCH_BWRAP_ALLOW_ROOT": "0"})
    assert p.returncode == 2 and "root" in p.stderr and log == ""
    src = _sx_src(tmp_path)
    p = subprocess.run(["bash", str(SEARXNG_SH), "run"], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "SEARXNG_SRC": str(src),
                            "SEARXNG_SECRET": "x", "WEBSEARCH_BWRAP": str(fake_bwrap)})
    assert p.returncode == 2 and "WEBSEARCH_BWRAP_ALLOW_ROOT" in p.stderr


def test_other_homes_and_mounts_are_hidden(tmp_path, fake_bwrap):
    p, log = run_wrapper(tmp_path, fake_bwrap, "sources")
    assert p.returncode == 0, p.stderr
    lines = log.splitlines()
    for d in ("/home", "/root", "/srv", "/mnt", "/media"):
        r = os.path.realpath(d)
        if os.path.isdir(r) and r != "/":
            assert f"--tmpfs\t{r}" in lines, d


def test_early_bwrap_failure_leaves_no_traceback(tmp_path, fake_bwrap):
    for _ in range(5):
        p, _ = run_wrapper(tmp_path, fake_bwrap, "sources", env={"FAKE_BWRAP_EARLY_EXIT": "1"})
        assert p.returncode != 0 and "early exit" in p.stderr
        assert "Traceback" not in p.stderr and "Broken pipe" not in p.stderr, p.stderr


def test_seccomp_unsupported_architecture(tmp_path):
    p = subprocess.run([sys.executable, "-I", str(TOOL), "seccomp", "mips"], capture_output=True, text=True)
    assert p.returncode == 3 and not p.stdout


# --------------------------------------------------------------------------- .env-Parser

def _tricky():
    spec = importlib.util.spec_from_file_location("_envfile_tests", ROOT / "tests" / "test_envfile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TRICKY


def test_deploy_dotenv_parser_matches_websearch_envfile(tmp_path):
    from websearch import envfile
    spec = importlib.util.spec_from_file_location("deploytool", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    extra = ("KEY=\"v\" # c\nX=x # c\nY='a # b'\nexport  Z=1\nW=  spaced  \nV=\"a\\qb\"\nU='x' junk\nT==eq\n"
             "S=\"\"\nR=a\t#tab\n\ufeffQ=1\n")
    p = tmp_path / ".env"
    p.write_text(_tricky() + extra, encoding="utf-8")
    assert tool.parse_env_file(p) == envfile.parse_env_file(p)
    for line in (_tricky() + extra).splitlines():
        assert tool.parse_line(line) == envfile.parse_line(line), line
    # Skript-Schnittstellen liefern dasselbe NUL-Format
    a = subprocess.run([sys.executable, "-I", str(TOOL), "env", str(p)], capture_output=True, check=True).stdout
    b = subprocess.run([sys.executable, "-I", "-m", "websearch.envfile", str(p)], capture_output=True,
                       check=True).stdout
    assert a == b
    assert not (tmp_path / "pwned").exists()


def test_render_escapes_for_systemd(tmp_path):
    t = tmp_path / "u.service"
    t.write_text('ExecStart="@REPO@/x" a "@REPO@/y"\nEnvironment="S=@REPO@"\nVolume=@REPO@/w\n')
    out = subprocess.run([sys.executable, "-I", str(TOOL), "render", str(t), 'REPO=/a b/$v%"\\&|'],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    # Programmpfad: kein $-Ersetzen durch systemd (geprüft mit systemd-analyze verify), Argumente: $$
    assert out == ['ExecStart="/a b/$v%%\\"\\\\&|/x" a "/a b/$$v%%\\"\\\\&|/y"',
                   'Environment="S=/a b/$v%%\\"\\\\&|"', 'Volume=/a b/$v%%"\\&|/w']
    t.write_text("X=@MISSING@\n")
    p = subprocess.run([sys.executable, "-I", str(TOOL), "render", str(t)], capture_output=True, text=True)
    assert p.returncode == 2 and "MISSING" in p.stderr


# =========================================================================== Podman (Quadlet)

def _stub(bindir: Path, name: str, log: Path, out: str = "") -> None:
    (bindir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n{out}')
    (bindir / name).chmod(0o755)


def _podman_setup(tmp_path, version="podman version 5.2.0"):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    (repo / "watches").mkdir(parents=True)
    (repo / "watches" / "watches.json").write_text("[]")
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()
    _stub(bindir, "podman", log, f'[ "$1" = --version ] && echo "{version}"\nexit 0\n')
    _stub(bindir, "systemctl", log)
    _stub(bindir, "quadlet", log, 'ls "$QUADLET_UNIT_DIRS" >> "' + str(log) + '"\n')
    home = tmp_path / "home"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home), "QUADLET_BIN": str(bindir / "quadlet")}
    return repo, env, home, log


def test_podman_install_renders_units_and_private_env(tmp_path):
    _bash()
    repo, env, home, log = _podman_setup(tmp_path)
    (repo / ".env").write_text('SEARXNG_SECRET=abc # Kommentar\nWEBSEARCH_UA="websearch/0.3 (ops@firma.de)"\n'
                               "EXA_API_KEY=k-1\nEMPTY=\nSEARXNG_VERSION=2026.11.1\n")
    p = subprocess.run(["bash", str(repo / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    units = home / ".config/containers/systemd"
    for f in units.iterdir():
        assert "@" not in re.sub(r"[\w.+-]+@[\w.-]+", "", f.read_text()), f.name     # alle Platzhalter ersetzt
    sx = (units / "websearch-searxng.container").read_text()
    assert "Image=docker.io/searxng/searxng:2026.11.1" in sx and "UserNS=keep-id:uid=977,gid=977" in sx
    assert "PublishPort=127.0.0.1:8888:8080" in sx and f"Volume={repo}/searxng:/etc/searxng:ro,Z" in sx
    conf = home / ".config/websearch"
    assert (conf / "searxng.env").read_text() == "SEARXNG_SECRET=abc\n"            # SearXNG: nur das Secret
    full = (conf / "podman.env").read_text()
    assert "WEBSEARCH_UA=websearch/0.3 (ops@firma.de)\n" in full and "EMPTY" not in full
    assert oct((conf / "podman.env").stat().st_mode & 0o777) == "0o600"
    mcp = (units / "websearch-mcp.container").read_text()
    assert "PublishPort=127.0.0.1:8765:8765" in mcp and "MCP_ALLOW_NO_AUTH=1" in mcp
    calls = log.read_text()
    assert f"podman build -t localhost/websearch:local {repo}" in calls
    assert "quadlet -dryrun -user" in calls and "websearch-mcp.container" in calls      # Units vorab geprüft
    assert "systemctl --user daemon-reload" in calls and "restart websearch-searxng.service" in calls
    assert "Umgebung in / environment in" in p.stdout
    assert "claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp" in p.stdout
    assert "--header" not in p.stdout and "MCP_AUTH_TOKEN" in p.stdout            # ohne Token: Hinweis
    for name in ("websearch-searxng.container", "websearch-mcp.container", "websearch-monitor.container"):
        text = (units / name).read_text()
        assert "PidsLimit=512" in text and "PodmanArgs=--memory" not in text, name          # memory nicht delegiert
    assert "memory" in p.stderr
    # mit Token: kein Opt-out mehr nötig; ohne SEARXNG_VERSION der getestete Tag statt latest
    (repo / ".env").write_text("SEARXNG_SECRET=abc\nMCP_AUTH_TOKEN=" + "x" * 40 + "\n")
    controllers = tmp_path / "cgroup.controllers"
    controllers.write_text("cpuset cpu io memory pids\n")
    p = subprocess.run(["bash", str(repo / "deploy/podman/install.sh"), "--no-start"], check=True, text=True,
                       capture_output=True, env={**env, "WEBSEARCH_CGROUP_CONTROLLERS": str(controllers),
                                                 "WEBSEARCH_MEMORY": "768m"})
    assert "MCP_ALLOW_NO_AUTH=0" in (units / "websearch-mcp.container").read_text()
    assert "PodmanArgs=--memory=768m" in (units / "websearch-mcp.container").read_text()
    assert ("T=$(python3 -I " + shlex.quote(str(repo / "deploy/lib/deploytool.py")) + " get "
            + shlex.quote(str(repo / ".env")) + " MCP_AUTH_TOKEN) && ") in p.stdout
    assert '--header "Authorization: Bearer $T"' in p.stdout
    assert "x" * 40 not in p.stdout                                                 # Token nie angezeigt
    assert "searxng:2026.11.5-3e95f6cd2" in (units / "websearch-searxng.container").read_text()


def test_podman_containers_are_hardened():
    for unit in ("websearch-searxng.container", "websearch-mcp.container", "websearch-monitor.container"):
        text = (PODMAN_DIR / unit).read_text()
        for key in ("NoNewPrivileges=true", "DropCapability=ALL", "ReadOnly=true", "PidsLimit=@PIDS@",
                "PodmanArgs=--memory=@MEMORY@"):
            assert re.search(rf"^{key}$", text, re.M), (unit, key)
    sx = (PODMAN_DIR / "websearch-searxng.container").read_text()
    assert "EnvironmentFile=@SXENVFILE@" in sx and ":/etc/searxng:ro," in sx


def test_podman_install_refuses_incomplete_setup(tmp_path):
    _bash()
    repo, env, _, _ = _podman_setup(tmp_path)
    shutil.rmtree(repo / "watches")
    p = subprocess.run(["bash", str(repo / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "websearch init" in p.stderr
    (repo / "watches").mkdir()
    (repo / "watches" / "watches.json").write_text("[]")
    (repo / ".env").write_text("SEARXNG_SECRET=\n")
    p = subprocess.run(["bash", str(repo / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "SEARXNG_SECRET" in p.stderr


def test_podman_install_checks_version_and_quadlet(tmp_path):
    _bash()
    repo, env, _, _ = _podman_setup(tmp_path, version="podman version 3.4.4")
    (repo / ".env").write_text("SEARXNG_SECRET=abc\n")
    p = subprocess.run(["bash", str(repo / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "3.4.4" in p.stderr and "4.7" in p.stderr
    shutil.rmtree(tmp_path / "bin")
    repo2, env, _, _ = _podman_setup(tmp_path / "b")
    (repo2 / ".env").write_text("SEARXNG_SECRET=abc\n")
    (tmp_path / "b" / "bin" / "quadlet").write_text('#!/bin/sh\necho "unsupported key X" >&2\nexit 1\n')
    p = subprocess.run(["bash", str(repo2 / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "unsupported key X" in p.stderr and "quadlet" in p.stderr.lower()


def test_podman_install_refuses_paths_podman_cannot_express(tmp_path):
    _bash()
    repo, env, _, _ = _podman_setup(tmp_path)
    bad = tmp_path / "my repo"
    shutil.move(str(repo), str(bad))
    (bad / ".env").write_text("SEARXNG_SECRET=abc\n")
    p = subprocess.run(["bash", str(bad / "deploy/podman/install.sh")], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "Leerzeichen" in p.stderr


def test_podman_help_is_the_comment_block():
    _bash()
    p = subprocess.run(["bash", str(PODMAN_DIR / "install.sh"), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "--uninstall" in p.stdout and "set -euo" not in p.stdout
    assert "Podman >= 4.7" in p.stdout


def test_quadlet_units_match_the_compose_setup():
    compose = (ROOT / "docker-compose.yml").read_text()
    for unit in ("websearch-mcp.container", "websearch-monitor.container"):
        text = (PODMAN_DIR / unit).read_text()
        assert "Image=@IMAGE@" in text and "HealthCmd=" in text and "Network=websearch.network" in text
        for line in re.findall(r"^Environment=(\w+)=", text, re.M):
            assert line in compose, f"{unit}: {line} fehlt in docker-compose.yml"
    for text in (PODMAN_DIR / u for u in os.listdir(PODMAN_DIR) if u.endswith(".container")):
        for port in re.findall(r"^PublishPort=(.*)$", text.read_text(), re.M):
            assert port.startswith("127.0.0.1:"), port                                # nie öffentlich
    assert all(p.startswith('"127.0.0.1:') for p in re.findall(r'^\s+- ("[\d.]+:\d+:\d+")', compose, re.M))


# =========================================================================== systemd (bwrap)

def test_systemd_units_point_to_existing_scripts():
    for unit in SYSTEMD_DIR.glob("*.service"):
        text = unit.read_text()
        (exe,) = re.findall(r'^ExecStart="@REPO@/([^"]+)"', text, re.M)
        assert (ROOT / exe).is_file() and os.access(ROOT / exe, os.X_OK), exe
        assert not re.search(r"^WorkingDirectory=", text, re.M)
    for script in (BWRAP_SH, SEARXNG_SH, PODMAN_DIR / "install.sh", SYSTEMD_DIR / "install.sh"):
        assert os.access(script, os.X_OK), script
        subprocess.run(["bash", "-n", str(script)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "deploy/lib/sandbox.sh")], check=True)


def _systemd_unquote(value: str) -> list[str]:
    return shlex.split(value.replace("%%", "\0").replace("$$", "$").replace("\0", "%"))


def test_systemd_install_quotes_paths_with_special_characters(tmp_path):
    _bash()
    repo = tmp_path / "my repo & co|100%"
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    (repo / "watches").mkdir()
    (repo / "watches" / "watches.json").write_text("[]")
    (repo / ".env").write_text("SEARXNG_SECRET=abc\n")
    src = _sx_src(tmp_path)
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()
    for name in ("systemctl", "bwrap"):
        _stub(bindir, name, log)
    unit_dir = tmp_path / "units"
    p = subprocess.run(["bash", str(repo / "deploy/systemd/install.sh"), "--no-start"], capture_output=True, text=True,
                       env={"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"), "WEBSEARCH_BWRAP_ALLOW_ROOT": "1",
                            "UNIT_DIR": str(unit_dir), "SEARXNG_SRC": str(src)})
    assert p.returncode == 0, p.stderr
    mon = (unit_dir / "websearch-monitor.service").read_text()
    (exec_line,) = re.findall(r"^ExecStart=(.*)$", mon, re.M)
    assert _systemd_unquote(exec_line) == [f"{repo}/deploy/bwrap/websearch-bwrap", "monitor",
                                           f"{repo}/watches/watches.json", "--loop"]
    sx = (unit_dir / "websearch-searxng.service").read_text()
    (env_line,) = re.findall(r"^Environment=(.*)$", sx, re.M)
    assert _systemd_unquote(env_line) == [f"SEARXNG_SRC={src}"]
    assert "systemctl --user daemon-reload" in log.read_text() and "enable" not in log.read_text()
    assert "claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp" in p.stdout
    p = subprocess.run(["bash", str(repo / "deploy/systemd/install.sh"), "--help"], capture_output=True, text=True)
    assert "set -euo" not in p.stdout and "--uninstall" in p.stdout


def test_every_environment_variable_is_documented_in_both_templates():
    code = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "websearch").glob("*.py"))
    used = set(re.findall(r'"((?:WEBSEARCH|MCP|REDDIT|SEARXNG|DDGS|EXA|PERPLEXITY|BRAVE|SERPER|TAVILY|LINKUP|'
                          r'MARGINALIA)_[A-Z0-9_]+)"', code))
    internal = {"WEBSEARCH_SANDBOX"}                 # setzt der bwrap-Wrapper selbst
    for template in (".env.example", ".env.example.de"):
        documented = set(re.findall(r"^#?\s?([A-Z][A-Z0-9_]+)=", (ROOT / template).read_text(encoding="utf-8"), re.M))
        assert not used - internal - documented, (template, sorted(used - internal - documented))
