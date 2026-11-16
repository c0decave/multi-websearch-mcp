# Gemeinsamer Sandbox-Aufbau für deploy/bwrap/websearch-bwrap und deploy/bwrap/searxng-bwrap (wird per
# "source" geladen, nicht direkt ausgeführt).
#
# Aufbau (Reihenfolge zählt - spätere Mounts überdecken frühere):
#   / nur lesbar; /dev, /proc neu; /tmp, /var/tmp, /run (+ /var/run, falls kein Symlink), /home, /root,
#   /srv, /mnt, /media und $HOME leer (tmpfs) - also auch keine fremden Home-Verzeichnisse und Datenträger. Aus /run kommt nur zurück, was die Namensauflösung braucht (Ziel von /etc/resolv.conf, z.B.
#   /run/systemd/resolve) - damit sind D-Bus-Session-Bus, systemd-User-Manager, ssh-/gpg-Agent und
#   Podman-Socket unter /run/user/$UID unsichtbar (ein nur lesbarer Socket ließe sich trotzdem verbinden).
#   Eigene User-/PID-/IPC-/UTS-/Cgroup-Namespaces, keine weiteren User-Namespaces (bwrap >= 0.8),
#   Netz geteilt, alle Capabilities weg, neue Session, endet mit dem Aufrufer.
#   seccomp (x86_64/aarch64): keine AF_UNIX-Sockets (abstrakte Sockets hängen am geteilten Netz-Namespace),
#   kein io_uring.
# Alle bwrap-Optionen gehen per --args über eine Pipe an bwrap, nicht über die Kommandozeile: sonst stünden
# --setenv-Werte (Tokens, API-Keys) in /proc/<pid>/cmdline, das jeder Nutzer des Rechners lesen kann.
#
# Schalter / switches: WEBSEARCH_BWRAP_SECCOMP=0 (seccomp aus), WEBSEARCH_BWRAP_DISABLE_USERNS=0 (für setuid-bwrap
# ohne unprivilegierte User-Namespaces), WEBSEARCH_BWRAP_ALLOW_ROOT=1 (als root starten - root kann in der
# Sandbox z.B. /etc/shadow lesen).

sb_args=()
sb_seccomp=0
sb_userns=0
sb_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
sb_tool="$sb_lib/deploytool.py"
sb_python="${sb_python:-python3}"

# Home-Verzeichnis, aufgelöst (Symlinks) - leer, wenn keins (oder /)
sb_home=""
if [[ -n "${HOME:-}" && "$HOME" != "/" && -d "$HOME" ]]; then
  sb_home="$(cd "$HOME" && pwd -P)"
  [[ "$sb_home" == "/" ]] && sb_home=""
fi

# Verzeichnisse, die die Sandbox leer überlagert (aufgelöst; /home ist z.B. auf Fedora Silverblue ein Symlink)
sb_hidden_dirs=(/tmp /var/tmp /run /var/run)
for _d in /home /root /srv /mnt /media; do
  _r="$(realpath -e "$_d" 2>/dev/null)" || continue
  [[ -d "$_r" && "$_r" != "/" ]] && sb_hidden_dirs+=("$_r")
done
[[ -n "$sb_home" ]] && sb_hidden_dirs+=("$sb_home")
unset _d _r

