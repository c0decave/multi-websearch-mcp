"""websearch - parallele, captcha-arme Websuche über freie Quellen (mit optionaler Bezahl-Reserve).

    import asyncio
    from websearch import SearchEngine

    async def main():
        async with SearchEngine() as eng:
            res = await eng.search("open source metasearch", n=5, lang="de")
            for r in res["results"]:
                print(r["title"], r["url"])

    asyncio.run(main())
"""

__version__ = "0.3.0rc1"

from .core import Result, SearchResponse, SourceStatus  # noqa: E402
from .engine import DEFAULT_POLICY, KINDS, SearchEngine, TierPolicy  # noqa: E402

__all__ = ["SearchEngine", "TierPolicy", "DEFAULT_POLICY", "KINDS", "Result", "SearchResponse",
           "SourceStatus", "__version__"]
