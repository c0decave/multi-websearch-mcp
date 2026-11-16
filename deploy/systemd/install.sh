#!/usr/bin/env bash
# websearch ohne Container als systemd-User-Dienste in bubblewrap-Sandboxen  /  without containers, as
# systemd user services inside bubblewrap
#
#   deploy/systemd/install.sh              # Units installieren, aktivieren und starten / install, enable, start
#   deploy/systemd/install.sh --no-start   # nur installieren / install only
#   deploy/systemd/install.sh --uninstall  # Units entfernen (Daten bleiben) / remove units (data stays)
#   deploy/systemd/install.sh --print      # nur anzeigen, was geschrieben würde / show what would be written
#
# Vorher / first: bubblewrap installieren / install bubblewrap, 'websearch init',
#   SearXNG einmalig / once: deploy/bwrap/searxng-bwrap install
# Umgebung / environment: SEARXNG_SRC (wie bei / as for searxng-bwrap), UNIT_DIR (Standard / default
#   ~/.config/systemd/user)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo="$(cd "$here/../.." && pwd -P)"
tool="$repo/deploy/lib/deploytool.py"
unit_dir="${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
share="${XDG_DATA_HOME:-$HOME/.local/share}"
# wie searxng-bwrap: der alte Ort im Datenverzeichnis (schreibbar für die websearch-Sandbox) wird nie
# automatisch genutzt - nur mit ausdrücklichem SEARXNG_SRC
src="${SEARXNG_SRC:-$share/websearch-searxng}"
legacy="$share/websearch/searxng-src"
units=(websearch-searxng.service websearch-mcp.service websearch-monitor.service)
start=1 print_only=0
usage() { awk 'NR == 1 && /^#!/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }
die() { echo "install.sh: $*" >&2; exit 2; }
for a in "$@"; do
  case "$a" in
    --no-start) start=0 ;;
    --print) print_only=1 ;;
    --uninstall)
      systemctl --user disable --now "${units[@]}" 2>/dev/null || true
      for u in "${units[@]}"; do rm -f "$unit_dir/$u"; done
      systemctl --user daemon-reload; echo "Units entfernt / units removed"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unbekannte Option / unknown option: $a" ;;
  esac
done
if [[ $print_only -eq 0 && "$(id -u)" -eq 0 && "${WEBSEARCH_BWRAP_ALLOW_ROOT:-0}" != 1 ]]; then
  die "nicht als root: die bwrap-Wrapper starten als root nicht (WEBSEARCH_BWRAP_ALLOW_ROOT=1 erzwingt es)" \
      "/ not as root: the bwrap wrappers refuse to run as root (WEBSEARCH_BWRAP_ALLOW_ROOT=1 overrides)"
fi
[[ $print_only -eq 1 ]] || command -v bwrap >/dev/null || die "bwrap nicht gefunden - bubblewrap installieren / bwrap not found - install bubblewrap"
command -v python3 >/dev/null || die "python3 fehlt / python3 missing"
[[ -f "$repo/.env" ]] || die "$repo/.env fehlt - erst 'websearch init' / missing - run 'websearch init' first"
if [[ $print_only -eq 0 && ! -x "$src/.venv/bin/granian" ]]; then
  if [[ -z "${SEARXNG_SRC:-}" && -d "$legacy/.git" ]]; then
    die "SearXNG liegt nur am alten Ort $legacy (schreibbar für die websearch-Sandbox, wird nicht mehr genutzt) -" \
        "neu installieren: $repo/deploy/bwrap/searxng-bwrap install, danach rm -rf '$legacy'" \
        "/ SearXNG only exists in the old location (writable by the websearch sandbox, no longer used) - reinstall"
  fi
  die "SearXNG fehlt - erst / missing - first: $repo/deploy/bwrap/searxng-bwrap install"
fi
[[ -f "$repo/watches/watches.json" ]] || die "$repo/watches/watches.json fehlt (websearch init) / missing"
src="$(realpath -m "$src")"
port="$(python3 -I "$tool" get "$repo/.env" MCP_PORT 2>/dev/null || echo 8765)"
if [[ $print_only -eq 1 ]]; then
  for u in "${units[@]}"; do echo "### $unit_dir/$u"; python3 -I "$tool" render "$here/$u" REPO="$repo" SEARXNG_SRC="$src"; echo; done
  echo "### Claude Code:"
  python3 -I "$tool" mcp-add "$repo/.env" "$port"
  exit 0
fi
mkdir -p "$unit_dir"
for u in "${units[@]}"; do
  # Vorlage mit systemd-Escaping füllen (Pfade dürfen Leerzeichen, &, | ... enthalten)
  python3 -I "$tool" render "$here/$u" REPO="$repo" SEARXNG_SRC="$src" > "$unit_dir/$u.tmp"
  mv "$unit_dir/$u.tmp" "$unit_dir/$u"
done
systemctl --user daemon-reload
echo "✓ Units in / units in $unit_dir"
if [[ $start -eq 1 ]]; then
  systemctl --user enable --now "${units[@]}"
  echo "✓ gestartet / started: systemctl --user status ${units[*]}"
fi
echo "Claude Code:"
python3 -I "$tool" mcp-add "$repo/.env" "$port"
echo "Dauerhaft ohne Login / without login:  loginctl enable-linger \"${USER:-$(id -un)}\""
