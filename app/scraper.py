"""On-demand fetch of channels from search-ace.stream's JSON API.

The site exposes `GET /search?query=<keyword>` returning a JSON array of
{content_id, name, pid, translated_name}. We hit it directly — no HTML
parsing — and cache per-query results in SQLite for `CACHE_TTL_S`.

For personal use only.
"""

import asyncio
import logging

import httpx

import db

log = logging.getLogger("scraper")

SOURCE = "search-ace.stream"
SEARCH_URL = "https://search-ace.stream/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ace-player-selfhost/1.0)",
    "Accept": "application/json",
    "Referer": "https://search-ace.stream/",
}
CACHE_TTL_S = 60 * 60      # don't re-hit the upstream within an hour
TIMEOUT_S = 10.0


async def _query_upstream(client: httpx.AsyncClient, query: str) -> list[dict]:
    r = await client.get(
        SEARCH_URL,
        params={"query": query},
        headers=HEADERS,
        timeout=TIMEOUT_S,
        follow_redirects=True,
    )
    r.raise_for_status()
    data = r.json()
    rows: list[dict] = []
    for item in data:
        cid = (item.get("content_id") or "").lower()
        if len(cid) != 40:
            continue
        name = item.get("name") or item.get("translated_name") or cid
        rows.append({
            "content_id": cid,
            "name": name[:200].strip() or cid,
            "category": None,
            "source": SOURCE,
        })
    return rows


async def search_live(query: str, force: bool = False) -> dict:
    """Fetch upstream for `query` unless we have a fresh cache hit."""
    query = (query or "").strip()
    if not query:
        return {"skipped": "empty query"}

    if not force and await db.is_query_cached(SOURCE, query, CACHE_TTL_S):
        return {"cached": True, "query": query}

    summary: dict[str, int | str] = {"query": query}
    try:
        async with httpx.AsyncClient(http2=False) as client:
            rows = await asyncio.wait_for(_query_upstream(client, query), timeout=TIMEOUT_S)
        n = await db.upsert_channels(rows)
        await db.record_query_run(SOURCE, query, n)
        summary[SOURCE] = n
    except asyncio.TimeoutError:
        await db.record_query_run(SOURCE, query, 0, error="timeout")
        summary[SOURCE] = "timeout"
    except Exception as e:
        log.exception("upstream fetch failed for %r", query)
        await db.record_query_run(SOURCE, query, 0, error=str(e))
        summary[SOURCE] = f"error: {e}"
    return summary
