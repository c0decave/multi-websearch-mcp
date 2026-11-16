#!/usr/bin/env bash
# websearch mit rootless Podman als systemd-Dienste (Quadlet) einrichten  /  set up as Podman Quadlet services
#
#   deploy/podman/install.sh             # Units installieren, Image bauen, Dienste starten
#                                        # / install units, build the image, start the services
#   deploy/podman/install.sh --no-start  # nur installieren / install only
#   deploy/podman/install.sh --print     # nur anzeigen, was geschrieben würde / only show what would be written
#   deploy/podman/install.sh --uninstall # Units entfernen (Volumes/Daten bleiben) / remove units (volumes stay)
#
# Nach jeder Änderung an .env erneut ausführen (die .env wird für Podman umgewandelt: Podman übernimmt Werte
# wörtlich, docker compose entfernt Anführungszeichen/Kommentare) / re-run after every change to .env.
# Voraussetzungen / requirements: Podman >= 4.7 (Quadlet mit VolumeName/NetworkName), systemd --user, python3;
# für Dienste ohne Login / for services without login: loginctl enable-linger
# Umgebung / environment: QUADLET_DIR (Standard / default ~/.config/containers/systemd),
#   WEBSEARCH_IMAGE (Standard / default localhost/websearch:local), SEARXNG_PORT (8888), MCP_PORT (8765),
#   QUADLET_BIN (Pfad zum Quadlet-Generator / path to the quadlet generator, für / for -dryrun),
#   WEBSEARCH_MEMORY (Speichergrenze pro Container / memory limit per container, Standard / default 1g),
#   WEBSEARCH_PIDS (Prozessgrenze / process limit, Standard / default 512)
# Container: NoNewPrivileges, alle Capabilities entzogen, Dateisystem nur lesbar (tmpfs für /tmp, /run),
# Prozess- und Speichergrenze, SearXNG-Einstellungen nur lesbar, SearXNG bekommt nur sein Secret
# / no new privileges, all capabilities dropped, read-only root fs, pids/memory limits, SearXNG gets only its
# secret. Die Speichergrenze entfällt (mit Hinweis), wenn systemd dem Nutzer den memory-Controller nicht
# delegiert / the memory limit is left out (with a note) if systemd does not delegate the memory controller.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo="$(cd "$here/../.." && pwd -P)"
tool="$repo/deploy/lib/deploytool.py"
quadlet_dir="${QUADLET_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd}"
conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/websearch"
image="${WEBSEARCH_IMAGE:-localhost/websearch:local}"
# getesteter SearXNG-Stand (wie docker-compose.yml / .env.example); SEARXNG_VERSION in .env überschreibt
default_searxng="2026.11.5-3e95f6cd2"
min_podman="4.7"
mode="install" start=1
units=(websearch.network websearch-data.volume searxng-cache.volume
       websearch-searxng.container websearch-mcp.container websearch-monitor.container)
services=(websearch-searxng.service websearch-mcp.service websearch-monitor.service)

usage() { awk 'NR == 1 && /^#!/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }
die() { echo "install.sh: $*" >&2; exit 2; }
for a in "$@"; do
  case "$a" in
    --no-start) start=0 ;;
    --print) mode="print" ;;
    --uninstall) mode="uninstall" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unbekannte Option / unknown option: $a" ;;
  esac
done

if [[ $mode == "uninstall" ]]; then
  systemctl --user stop "${services[@]}" 2>/dev/null || true
  for u in "${units[@]}"; do rm -f "$quadlet_dir/$u"; done
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Units entfernt / units removed. Daten bleiben / data kept: podman volume ls | grep websearch"
  exit 0
fi

command -v python3 >/dev/null || die "python3 fehlt / python3 missing"
[[ -f "$repo/.env" ]] || die "$repo/.env fehlt - erst 'websearch init' / missing - run 'websearch init' first"
[[ -f "$repo/watches/watches.json" ]] || die "$repo/watches/watches.json fehlt (websearch init) / missing"

# .env einmal lesen (Regeln wie docker compose, ohne sie auszuführen)
declare -A dotenv=()
while IFS= read -r -d '' k && IFS= read -r -d '' v; do dotenv["$k"]="$v"; done \
  < <(python3 -I "$tool" env "$repo/.env")
[[ -n "${dotenv[SEARXNG_SECRET]:-}" ]] || die "SEARXNG_SECRET in .env ist leer / is empty (websearch init)"

