<img src="https://raw.githubusercontent.com/c0decave/multi-websearch-mcp/main/docs/assets/logo.png" alt="" width="72" align="right">

# websearch – web search MCP server that avoids captchas

[![CI](https://github.com/c0decave/multi-websearch-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/c0decave/multi-websearch-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/multi-websearch-mcp?include_prereleases&label=PyPI)](https://pypi.org/project/multi-websearch-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.c0decave%2Fmulti--websearch--mcp-6E56CF)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.c0decave/multi-websearch-mcp)

<!-- mcp-name: io.github.c0decave/multi-websearch-mcp -->

**English** · [Deutsch](README.de.md) · [Quickstart](docs/QUICKSTART.md) · [Usage](docs/USAGE.md) · [Changelog](CHANGELOG.md)

Parallel web, news, image and video search **plus page fetching** for AI agents, built on free sources:
a self-hosted [SearXNG](https://github.com/searxng/searxng) (Google, Bing, Brave, DuckDuckGo, Qwant …) and
official feeds and APIs that never show captchas (Bing RSS, Google News, GDELT, Hacker News, Wikipedia,
Marginalia, Reddit). Cheap paid APIs are an optional last resort with hard spending limits. Comes as an
**MCP server** (stdio and HTTP), a CLI, a Python API and a **monitoring service** that reports only new results to
Slack, Discord, Teams, Telegram, ntfy, e-mail or a webhook.

> **Pre-release 0.3.0rc2.** Feature-complete and tested (≈680 offline tests), but still in its pilot phase.
> Please report problems as [issues](https://github.com/c0decave/multi-websearch-mcp/issues).

## Why

Free official web search APIs are basically gone: the Bing Search API was shut down in August 2025, Google's
Custom Search JSON API takes no new customers and ends on 1 January 2027, and more search engines put
proof-of-work captchas in front of automated clients. Scraping naively gets you blocked within minutes.

This server is built to **avoid** captchas instead of solving them:

- **Rate limits per source that hold across processes**, so the CLI, the monitor and the MCP server share one budget.
- **Each engine is asked only once per query**, and results are cached.
- **Blocked sources are paused automatically** (the pause doubles with each block, capped at 6 h), and *silent*
  blocks (empty answers instead of errors) are detected.
- **Captcha-free sources come first:** official feeds and APIs, with search engines as fallback in tiers.
- **Paid APIs only as a last resort** (Exa, Linkup, Tavily, Brave, Perplexity, Serper), only when the free sources
  failed because of blocks, always in the cheapest mode and within a daily and monthly limit.

The background (engine comparison, prices, captcha strategies, proxies) is in [ANALYSE.md](ANALYSE.md) (German).

## MCP tools

| Tool | Purpose |
|---|---|
| `web_search(query, max_results=10, language="auto", days=None)` | web search across all tiers |
| `news_search(query, max_results=15, language="auto", days=None)` | current news, newest day first |
| `search_many(queries[≤20], max_results=8, kind="web"\|"news"\|"ref", language, days)` | several sub-questions in parallel |
| `image_search(query, max_results=10, language="auto", days=None)` | images: page, image and thumbnail URL |
| `video_search(query, max_results=10, language="auto", days=None)` | videos with thumbnail, duration and date |
| `fetch_url(url, max_chars=20000)` | read a page as Markdown (also resolves Google News links) |
| `source_status()` | which sources are currently paused, and why |

Results from all sources are merged, de-duplicated and ranked (Reciprocal Rank Fusion); `engines` shows which
sources found each hit. All tools have input and output schemas, and errors reach the model as readable tool errors.

## Quickstart

**Full setup with Docker** (all sources, monitoring, HTTP MCP server with a token):

```bash
git clone https://github.com/c0decave/multi-websearch-mcp.git && cd multi-websearch-mcp
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
websearch init --contact you@your-company.com --lang en   # .env with secrets and your contact
docker compose up -d                                       # SearXNG, MCP server on 127.0.0.1:8765, monitor
docker compose exec websearch-mcp websearch doctor         # checks everything, tells you what to fix
T=$(.venv/bin/python -m websearch.envfile --get MCP_AUTH_TOKEN .env) && \
  claude mcp add -s user --transport http websearch http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T"
```

**Try it in one minute without Docker** (stdio; no SearXNG, the feeds and APIs still answer):

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

Use a real contact (e-mail or URL) instead of `you@your-company.com`: Wikimedia, GDELT and Hacker News ask
automated clients to identify themselves. Configs for Claude Code, Claude Desktop, Cursor, VS Code and Windsurf,
plus Podman, systemd and bubblewrap setups: **[docs/QUICKSTART.md](docs/QUICKSTART.md)**.

Run it from an office or home connection if you can: data-center IPs get far more captchas.

## Command line

```bash
websearch search "heat pump subsidies" -l en           # web search
websearch search "EU AI Act" -k news --days 7          # news of the last 7 days
websearch search "rust ownership" -k videos            # images / videos: -k images, -k videos
websearch many queries.txt -n 5                        # one query per line, in parallel
websearch fetch https://example.org/article            # page as Markdown
websearch monitor watches/watches.json --loop          # report only new results
websearch status                                       # sources and current pauses
websearch doctor                                       # check the setup, with fixes
```

Everything else (all options, JSON format, Python API, monitoring rules, notifications, configuration,
Prometheus metrics, troubleshooting): **[docs/USAGE.md](docs/USAGE.md)**.

## How it works

```
 MCP client / CLI / monitor
            │
            ▼
   websearch engine ── cache, host-wide rate limits, block detection and pauses, budget for paid APIs
            │
            ├─ tier 1 (free):      own SearXNG · Google News RSS · Bing RSS · GDELT · Hacker News · Wikipedia · Reddit
            ├─ tier 2 (fallback):  Bing RSS · DuckDuckGo / Yahoo / Brave via ddgs · Marginalia   (only if tier 1 is thin)
            └─ tier 3 (optional):  Exa · Linkup · Tavily · Brave API · Perplexity · Serper         (only after blocks)
            │
            ▼
   merge, de-duplicate, rank (RRF), date filter ──► results + per-source status
```

## Security

- **HTTP server:** listens on `127.0.0.1` only, requires a bearer token (`websearch init` creates one) and checks
  `Host`/`Origin` against DNS rebinding. It refuses to start when it is reachable from outside without a token.
- **`fetch_url`:** http(s) only. It refuses internal targets (localhost, private and link-local networks), also
  after redirects and with DNS pinning, respects `robots.txt` and limits size and rate.
- **Containers:** no capabilities, `no-new-privileges`, read-only file systems, SearXNG not running as root.
- **Without containers:** a [bubblewrap](https://github.com/containers/bubblewrap) sandbox with seccomp, an empty
  `$HOME` and only the paths it needs ([Usage → bubblewrap](docs/USAGE.md#bubblewrap-without-containers)).
- **Secrets:** tokens and webhook URLs never appear in logs, and paid APIs can be forbidden for agents
  (`WEBSEARCH_MCP_PAID=0`).

## Development

```bash
pip install -e '.[dev]'
python -m pytest          # offline tests: all sources (recorded responses), monitoring, fetch, MCP, CLI, deploy
mypy websearch
```

The release process is in [Usage → Development](docs/USAGE.md#development-tests-and-release).

## License

[MIT](LICENSE)
