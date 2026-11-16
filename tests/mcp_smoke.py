"""Live-Smoke-Test des MCP-Servers (braucht Netz, optional SearXNG):

    python tests/mcp_smoke.py            # stdio + streamable-http
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data(res):
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def exercise(s: ClientSession, label: str) -> None:
    await s.initialize()
    tools = sorted(t.name for t in (await s.list_tools()).tools)
    assert tools == ["fetch_url", "image_search", "news_search", "search_many", "source_status", "video_search",
                     "web_search"], tools
    many = _data(await s.call_tool("search_many", {"queries": ["fastapi websocket", "sqlite fts5"],
                                                   "max_results": 3}))
    for d in many:
        print(f"  [{label}] {d['query']!r}: {[x['url'] for x in d['results']]}  Fehler: {d['source_errors']}")
        assert d["results"], f"keine Treffer für {d['query']}"
    news = _data(await s.call_tool("news_search", {"query": "Bundestag", "max_results": 3, "language": "de"}))
    print(f"  [{label}] news: {[x.get('published', '')[:10] for x in news['results']]}")
    bad = await s.call_tool("web_search", {"query": "   "})
    assert bad.is_error and ("leere Suchanfrage" in bad.content[0].text or "empty query" in bad.content[0].text), bad.content   # Meldung erreicht das Modell
    url = many[0]["results"][0]["url"]
    page = await s.call_tool("fetch_url", {"url": url, "max_chars": 800})
    if page.is_error:
        print(f"  [{label}] fetch {url}: {page.content[0].text}")
    else:
        p = _data(page)
        print(f"  [{label}] fetch {p['final_url']}: {p['title']!r}, {len(p['content'])} Zeichen")
    st = _data(await s.call_tool("source_status", {}))
    print(f"  [{label}] pausiert: {[k for k, v in st.items() if v['blocked']] or 'keine'}")


async def stdio() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "websearch", "mcp"],
                                   env={**os.environ}, cwd=ROOT)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await exercise(s, "stdio")


async def http() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "websearch", "mcp", "--http"], cwd=ROOT,
                            env={**os.environ, "MCP_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (r, w), ClientSession(r, w) as s:
            await exercise(s, "http")
    finally:
        proc.terminate()
        proc.wait(5)


if __name__ == "__main__":
    asyncio.run(stdio())
    asyncio.run(http())
    print("MCP-Smoke-Test OK")
