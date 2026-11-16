"""Zweisprachige Meldungen (Deutsch/Englisch) ohne Übersetzungskatalog.

Jede Meldung steht an der Aufrufstelle in beiden Sprachen:

    raise InputError(t("Anfrage ist leer", "query is empty"))
    log.warning(t("Quelle %s pausiert", "source %s paused"), name)       # Logging formatiert selbst
    print(t("{n} Treffer", "{n} results").format(n=len(hits)))

So kann keine Übersetzung fehlen oder veralten; tests/test_features.py prüft per AST, dass jeder
t()-Aufruf zwei String-Literale mit denselben Platzhaltern bekommt.

Sprache (pro Aufruf ermittelt, damit Tests und Prozesse sie per Umgebung umstellen können):
  1. WEBSEARCH_LANG = de | en
  2. sonst LC_ALL / LC_MESSAGES / LANG: beginnt mit "de" -> Deutsch
  3. sonst Englisch
"""

from __future__ import annotations

import os

LANGS = ("de", "en")


def lang() -> str:
    """Aktive Sprache der Meldungen: "de" oder "en"."""
    explicit = os.environ.get("WEBSEARCH_LANG", "").strip().lower()
    if explicit:
        return "de" if explicit.startswith("de") else "en"
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").strip()
        if value:
            return "de" if value.lower().startswith("de") else "en"
    return "en"


def check_lang() -> None:
    """Beim Start: unbekannte WEBSEARCH_LANG-Werte melden statt still Englisch zu nehmen."""
    from .core import InputError
    explicit = os.environ.get("WEBSEARCH_LANG", "").strip().lower()
    if explicit and explicit.split("_")[0].split("-")[0] not in LANGS:
        raise InputError(t(f"WEBSEARCH_LANG={explicit!r} ist ungültig - erlaubt: de, en",
                           f"WEBSEARCH_LANG={explicit!r} is invalid - allowed: de, en"))


def t(de: str, en: str) -> str:
    """Meldung in der aktiven Sprache."""
    return de if lang() == "de" else en
