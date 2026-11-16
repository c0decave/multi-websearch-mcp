# websearch – parallele, captcha-arme Websuche für Agents und Monitoring

**Deutsch** · [English](README.md) · [Quickstart (EN)](docs/QUICKSTART.md) · [Changelog](CHANGELOG.md)

> **Vorabversion 0.3.0rc1:** Funktionsumfang komplett, aber noch im Pilotbetrieb. Fehler und Wünsche bitte als
> [Issue](https://github.com/c0decave/web-search-mcp/issues) melden.

Kostenlose Quellen als Rückgrat (selbst gehostetes SearXNG, offizielle Feeds und APIs), optional günstige
Bezahl-APIs als Reserve. Mit Kommandozeile, Python-API, MCP-Server für KI-Agents (Web-, News-, Bild- und
Videosuche **und** Seitenabruf) und einem Monitoring-Dienst, der nur neue Treffer meldet – per Webhook,
Slack, Discord, Teams, Telegram oder E-Mail. Meldungen auf Deutsch oder Englisch.

Hintergrund, Engine-Vergleich, Preise und Empfehlung: **[ANALYSE.md](ANALYSE.md)**

## Inhalt

1. [Schnellstart in 5 Minuten](#schnellstart-in-5-minuten)
2. [Installation](#installation)
3. [Kommandozeile](#kommandozeile)
4. [MCP für KI-Agents](#mcp-für-ki-agents)
5. [Monitoring und Benachrichtigungen](#monitoring-und-benachrichtigungen)
6. [Quellen](#quellen)
7. [Python-API und JSON-Format](#python-api-und-json-format)
8. [Konfiguration](#konfiguration)
9. [Betrieb, Metriken und Fehlersuche](#betrieb-metriken-und-fehlersuche)
10. [Entwicklung, Tests und Release](#entwicklung-tests-und-release)

## Schnellstart in 5 Minuten

Voraussetzung: Docker mit Compose ≥ 2.24 (oder Podman, oder nur bubblewrap – siehe
[Installation](#installation)) und Python ≥ 3.10 für `websearch init` (geht auch ohne, siehe unten).

```bash
cd websearch                                           # Repo-Verzeichnis (geklont oder entpackt)
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
websearch init --contact du@eure-firma.de --lang de   # .env mit Secret, Kontakt, Sprache; watches/ anlegen
docker compose up -d                                   # SearXNG :8888, MCP-Server :8765, Monitoring
docker compose exec websearch-mcp websearch doctor     # prüft alles und sagt, was zu tun ist
docker compose exec websearch-mcp websearch search "open source metasearch" -n 5
T=$(.venv/bin/python -m websearch.envfile --get MCP_AUTH_TOKEN .env) && \
  claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"
```

`websearch init` fragt ohne Optionen interaktiv nach Kontakt und Sprache und findet das Projektverzeichnis
selbst (das nächste mit `docker-compose.yml`, auch aus einem Unterordner – wie `docker compose`). Eine neue
`.env` bekommt ein zufälliges `SEARXNG_SECRET` **und ein `MCP_AUTH_TOKEN`** – auch auf `127.0.0.1` kann sonst
jeder lokale Nutzer den Dienst (und eure API-Keys) verwenden; der `claude mcp add`-Befehl oben liest das Token
aus der `.env`. In einer vorhandenen `.env` ändert `--contact`/`--lang` nur diese Zeilen; `--force` erzeugt sie
aus der aktuellen Vorlage neu und übernimmt alle bisherigen Werte. Vorher wird immer eine Sicherung
`.env.bak-<Zeitstempel>` angelegt (von git und Docker ignoriert), nichts wird überschrieben. Es erkennt Docker
Compose, `podman compose` und `podman-compose` und zeigt die passenden Befehle. Ohne Python auf dem Host:
`cp .env.example.de .env`, darin `SEARXNG_SECRET` und `MCP_AUTH_TOKEN` (je `openssl rand -hex 32`) und den
Kontakt in `WEBSEARCH_UA` eintragen, `mkdir -p watches && cp watches.example.json watches/watches.json`; beim
`claude mcp add` dann das Token aus der `.env` direkt einsetzen (`--header "Authorization: Bearer <token>"`).

`websearch doctor` prüft Konfiguration, Kontakt im User-Agent, SearXNG (erreichbar, JSON-API frei, Engines
mit Captchas), pausierte Quellen, Absicherung des MCP-Servers und macht je eine Web- und News-Testsuche
(`--offline` ohne Testsuche, `--json` maschinenlesbar). Zu jedem Problem steht der Lösungsschritt dabei.

**Wichtig:** Am besten auf einem Rechner mit **Büro- oder Heim-IP** betreiben, nicht in der Cloud
(ANALYSE.md, Abschnitt 3): Rechenzentrums-IPs bekommen deutlich mehr Captchas.

## Installation

### Docker (empfohlen)

```bash
docker compose up -d                        # baut das Image lokal aus requirements.lock
docker compose ps                           # alle drei Dienste sollten "healthy" werden
docker compose logs -f websearch-monitor    # neue Treffer (JSON-Zeilen) und Meldungen
```

- MCP-Server und Monitor starten erst, wenn SearXNG gesund ist. Alle Dienste haben Healthchecks; der
  MCP-Server unter `/healthz`, der Monitor schreibt nach jedem Durchlauf einen Heartbeat.
- Alle Ports sind nur an `127.0.0.1` gebunden. **Nicht ungeschützt ins Netz stellen** (SearXNG-Limiter ist
  aus); für Fernzugriff auf den MCP-Server `MCP_AUTH_TOKEN` setzen ([MCP](#mcp-für-ki-agents)).
- Gehärtet: alle Container ohne Capabilities, mit `no-new-privileges` und nur lesbarem Dateisystem; SearXNG läuft
  als UID 977 statt root und bindet `searxng/` nur lesend ein (die Datei bleibt auf dem Host ganz normal
  editierbar). Wer von einer älteren Version kommt, deren SearXNG `./searxng` schon übereignet hat:
  einmal `sudo chown -R "$USER" searxng`.
- Fertiges Image statt lokalem Build ([Releases](https://github.com/c0decave/web-search-mcp/releases)):
  `WEBSEARCH_IMAGE=ghcr.io/c0decave/web-search-mcp:0.3.0rc1` in `.env`, dann
  `docker compose pull && docker compose up -d --no-build` (linux/amd64 und linux/arm64).
- Update: `git pull && docker compose build && docker compose pull --ignore-buildable && docker compose up -d`.
  Das SearXNG-Image ist auf die getestete Version festgelegt (`SEARXNG_VERSION` in `.env`, z.B. `latest`).

### Podman

**Mit compose:** `podman compose up -d` (Podman ≥ 5) bzw. `podman-compose up -d` nutzt dieselbe
`docker-compose.yml` (die Bind-Mounts tragen schon `:Z` für SELinux). Rootless gehört `./searxng` danach einer
Unter-UID, falls eine ältere Version es übereignet hat (`podman unshare chown -R 0:0 searxng`); besser gleich
Quadlet nutzen.

**Als systemd-Dienste (Quadlet, rootless, empfohlen für Podman):**

```bash
websearch init --contact du@eure-firma.de      # wie oben
deploy/podman/install.sh                       # Units nach ~/.config/containers/systemd, Image bauen, starten
systemctl --user status websearch-searxng websearch-mcp websearch-monitor
podman exec websearch-mcp websearch doctor
loginctl enable-linger "$USER"                 # Dienste auch ohne Anmeldung
```

- Braucht **Podman ≥ 4.7** (Ubuntu 22.04/Debian 12 sind zu alt → docker compose oder bwrap). `install.sh` prüft
  die Version und die Units vorab mit `quadlet -dryrun`.
- Gleiche Dienste, Ports (nur `127.0.0.1`), Healthchecks und Volumes wie mit compose. Die Container laufen mit
  `NoNewPrivileges`, ohne Capabilities und mit nur lesbarem Dateisystem; die SearXNG-Einstellungen sind nur lesbar
  eingebunden (auf dem Host in `searxng/settings.yml` bearbeiten). Podman < 5.8.2 kann Healthchecks fälschlich als
  „unhealthy“ anzeigen (Quoting-Fehler in Quadlet); die Dienste laufen trotzdem.
- `UserNS=keep-id:uid=977`: Im SearXNG-Container erscheint der eigene Nutzer als `searxng` – `searxng/` bleibt
  ganz normal editierbar.
- Podman übernimmt `.env`-Werte wörtlich (auch Anführungszeichen). `install.sh` schreibt deshalb eine bereinigte
  Kopie nach `~/.config/websearch/podman.env` (Rechte 0600); SearXNG bekommt nur sein Secret. **Nach jeder
  Änderung an `.env` `deploy/podman/install.sh` erneut ausführen.** `--print` zeigt vorher alles an, ohne zu
  schreiben; `--uninstall` entfernt die Units (Volumes bleiben).

### bubblewrap (ohne Container)

Für Rechner ohne Docker/Podman: [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) sperrt jeden
Prozess in eine Sandbox:
- Alles ist nur lesbar; `$HOME`, `/tmp`, `/var/tmp` und `/run` sind leer überlagert (SSH-Schlüssel, Browserprofile,
  D-Bus-/ssh-agent-/Podman-Sockets unsichtbar). Sichtbar sind nur das Repo (lesend, ohne `.env*`/`.git`), die
  Python-Umgebung, an `monitor`/`many` übergebene Dateien und das Datenverzeichnis `~/.local/share/websearch`
  (schreibbar, zugleich Arbeitsverzeichnis). Suchbegriffe werden nie als Pfade behandelt.
- Eigene Namespaces, keine weiteren User-Namespaces (bwrap ≥ 0.8), Netz geteilt, keine Capabilities. seccomp sperrt
  Unix-Sockets außer verbundenen Stream-Paaren (`socketpair(SOCK_STREAM)`, braucht asyncio) sowie io_uring –
  abstrakte Sockets im geteilten Netz (X11, alte D-Bus-Busse) sind damit unerreichbar. Leere Umgebung: nur Variablen,
  die websearch liest (aus `.env` und der Umgebung, nicht `SEARXNG_SECRET`). Alle bwrap-Optionen gehen über eine
  Pipe – Tokens stehen nie in `/proc/*/cmdline`.
- Schreibbar sind nur das Datenverzeichnis und `--rw`-Pfade; in `$HOME` nur `~/.local/share/websearch*` und
  `~/.cache/websearch*` – alles andere dort braucht `--rw-unsafe PFAD` (fast jeder andere Ort in `~` wird von
  irgendeinem Programm ausgeführt oder als Konfiguration gelesen). `WEBSEARCH_CACHE`, `WEBSEARCH_MONITOR_DB`,
  `WEBSEARCH_MONITOR_OUT` und `WEBSEARCH_HEARTBEAT` müssen ins Datenverzeichnis zeigen (relative Pfade liegen darin)
  oder in ein `--rw`-Verzeichnis.
- SearXNG liegt in `~/.local/share/websearch-searxng` (außerhalb des schreibbaren Datenverzeichnisses);
  `install`/`update` führen auch git und pip in der Sandbox aus und nutzen standardmäßig den getesteten Commit
  (`SEARXNG_REF=master` für den neuesten Stand).
- Auch `/home`, `/root`, `/srv`, `/mnt` und `/media` sind ausgeblendet; als root starten die Wrapper nicht
  (`WEBSEARCH_BWRAP_ALLOW_ROOT=1` erzwingt es). Eine alte SearXNG-Installation in
  `~/.local/share/websearch/searxng-src` wird nicht mehr automatisch genutzt – mit `searxng-bwrap install` neu
  installieren.
- Restrisiko: das geteilte Netz – die Sandbox erreicht lokale TCP-Dienste (der SSRF-Schutz gilt nur für die
  Abrufe von websearch selbst).

```bash
sudo apt install bubblewrap                       # oder dnf/pacman install bubblewrap
websearch init --contact du@eure-firma.de
deploy/bwrap/searxng-bwrap install                # einmalig: SearXNG-Quellcode + venv (in der Sandbox, fester Commit)
# entweder: alles als systemd-User-Dienste (SearXNG, MCP auf 127.0.0.1:8765, Monitor)
deploy/systemd/install.sh
# oder: von Hand im Vordergrund (nicht zusammen mit den Diensten - beide nutzen Port 8888)
deploy/bwrap/searxng-bwrap run                    # Terminal 1: SearXNG auf 127.0.0.1:8888
deploy/bwrap/websearch-bwrap doctor               # Terminal 2: beliebiger websearch-Befehl, liest .env selbst
```

**Nach Änderungen an `.env` oder `searxng/settings.yml`:** systemd-Dienste: `systemctl --user restart
websearch-searxng websearch-mcp websearch-monitor` (die Wrapper lesen `.env` bei jedem Start neu;
`deploy/systemd/install.sh` muss nur nach einem Umzug von Repo oder SearXNG erneut laufen). Im Vordergrund: mit
Strg+C beenden und den Befehl neu starten. Änderungen an `watches.json` übernimmt der Monitor im laufenden Betrieb.

- `websearch-bwrap --print …` zeigt den kompletten bwrap-Aufruf (Geheimnisse ausgeblendet); `--ro`/`--rw PFAD`
  blenden weitere Pfade ein, `--rw-unsafe PFAD` wie `--rw` auch an anderen Orten in `$HOME` (mit Warnung). Der Wrapper nutzt `<repo>/.venv` (aus dem Schnellstart) oder `WEBSEARCH_PYTHON`.
- Für Claude Code über stdio, mit `.env`: `claude mcp add -s user websearch -- /pfad/zum/repo/deploy/bwrap/websearch-bwrap mcp`.
- Braucht User-Namespaces (Standard auf aktuellen Distributionen; in manchen Containern/VMs gesperrt) und für
  `--disable-userns` bwrap ≥ 0.8 (ältere Versionen laufen mit Hinweis ohne diese Option).

### Mit pip (ohne Docker)

```bash
pip install websearch-mcp                   # Paketname auf PyPI (sobald veröffentlicht); Befehl: websearch
# oder aus dem Repo: python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
export SEARXNG_URL=http://127.0.0.1:8888    # eigene SearXNG-Instanz; ohne sie laufen die übrigen Quellen
set -a; . ./.env; set +a                    # optional: eigene .env laden (API-Keys, Webhook, ...)
websearch doctor
websearch search "open source metasearch" -n 5
```

Die pip-Warnung `lxml … does not provide the extra 'html_clean'` (pip < 23.3) ist harmlos – das Paket wird direkt
installiert. `ddgs` ist auf `<10` gepinnt, weil das Tool dessen Interna nutzt. `requirements.lock` enthält die exakt
getesteten Versionen für Python 3.10–3.13 (`pip install -r requirements.lock && pip install --no-deps .`).

## Kommandozeile

```bash
websearch search "Wärmepumpe Förderung" -l de          # Websuche (mit Textausschnitten)
websearch search "EU AI Act" -k news -l de --days 7    # News der letzten 7 Tage, neuester Tag zuerst
websearch search "Reciprocal Rank Fusion" -k ref       # Nachschlagen (Wikipedia + SearXNG)
websearch search "Nordlicht" -k images                 # Bilder (Seite + direkte Bild-URL)
websearch search "rust ownership" -k videos            # Videos (mit Laufzeit)
websearch search "x" -k ref -s wikipedia,bing_rss --json   # feste Quellen (ohne Stufen), JSON
websearch search "x" --fresh --no-paid --deadline 10   # ohne Cache, nie bezahlen, 10 s Budget
websearch many queries.txt -n 5                        # eine Anfrage pro Zeile (# = Kommentar), parallel
cat queries.txt | websearch many - --json              # stdin; --json = ein gültiges JSON-Array
websearch fetch https://example.org/artikel            # Seite als Markdown (auch Google-News-Links)
websearch monitor watches/watches.json [--loop]        # nur neue Treffer melden
websearch status                                       # Quellen, Pausen (hostweit) und SearXNG-Zustand
websearch status --reset                               # Pausen/Tempo-Slots dieses Tools zurücksetzen
websearch sources                                      # alle Quellen mit Beschreibung, Sucharten, Stufen
websearch doctor                                       # Einrichtung prüfen, mit Lösungshinweisen
websearch metrics                                      # Prometheus-Metriken (hostweit)
websearch init                                         # .env anlegen (siehe Schnellstart)
websearch -v search "x"                                # mit Log-Ausgaben (-vv = Debug)
```

`websearch BEFEHL --help` erklärt alle Optionen.

**Sprache:** `WEBSEARCH_LANG=de` oder `en`; ohne Angabe gilt die Systemsprache (`LC_ALL`/`LC_MESSAGES`/`LANG`
beginnt mit `de` → Deutsch, sonst Englisch). Das gilt für Hilfe, Ausgaben, Fehlermeldungen, Logs und
Benachrichtigungen.

**Zeitfilter `--days N`** (1–3650, nicht für `-k ref`): Quellen, die selbst filtern können, bekommen den
Filter mitgeschickt (SearXNG `time_range`, Google News `when:`, GDELT, Hacker News, ddgs außer Mojeek, Reddit,
die Bezahl-APIs außer Perplexity). Danach wird jeder Treffer anhand seines Datums nachgeprüft; undatierte Treffer
bleiben nur von Quellen, die genau auf diese Spanne gefiltert haben – bei Bildern und Videos (fast nie datiert)
genügt das Zeitfenster der Quelle (z.B. „Woche“ für `--days 3`).

**Exit-Codes:** `0` ok · `1` keine Treffer und alle Quellen ausgefallen (bzw. Abruf fehlgeschlagen, bzw.
`doctor` hat Fehler gefunden, bzw. ein einzelner `monitor`-Lauf – ohne `--loop`, z.B. per Cron –, nach dem jeder
Watch blind ist; das greift schon ab dem ersten blinden Lauf, die Blind-Meldung erst nach `WEBSEARCH_BLIND_AFTER`) · `2` ungültige Eingabe/Konfiguration (auch ein verbotenes Abrufziel) ·
`130` abgebrochen (Strg+C). `-v`/`-q` haben Vorrang vor `WEBSEARCH_LOG_LEVEL`.

**Stufen** (Details in ANALYSE.md, Abschnitt 5):
1. Kostenlose Hauptquellen.
2. Kostenlose Fallbacks, nur wenn zu wenig kam. Suchmaschinen, die SearXNG gerade als gesperrt meldet,
   werden übersprungen.
3. Bezahl-APIs (nur Web/News/Nachschlagen) nur, wenn fast nichts kam **und** das an Sperren/Fehlern lag. Sie
   werden der Reihe nach probiert, bis **eine** normal antwortet (auch mit 0 Treffern – eine zweite Bezahl-API
   findet einen seltenen Begriff auch nicht; leere Bezahl-Antworten werden gecacht), immer mit höchstens 10
   Treffern (standardmäßig im günstigsten Tarifmodus) und innerhalb einer harten, hostweiten Grenze
   (`WEBSEARCH_PAID_DAILY_LIMIT` = 100, `WEBSEARCH_PAID_MONTHLY_LIMIT` = 1000). Zusätzlich im Dashboard jedes
   Anbieters ein Ausgabelimit setzen (Brave bucht nach dem Freiguthaben weiter ab). Mit `--days` fragt die Stufe nur
   Bezahl-APIs, die genau auf diese Spanne filtern (Exa, Linkup, Brave immer; Serper/Tavily nur bei 1, 7, 31 oder 366
   Tagen; Perplexity nie) – und probiert die nächste, wenn eine Antwort nach dem Datumsfilter leer ist.

## MCP für KI-Agents

Eine der folgenden Varianten wählen (alle heißen `websearch`; `-s user` = in allen Projekten verfügbar, ohne
`-s` gilt der Eintrag nur im aktuellen Verzeichnis):

```bash
# stdio (liest die .env selbst; vorhandene Umgebungsvariablen gewinnen)
claude mcp add -s user websearch -- /pfad/zum/repo/.venv/bin/python -m websearch --env-file /pfad/zum/repo/.env mcp
# HTTP-Dienst (Docker/Podman/systemd) mit dem Token aus der .env
T=$(/pfad/zum/repo/.venv/bin/python -m websearch.envfile --get MCP_AUTH_TOKEN /pfad/zum/repo/.env) && \
  claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"
# stdio in der bubblewrap-Sandbox
claude mcp add -s user websearch -- /pfad/zum/repo/deploy/bwrap/websearch-bwrap mcp
# stdio aus PyPI (sobald veröffentlicht)
claude mcp add -s user websearch -- uvx --from websearch-mcp websearch --env-file /pfad/zur/.env mcp
```

Einen Eintrag ändern (anderes Token, stdio statt HTTP): erst `claude mcp remove -s user websearch`, dann neu
hinzufügen – `claude mcp add` lehnt einen vorhandenen Namen ab. Nach einem neuen Token die Dienste neu starten
(siehe [Fehlersuche](#betrieb-metriken-und-fehlersuche)).

Auf Rechnern mit mehreren Nutzern: `claude mcp add … --header` übergibt das Token als Argument, es ist dann für
etwa eine Sekunde in der Prozessliste sichtbar. Dort den Header lieber direkt in `~/.claude.json` eintragen (oder
stdio nutzen – das braucht kein Token).

Im Repo liegt außerdem eine `.mcp.json` (Server `websearch-repo`, stdio mit `--env-file .env`): Claude Code bietet
ihn an, wenn es im Repo gestartet wird (beim ersten Start einmal freigeben) – praktisch für die Entwicklung.

| Tool | Zweck |
|---|---|
| `web_search(query, max_results=10, language="auto", days=None)` | Websuche über alle Stufen |
| `news_search(query, max_results=15, language="auto", days=None)` | aktuelle Nachrichten, neuester Tag zuerst (innerhalb eines Tages nach Relevanz) |
| `search_many(queries[≤20], max_results=8, kind="web"\|"news"\|"ref", language, days)` | mehrere Teilfragen parallel |
| `image_search(query, max_results=10, language="auto", days=None)` | Bilder: `url` (Seite), `image` (Bild), `thumbnail` |
| `video_search(query, max_results=10, language="auto", days=None)` | Videos: `url`, `thumbnail`, `duration`, `published` |
| `fetch_url(url, max_chars=20000)` | Seite als Markdown lesen (löst Google-News-Links auf) |
| `source_status()` | welche Quellen gerade pausiert sind |

Alle Tools haben Ein- und Ausgabe-Schemas (Grenzen, Enums). Fehler wie eine leere Anfrage oder ein
verbotenes Abrufziel kommen als lesbare Tool-Fehler beim Modell an. Bild- und Videosuche nutzen nie
Bezahl-APIs und laufen mit SearXNG-Safe-Search „moderat“ (`safe_search: 1` in `searxng/settings.yml`);
`WEBSEARCH_MCP_PAID=0` verbietet Agents die Bezahl-APIs ganz.

**`fetch_url`-Schutz:** nur http(s); Ziele im internen Netz (localhost, 10.x, 192.168.x, 169.254.x,
fc00::/7 …) werden abgelehnt, auch nach Weiterleitungen; robots.txt wird respektiert; max. 3 MB,
1 Abruf/s pro Host (hostweit), 6 h Cache. Intranet bewusst erlauben: `WEBSEARCH_FETCH_ALLOW_PRIVATE=1`.

**Zugriffsschutz des HTTP-Servers:**
- **Token:** `websearch init` erzeugt `MCP_AUTH_TOKEN` (64 Hex-Zeichen). Jede Anfrage an `/mcp` und `/metrics`
  braucht dann `Authorization: Bearer <token>` (sonst `401`, Vergleich in konstanter Zeit); `/healthz` bleibt
  offen. Neues Token: `sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=$(openssl rand -hex 32)/" .env` (in `.env` selbst
  wird `$(…)` nicht ausgeführt). Tokens unter 32 Zeichen ergeben eine Warnung; ohne Token warnt der Server auch
  auf `127.0.0.1` (jeder lokale Nutzer könnte ihn verwenden).
- Port nur an `127.0.0.1`, dazu DNS-Rebinding-Schutz über `Host`-/`Origin`-Header (`MCP_ALLOWED_HOSTS`, Standard
  `127.0.0.1:*,localhost:*,[::1]:*`). Der Host-Header-Schutz ist keine Zugangskontrolle (Nicht-Browser-Clients
  können ihn fälschen) – die übernimmt das Token.
- **Fail closed:** Ist der Server über den eigenen Rechner hinaus erreichbar (`MCP_HOST` keine Loopback-Adresse –
  im Container immer `0.0.0.0` – oder ein öffentlicher Name in `MCP_ALLOWED_HOSTS`) und kein Token gesetzt, startet
  er nicht. Bewusst ohne Token im Container (Port nur auf `127.0.0.1`, Einzelplatzrechner): `MCP_ALLOW_NO_AUTH=1`
  in `.env`; ein öffentlicher Name ohne Token wird immer abgelehnt.
- **Fernzugriff:** Port weiterhin nur auf `127.0.0.1` lassen und einen TLS-Reverse-Proxy (Caddy, nginx, Traefik)
  davorsetzen; Token setzen und den öffentlichen Namen mit und ohne Port in `MCP_ALLOWED_HOSTS` aufnehmen, z.B.
  `…,suche.example.org,suche.example.org:*`. Docker veröffentlicht Ports an der Host-Firewall (ufw) vorbei – den
  Port deshalb nicht direkt auf `0.0.0.0` freigeben.

```bash
claude mcp add -s user --transport http websearch https://suche.example.org/mcp \
  --header "Authorization: Bearer $MCP_AUTH_TOKEN"
```

## Monitoring und Benachrichtigungen

`watches/watches.json` (Beispiel: `watches.example.json`):

```json
[
  {"name": "eu-ai-act", "query": "\"AI Act\" EU", "kind": "news", "every_min": 60, "n": 20},
  {"name": "searxng-releases", "query": "SearXNG", "kind": "news", "every_min": 240,
   "sources": ["hackernews", "google_news", "bing_rss"]},
  {"name": "brand-web", "query": "\"Example Inc\"", "kind": "web", "every_min": 1440}
]
```

| Feld | Pflicht | Standard | Bedeutung |
|---|---|---|---|
| `name` | ja | – | eindeutiger Name |
| `query` | ja | – | Suchbegriff |
| `kind` | | `news` | `web`, `news`, `ref`, `images` oder `videos` |
| `lang` | | `auto` | z.B. `de` |
| `every_min` | | `60` | Prüfabstand in Minuten (mind. 5) |
| `n` | | `20` | Top-n, die gemeldet werden (1–100) |
| `sources` | | Stufen | feste Quellenliste |
| `deadline` | | `60` | Zeitbudget je Lauf in s (5–600) |
| `max_age_days` | | `3` | ältere datierte Treffer gelten nicht als „neu“ |

Die Beispiel-Watches vor dem ersten Start durch eigene ersetzen (`websearch init` erinnert daran).
Unbekannte Felder und unbekannte Quellnamen werden beim Start mit Tippfehler-Hinweis abgelehnt (Exit-Code 2).
Im `--loop`-Betrieb wird die Datei bei jeder Änderung neu geladen; ist sie fehlerhaft, laufen die alten
Watches weiter (Fehler im Log).

**Was gemeldet wird** (stdout als JSON-Zeilen, optional Benachrichtigung und JSONL-Datei):
- `{"type": "hits", "watch": …, "query": …, "new": [Treffer]}` – neue Treffer
- `{"type": "health", "watch": …, "status": "blind" | "recovered", …}` – nach `WEBSEARCH_BLIND_AFTER`
  (Standard 3) „blinden“ Läufen in Folge, und wenn es wieder geht. Blind heißt: Eine Quelle ist wirklich
  ausgefallen (Sperre, Störung, SearXNG mit lauter gesperrten Web-Engines, abgestürzter Lauf), und keine Quelle
  hat belastbar geantwortet. Treffer zählen immer. „ok, 0 Treffer“ zählt nie von gescrapten Seiten (mögliche
  stille Sperre) und nie von Hacker News/GDELT/Reddit (die liefern für fast jeden Markennamen 0); von anderen
  Quellen zählt es, wenn die Quelle für diesen Watch schon einmal geliefert hat – oder, solange der Watch noch
  keine Vorgeschichte hat, von jeder breiten Quelle (SearXNG, Google News, Wikipedia …). Nur übersprungene oder
  gedrosselte Quellen machen einen Lauf nie blind; Watches, deren feste Quellen ihre Anfrage nie suchen können,
  werden schon beim Laden abgelehnt.

Treffer gelten erst als gesehen, wenn sie ausgegeben wurden (stdout/JSONL) – scheitert die Ausgabe, geht
nichts verloren. Eine fehlgeschlagene Benachrichtigung wird zwischengespeichert und in jedem Durchlauf erneut
versucht (Wartezeit bis 1 h, verworfen nach 24 h); jeder Versand hat ein Gesamtlimit von 15 s, und nach einem
Fehlschlag gehen die übrigen Meldungen des Durchlaufs direkt in die Warteschlange. Zustellung „mindestens
einmal“: Jede Meldung hat eine `id`, und jeder Treffer in `new` hat eine eigene, stabile `id` (Watch +
normalisierte URL) – beim Empfänger über die Treffer-`id` deduplizieren.

Gegen Lastspitzen (ein Captcha-Auslöser) laufen höchstens `WEBSEARCH_MONITOR_CONCURRENCY` Watches (Standard 2)
gleichzeitig, im `--loop`-Betrieb starten die Watches gestaffelt, und jedes Intervall bekommt ±10 % Zufall.
Jeder Watch läuft nach eigenem Zeitplan – ein langsamer Watch hält die anderen nicht auf.

**Regeln gegen Fehlalarme:**
- Der erste Lauf mit Treffern legt nur die Basislinie an (`--alert-first` meldet sofort).
- Abgefragt wird ein größerer Pool, min(100, max(3n, 30)); gemeldet werden nur neue Treffer in den Top-n.
- Treffer, die nur von einer Quelle kommen, die zum ersten Mal liefert, werden still übernommen.

**Benachrichtigungen** – genau ein Ziel, gewählt mit `WEBSEARCH_WEBHOOK_FORMAT`:

| Format | Ziel | Darstellung | Pflicht-Einstellungen |
|---|---|---|---|
| `json` (Standard) | n8n, eigene Dienste | Payload unverändert als JSON | `WEBSEARCH_WEBHOOK` |
| `text` | ntfy | Klartext, Titel als `?title=` (Umlaute ok) | `WEBSEARCH_WEBHOOK` |
| `slack` | Slack Incoming Webhook | `{"text": …}` mit `<url\|Titel>`-Links | `WEBSEARCH_WEBHOOK` |
| `discord` | Discord-Webhook | Links `[Titel](url)`, max. 2000 Zeichen, keine @everyone-Pings, keine Link-Vorschau | `WEBSEARCH_WEBHOOK` |
| `teams` | Microsoft Teams (Workflows / Power Automate) | Adaptive Card 1.4, max. ~28 KB | `WEBSEARCH_WEBHOOK` |
| `telegram` | Telegram-Bot | HTML, max. 4096 Zeichen, keine Link-Vorschau | `WEBSEARCH_WEBHOOK`, `WEBSEARCH_TELEGRAM_CHAT_ID` |
| `email` | SMTP-Server | Klartext + HTML, Betreff `[websearch] <watch>: 3 neue Treffer` | `WEBSEARCH_SMTP_HOST`, `WEBSEARCH_SMTP_TO`, Absender |

Zu lange Meldungen werden an ganzen Treffern gekürzt und enden mit `… +N weitere`. Erfolg zählt nur bei
HTTP 2xx (Telegram zusätzlich `"ok": true`, E-Mail: vom Server angenommen); sonst landet die Meldung in der
Outbox. E-Mails bekommen aus der Meldungs-`id` eine feste `Message-ID`. Fehlende oder ungültige Einstellungen
bricht `websearch monitor` beim Start mit einer klaren Meldung ab. Logs zeigen von Webhook-URLs nur Schema +
Host – Tokens erscheinen nie im Log.

```bash
# Discord: Kanal -> Integrationen -> Webhooks -> URL kopieren
WEBSEARCH_WEBHOOK_FORMAT=discord
WEBSEARCH_WEBHOOK=https://discord.com/api/webhooks/<id>/<token>

# Teams: Kanal -> ... -> Workflows -> "Send webhook alerts to a channel" -> URL kopieren
# (die alten Office-365-Connectors gibt es nicht mehr)
WEBSEARCH_WEBHOOK_FORMAT=teams
WEBSEARCH_WEBHOOK="https://<…>.logic.azure.com/workflows/<…>/triggers/manual/paths/invoke?api-version=2016-06-01&sig=<…>"   # Anführungszeichen wegen &

# Telegram: Bot bei @BotFather anlegen, Bot in den Chat holen; Chat-ID z.B. über getUpdates
WEBSEARCH_WEBHOOK_FORMAT=telegram
WEBSEARCH_WEBHOOK=https://api.telegram.org/bot<TOKEN>/sendMessage
WEBSEARCH_TELEGRAM_CHAT_ID=-1001234567890      # oder @kanalname

# ntfy (Format text)
WEBSEARCH_WEBHOOK_FORMAT=text
WEBSEARCH_WEBHOOK=https://ntfy.sh/mein-thema

# E-Mail (WEBSEARCH_WEBHOOK wird nicht gebraucht)
WEBSEARCH_WEBHOOK_FORMAT=email
WEBSEARCH_SMTP_HOST=smtp.example.org
WEBSEARCH_SMTP_PORT=587                         # Standard 587, bei TLS=ssl 465
WEBSEARCH_SMTP_TLS=starttls                     # starttls | ssl | none
WEBSEARCH_SMTP_USER=alerts@example.org
WEBSEARCH_SMTP_PASSWORD=…
WEBSEARCH_SMTP_FROM="websearch <alerts@example.org>"   # Standard: WEBSEARCH_SMTP_USER
WEBSEARCH_SMTP_TO="ich@example.org,team@example.org"   # in Anführungszeichen, falls die .env per Shell geladen wird
```

## Quellen

`websearch sources` zeigt alle Quellen mit Beschreibung, Sucharten und Stufen.

| Quelle | Sucharten | Art |
|---|---|---|
| `searxng` | web, news, ref, images, videos | eigene SearXNG-Instanz (Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia, YouTube …) |
| `bing_rss`, `google_news` | web/news/ref bzw. news | offizielle RSS-Feeds |
| `hackernews`, `gdelt`, `wikipedia`, `marginalia` | news bzw. ref/web | offizielle, freie APIs |
| `reddit` | web, news | offizielle API, kostenlose OAuth-App nötig (siehe unten) |
| `ddgs:yahoo`, `ddgs:duckduckgo`, `ddgs:brave` | web, news, ref (DuckDuckGo auch images, videos) | Fallback über die `ddgs`-Bibliothek |
| `ddgs:startpage`, `ddgs:mojeek` | web, news, ref | nur explizit per `-s` (Proof-of-Work-Captcha) |
| `exa`, `linkup`, `tavily`, `brave_api`, `perplexity`, `serper` | web, news, ref | optional mit API-Key, nur Reserve |

**Reddit** nutzt die offizielle API mit OAuth („application-only“): für „script“-Apps kostenlos, ca. 100
Anfragen pro Minute. Link-Beiträge zeigen auf den verlinkten Artikel, Text-Beiträge auf den Reddit-Beitrag;
NSFW wird nie geliefert; Reddits Rate-Limit-Header werden beachtet. App anlegen:
1. Mit einem Reddit-Konto <https://www.reddit.com/prefs/apps> öffnen, „create another app…“ wählen.
2. Name (z.B. `websearch`), Typ **script**, „redirect uri“ `http://localhost:8080` (Pflichtfeld, unbenutzt).
3. Die **Client-ID** steht unter dem App-Namen, das **Secret** daneben → `REDDIT_CLIENT_ID`,
   `REDDIT_CLIENT_SECRET` in `.env`. Optional `REDDIT_USER_AGENT` (Standard: `WEBSEARCH_UA`).

**Google-News-Links:** Treffer von Google News sind Weiterleitungen (`redirect: true`). Die Suche löst sie
nicht auf (zu viele Requests) und bevorzugt beim Zusammenführen die direkte URL eines anderen Treffers mit
derselben Schlagzeile. `fetch_url` löst sie auf: altes ID-Format offline, neues mit zwei Requests an
news.google.com (Artikelseite + `batchexecute`, wie das Open-Source-Paket `googlenewsdecoder`), im selben
hostweiten Tempo-Slot wie die Suchquelle `google_news`, jeder Link nur einmal (Cache), Sperren (Consent,
Captcha, 429) pausieren das Auflösen. Für EU-IPs wird das Cookie `SOCS=CAI` gesendet (die Antwort „Alle
ablehnen“). Die aufgelöste URL durchläuft dieselben Prüfungen wie jede andere (internes Netz, robots.txt des
Herausgebers). Hinweis: Googles robots.txt sperrt diese Pfade für Crawler; das Auflösen ist ein einzelner,
vom Nutzer angestoßener Abruf wie ein Klick im Browser. Wer das nicht will: `WEBSEARCH_GNEWS_DECODE=0`.

## Python-API und JSON-Format

```python
import asyncio
from websearch import SearchEngine

async def main():
    async with SearchEngine() as eng:
        res = await eng.search("open source metasearch", n=5, kind="web", lang="de", days=30)
        for r in res["results"]:
            print(r["title"], r["url"], r["engines"])
        page = await eng.fetch(res["results"][0]["url"])
        print(page["content"][:500])

asyncio.run(main())
```

Antwort von `search` (auch `websearch search --json`):

```json
{
  "query": "…", "kind": "web",
  "results": [{"title": "…", "url": "…", "snippet": "…", "source": "searxng", "rank": 1,
               "published": "2026-11-05T08:00:00+00:00", "engines": ["searxng", "searxng:brave"],
               "score": 0.0327, "redirect": false, "image": "", "thumbnail": "", "duration": ""}],
  "sources": {"searxng": {"state": "ok", "count": 10, "ms": 812, "message": null,
                          "meta": {"engines_failed": [["google", "Suspended: access denied"]]}}}
}
```

`image`, `thumbnail` und `duration` sind nur bei `kind="images"`/`"videos"` gefüllt (bei Bildern ist `url`
die Seite, `image` das Bild selbst).

`state` ist eines von `ok`, `cached`, `blocked` (Sperre erkannt, jetzt pausiert), `paused`, `rate_limited`
(nächster Tempo-Slot außerhalb des Zeitbudgets), `unavailable`, `unsupported`, `not_configured`,
`budget` (Bezahl-Limit erreicht), `skipped` (bewusst nicht gefragt, z.B. GDELT bei sehr kurzen Wörtern),
`deadline` (Zeitbudget vorbei oder – mit `meta.early: true` – nicht abgewartet, weil schnellere Quellen schon
genug Treffer geliefert hatten), `error` (Bug, wird mit Traceback geloggt).
Nur echte Ausfälle zählen für die Bezahlstufe und für die „blind“-Warnungen des Monitors – `skipped`,
`rate_limited`, `unsupported`, `not_configured` und `budget` nicht. Eigene Stufen: `SearchEngine(policy=TierPolicy(...))`.
Eigene Quellen: Unterklasse von `websearch.providers.Provider` mit
`async def search(self, client, query, n, lang, kind, days=None)`. Zeitfilter: `time_windows = None` (Standard:
die Quelle ignoriert `days`, die Engine filtert nur nach), `()` (genau in Tagen) oder z.B. `(1, 7, 31, 366)` (feste
Fenster – die Quelle bekommt dann das ganze Fenster als `days`); `max_native_days` = bis wohin die Quelle selbst
filtert. Quellen ohne `days`-Parameter funktionieren weiter (sie werden nur nachgefiltert).

## Konfiguration

**Werte mit `$`:** In unquotierten und "doppelt" quotierten Werten werden `$VAR` und `${VAR}` ersetzt – wie bei
docker compose und `set -a; . ./.env`. Passwörter oder Tokens mit `$` deshalb in 'einfache Anführungszeichen'.

Alle Variablen mit Erklärung stehen in [`.env.example.de`](.env.example.de) (englisch: `.env.example`).
Pfade und Dienst-Adressen sind dort auskommentiert – Docker setzt sie selbst; nur für lokale Läufe
einkommentieren. Die wichtigsten:

| Variable | Bedeutung |
|---|---|
| `WEBSEARCH_LANG` | Sprache der Meldungen: `de` oder `en` (Standard: Systemsprache) |
| `SEARXNG_URL` | SearXNG-Instanz (Standard `http://127.0.0.1:8888`) |
| `SEARXNG_SECRET`, `SEARXNG_VERSION` | nur Docker: Secret (Pflicht), Image-Tag |
| `SEARXNG_PORT` | Podman-Quadlet: Host-Port von SearXNG (Standard 8888) |
| `WEBSEARCH_UA` | User-Agent mit Kontakt (Wikimedia/GDELT/HN) |
| `MARGINALIA_KEY` | eigener kostenloser Key, sonst `public` |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | Reddit-Quelle (kostenlose script-App) |
| `WEBSEARCH_DISABLE` | Quellen abschalten, kommagetrennt (z.B. `bing_rss` bei kommerzieller Nutzung) |
| `EXA_API_KEY`, `LINKUP_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `PERPLEXITY_API_KEY`, `SERPER_API_KEY` | optionale Bezahl-Reserve |
| `EXA_SEARCH_TYPE`, `PERPLEXITY_SEARCH_TYPE` | Tarifmodus, Standard `fast` (günstigster) |
| `WEBSEARCH_PAID_DAILY_LIMIT`, `WEBSEARCH_PAID_MONTHLY_LIMIT` | harte Grenze für Bezahl-Abfragen pro Tag/Monat (Standard 100/1000, UTC, angezeigt in `websearch status`) |
| `WEBSEARCH_PROXY` | optionaler Proxy für die eigenen öffentlichen HTTP-Quellen und `fetch_url` (ANALYSE.md, Abschnitt 3.1). Die Standardvariablen `HTTP(S)_PROXY`/`ALL_PROXY` werden bewusst ignoriert, damit ein System-Proxy weder die eigene SearXNG-Instanz noch die API-Keys sieht |
| `DDGS_PROXY` | optionaler Proxy für die `ddgs`-Quellen |
| `WEBSEARCH_FETCH_ALLOW_PRIVATE`, `WEBSEARCH_FETCH_IGNORE_ROBOTS`, `WEBSEARCH_GNEWS_DECODE` | `fetch_url`: Intranet erlauben / robots.txt ignorieren / Google-News-Links nicht auflösen (`0`) |
| `WEBSEARCH_WEBHOOK`, `WEBSEARCH_WEBHOOK_FORMAT`, `WEBSEARCH_TELEGRAM_CHAT_ID`, `WEBSEARCH_SMTP_*` | Benachrichtigung (siehe [Monitoring](#monitoring-und-benachrichtigungen)) |
| `WEBSEARCH_BLIND_AFTER`, `WEBSEARCH_MONITOR_OUT`, `WEBSEARCH_HEARTBEAT` | Blind-Warnung, JSONL-Export (normale Datei, Rechte 0600; Symlinks/FIFOs werden abgelehnt), Heartbeat-Datei |
| `WEBSEARCH_MONITOR_CONCURRENCY` | gleichzeitig laufende Watches (Standard 2) |
| `WEBSEARCH_CACHE`, `WEBSEARCH_MONITOR_DB` | SQLite-Dateien (Standard `~/.cache/websearch/`). Die Cache-Datei enthält auch Sperren, Tempo-Slots und Metriken: alle Prozesse eines Hosts sollten dieselbe nutzen. Die Docker-Dienste haben ein eigenes Volume – ein CLI-Aufruf auf dem Host teilt deren Pausen/Tempo nicht |
| `WEBSEARCH_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` (Standard), `ERROR` |
| `MCP_HOST`, `MCP_PORT`, `MCP_ALLOWED_HOSTS`, `MCP_AUTH_TOKEN` | HTTP-MCP: Bind-Adresse, Port, erlaubte Host-Header, Bearer-Token |
| `MCP_ALLOW_NO_AUTH` | `1` = ohne Token auf `0.0.0.0` lauschen erlaubt (nur Container mit Port auf `127.0.0.1`) |
| `WEBSEARCH_IN_CONTAINER` | Container-Erkennung erzwingen (`1`/`0`); sonst `/.dockerenv` bzw. `/run/.containerenv` |
| `WEBSEARCH_MCP_PAID` | `0` = MCP-Agents dürfen Bezahl-APIs nie nutzen |
| `WEBSEARCH_IMAGE` | fertiges Docker-Image statt lokalem Build |

**Anpassen im Code:** Tempo pro Quelle (`min_interval`, `concurrency`) in `websearch/providers.py`;
Stufen in `DEFAULT_POLICY`, Cache-Dauer je Suchart in `DEFAULT_MAX_AGE` (`websearch/engine.py`).
Regionale Engines (Yandex, Baidu, Seznam, Naver) und weitere Bild-/Video-Engines lassen sich in
`searxng/settings.yml` zuschalten: Namen unter `keep_only` ergänzen und – bei upstream deaktivierten Engines –
unter `engines:` mit `disabled: false` eintragen (ANALYSE.md, Abschnitt 2.1).

## Betrieb, Metriken und Fehlersuche

**Erste Anlaufstelle:** `websearch doctor` (im Container: `docker compose exec websearch-mcp websearch doctor`).

**Metriken** (Prometheus-Textformat, hostweit für CLI, Monitor und MCP-Server gemeinsam): `websearch metrics`
oder `GET /metrics` am MCP-HTTP-Server (mit Token, falls `MCP_AUTH_TOKEN` gesetzt). `/healthz` liefert
`200 ok` ohne Token.

| Metrik | Bedeutung |
|---|---|
| `websearch_searches_total{kind,result}` | Suchen je Suchart; `result="empty"` = gar kein Treffer |
| `websearch_source_requests_total{source,state}` | Anfragen je Quelle und Ergebnis (`ok`, `cached`, `blocked`, …) |
| `websearch_source_duration_seconds_total{source}` | Gesamtzeit je Quelle (÷ Anfragen = Durchschnitt) |
| `websearch_source_blocked`, `_retry_seconds`, `_strikes`, `_available` | aktueller Zustand je Quelle |
| `websearch_paid_queries{period}`, `websearch_paid_limit{period}` | Bezahl-Abfragen heute/Monat und Limits |

```yaml
# prometheus.yml
scrape_configs:
  - job_name: websearch
    static_configs: [{ targets: ["127.0.0.1:8765"] }]
    authorization: { credentials_file: /etc/prometheus/websearch.token }   # nur mit MCP_AUTH_TOKEN
```

Sinnvolle Alarme: `websearch_source_blocked{source="searxng"} == 1`, ein steigender Anteil
`state="blocked"`, `websearch_paid_queries{period="day"}` nahe am Limit.

| Symptom | Ursache / Abhilfe |
|---|---|
| Quelle steht auf `paused` | Sperre erkannt; Pause verdoppelt sich bis max. 6 h. `websearch status` zeigt Restzeit und Grund. Nicht vorzeitig zurücksetzen – das verlängert Sperren meist. |
| Google liefert nichts | SearXNG hat Google pausiert (`meta.engines_failed`, `http://localhost:8888/stats`). Normal von Rechenzentrums-IPs; die anderen Engines liefern weiter. Optional `SERPER_API_KEY` als Reserve. |
| `rate_limited` | der nächste Tempo-Slot der Quelle liegt hinter dem Zeitbudget (z.B. GDELT: 1 Anfrage pro 6 s – GDELT verlangt höchstens 1 pro 5 s, das Tool hält Sicherheitsabstand); die Suche wartet bewusst nicht. |
| `skipped` | die Quelle wurde bewusst nicht gefragt, z.B. GDELT bei Anfragen, die überwiegend aus Wörtern unter 3 Buchstaben bestehen (es würde nach etwas anderem suchen). |
| Monitor meldet `blind` | mehrere Läufe ohne Treffer wegen Fehlern: `docker compose logs websearch-monitor`, `websearch status`. |
| `status --reset` hilft nicht | es setzt nur die Pausen dieses Tools zurück; SearXNG-interne Sperren hebt nur ein Neustart von SearXNG auf. |
| Google-News-Link lässt sich nicht lesen | Google zeigt Consent/Captcha → das Auflösen pausiert (ab 15 min). Anderen Treffer mit derselben Schlagzeile nehmen. |
| `401` vom MCP-Server | `MCP_AUTH_TOKEN` ist gesetzt: Client mit `Authorization: Bearer …` einbinden. |
| Änderung an `.env` oder `searxng/settings.yml` wirkt nicht | Dienste neu starten: Docker `docker compose up -d` (bzw. `docker compose restart searxng`), Podman-Quadlet `deploy/podman/install.sh` erneut, systemd/bwrap `systemctl --user restart websearch-searxng websearch-mcp websearch-monitor`. |
| SearXNG/Monitor-Container startet nicht, „Permission denied“ | `umask 077` auf dem Host: `searxng/settings.yml` und `watches/` müssen für andere lesbar sein (`chmod a+r searxng/settings.yml watches/*.json; chmod a+rx searxng watches`). |

## Entwicklung, Tests und Release

```bash
pip install -e '.[dev]'
python -m pytest                 # offline: Kern, alle Quellen (nachgebaute Antworten), Monitoring, fetch, MCP, CLI
mypy websearch                   # Typprüfung
python tests/mcp_smoke.py        # live: startet den MCP-Server (stdio + HTTP) und ruft alle Tools auf
```

`tests/test_deploy.py` prüft die Podman-Units und die bwrap-Skripte; ohne nutzbares bwrap (User-Namespaces) führt
ein Test-Double die Sandbox-Optionen aus und startet websearch darin.

Meldungen stehen an der Aufrufstelle in beiden Sprachen: `t("deutsch", "english")` (`websearch/i18n.py`);
ein Test prüft, dass jeder Aufruf zwei Texte mit denselben Platzhaltern hat.

CI (`.github/workflows/ci.yml`): pytest auf Python 3.10–3.13 mit `requirements.lock`, mypy, Paket-Build mit
`twine check` und Installation des Wheels in eine frische venv.

**Release:**
1. Version in `websearch/__init__.py` (`pyproject.toml` liest sie von dort) und in `server.json` erhöhen.
2. Bei geänderten Abhängigkeiten: `uv pip compile pyproject.toml --universal --python-version 3.10 -o requirements.lock`.
3. Abschnitt in `CHANGELOG.md` ergänzen (`## [X.Y.Z] – Datum`; er wird zum Text des GitHub-Releases).
4. `git tag vX.Y.Z && git push origin vX.Y.Z` – `.github/workflows/release.yml` prüft Tag und Version, testet,
   veröffentlicht auf PyPI (Trusted Publishing, Paket `websearch-mcp`), das Multi-Arch-Image nach
   `ghcr.io/c0decave/web-search-mcp` und legt das GitHub-Release an. Versionen mit `a`, `b` oder `rc`
   (z.B. `0.3.0rc1`) werden als Vorabversion markiert: kein `latest`-Image, pip installiert sie nur mit `--pre`.
   Nach PyPI trägt der Job `mcp-registry` `server.json` in die offizielle MCP Registry ein (Login per GitHub-OIDC,
   kein Token). Die Version in `server.json` mit erhöhen – ein Test prüft, dass sie passt.
   Schlug PyPI oder die Registry fehl: Actions → „Publish existing release“ → Run workflow mit dem Tag
   (`.github/workflows/publish.yml` lädt die Dateien dieses Releases hoch und wiederholt den Registry-Schritt).
5. Einmalig: Trusted Publisher auf PyPI anlegen (Workflow `release.yml`, Environment `pypi`), Environment
   `pypi` in GitHub anlegen, das ghcr-Paket nach dem ersten Push auf „public“ stellen.
