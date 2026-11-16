""".env lesen wie docker compose - ohne Ausführung."""
import os

import pytest

from websearch import __main__ as cli
from websearch.envfile import load_env_file, main, parse_env_file, parse_line

TRICKY = r'''# Kommentar
export A=1
B="websearch/0.3 (ops@firma.de)"   # Kommentar nach Anführungszeichen
C='wörtlich $HOME \n'
D=a#b
E=a #kommentar
F="zeile\nzwei \"q\" \\"
G=
H="https://x.logic.azure.com/w?a=1&sig=2"
1BAD=x
I="offen
J=`touch pwned`
K=$A/x${NOPE_WS:-d}
L="q\$A"
'''


def test_dotenv_rules(tmp_path):
    p = tmp_path / ".env"
    p.write_text(TRICKY, encoding="utf-8")
    env = parse_env_file(p)
    assert env == {"A": "1", "B": "websearch/0.3 (ops@firma.de)", "C": "wörtlich $HOME \\n", "D": "a#b", "E": "a",
                   "F": 'zeile\nzwei "q" \\', "H": "https://x.logic.azure.com/w?a=1&sig=2", "J": "`touch pwned`",
                   "K": "1/xd", "L": "q$A"}
    assert not (tmp_path / "pwned").exists() and parse_line("   ") is None


def test_main_prints_nul_separated_pairs(tmp_path, capsys):
    p = tmp_path / ".env"
    p.write_text("A=1\nB='x y'\n")
    assert main([str(p)]) == 0
    assert capsys.readouterr().out == "A\x001\x00B\x00x y\x00"
    assert main([]) == 2


def test_cli_env_file_loads_without_overriding(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("WEBSEARCH_LANG=en\nWEBSEARCH_UA=from-file\n")
    monkeypatch.setenv("WEBSEARCH_UA", "from-env")
    monkeypatch.delenv("WEBSEARCH_LANG")
    monkeypatch.delenv("WEBSEARCH_LANG", raising=False)
    assert cli.main(["--env-file", str(p), "sources"]) == 0
    assert os.environ["WEBSEARCH_UA"] == "from-env" and os.environ["WEBSEARCH_LANG"] == "en"
    assert "Self-hosted SearXNG" in capsys.readouterr().out
    assert cli.main(["--env-file", str(tmp_path / "fehlt"), "sources"]) == 0        # fehlt: nur Warnung
    assert "does not exist" in capsys.readouterr().err
    (tmp_path / "dir.env").mkdir()
    assert cli.main(["--env-file", str(tmp_path / "dir.env"), "sources"]) == 2       # nicht lesbar: Fehler


def test_env_file_language_applies_to_help(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("WEBSEARCH_LANG=en\n")
    monkeypatch.delenv("WEBSEARCH_LANG")
    with pytest.raises(SystemExit):
        cli.main([f"--env-file={p}", "--help"])
    assert "Getting started" in capsys.readouterr().out
    monkeypatch.setenv("WEBSEARCH_LANG", "de")


def test_load_env_file_override(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("WEBSEARCH_X=neu\n")
    monkeypatch.setenv("WEBSEARCH_X", "alt")
    assert load_env_file(p) == [] and os.environ["WEBSEARCH_X"] == "alt"
    assert load_env_file(p, override=True) == ["WEBSEARCH_X"] and os.environ["WEBSEARCH_X"] == "neu"
    monkeypatch.delenv("WEBSEARCH_X")


def test_interpolation_like_compose_and_the_shell(tmp_path, monkeypatch):
    """Unquotiert und "doppelt": $VAR/${VAR}/${VAR:-x} werden ersetzt, 'einfach' und \\$ schützen - genau wie
    `set -a; . ./.env` in der Shell (und docker compose)."""
    import subprocess
    monkeypatch.setenv("WS_TEST_HOST", "proxy.local")
    monkeypatch.delenv("WS_TEST_NOPE", raising=False)
    content = ('A=http://$WS_TEST_HOST:3128\nB="${WS_TEST_HOST}/x"\nC=\'pa$word\'\nD="p\\$q"\n'
               "E=${WS_TEST_NOPE:-vorgabe}\nF=${A}/y\nG=a$1b\n")
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    env = parse_env_file(p)
    assert env == {"A": "http://proxy.local:3128", "B": "proxy.local/x", "C": "pa$word", "D": "p$q",
                   "E": "vorgabe", "F": "http://proxy.local:3128/y", "G": "a$1b"}
    shell = subprocess.run(["bash", "-c", "set -a; . ./.env; set +a; for k in A B C D E F; do printf '%s\\0' "
                            "\"${!k}\"; done"], cwd=tmp_path, capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "WS_TEST_HOST": "proxy.local"})
    assert shell.stdout.split("\0")[:6] == [env[k] for k in "ABCDEF"]
