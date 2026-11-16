# Usage and reference

[README](../README.md) · [Quickstart](QUICKSTART.md) · **Usage** · [Changelog](../CHANGELOG.md) ·
[Deutsch (vollständig)](../README.de.md) · [Background analysis (German)](../ANALYSE.md)

Everything beyond the [quickstart](QUICKSTART.md): all install variants, the command line, the MCP tools,
monitoring, sources, the Python API, configuration, operations and development.

## Contents

1. [Installation](#installation) – [Docker](#docker-recommended) · [Podman](#podman) ·
   [bubblewrap](#bubblewrap-without-containers) · [pip](#with-pip-without-docker)
2. [Command line](#command-line)
3. [MCP for AI agents](#mcp-for-ai-agents)
4. [Monitoring and notifications](#monitoring-and-notifications)
5. [Sources](#sources)
6. [Python API and JSON format](#python-api-and-json-format)
7. [Configuration](#configuration)
8. [Operations, metrics and troubleshooting](#operations-metrics-and-troubleshooting)
9. [Development, tests and release](#development-tests-and-release)

## Installation

### Docker (recommended)

```bash
docker compose up -d                        # builds the image locally from requirements.lock
docker compose ps                           # all three services should become "healthy"
docker compose logs -f websearch-monitor    # new results (JSON lines) and messages
```

- The MCP server and the monitor start only once SearXNG is healthy. All services have healthchecks; the MCP
  server at `/healthz`, the monitor writes a heartbeat after every pass.
- All ports are bound to `127.0.0.1` only. **Do not expose them unprotected** (the SearXNG limiter is off);
  for remote access to the MCP server set `MCP_AUTH_TOKEN` ([MCP](#mcp-for-ai-agents)).
- Hardened: all containers without capabilities, with `no-new-privileges` and a read-only file system; SearXNG
  runs as UID 977 instead of root and mounts `searxng/` read-only (the file stays normally editable on the host).
  Coming from an older version whose SearXNG already took over `./searxng`: run `sudo chown -R "$USER" searxng`
  once.
- Prebuilt image instead of a local build ([releases](https://github.com/c0decave/web-search-mcp/releases)):
  `WEBSEARCH_IMAGE=ghcr.io/c0decave/web-search-mcp:0.3.0rc1` in `.env`, then
  `docker compose pull && docker compose up -d --no-build` (linux/amd64 and linux/arm64).
- Update: `git pull && docker compose build && docker compose pull --ignore-buildable && docker compose up -d`.
  The SearXNG image is pinned to the tested version (`SEARXNG_VERSION` in `.env`, e.g. `latest`).

### Podman

**With compose:** `podman compose up -d` (Podman ≥ 5) or `podman-compose up -d` uses the same
`docker-compose.yml` (the bind mounts already carry `:Z` for SELinux). Rootless, `./searxng` belongs to a
sub-UID if an older version took it over (`podman unshare chown -R 0:0 searxng`); better use Quadlet right away.

**As systemd services (Quadlet, rootless, recommended for Podman):**

```bash
websearch init --contact you@your-company.com   # as above
deploy/podman/install.sh                       # units into ~/.config/containers/systemd, build image, start
systemctl --user status websearch-searxng websearch-mcp websearch-monitor
podman exec websearch-mcp websearch doctor
loginctl enable-linger "$USER"                 # keep services running without a login
```

- Needs **Podman ≥ 4.7** (Ubuntu 22.04/Debian 12 are too old → use docker compose or bwrap). `install.sh` checks
  the version and validates the units with `quadlet -dryrun` first.
- Same services, ports (`127.0.0.1` only), healthchecks and volumes as with compose. Containers run with
  `NoNewPrivileges`, no capabilities and a read-only root file system; SearXNG settings are mounted read-only
  (edit `searxng/settings.yml` on the host). Podman < 5.8.2 may show health checks as "unhealthy" (Quadlet quoting
  bug); the services run anyway.
- `UserNS=keep-id:uid=977`: inside the SearXNG container your own user appears as `searxng` – `searxng/` stays
  normally editable.
- Podman takes `.env` values literally (quotes included). `install.sh` therefore writes a cleaned copy to
  `~/.config/websearch/podman.env` (mode 0600); SearXNG only gets its secret. **Re-run `deploy/podman/install.sh`
  after every change to `.env`.** `--print` shows everything first without writing; `--uninstall` removes the
  units (volumes stay).

### bubblewrap (without containers)

For machines without Docker/Podman: [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) locks every
process into a sandbox:
- Everything is read-only; `$HOME`, `/tmp`, `/var/tmp` and `/run` are overlaid empty (SSH keys, browser profiles,
  D-Bus/ssh-agent/Podman sockets are invisible). Only the repo (read-only, without `.env*`/`.git`), the Python
  environment, files passed to `monitor`/`many`, and the data directory `~/.local/share/websearch` (writable, also
  the working directory) are visible. Search terms are never treated as paths.
- Own namespaces, no nested user namespaces (bwrap ≥ 0.8), shared network, no capabilities. seccomp blocks Unix
  sockets except connected stream pairs (`socketpair(SOCK_STREAM)`, used by asyncio) and io_uring, so abstract
  sockets on the shared network (X11, old D-Bus buses) are unreachable. Empty environment: only variables websearch
  reads (from `.env` and the environment, not `SEARXNG_SECRET`). All bwrap options go through a pipe, so tokens never
  appear in `/proc/*/cmdline`.
- Writable are only the data directory and `--rw` paths; inside `$HOME` only `~/.local/share/websearch*` and
  `~/.cache/websearch*` – anything else there needs `--rw-unsafe PATH` (almost every other location in `~` is
  executed or read as configuration by some program). `WEBSEARCH_CACHE`, `WEBSEARCH_MONITOR_DB`,
  `WEBSEARCH_MONITOR_OUT` and `WEBSEARCH_HEARTBEAT` must point into the data directory (relative paths are resolved
  inside it) or into a `--rw` directory.
- SearXNG lives in `~/.local/share/websearch-searxng` (outside the writable data directory); `install`/`update`
  run git and pip sandboxed as well and use the tested commit by default (`SEARXNG_REF=master` for the latest).
- `/home`, `/root`, `/srv`, `/mnt` and `/media` are hidden as well; the wrappers refuse to run as root
  (`WEBSEARCH_BWRAP_ALLOW_ROOT=1` overrides). An old SearXNG installation in `~/.local/share/websearch/searxng-src`
  is no longer used automatically – reinstall with `searxng-bwrap install`.
- Remaining risk: the shared network – the sandbox can reach local TCP services (the SSRF guard only covers
  websearch's own fetches).

```bash
sudo apt install bubblewrap                       # or dnf/pacman install bubblewrap
websearch init --contact you@your-company.com
deploy/bwrap/searxng-bwrap install                # once: SearXNG source + venv (sandboxed, pinned commit)
# either: everything as systemd user services (SearXNG, MCP on 127.0.0.1:8765, monitor)
deploy/systemd/install.sh
# or: by hand in the foreground (don't combine with the services - both use port 8888)
deploy/bwrap/searxng-bwrap run                    # terminal 1: SearXNG on 127.0.0.1:8888
deploy/bwrap/websearch-bwrap doctor               # terminal 2: any websearch command, reads .env itself
```

**After changing `.env` or `searxng/settings.yml`:** systemd services: `systemctl --user restart websearch-searxng
websearch-mcp websearch-monitor` (the wrappers re-read `.env` on every start; `deploy/systemd/install.sh` only needs
to be re-run after moving the repo or SearXNG). Foreground: stop with Ctrl+C and start the command again.
`watches.json` changes are picked up live by the monitor.

- `websearch-bwrap --print …` shows the complete bwrap call (secrets hidden); `--ro`/`--rw PATH` add more paths,
  `--rw-unsafe PATH` like `--rw` also elsewhere in `$HOME` (warns).
  The wrapper uses `<repo>/.venv` (from the quick start) or `WEBSEARCH_PYTHON`.
- For Claude Code over stdio, with `.env`: `claude mcp add -s user websearch -- /path/to/repo/deploy/bwrap/websearch-bwrap mcp`.
- Needs user namespaces (default on current distributions; blocked in some containers/VMs), and bwrap ≥ 0.8 for
  `--disable-userns` (older versions run without it, with a notice).

### With pip (without Docker)

```bash
pip install --pre websearch-mcp             # package name on PyPI (--pre while it is a pre-release); command: websearch
# or from the repo: python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
export SEARXNG_URL=http://127.0.0.1:8888    # your SearXNG instance; without it the other sources still run
set -a; . ./.env; set +a                    # optional: load your .env (API keys, webhook, ...)
websearch doctor
websearch search "open source metasearch" -n 5
```

The pip warning `lxml … does not provide the extra 'html_clean'` (pip < 23.3) is harmless – the package is
installed directly. `ddgs` is pinned to `<10` because the tool uses its internals. `requirements.lock` contains the exactly tested
versions for Python 3.10–3.13 (`pip install -r requirements.lock && pip install --no-deps .`).

## Command line

```bash
websearch search "heat pump subsidies" -l en           # web search (with snippets)
websearch search "EU AI Act" -k news -l en --days 7    # news of the last 7 days, newest day first
websearch search "Reciprocal Rank Fusion" -k ref       # reference lookup (Wikipedia + SearXNG)
websearch search "northern lights" -k images           # images (page + direct image URL)
websearch search "rust ownership" -k videos            # videos (with duration)
websearch search "x" -k ref -s wikipedia,bing_rss --json   # fixed sources (no tiers), JSON
websearch search "x" --fresh --no-paid --deadline 10   # no cache, never pay, 10 s budget
websearch many queries.txt -n 5                        # one query per line (# = comment), in parallel
cat queries.txt | websearch many - --json              # stdin; --json = one valid JSON array
websearch fetch https://example.org/article            # page as Markdown (Google News links too)
websearch monitor watches/watches.json [--loop]        # report only new results
websearch status                                       # sources, pauses (host-wide) and SearXNG state
websearch status --reset                               # reset this tool's pauses/rate slots
websearch sources                                      # all sources with description, kinds, tiers
websearch doctor                                       # check the setup, with hints how to fix problems
websearch metrics                                      # Prometheus metrics (host-wide)
websearch init                                         # create .env (see quick start)
websearch -v search "x"                                # with log output (-vv = debug)
```

`websearch COMMAND --help` explains all options.

**Language:** `WEBSEARCH_LANG=en` or `de`; without it the system language applies (`LC_ALL`/`LC_MESSAGES`/`LANG`
starting with `de` → German, otherwise English). This covers help, output, error messages, logs and
notifications.

**Time filter `--days N`** (1–3650, not for `-k ref`): sources that can filter themselves receive the filter
(SearXNG `time_range`, Google News `when:`, GDELT, Hacker News, ddgs except Mojeek, Reddit, the paid APIs except
Perplexity). Afterwards every result is re-checked against its date; undated results are kept only from sources
that filtered to exactly that span – for images and videos (almost never dated) the source's window is enough
(e.g. "week" for `--days 3`).

**Exit codes:** `0` ok · `1` no results and all sources failed (or fetch failed, or `doctor` found errors, or a
single `monitor` run – without `--loop`, e.g. from cron – after which every watch is blind; this applies from the
first blind run, the blind alert only after `WEBSEARCH_BLIND_AFTER`) ·
`2` invalid input/configuration (including a forbidden fetch target) · `130` interrupted (Ctrl+C).
`-v`/`-q` take precedence over `WEBSEARCH_LOG_LEVEL`.

**Tiers** (details in ANALYSE.md, section 5):
1. Free primary sources.
2. Free fallbacks, only if too little came back. Search engines that SearXNG currently reports as blocked are
   skipped.
3. Paid APIs (web/news/reference only) only if almost nothing came back **and** that was due to blocks/errors.
   They are tried in order until **one** answers normally (even with 0 results – a second paid API won't find
   a rare term either; empty paid answers are cached), always with at most 10 results (by default in the
   cheapest pricing mode) and within a hard, host-wide limit (`WEBSEARCH_PAID_DAILY_LIMIT` = 100,
   `WEBSEARCH_PAID_MONTHLY_LIMIT` = 1000). Also set a spending limit in every provider's dashboard (Brave keeps
   billing after the free credit). With `--days` the tier only asks paid APIs that filter to exactly that span
   (Exa, Linkup, Brave always; Serper/Tavily only for 1, 7, 31 or 366 days; Perplexity never) – and tries the next
   one if an answer is empty after the date filter.

## MCP for AI agents

Pick one of the following variants (all named `websearch`; `-s user` = available in all projects, without `-s`
the entry only applies to the current directory):

```bash
# stdio (reads the .env itself; variables already set in the environment win)
claude mcp add -s user websearch -- /path/to/repo/.venv/bin/python -m websearch --env-file /path/to/repo/.env mcp
# HTTP service (Docker/Podman/systemd) with the token from .env
T=$(/path/to/repo/.venv/bin/python -m websearch.envfile --get MCP_AUTH_TOKEN /path/to/repo/.env) && \
  claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"
# stdio inside the bubblewrap sandbox
claude mcp add -s user websearch -- /path/to/repo/deploy/bwrap/websearch-bwrap mcp
# stdio from PyPI (no clone needed; pin the version while it is a pre-release)
claude mcp add -s user websearch -- uvx --from websearch-mcp==0.3.0rc1 websearch --env-file /path/to/.env mcp
```

To change an entry (new token, stdio instead of HTTP): first `claude mcp remove -s user websearch`, then add it
again – `claude mcp add` refuses an existing name. After a new token, restart the services (see
[troubleshooting](#operations-metrics-and-troubleshooting)).

On multi-user machines: `claude mcp add … --header` passes the token as an argument, so it is visible in the
process list for about a second. There, put the header directly into `~/.claude.json` instead (or use stdio –
it needs no token).

The repo also contains a `.mcp.json` (server `websearch-repo`, stdio with `--env-file .env`): Claude Code offers it
when started inside the repo (approve it once on the first start) – handy for development.

| Tool | Purpose |
|---|---|
| `web_search(query, max_results=10, language="auto", days=None)` | web search across all tiers |
| `news_search(query, max_results=15, language="auto", days=None)` | current news, newest day first (by relevance within a day) |
| `search_many(queries[≤20], max_results=8, kind="web"\|"news"\|"ref", language, days)` | several sub-questions in parallel |
| `image_search(query, max_results=10, language="auto", days=None)` | images: `url` (page), `image` (picture), `thumbnail` |
| `video_search(query, max_results=10, language="auto", days=None)` | videos: `url`, `thumbnail`, `duration`, `published` |
| `fetch_url(url, max_chars=20000)` | read a page as Markdown (resolves Google News links) |
| `source_status()` | which sources are currently paused |

All tools have input and output schemas (limits, enums). Errors such as an empty query or a forbidden fetch
target reach the model as readable tool errors. Image and video search never use paid APIs and run with
SearXNG safe search "moderate" (`safe_search: 1` in `searxng/settings.yml`); `WEBSEARCH_MCP_PAID=0` forbids paid
APIs for agents altogether.

**`fetch_url` protection:** http(s) only; targets in the internal network (localhost, 10.x, 192.168.x,
169.254.x, fc00::/7 …) are refused, also after redirects; robots.txt is respected; max. 3 MB, 1 fetch/s per
host (host-wide), 6 h cache. Deliberately allow the intranet: `WEBSEARCH_FETCH_ALLOW_PRIVATE=1`.

**Access protection of the HTTP server:**
- **Token:** `websearch init` creates `MCP_AUTH_TOKEN` (64 hex characters). Every request to `/mcp` and `/metrics`
  then needs `Authorization: Bearer <token>` (otherwise `401`, constant-time comparison); `/healthz` stays open.
  New token: `sed -i "s/^MCP_AUTH_TOKEN=.*/MCP_AUTH_TOKEN=$(openssl rand -hex 32)/" .env` (`$(…)` is not executed
  inside `.env` itself). Tokens shorter than 32 characters cause a warning; without a token the server also warns
  on `127.0.0.1` (every local user could use it).
- Port bound to `127.0.0.1` only, plus DNS rebinding protection via the `Host`/`Origin` headers
  (`MCP_ALLOWED_HOSTS`, default `127.0.0.1:*,localhost:*,[::1]:*`). The Host header protection is no access control
  (non-browser clients can fake it) – the token is.
- **Fail closed:** if the server is reachable beyond the own machine (`MCP_HOST` not a loopback address – always
  `0.0.0.0` inside a container – or a public name in `MCP_ALLOWED_HOSTS`) and no token is set, it refuses to start.
  Deliberately without a token in a container (port on `127.0.0.1` only, single-user machine): `MCP_ALLOW_NO_AUTH=1`
  in `.env`; a public name without a token is always refused.
- **Remote access:** keep the port on `127.0.0.1` and put a TLS reverse proxy (Caddy, nginx, Traefik) in front;
  set a token and add the public name with and without port to `MCP_ALLOWED_HOSTS`, e.g.
  `…,search.example.org,search.example.org:*`. Docker publishes ports past the host firewall (ufw) – so do not
  publish the port directly on `0.0.0.0`.

```bash
claude mcp add -s user --transport http websearch https://search.example.org/mcp \
  --header "Authorization: Bearer $MCP_AUTH_TOKEN"
```

## Monitoring and notifications

`watches/watches.json` (example: `watches.example.json`):

```json
[
  {"name": "eu-ai-act", "query": "\"AI Act\" EU", "kind": "news", "every_min": 60, "n": 20},
  {"name": "searxng-releases", "query": "SearXNG", "kind": "news", "every_min": 240,
   "sources": ["hackernews", "google_news", "bing_rss"]},
  {"name": "brand-web", "query": "\"Example Inc\"", "kind": "web", "every_min": 1440}
]
```

| Field | Required | Default | Meaning |
|---|---|---|---|
| `name` | yes | – | unique name |
| `query` | yes | – | search query |
| `kind` | | `news` | `web`, `news`, `ref`, `images` or `videos` |
| `lang` | | `auto` | e.g. `en` |
| `every_min` | | `60` | check interval in minutes (min. 5) |
| `n` | | `20` | top n that are reported (1–100) |
| `sources` | | tiers | fixed list of sources |
| `deadline` | | `60` | time budget per run in s (5–600) |
| `max_age_days` | | `3` | older dated results do not count as "new" |

Replace the example watches with your own before the first start (`websearch init` reminds you).
Unknown fields and unknown source names are rejected at start with a typo hint (exit code 2). In `--loop`
mode the file is reloaded on every change; if it is invalid, the old watches keep running (error in the log).

**What is reported** (stdout as JSON lines, optionally a notification and a JSONL file):
- `{"type": "hits", "watch": …, "query": …, "new": [results]}` – new results
- `{"type": "health", "watch": …, "status": "blind" | "recovered", …}` – after `WEBSEARCH_BLIND_AFTER`
  (default 3) "blind" runs in a row, and when it works again. Blind means: a source really failed (block,
  outage, SearXNG with all web engines blocked, crashed run), and no source gave a trustworthy answer. Results
  always count. "ok, 0 results" never counts from scraped sites (possible silent block) and never from Hacker
  News/GDELT/Reddit (they return 0 for almost every brand name); from other sources it counts if the source has
  delivered for this watch before – or, while the watch has no history yet, from any broad source (SearXNG,
  Google News, Wikipedia …). Merely skipped or throttled sources never make a run blind; watches whose fixed
  sources can never search their query are rejected at load time.

Results count as seen only once they have been output (stdout/JSONL) – if the output fails, nothing is lost. A
failed notification is stored and retried in every pass (wait up to 1 h, dropped after 24 h); every delivery
has a total limit of 15 s, and after a failure the remaining alerts of the pass go straight into the queue.
Delivery is "at least once": every alert has an `id`, and every result in `new` has its own stable `id` (watch +
normalized URL) – deduplicate by the result `id` on the receiving side.

Against load peaks (a captcha trigger) at most `WEBSEARCH_MONITOR_CONCURRENCY` watches (default 2) run at the
same time, in `--loop` mode the watches start staggered, and every interval gets ±10 % jitter. Every watch runs
on its own schedule – a slow watch does not hold up the others.

**Rules against false alarms:**
- The first run with results only creates the baseline (`--alert-first` reports immediately).
- A larger pool is queried, min(100, max(3n, 30)); only new results within the top n are reported.
- Results that come only from a source delivering for the first time are taken over silently.

**Notifications** – exactly one target, selected with `WEBSEARCH_WEBHOOK_FORMAT`:

| Format | Target | Rendering | Required settings |
|---|---|---|---|
| `json` (default) | n8n, own services | payload unchanged as JSON | `WEBSEARCH_WEBHOOK` |
| `text` | ntfy | plain text, title as `?title=` (umlauts fine) | `WEBSEARCH_WEBHOOK` |
| `slack` | Slack incoming webhook | `{"text": …}` with `<url\|title>` links | `WEBSEARCH_WEBHOOK` |
| `discord` | Discord webhook | links `[title](url)`, max. 2000 characters, no @everyone pings, no link previews | `WEBSEARCH_WEBHOOK` |
| `teams` | Microsoft Teams (Workflows / Power Automate) | Adaptive Card 1.4, max. ~28 KB | `WEBSEARCH_WEBHOOK` |
| `telegram` | Telegram bot | HTML, max. 4096 characters, no link preview | `WEBSEARCH_WEBHOOK`, `WEBSEARCH_TELEGRAM_CHAT_ID` |
| `email` | SMTP server | plain text + HTML, subject `[websearch] <watch>: 3 new results` | `WEBSEARCH_SMTP_HOST`, `WEBSEARCH_SMTP_TO`, sender |

Messages that are too long are cut at whole results and end with `… +N more`. Success counts only for HTTP
2xx (Telegram additionally `"ok": true`, e-mail: accepted by the server); otherwise the alert goes into the
outbox. E-mails get a fixed `Message-ID` derived from the alert `id`. Missing or invalid settings stop
`websearch monitor` at start with a clear message. Logs show only scheme + host of webhook URLs – tokens never
appear in the log.

```bash
# Discord: channel -> Integrations -> Webhooks -> copy URL
WEBSEARCH_WEBHOOK_FORMAT=discord
WEBSEARCH_WEBHOOK=https://discord.com/api/webhooks/<id>/<token>

# Teams: channel -> ... -> Workflows -> "Send webhook alerts to a channel" -> copy URL
# (the old Office 365 connectors no longer exist)
WEBSEARCH_WEBHOOK_FORMAT=teams
WEBSEARCH_WEBHOOK="https://<…>.logic.azure.com/workflows/<…>/triggers/manual/paths/invoke?api-version=2016-06-01&sig=<…>"   # quoted because of &

# Telegram: create a bot with @BotFather, add the bot to the chat; chat id e.g. via getUpdates
WEBSEARCH_WEBHOOK_FORMAT=telegram
WEBSEARCH_WEBHOOK=https://api.telegram.org/bot<TOKEN>/sendMessage
WEBSEARCH_TELEGRAM_CHAT_ID=-1001234567890      # or @channelname

# ntfy (format text)
WEBSEARCH_WEBHOOK_FORMAT=text
WEBSEARCH_WEBHOOK=https://ntfy.sh/my-topic

# E-mail (WEBSEARCH_WEBHOOK is not needed)
WEBSEARCH_WEBHOOK_FORMAT=email
WEBSEARCH_SMTP_HOST=smtp.example.org
WEBSEARCH_SMTP_PORT=587                         # default 587, 465 with TLS=ssl
WEBSEARCH_SMTP_TLS=starttls                     # starttls | ssl | none
WEBSEARCH_SMTP_USER=alerts@example.org
WEBSEARCH_SMTP_PASSWORD=…
WEBSEARCH_SMTP_FROM="websearch <alerts@example.org>"   # default: WEBSEARCH_SMTP_USER
WEBSEARCH_SMTP_TO="me@example.org,team@example.org"   # quoted, in case the .env is loaded by a shell
```

## Sources

`websearch sources` lists all sources with description, kinds and tiers.

| Source | Kinds | Type |
|---|---|---|
| `searxng` | web, news, ref, images, videos | own SearXNG instance (Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia, YouTube …) |
| `bing_rss`, `google_news` | web/news/ref and news | official RSS feeds |
| `hackernews`, `gdelt`, `wikipedia`, `marginalia` | news or ref/web | official free APIs |
| `reddit` | web, news | official API, needs a free OAuth app (see below) |
| `ddgs:yahoo`, `ddgs:duckduckgo`, `ddgs:brave` | web, news, ref (DuckDuckGo also images, videos) | fallback via the `ddgs` library |
| `ddgs:startpage`, `ddgs:mojeek` | web, news, ref | only explicitly via `-s` (proof-of-work captcha) |
| `exa`, `linkup`, `tavily`, `brave_api`, `perplexity`, `serper` | web, news, ref | optional with API key, reserve only |

**Reddit** uses the official API with OAuth ("application-only"): free for "script" apps, about 100 requests
per minute. Link posts point to the linked article, text posts to the Reddit post; NSFW is never returned;
Reddit's rate-limit headers are respected. Create an app:
1. Open <https://www.reddit.com/prefs/apps> with a Reddit account, choose "create another app…".
2. Name (e.g. `websearch`), type **script**, "redirect uri" `http://localhost:8080` (required, unused).
3. The **client id** is shown under the app name, the **secret** next to it → `REDDIT_CLIENT_ID`,
   `REDDIT_CLIENT_SECRET` in `.env`. Optionally `REDDIT_USER_AGENT` (default: `WEBSEARCH_UA`).

**Google News links:** Google News results are redirects (`redirect: true`). The search does not resolve them
(too many requests) and prefers the direct URL of another result with the same headline when merging.
`fetch_url` resolves them: the old id format offline, the new one with two requests to news.google.com
(article page + `batchexecute`, like the open-source package `googlenewsdecoder`), in the same host-wide rate
slot as the `google_news` search source, every link only once (cache); blocks (consent, captcha, 429) pause
the resolving. For EU IPs the cookie `SOCS=CAI` is sent (the "reject all" answer). The resolved URL passes the
same checks as any other (internal network, the publisher's robots.txt). Note: Google's robots.txt disallows
these paths for crawlers; resolving is a single, user-initiated fetch like a click in a browser. If you do not
want that: `WEBSEARCH_GNEWS_DECODE=0`.

## Python API and JSON format

```python
import asyncio
from websearch import SearchEngine

async def main():
    async with SearchEngine() as eng:
        res = await eng.search("open source metasearch", n=5, kind="web", lang="en", days=30)
        for r in res["results"]:
            print(r["title"], r["url"], r["engines"])
        page = await eng.fetch(res["results"][0]["url"])
        print(page["content"][:500])

asyncio.run(main())
```

Response of `search` (also `websearch search --json`):

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

`image`, `thumbnail` and `duration` are filled only for `kind="images"`/`"videos"` (for images `url` is the
page and `image` the picture itself).

`state` is one of `ok`, `cached`, `blocked` (block detected, now paused), `paused`, `rate_limited` (next rate
slot outside the time budget), `unavailable`, `unsupported`, `not_configured`, `budget` (paid limit reached),
`skipped` (deliberately not asked, e.g. GDELT for very short words), `deadline` (time budget over or – with
`meta.early: true` – not awaited because faster sources already had enough results), `error` (bug, logged with
traceback). Only real failures count for the paid tier and for the monitor's "blind" warnings – `skipped`,
`rate_limited`, `unsupported`, `not_configured` and `budget` do not. Own tiers:
`SearchEngine(policy=TierPolicy(...))`. Own sources: subclass `websearch.providers.Provider` with
`async def search(self, client, query, n, lang, kind, days=None)`. Time filter: `time_windows = None` (default: the
source ignores `days`, the engine only post-filters), `()` (exact days) or e.g. `(1, 7, 31, 366)` (fixed windows –
the source then receives the whole window as `days`); `max_native_days` = how far the source filters itself.
Sources without a `days` parameter keep working (they are only post-filtered).

## Configuration

**Values containing `$`:** in unquoted and "double-quoted" values `$VAR` and `${VAR}` are expanded – like docker
compose and `set -a; . ./.env`. Put passwords or tokens containing `$` in 'single quotes'.

All variables with explanations are in [`.env.example`](../.env.example) (German: `.env.example.de`). Paths and
service addresses are commented out there – Docker sets them itself; uncomment them only for local runs. The
most important ones:

| Variable | Meaning |
|---|---|
| `WEBSEARCH_LANG` | language of messages: `en` or `de` (default: system language) |
| `SEARXNG_URL` | SearXNG instance (default `http://127.0.0.1:8888`) |
| `SEARXNG_SECRET`, `SEARXNG_VERSION` | Docker only: secret (required), image tag |
| `SEARXNG_PORT` | Podman Quadlet: host port of SearXNG (default 8888) |
| `WEBSEARCH_UA` | User-Agent with contact (Wikimedia/GDELT/HN) |
| `MARGINALIA_KEY` | own free key, otherwise `public` |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | Reddit source (free script app) |
| `WEBSEARCH_DISABLE` | disable sources, comma-separated (e.g. `bing_rss` for commercial use) |
| `EXA_API_KEY`, `LINKUP_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `PERPLEXITY_API_KEY`, `SERPER_API_KEY` | optional paid reserve |
| `EXA_SEARCH_TYPE`, `PERPLEXITY_SEARCH_TYPE` | pricing mode, default `fast` (cheapest) |
| `WEBSEARCH_PAID_DAILY_LIMIT`, `WEBSEARCH_PAID_MONTHLY_LIMIT` | hard limit for paid queries per day/month (default 100/1000, UTC, shown in `websearch status`) |
| `WEBSEARCH_PROXY` | optional proxy for the own public HTTP sources and `fetch_url` (ANALYSE.md, section 3.1). The standard variables `HTTP(S)_PROXY`/`ALL_PROXY` are deliberately ignored so that a system proxy sees neither your own SearXNG instance nor the API keys |
| `DDGS_PROXY` | optional proxy for the `ddgs` sources |
| `WEBSEARCH_FETCH_ALLOW_PRIVATE`, `WEBSEARCH_FETCH_IGNORE_ROBOTS`, `WEBSEARCH_GNEWS_DECODE` | `fetch_url`: allow the intranet / ignore robots.txt / do not resolve Google News links (`0`) |
| `WEBSEARCH_WEBHOOK`, `WEBSEARCH_WEBHOOK_FORMAT`, `WEBSEARCH_TELEGRAM_CHAT_ID`, `WEBSEARCH_SMTP_*` | notification (see [Monitoring](#monitoring-and-notifications)) |
| `WEBSEARCH_BLIND_AFTER`, `WEBSEARCH_MONITOR_OUT`, `WEBSEARCH_HEARTBEAT` | blind warning, JSONL export (regular file, mode 0600; symlinks/FIFOs are refused), heartbeat file |
| `WEBSEARCH_MONITOR_CONCURRENCY` | watches running at the same time (default 2) |
| `WEBSEARCH_CACHE`, `WEBSEARCH_MONITOR_DB` | SQLite files (default `~/.cache/websearch/`). The cache file also holds blocks, rate slots and metrics: all processes of a host should share it. The Docker services have their own volume – a CLI call on the host does not share their pauses/rate slots |
| `WEBSEARCH_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` (default), `ERROR` |
| `MCP_HOST`, `MCP_PORT`, `MCP_ALLOWED_HOSTS`, `MCP_AUTH_TOKEN` | HTTP MCP: bind address, port, allowed Host headers, bearer token |
| `MCP_ALLOW_NO_AUTH` | `1` = listening on `0.0.0.0` without a token is allowed (containers with the port on `127.0.0.1` only) |
| `WEBSEARCH_IN_CONTAINER` | force container detection (`1`/`0`); otherwise `/.dockerenv` or `/run/.containerenv` |
| `WEBSEARCH_MCP_PAID` | `0` = MCP agents may never use paid APIs |
| `WEBSEARCH_IMAGE` | prebuilt Docker image instead of a local build |

**Adjust in code:** rate per source (`min_interval`, `concurrency`) in `websearch/providers.py`; tiers in
`DEFAULT_POLICY`, cache duration per kind in `DEFAULT_MAX_AGE` (`websearch/engine.py`). Regional engines
(Yandex, Baidu, Seznam, Naver) and more image/video engines can be enabled in `searxng/settings.yml`: add the
name under `keep_only` and – for engines disabled upstream – list it under `engines:` with `disabled: false`
(ANALYSE.md, section 2.1).

## Operations, metrics and troubleshooting

**First stop:** `websearch doctor` (in the container: `docker compose exec websearch-mcp websearch doctor`).

**Metrics** (Prometheus text format, host-wide for CLI, monitor and MCP server together): `websearch metrics`
or `GET /metrics` on the MCP HTTP server (with token if `MCP_AUTH_TOKEN` is set). `/healthz` returns `200 ok`
without a token.

| Metric | Meaning |
|---|---|
| `websearch_searches_total{kind,result}` | searches per kind; `result="empty"` = no result at all |
| `websearch_source_requests_total{source,state}` | queries per source and outcome (`ok`, `cached`, `blocked`, …) |
| `websearch_source_duration_seconds_total{source}` | total time per source (÷ requests = average) |
| `websearch_source_blocked`, `_retry_seconds`, `_strikes`, `_available` | current state per source |
| `websearch_paid_queries{period}`, `websearch_paid_limit{period}` | paid queries today/this month and limits |

```yaml
# prometheus.yml
scrape_configs:
  - job_name: websearch
    static_configs: [{ targets: ["127.0.0.1:8765"] }]
    authorization: { credentials_file: /etc/prometheus/websearch.token }   # only with MCP_AUTH_TOKEN
```

Useful alerts: `websearch_source_blocked{source="searxng"} == 1`, a rising share of `state="blocked"`,
`websearch_paid_queries{period="day"}` close to the limit.

| Symptom | Cause / remedy |
|---|---|
| source shows `paused` | block detected; the pause doubles up to max. 6 h. `websearch status` shows remaining time and reason. Do not reset early – that usually prolongs blocks. |
| Google returns nothing | SearXNG has paused Google (`meta.engines_failed`, `http://localhost:8888/stats`). Normal from data-center IPs; the other engines keep delivering. Optionally `SERPER_API_KEY` as a reserve. |
| `rate_limited` | the source's next rate slot lies beyond the time budget (e.g. GDELT: 1 request per 6 s – GDELT allows at most 1 per 5 s, the tool keeps a safety margin); the search deliberately does not wait. |
| `skipped` | the source was deliberately not asked, e.g. GDELT for queries consisting mostly of words under 3 letters (it would search for something else). |
| monitor reports `blind` | several runs without results because of errors: `docker compose logs websearch-monitor`, `websearch status`. |
| `status --reset` does not help | it only resets this tool's pauses; SearXNG's own suspensions are only lifted by restarting SearXNG. |
| a Google News link cannot be read | Google shows consent/captcha → resolving pauses (from 15 min). Use another result with the same headline. |
| `401` from the MCP server | `MCP_AUTH_TOKEN` is set: add the client with `Authorization: Bearer …`. |
| a change to `.env` or `searxng/settings.yml` has no effect | restart the services: Docker `docker compose up -d` (or `docker compose restart searxng`), Podman Quadlet re-run `deploy/podman/install.sh`, systemd/bwrap `systemctl --user restart websearch-searxng websearch-mcp websearch-monitor`. |
| SearXNG/monitor container does not start, "Permission denied" | `umask 077` on the host: `searxng/settings.yml` and `watches/` must be world-readable (`chmod a+r searxng/settings.yml watches/*.json; chmod a+rx searxng watches`). |

## Development, tests and release

```bash
pip install -e '.[dev]'
python -m pytest                 # offline: core, all sources (recorded responses), monitoring, fetch, MCP, CLI
mypy websearch                   # type check
python tests/mcp_smoke.py        # live: starts the MCP server (stdio + HTTP) and calls all tools
```

`tests/test_deploy.py` checks the Podman units and the bwrap scripts; without a usable bwrap (user namespaces) a
test double executes the sandbox options and starts websearch inside.

Messages are written in both languages at the call site: `t("deutsch", "english")` (`websearch/i18n.py`); a
test checks that every call has two texts with the same placeholders.

CI (`.github/workflows/ci.yml`): pytest on Python 3.10–3.13 with `requirements.lock`, mypy, package build with
`twine check` and installation of the wheel into a fresh venv.

**Release:**
1. Bump the version in `websearch/__init__.py` (`pyproject.toml` reads it from there).
2. If dependencies changed: `uv pip compile pyproject.toml --universal --python-version 3.10 -o requirements.lock`.
3. Add a section to `CHANGELOG.md` (`## [X.Y.Z] – date`; it becomes the text of the GitHub release).
4. `git tag vX.Y.Z && git push origin vX.Y.Z` – `.github/workflows/release.yml` checks tag and version, runs
   the tests, publishes to PyPI (trusted publishing, package `websearch-mcp`), the multi-arch image to
   `ghcr.io/c0decave/web-search-mcp`, and creates the GitHub release. Versions with `a`, `b` or `rc` (e.g.
   `0.3.0rc1`) are marked as pre-release: no `latest` image, and pip installs them only with `--pre`.
5. Once: create a trusted publisher on PyPI (workflow `release.yml`, environment `pypi`), create the `pypi`
   environment in GitHub, set the ghcr package to "public" after the first push.
