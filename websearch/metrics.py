"""Prometheus-Metriken (Textformat 0.0.4).

Ausgabe über 'websearch metrics' (z.B. für den node_exporter-Textfile-Collector) und, beim MCP-Server
im HTTP-Modus, unter /metrics. Alle Zähler sind hostweit (SQLite-Zustandsdatei): CLI, Monitor und
MCP-Server zählen gemeinsam, egal welcher Prozess abgefragt wird.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Mapping

from . import __version__

if TYPE_CHECKING:
    from .engine import SearchEngine


def _label(value: object) -> str:
    """Label-Wert nach Prometheus-Regeln escapen."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _family(name: str, kind: str, help_text: str,
            samples: Iterable[tuple[Mapping[str, object], float]]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        lab = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
        number = repr(float(value)) if isinstance(value, float) and not float(value).is_integer() else str(int(value))
        lines.append(f"{name}{{{lab}}} {number}" if lab else f"{name} {number}")
    return lines


def render(engine: "SearchEngine") -> str:
    """Alle Metriken als Prometheus-Text (endet mit Zeilenumbruch). Die Engine muss geöffnet sein."""
    engine.flush_stats()             # Zähler dieses Prozesses mitnehmen
    status = engine.status()
    try:
        stats = engine.store.stats()
    except Exception:            # Zustandsdatei gerade gesperrt: Zähler diesmal weglassen, Rest liefern
        stats = []
    usage = engine.paid_usage()

    requests = [({"source": n, "state": s}, c) for n, s, c, _ in stats if not n.startswith("_search:")]
    seconds: dict[str, float] = {}
    for n, _, _, ms in stats:
        if not n.startswith("_search:"):
            seconds[n] = seconds.get(n, 0.0) + ms / 1000
    searches = [({"kind": n.split(":", 1)[1], "result": s}, c) for n, s, c, _ in stats if n.startswith("_search:")]

    lines: list[str] = []
    lines += _family("websearch_info", "gauge", "Version of websearch", [({"version": __version__}, 1)])
    lines += _family("websearch_searches_total", "counter",
                     "Searches by kind; result=empty means no result at all", searches)
    lines += _family("websearch_source_requests_total", "counter",
                     "Queries per source by outcome state (ok, cached, blocked, unavailable, ...)", requests)
    lines += _family("websearch_source_duration_seconds_total", "counter",
                     "Total time spent per source (divide by requests for the average)",
                     [({"source": n}, round(v, 3)) for n, v in sorted(seconds.items())])
    lines += _family("websearch_source_available", "gauge", "1 if the source is configured (e.g. API key set)",
                     [({"source": n}, int(s["available"])) for n, s in status.items()])
    lines += _family("websearch_source_blocked", "gauge", "1 if the source is currently paused because of a block",
                     [({"source": n}, int(s["blocked"])) for n, s in status.items()])
    lines += _family("websearch_source_retry_seconds", "gauge", "Seconds until a paused source is tried again",
                     [({"source": n}, s["retry_in_s"]) for n, s in status.items()])
    lines += _family("websearch_source_strikes", "gauge", "Consecutive blocks (the pause grows with each strike)",
                     [({"source": n}, s["strikes"]) for n, s in status.items()])
    if usage["day"] >= 0:
        lines += _family("websearch_paid_queries", "gauge", "Paid API queries in the current UTC period",
                         [({"period": "day"}, usage["day"]), ({"period": "month"}, usage["month"])])
    lines += _family("websearch_paid_limit", "gauge", "Configured hard limit for paid API queries",
                     [({"period": "day"}, usage["daily_limit"]), ({"period": "month"}, usage["monthly_limit"])])
    return "\n".join(lines) + "\n"
