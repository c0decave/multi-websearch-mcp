# Quickstart

[README](../README.md) · **Quickstart** · [Usage](USAGE.md) · [Changelog](../CHANGELOG.md) ·
[Deutsch](../README.de.md#schnellstart-in-5-minuten)

Two ways to get going. Pick **A** for the full setup (all sources, monitoring, best results) or **B** to try the
MCP server in one minute without Docker.

| | A: Docker (recommended) | B: `uvx`, no Docker |
|---|---|---|
| Needs | Docker with Compose ≥ 2.24 (or Podman), Python ≥ 3.10 for `websearch init` | [uv](https://docs.astral.sh/uv/) |
| Sources | own SearXNG (Google, Bing, Brave, DuckDuckGo, Qwant …) **plus** all feeds and APIs | feeds and APIs only (Bing RSS, Google News, GDELT, Hacker News, Wikipedia, Marginalia, DuckDuckGo via `ddgs`) |
| Monitoring service | yes | run `websearch monitor --loop` yourself |
| MCP transport | HTTP on `127.0.0.1:8765` with a token (stdio also works) | stdio |

> **Run it from an office or home connection if you can.** Data-center IPs get far more captchas from the
> search engines ([ANALYSE.md](../ANALYSE.md), section 3, German).

## A: Docker (5 minutes)

```bash
git clone https://github.com/c0decave/multi-websearch-mcp.git && cd multi-websearch-mcp
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
websearch init --contact you@your-company.com --lang en   # .env with secrets, contact, language; creates watches/
docker compose up -d                                       # SearXNG :8888, MCP server :8765, monitoring
docker compose exec websearch-mcp websearch doctor         # checks everything and tells you what to fix
docker compose exec websearch-mcp websearch search "open source metasearch" -n 5
```

- Replace `you@your-company.com` with a real contact (e-mail or URL). Wikimedia, GDELT and Hacker News ask
  automated clients to identify themselves; `websearch doctor` complains about placeholders.
- `websearch init` generates a random `SEARXNG_SECRET` and an `MCP_AUTH_TOKEN`. It never overwrites anything
  and always makes a backup of an existing `.env` first.
- Without Python on the host: `cp .env.example .env`, fill in `SEARXNG_SECRET` and `MCP_AUTH_TOKEN` (each
  `openssl rand -hex 32`) and the contact in `WEBSEARCH_UA`, then
  `mkdir -p watches && cp watches.example.json watches/watches.json`.
- Podman, systemd and bubblewrap (no containers at all) are described in [Usage → Installation](USAGE.md#installation).

### Connect your MCP client (HTTP)

**Claude Code:**

```bash
T=$(.venv/bin/python -m websearch.envfile --get MCP_AUTH_TOKEN .env) && \
  claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"
```

**Cursor, VS Code, Windsurf and other clients with HTTP support** – put the token from `.env` (`MCP_AUTH_TOKEN`)
into the header:

```json
{
  "mcpServers": {
    "websearch": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN from .env>" }
    }
  }
}
```

(VS Code uses `"servers"` instead of `"mcpServers"` and needs `"type": "http"` in the entry.)

**Clients that only speak stdio** (e.g. Claude Desktop): use the stdio command from the repo – it reads the same
`.env` and talks to the SearXNG container on `127.0.0.1:8888`:

```json
{
  "mcpServers": {
    "websearch": {
      "command": "/path/to/multi-websearch-mcp/.venv/bin/python",
      "args": ["-m", "websearch", "--env-file", "/path/to/multi-websearch-mcp/.env", "mcp"]
    }
  }
}
```

## B: `uvx`, no Docker (1 minute)

Nothing to clone. The server runs over stdio; SearXNG is simply reported as unavailable and the other sources
answer.

**Claude Code:**

```bash
claude mcp add -s user websearch \
  -e WEBSEARCH_UA="websearch/0.3 (you@your-company.com)" \
  -- uvx multi-websearch-mcp@0.3.0rc2
```

**Claude Desktop, Cursor, Windsurf** (`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "websearch": {
      "command": "uvx",
      "args": ["multi-websearch-mcp@0.3.0rc2"],
      "env": { "WEBSEARCH_UA": "websearch/0.3 (you@your-company.com)" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "websearch": {
      "type": "stdio",
      "command": "uvx",
      "args": ["multi-websearch-mcp@0.3.0rc2"],
      "env": { "WEBSEARCH_UA": "websearch/0.3 (you@your-company.com)" }
    }
  }
}
```

The version is pinned because this is a pre-release; once 0.3.0 is out, `uvx multi-websearch-mcp` is enough.
To try it on the command line first: `uvx --from multi-websearch-mcp==0.3.0rc2 websearch search "open source metasearch" -n 5`.

Later you can point the same server at a SearXNG instance of your own with `SEARXNG_URL` in `env`, or keep all
settings in a file: `"args": ["multi-websearch-mcp@0.3.0rc2", "--env-file", "/path/to/.env"]`.

## Check that it works

Ask your agent something current, e.g. *"Search the news from the last 7 days about the EU AI Act and summarize
the three most important articles."* It should call `news_search` (with `days=7`) and then `fetch_url`.

If results are thin, call the `source_status` tool or run `websearch doctor` / `websearch status`: they show
which sources are paused because of a block and for how long. Pauses are normal and end on their own.

## Next steps

- **Monitoring:** edit `watches/watches.json` and set a notification target (Slack, Discord, Teams, Telegram,
  ntfy, e-mail or a webhook) in `.env` – see [Usage → Monitoring](USAGE.md#monitoring-and-notifications).
- **Paid reserve (optional):** add an API key for Exa, Linkup, Tavily, Brave, Perplexity or Serper to `.env`. They
  are only used when the free sources fail, always with a hard daily and monthly limit
  ([Usage → Command line](USAGE.md#command-line), "Tiers").
- **Remote access:** keep the port on `127.0.0.1` and put a TLS reverse proxy in front
  ([Usage → MCP for AI agents](USAGE.md#mcp-for-ai-agents)).
- **All commands and options:** [Usage](USAGE.md) or `websearch COMMAND --help`.
