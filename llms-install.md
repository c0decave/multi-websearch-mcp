# Installing multi-websearch-mcp (instructions for AI agents)

This file tells an AI agent (Cline, Claude Code, Cursor …) how to set up this MCP server for its user.
Humans: see [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Recommended: `uvx`, no clone, no Docker

Requirements: [uv](https://docs.astral.sh/uv/) (`uvx --version` must work). If it is missing, install it with
`curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows), then restart the
client so `uvx` is on its PATH.

Add this entry to the client's MCP configuration (for Cline: `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "websearch": {
      "command": "uvx",
      "args": ["multi-websearch-mcp@0.3.0rc2"],
      "env": { "WEBSEARCH_UA": "websearch/0.3 (CONTACT)" }
    }
  }
}
```

- Ask the user for a contact (e-mail address or URL) and put it in place of `CONTACT`. Wikimedia, GDELT and
  Hacker News ask automated clients to identify themselves. Do not invent an address; if the user declines,
  remove the `env` block (the server then sends a generic User-Agent).
- No API key is needed. All default sources are free.
- The version is pinned because 0.3.0 is still a pre-release. Check https://pypi.org/project/multi-websearch-mcp/
  for the newest version if the user wants it.

## Verify

The server exposes 7 tools: `web_search`, `news_search`, `image_search`, `video_search`, `search_many`,
`fetch_url`, `source_status`. Call `web_search` with `{"query": "model context protocol", "max_results": 3}`; it
should return results with `title`, `url` and `found_by`. If results are thin, call `source_status`: a source
may be paused after a block, which ends on its own.

## Optional settings (`env`)

| Variable | Purpose |
|---|---|
| `SEARXNG_URL` | URL of an own SearXNG instance (adds Google, Bing, Brave, DuckDuckGo, Qwant via SearXNG). Without it SearXNG is reported as unavailable and the other sources answer. |
| `WEBSEARCH_MCP_PAID` | `0` forbids the MCP tools to use paid APIs at all. Paid APIs (Exa, Tavily, Brave …) are only ever used when their API keys are set, as a reserve when the free sources fail, within hard daily and monthly limits. |

The full setup with its own SearXNG, monitoring and HTTP transport runs with Docker Compose; see
[docs/QUICKSTART.md](docs/QUICKSTART.md), path A.