# Pfade landen in Volume=/EnvironmentFile= (durch : , und Leerzeichen getrennt, % ist ein systemd-Specifier)
# - solche Zeichen ablehnen statt auf Quadlets Quoting zu vertrauen
for p in "$repo" "$conf_dir"; do
  case "$p" in *[[:space:]:,\"\'\\%]*) ;; *) continue ;; esac
  die "Pfad $p enthält Leerzeichen oder eines von : , \" ' \\ % - in Podman-Volume-Optionen nicht möglich," \
    "Repo bzw. \$XDG_CONFIG_HOME verschieben / path contains whitespace or one of : , \" ' \\ % - not possible" \
    "in Podman volume options, move the repo"
done

envfile="$conf_dir/podman.env"
# SearXNG bekommt nur sein Secret - nicht die API-Keys und Tokens von websearch (wie bei docker compose)
sx_envfile="$conf_dir/searxng.env"
# wie docker compose: ohne Token startet der MCP-Server nicht - außer bewusst MCP_ALLOW_NO_AUTH=1 in .env
allow_no_auth=0
# Port auf dem Host: Umgebung vor .env vor 8765 (wie bei den systemd-Units)
mcp_port="${MCP_PORT:-${dotenv[MCP_PORT]:-8765}}"
searxng_port="${SEARXNG_PORT:-${dotenv[SEARXNG_PORT]:-8888}}"
for p in "$mcp_port" "$searxng_port"; do
  [[ "$p" =~ ^[1-9][0-9]*$ ]] && (( 10#$p >= 1 && 10#$p <= 65535 )) \
    || die "Port '$p' ungültig (MCP_PORT/SEARXNG_PORT: 1-65535) / invalid port (MCP_PORT/SEARXNG_PORT: 1-65535)"
done
v="${dotenv[MCP_ALLOW_NO_AUTH]:-0}"
[[ "${v,,}" =~ ^(1|true|yes|on|ja)$ ]] && allow_no_auth=1                # wie core.env_flag: ohne Groß/klein
if [[ -z "${dotenv[MCP_AUTH_TOKEN]:-}" && $allow_no_auth -eq 0 ]]; then
  echo "! MCP_AUTH_TOKEN fehlt in .env - websearch-mcp startet so nicht (websearch init erzeugt eines; bewusst" \
       "ohne Token: MCP_ALLOW_NO_AUTH=1) / MCP_AUTH_TOKEN missing in .env - websearch-mcp will not start" \
       "(websearch init creates one; deliberately without a token: MCP_ALLOW_NO_AUTH=1)" >&2
fi

memory="${WEBSEARCH_MEMORY:-1g}" pids="${WEBSEARCH_PIDS:-512}"
[[ "$memory" =~ ^[0-9]+[bkmgBKMG]?$ ]] || die "WEBSEARCH_MEMORY=$memory ungültig / invalid (z.B. / e.g. 1g)"
[[ "$pids" =~ ^[0-9]+$ ]] || die "WEBSEARCH_PIDS=$pids ungültig / invalid"
# Rootless-Podman kann --memory nur setzen, wenn systemd dem Nutzer den memory-Controller delegiert
# (cgroup v2); sonst startet der Container gar nicht -> Grenze dann weglassen
controllers="${WEBSEARCH_CGROUP_CONTROLLERS:-/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers}"
memory_ok=0
grep -qw memory "$controllers" 2>/dev/null && memory_ok=1

render() {   # Platzhalter einer Unit-Vorlage ersetzen (mit systemd-Escaping, z.B. % -> %%)
  python3 -I "$tool" render "$1" REPO="$repo" ENVFILE="$envfile" SXENVFILE="$sx_envfile" IMAGE="$image" \
    SEARXNG_VERSION="${dotenv[SEARXNG_VERSION]:-$default_searxng}" SEARXNG_PORT="$searxng_port" \
    MCP_PORT="$mcp_port" MCP_ALLOW_NO_AUTH="$allow_no_auth" MEMORY="$memory" PIDS="$pids" \
    | if [[ $memory_ok -eq 1 ]]; then cat; else sed '/^PodmanArgs=--memory=/d'; fi
}

if [[ $mode == "print" ]]; then
  for u in "${units[@]}"; do echo "### $quadlet_dir/$u"; render "$here/$u"; echo; done
  echo "### $envfile (aus / from $repo/.env, Werte ausgeblendet / values hidden)"
  python3 -I "$tool" podman-env "$repo/.env" | sed 's/=.*/=***/'
  exit 0
fi

# --- Podman/Quadlet vorhanden und neu genug?
command -v podman >/dev/null || die "podman nicht gefunden / not found"
ver="$(podman --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)"
[[ -n "$ver" ]] || die "Podman-Version nicht erkennbar / cannot determine the Podman version"
if [[ "$(printf '%s\n' "$min_podman" "$ver" | sort -V | head -n1)" != "$min_podman" ]]; then
  die "Podman $ver ist zu alt - Quadlet mit diesen Units braucht >= $min_podman (Ubuntu 22.04: 3.4, Debian 12:" \
      "4.3 -> docker compose oder deploy/bwrap nutzen) / Podman $ver is too old - these Quadlet units need" \
      ">= $min_podman (use docker compose or deploy/bwrap instead)"
fi
if [[ "$(printf '%s\n' 5.8.2 "$ver" | sort -V | head -n1)" != "5.8.2" ]]; then
  echo "Hinweis: Podman < 5.8.2 kann Healthchecks mit Anführungszeichen falsch auswerten ('unhealthy'," \
       "Dienste laufen trotzdem) / note: Podman < 5.8.2 may report quoted health checks as unhealthy" >&2
fi
quadlet_bin="${QUADLET_BIN:-}"
if [[ -z "$quadlet_bin" ]]; then
  for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet /usr/local/libexec/podman/quadlet; do
    [[ -x "$q" ]] && { quadlet_bin="$q"; break; }
  done
fi
if [[ -z "$quadlet_bin" ]] && [[ ! -e /usr/lib/systemd/user-generators/podman-user-generator ]] \
   && [[ ! -e /etc/systemd/user-generators/podman-user-generator ]]; then
  die "Quadlet-Generator nicht gefunden (Paket podman bzw. podman-quadlet?) / quadlet generator not found"
fi

if [[ $memory_ok -eq 0 ]]; then
  echo "Hinweis: memory-Controller nicht an den Nutzer delegiert ($controllers) - Container ohne Speichergrenze" \
       "/ note: memory controller not delegated - containers run without a memory limit" >&2
fi
mkdir -p "$quadlet_dir" "$conf_dir"
umask 077
python3 -I "$tool" podman-env "$repo/.env" > "$envfile.tmp" && mv "$envfile.tmp" "$envfile"
python3 -I "$tool" podman-env "$repo/.env" SEARXNG_SECRET > "$sx_envfile.tmp" && mv "$sx_envfile.tmp" "$sx_envfile"
umask 022
for u in "${units[@]}"; do render "$here/$u" > "$quadlet_dir/$u.tmp" && mv "$quadlet_dir/$u.tmp" "$quadlet_dir/$u"; done
echo "✓ Units in $quadlet_dir, Umgebung in / environment in $envfile"

# Units prüfen, bevor systemd sie (bei Fehlern still) ignoriert
if [[ -n "$quadlet_bin" ]]; then
  if ! out="$(QUADLET_UNIT_DIRS="$quadlet_dir" "$quadlet_bin" -dryrun -user 2>&1)"; then
    echo "$out" >&2
    die "Quadlet lehnt die Units ab (siehe oben) / quadlet rejected the units (see above)"
  fi
fi

if [[ "$image" == localhost/* ]]; then
  podman build -t "$image" "$repo"
fi
systemctl --user daemon-reload
if [[ $start -eq 1 ]]; then
  if ! systemctl --user restart "${services[@]}"; then
    die "Start fehlgeschlagen / start failed - Units fehlen nach daemon-reload? / units missing after" \
        "daemon-reload? Prüfen / check: ${quadlet_bin:-/usr/libexec/podman/quadlet} -dryrun -user;" \
        "journalctl --user -xeu websearch-searxng"
  fi
  echo "✓ gestartet / started:  systemctl --user status websearch-searxng websearch-mcp websearch-monitor"
  echo "  Prüfen / check:         podman exec websearch-mcp websearch doctor"
fi
echo "Claude Code:"
python3 -I "$tool" mcp-add "$repo/.env" "$mcp_port"
echo "Dauerhaft ohne Login / without login:  loginctl enable-linger \"${USER:-$(id -un)}\""
