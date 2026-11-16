"""Gemeinsame Test-Einstellungen."""

import pytest


@pytest.fixture(autouse=True)
def _german_messages(monkeypatch):
    """Die meisten Tests prüfen deutsche Meldungen: Sprache fest auf Deutsch, unabhängig vom System.
    Tests für Englisch setzen WEBSEARCH_LANG=en selbst."""
    monkeypatch.setenv("WEBSEARCH_LANG", "de")
