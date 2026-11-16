"""Sprach-Hygiene: keine deutschen Texte im englischen Modus (und umgekehrt).

Ergänzt test_features.py::test_every_message_exists_in_both_languages_with_same_placeholders: Dort wird
geprüft, dass jeder t()-Aufruf zwei Texte hat; hier, dass außerhalb von t() keine deutschen Meldungen stehen
und dass der englische Text von t() nicht versehentlich deutsch ist."""
import ast
import re
from pathlib import Path

import pytest

SRC = sorted(Path(__file__).parent.parent.joinpath("websearch").glob("*.py"))

# typisch deutsche Wörter/Zeichen, die in englischen Meldungen nichts zu suchen haben
GERMAN = re.compile(r"(?i)[äöüÄÖÜß]|\b(lauf|läufe|nicht|kein|keine|keiner|muss|müssen|fehlt|oder|gesperrt|Treffer|erkannt|bitte|"
                    r"Fehler|Antwort|meinten|mindestens|zwischen|unerwartetes|Guthaben|aufgebraucht|braucht|"
                    r"Quelle|Quellen|Datei|Anfrage|Suchanfrage|Zeichen|Zahl|Seite|abgelehnt|internen|Netz|"
                    r"Weiterleitung|gekürzt|weitere|neue|wieder|Läufen|Stunden|Minuten)\b")


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


def _strings(node: ast.AST):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n


@pytest.mark.parametrize("path", SRC, ids=lambda p: p.name)
def test_no_german_outside_t_and_no_german_in_english_texts(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree)
    for node in ast.walk(tree):          # ausdrücklich deutsche Tabellen (z.B. _ARGPARSE_DE) sind gewollt
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.endswith("_DE") for t in node.targets):
            skip.update(id(s) for s in _strings(node.value))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args:
            skip.update(id(s) for s in _strings(node.args[0]))          # deutscher Text: darf deutsch sein
            for arg in node.args[1:]:
                for s in _strings(arg):
                    skip.add(id(s))
                    if GERMAN.search(s.value):
                        problems.append(f"{path.name}:{s.lineno}: deutsch im englischen Text: {s.value[:80]!r}")
    for s in _strings(tree):
        if id(s) not in skip and len(s.value) > 3 and GERMAN.search(s.value):
            problems.append(f"{path.name}:{s.lineno}: deutscher Text außerhalb von t(): {s.value[:80]!r}")
    assert not problems, "\n".join(problems)


def test_english_source_descriptions_cover_all_sources(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_LANG", "en")
    from websearch.providers import default_providers
    for name, p in default_providers().items():
        assert not GERMAN.search(p.describe()), (name, p.describe())
