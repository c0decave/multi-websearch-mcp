# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/)
(`0.3.0rc1` = release candidate 1 of 0.3.0). The release workflow uses each version's section as the text of its
GitHub release.

## [0.3.0rc1] – 2026-11-16

First public pre-release.

### Features

- **MCP server** (stdio and streamable HTTP) with seven tools: `web_search`, `news_search`, `search_many`,
  `image_search`, `video_search`, `fetch_url` (page as Markdown, also resolves Google News links) and
  `source_status`. All tools have input and output schemas.
- **Sources:** self-hosted SearXNG (Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia, YouTube …), Bing RSS,
  Google News RSS, GDELT, Hacker News, Wikipedia, Marginalia, Reddit (official OAuth API), `ddgs` as fallback.
  Results are merged, de-duplicated and ranked with Reciprocal Rank Fusion.
- **Avoids captchas:** host-wide rate limits per source (shared by every process), cache, automatic pauses after
  blocks (doubling, max. 6 h), detection of silent blocks, sources in tiers (free → fallback → paid).
- **Optional paid reserve:** Exa, Linkup, Tavily, Brave Search API, Perplexity, Serper – only after blocks,
  cheapest pricing mode, max. 10 results per query, hard daily and monthly limits.
- **Time filter** `days` for all kinds except reference lookups, passed to the sources that can filter
  themselves and re-checked afterwards.
- **Monitoring service** that reports only new results, with protection against false alarms, a warning when a
  watch goes "blind", at-least-once delivery with stable ids, and notifications via webhook (JSON), ntfy, Slack,
  Discord, Microsoft Teams, Telegram or e-mail.
- **CLI** (`search`, `many`, `fetch`, `monitor`, `status`, `sources`, `doctor`, `init`, `metrics`, `mcp`) and a
  **Python API** (`websearch.SearchEngine`).
- **`websearch init`** creates `.env` with random secrets and an MCP token; **`websearch doctor`** checks the
  whole setup and says how to fix each problem.
- **Prometheus metrics** (`/metrics`, `websearch metrics`) and a health check (`/healthz`).
- Messages, logs and notifications in **English or German**.

### Deployment and security

- Docker Compose (multi-arch image on `ghcr.io/c0decave/web-search-mcp`), Podman Quadlet, systemd and a
  bubblewrap sandbox for machines without containers.
- HTTP MCP server: bound to `127.0.0.1`, bearer token, DNS rebinding protection, refuses to start when it would
  be reachable from outside without a token.
- `fetch_url`: refuses internal network targets (also after redirects, with DNS pinning), respects robots.txt,
  size and rate limits.
- Containers without capabilities, with `no-new-privileges` and read-only file systems.

### Known limitations

- Whether Google (via SearXNG) and DuckDuckGo answer reliably depends on your IP. From data-center IPs they are
  often blocked; this has not yet been measured from an office or home connection
  ([ANALYSE.md](ANALYSE.md), section 3).
- Startpage and Mojeek use proof-of-work captchas since 2026 and are only used when chosen explicitly.
- Podman Quadlet needs Podman ≥ 4.7; bubblewrap and Quadlet are Linux-only (Docker and `uvx` run everywhere).
