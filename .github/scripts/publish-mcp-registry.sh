#!/usr/bin/env bash
# server.json in die offizielle MCP Registry eintragen (registry.modelcontextprotocol.io).
# Läuft in GitHub Actions (Login per OIDC, braucht "permissions: id-token: write").
# Aufruf: .github/scripts/publish-mcp-registry.sh <version>   (z.B. 0.3.0rc2, ohne "v")
#
# Die Registry prüft die Eigentümerschaft über die Zeile "mcp-name: io.github.c0decave/multi-websearch-mcp"
# in der README-Beschreibung des PyPI-Pakets - das Paket muss also schon auf PyPI liegen.
set -euo pipefail

version="${1:?Version fehlt / version missing}"
publisher_version="v1.8.6"        # festgelegt; beim Aktualisieren auch die Prüfsumme anpassen
publisher_sha256="1fbe93fc0783c2394d8012c8d6e50a59e98cb2cc2d216264cc69f405d0e21340"   # mcp-publisher_linux_amd64.tar.gz

# server.json muss genau die zu veröffentlichende Version beschreiben
python3 - "$version" <<'PY'
import json, sys
want = sys.argv[1]
d = json.load(open("server.json"))
got = {d["version"]} | {p["version"] for p in d["packages"]}
if got != {want}:
    sys.exit(f"::error::server.json beschreibt {sorted(got)}, veröffentlicht wird {want}")
PY

tmp="$(mktemp -d)"
curl -fsSL -o "$tmp/mcp-publisher.tar.gz" \
  "https://github.com/modelcontextprotocol/registry/releases/download/${publisher_version}/mcp-publisher_linux_amd64.tar.gz"
echo "${publisher_sha256}  $tmp/mcp-publisher.tar.gz" | sha256sum -c -
tar xzf "$tmp/mcp-publisher.tar.gz" -C "$tmp" mcp-publisher
publisher="$tmp/mcp-publisher"

"$publisher" validate
"$publisher" login github-oidc

# PyPI braucht nach dem Upload manchmal einen Moment, bis die neue Version in der JSON-API steht
for attempt in 1 2 3 4 5 6; do
  if "$publisher" publish; then
    echo "MCP Registry: io.github.c0decave/multi-websearch-mcp $version veröffentlicht"
    exit 0
  fi
  echo "Versuch $attempt fehlgeschlagen, neuer Versuch in 30 s ..."
  sleep 30
done
echo "::error::Eintrag in die MCP Registry fehlgeschlagen"
exit 1