# liegt $2 in $1 (oder ist es)?
sb_within() { [[ "$2" == "$1" || "$2" == "${1%/}"/* ]]; }
# überschneiden sich $1 und $2 (gleich, oder eines enthält das andere)?
sb_overlaps() { sb_within "$1" "$2" || sb_within "$2" "$1"; }
# würde ein Mount von $1 das versteckte Home-Verzeichnis (ganz) wieder sichtbar machen?
sb_exposes_home() {
  [[ "$1" == "/" ]] && return 0
  [[ -n "$sb_home" ]] && sb_within "$1" "$sb_home" && return 0
  [[ -n "${HOME:-}" && "$HOME" != "/" ]] && sb_within "$1" "$HOME" && return 0
  return 1
}
# liegt $1 in einem Bereich, den die Sandbox leer überlagert (muss dann eigens eingeblendet werden)?
sb_hidden() {
  local p="$1" a
  for a in "${sb_hidden_dirs[@]}"; do
    sb_within "$a" "$p" && return 0
  done
  return 1
}

# Schreibbar im Home-Verzeichnis nur ausdrücklich erlaubte Orte (Allow-List): ~/.local/share/websearch*,
# ~/.cache/websearch* (bzw. unter $XDG_DATA_HOME/$XDG_CACHE_HOME). Fast jeder andere Ort in ~ wird von
# irgendeinem Programm ausgeführt oder als Konfiguration gelesen (~/.config, ~/.local/bin, ~/go/bin,
# ~/.local/share/pipx, uv/tools, nvim, fish/vendor_conf.d, direnv ...).
sb_home_allowed() {
  local p="$1" h b
  for h in "$sb_home" "${HOME:-}"; do
    [[ -n "$h" && "$h" != "/" ]] || continue
    sb_within "$h" "$p" || continue
    for b in "$h/.local/share" "$h/.cache" "${XDG_DATA_HOME:-}" "${XDG_CACHE_HOME:-}"; do
      [[ -n "$b" ]] || continue
      b="$(realpath -m "$b")"
      [[ "$p" == "$b"/websearch* ]] && return 0
    done
    return 1
  done
  return 0                                       # außerhalb von $HOME: nur die übrigen Prüfungen
}

# als root nicht starten (root liest in der Sandbox /etc/shadow, Host-Schlüssel ...), außer ausdrücklich erlaubt
sb_check_root() {
  [[ "$(id -u)" != "0" || "${WEBSEARCH_BWRAP_ALLOW_ROOT:-0}" == "1" ]] && return 0
  echo "${sb_name:-bwrap}: nicht als root starten (root kann auch in der Sandbox z.B. /etc/shadow lesen) -" \
       "als normaler Nutzer ausführen oder WEBSEARCH_BWRAP_ALLOW_ROOT=1 setzen / refusing to run as root - run" \
       "as a normal user or set WEBSEARCH_BWRAP_ALLOW_ROOT=1" >&2
  exit 2
}

# bwrap-Version prüfen: --disable-userns gibt es ab 0.8
sb_detect() {
  local bin="$1" ver major minor
  sb_userns=0
  if [[ "${WEBSEARCH_BWRAP_DISABLE_USERNS:-1}" != "0" ]]; then
    if ver="$("$bin" --version 2>/dev/null)" && [[ "$ver" =~ ([0-9]+)\.([0-9]+) ]]; then
      major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}"
      if (( major > 0 || minor >= 8 )); then sb_userns=1
      else
        echo "${sb_name:-bwrap}: Hinweis: bwrap $major.$minor < 0.8 - Sandbox kann weitere User-Namespaces anlegen" \
             "/ note: bwrap < 0.8 cannot disable nested user namespaces" >&2
      fi
    else
      sb_userns=1                       # nur --print ohne bwrap: vollständigen Aufruf zeigen
    fi
  fi
  sb_seccomp=0
  if [[ "${WEBSEARCH_BWRAP_SECCOMP:-1}" != "0" ]]; then
    if "$sb_python" -I "$sb_tool" seccomp >/dev/null 2>&1; then
      sb_seccomp=1
    else
      echo "${sb_name:-bwrap}: Warnung: kein seccomp-Filter ($(uname -m) nicht unterstützt oder $sb_python fehlt) -" \
           "abstrakte Unix-Sockets (z.B. X11) bleiben über das geteilte Netz erreichbar / warning: no seccomp" \
           "filter (unsupported architecture or python missing) - abstract unix sockets stay reachable" >&2
    fi
  fi
}

# Grundgerüst der Sandbox (Hostname $1)
sb_base() {
  local host="$1" t d
  sb_args=(--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run)
  [[ -d /var/tmp && ! -L /var/tmp ]] && sb_args+=(--tmpfs /var/tmp)
  [[ -d /var/run && ! -L /var/run ]] && sb_args+=(--tmpfs /var/run)
  # fremde Home-Verzeichnisse, /root, Server-Daten und eingehängte Datenträger (benötigte Pfade darin werden
  # danach einzeln wieder eingeblendet)
  for d in "${sb_hidden_dirs[@]:4}"; do
    [[ "$d" == "$sb_home" ]] && continue
    sb_args+=(--tmpfs "$d")
  done
  # DNS: /etc/resolv.conf zeigt oft nach /run (systemd-resolved, NetworkManager, resolvconf) -> dieses
  # Verzeichnis nur lesbar zurück (Verzeichnis statt Datei: die Datei wird beim Netzwechsel ersetzt)
  if t="$(realpath -e /etc/resolv.conf 2>/dev/null)"; then
    d="$(dirname "$t")"
    if [[ "$d" == /run/* || "$d" == /var/run/* ]] && [[ "$d" != /run/user/* ]]; then
      sb_args+=(--ro-bind "$d" "$d")
    fi
  fi
  if [[ -d /run/systemd/resolve && "${d:-}" != /run/systemd/resolve ]]; then
    sb_args+=(--ro-bind /run/systemd/resolve /run/systemd/resolve)
  fi
  # NixOS: Programme und /etc-Einträge verweisen auf /run/current-system (nur ein Symlink in den Store)
  [[ -L /run/current-system ]] && sb_args+=(--symlink "$(readlink /run/current-system)" /run/current-system)
  [[ -n "$sb_home" ]] && sb_args+=(--tmpfs "$sb_home")
  sb_args+=(--unshare-all --share-net)
  [[ $sb_userns -eq 1 ]] && sb_args+=(--unshare-user --disable-userns)
  sb_args+=(--die-with-parent --new-session --cap-drop ALL --hostname "$host")
}

# Geheimnisse in einem eingeblendeten Verzeichnis überdecken: .env, .env.* (außer .env.example*), .git
sb_mask_dir() {
  local dir="$1" f
  [[ -d "$dir" ]] || return 0
  for f in "$dir"/.env "$dir"/.env.*; do
    [[ -e "$f" || -L "$f" ]] || continue
    case "${f##*/}" in .env.example|.env.example.*) continue ;; esac
    [[ -d "$f" ]] && { sb_args+=(--tmpfs "$f"); continue; }
    sb_args+=(--ro-bind /dev/null "$f")
  done
  # .git/config kann Zugangsdaten enthalten (https://user:token@...)
  if [[ -d "$dir/.git" ]]; then sb_args+=(--tmpfs "$dir/.git")
  elif [[ -f "$dir/.git" ]]; then sb_args+=(--ro-bind /dev/null "$dir/.git")
  fi
}

# einzelne Datei überdecken (z.B. die geladene .env), falls sie existiert
sb_mask_file() {
  local f
  f="$(realpath -e "$1" 2>/dev/null)" || return 0
  [[ -f "$f" ]] && sb_args+=(--ro-bind /dev/null "$f")
}

# Wert für --print: geheime Variablen ganz, Zugangsdaten in URLs (user:pass@, ?token=...) unkenntlich
sb_shown_value() {
  local k="$1" v="$2" re
  if [[ "$k" =~ (KEY|SECRET|TOKEN|PASSWORD|PASSWD|WEBHOOK|CREDENTIAL) ]]; then
    printf '%s' "***"; return
  fi
  # bis zum letzten @ der Authority (Passwörter dürfen @ enthalten)
  if [[ "$v" =~ ^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@([^/?#@]*)(.*)$ ]]; then
    v="${BASH_REMATCH[1]}***@${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
  fi
  # Query-Parameter mit geheimen Namen (?sig=, &token=, &key= ...)
  re='^(.*[?&;]([A-Za-z0-9_.-]*([Ss][Ii][Gg]|[Tt][Oo][Kk][Ee][Nn]|[Kk][Ee][Yy]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss]|[Aa][Uu][Tt][Hh]|[Cc][Oo][Dd][Ee])[A-Za-z0-9_.-]*)=)([^&;#*][^&;#]*)(.*)$'
  while [[ "$v" =~ $re ]]; do
    v="${BASH_REMATCH[1]}***${BASH_REMATCH[5]}"
  done
  printf '%s' "$v"
}

# bwrap-Aufruf anzeigen (wie ausgeführt, die Optionen aus --args ausgeschrieben), Geheimnisse maskiert
sb_print() {
  local bin="$1" i=0 n=${#sb_args[@]} shown=()
  shift
  while (( i < n )); do
    if [[ "${sb_args[i]}" == "--setenv" ]] && (( i + 2 < n )); then
      shown+=(--setenv "${sb_args[i+1]}" "$(sb_shown_value "${sb_args[i+1]}" "${sb_args[i+2]}")")
      (( i += 3 ))
    else
      shown+=("${sb_args[i]}"); (( i += 1 ))
    fi
  done
  [[ $sb_seccomp -eq 1 ]] && shown+=(--seccomp FD)
  printf '%q ' "$bin" "${shown[@]}" -- "$@"; echo
  echo "# Optionen gehen per 'bwrap --args FD' über eine Pipe (nicht sichtbar in /proc/*/cmdline)" \
       "/ options are passed via 'bwrap --args FD' through a pipe" >&2
}

# bwrap starten: Optionen NUL-getrennt über FD 3, seccomp-Programm über FD 4 - nur der Befehl selbst steht
# in der Kommandozeile. Endet bwrap vorzeitig, scheitert das Schreiben (EPIPE) - Fehlermeldung dazu verworfen. sb_exec ersetzt die Shell, sb_run kehrt zurück.
sb_exec() {
  local bin="$1"; shift
  if [[ $sb_seccomp -eq 1 ]]; then
    exec "$bin" --args 3 --seccomp 4 -- "$@" 3< <(printf '%s\0' "${sb_args[@]}" 2>/dev/null) \
      4< <("$sb_python" -I "$sb_tool" seccomp 2>/dev/null)
  fi
  exec "$bin" --args 3 -- "$@" 3< <(printf '%s\0' "${sb_args[@]}" 2>/dev/null)
}
sb_run() {
  local bin="$1"; shift
  if [[ $sb_seccomp -eq 1 ]]; then
    "$bin" --args 3 --seccomp 4 -- "$@" 3< <(printf '%s\0' "${sb_args[@]}" 2>/dev/null) \
      4< <("$sb_python" -I "$sb_tool" seccomp 2>/dev/null)
  else
    "$bin" --args 3 -- "$@" 3< <(printf '%s\0' "${sb_args[@]}" 2>/dev/null)
  fi
}

# Hilfetext: der Kommentarblock am Anfang des Skripts (ohne Shebang) bis zur ersten Nicht-Kommentarzeile
sb_usage() { awk 'NR == 1 && /^#!/ { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"; }
