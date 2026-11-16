"""Offline-Tests der Kommandozeile (Exit-Codes, JSON, Ausgabe) mit Fake-Quellen."""
import json

import pytest

from websearch import __main__ as cli
from websearch.core import BlockedError, Result
from websearch.engine import SearchEngine, TierPolicy
from websearch.providers import Provider


class Fake(Provider):
    min_interval = 0
    kinds = ("web", "news", "ref")

    def __init__(self, name, n=2, block=False):
        self.name, self._n, self._block = name, n, block
        super().__init__()

    async def search(self, client, query, n, lang, kind):
        if self._block:
            raise BlockedError("captcha")
        return [Result(f"T{i}", f"https://{self.name}.example/{i}", snippet="Ausschnitt " * 30, source=self.name,
                       rank=i, published="2026-11-01T00:00:00Z") for i in range(1, self._n + 1)]


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    def install(**provs):
        def factory(*a, **k):
            return SearchEngine(provs, policy=TierPolicy(primary={x: list(provs) for x in ("web", "news", "ref")}),
                                cache_path=tmp_path / "c.db")
        monkeypatch.setattr(cli, "SearchEngine", factory)
    return install


def test_search_text_output_and_exit_ok(fake_engine, capsys):
    fake_engine(good=Fake("good"), bad=Fake("bad", block=True))
    assert cli.main(["search", "test"]) == 0
    out = capsys.readouterr().out
    assert "https://good.example/1" in out and "Ausschnitt" in out and "[2026-11-01]" in out
    assert "blocked" in out


def test_search_json_is_valid(fake_engine, capsys):
    fake_engine(good=Fake("good"))
    assert cli.main(["search", "test", "--json", "-n", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["results"]) == 1 and data["sources"]["good"]["state"] == "ok"


def test_exit_code_when_all_sources_fail(fake_engine):
    fake_engine(bad=Fake("bad", block=True))
    assert cli.main(["search", "test"]) == 1


def test_many_from_stdin_json_array(fake_engine, capsys, monkeypatch):
    import io
    fake_engine(good=Fake("good"))
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n# Kommentar\n\nb\n"))
    assert cli.main(["many", "-", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["query"] for d in data] == ["a", "b"]


@pytest.mark.parametrize("argv", [["search", ""], ["search", "x", "-n", "0"], ["many", "/gibt/es/nicht"],
                                  ["search", "x", "-s", "unbekannt"]])
def test_usage_errors_exit_2(fake_engine, argv, capsys):
    fake_engine(good=Fake("good"))
    assert cli.main(argv) == 2
    assert capsys.readouterr().err.startswith("Fehler:")


def test_status_and_reset(fake_engine, capsys):
    fake_engine(bad=Fake("bad", block=True))
    cli.main(["search", "x"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert "PAUSIERT" in capsys.readouterr().out
    assert cli.main(["status", "--reset", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"]["bad"]["blocked"] is False


def test_sources_lists_all(capsys):
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    for name in ("searxng", "google_news", "gdelt", "exa", "perplexity"):
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    from websearch import __version__
    assert f"websearch {__version__}" in capsys.readouterr().out


def test_monitor_interval_is_validated(tmp_path, capsys):
    wf = tmp_path / "w.json"
    wf.write_text('[{"name": "a", "query": "q"}]')
    assert cli.main(["monitor", str(wf), "--loop", "--interval", "0"]) == 2
    assert "--interval" in capsys.readouterr().err


def test_unknown_log_level_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("WEBSEARCH_LOG_LEVEL", "LAUT")
    assert cli.main(["sources"]) == 0
    assert "unbekannt" in capsys.readouterr().err


def test_many_validates_deadline(fake_engine, tmp_path):
    fake_engine(good=Fake("good"))
    q = tmp_path / "q.txt"
    q.write_text("a\n")
    assert cli.main(["many", str(q), "--deadline", "0"]) == 2


def test_many_with_non_utf8_file_is_usage_error(tmp_path, capsys):
    q = tmp_path / "q.txt"
    q.write_bytes("Straße\n".encode("latin-1"))
    assert cli.main(["many", str(q)]) == 2
    assert "UTF-8" in capsys.readouterr().err


def test_websearch_mcp_entry_point_starts_the_mcp_server(monkeypatch):
    """uvx websearch-mcp [--env-file F] [--http] == websearch [--env-file F] mcp [--http]"""
    import websearch.__main__ as M
    seen = []
    monkeypatch.setattr(M, "run", lambda: seen.append(list(M.sys.argv[1:])))
    for argv, want in (([], ["mcp"]), (["--http"], ["mcp", "--http"]),
                       (["--env-file", ".env", "-v", "--http"], ["--env-file", ".env", "-v", "mcp", "--http"])):
        monkeypatch.setattr(M.sys, "argv", ["websearch-mcp", *argv])
        M.run_mcp()
        assert seen[-1] == want
