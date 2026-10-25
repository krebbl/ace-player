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


async def _query_upstream_for_category(
    client: httpx.AsyncClient, category: str, term: str
) -> list[dict]:
    r = await client.get(
        SEARCH_URL,
        params={"query": term},
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
            "category": category,
            "source": SOURCE,
        })
    return rows


async def import_category(category: str, terms: list[str] | None = None) -> int:
    """Import all channels for a single category from upstream.

    `terms` are the search terms for this category (defaults to the category
    name itself). Results are merged and deduped by content_id; the first
    occurrence wins, so earlier terms keep their channel names.
    """
    terms = terms or [category]
    try:
        rows: list[dict] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(http2=False) as client:
            for term in terms:
                fetched = await asyncio.wait_for(
                    _query_upstream_for_category(client, category, term),
                    timeout=TIMEOUT_S * 3,
                )
                for row in fetched:
                    if row["content_id"] not in seen:
                        seen.add(row["content_id"])
                        rows.append(row)
        n = await db.upsert_channels(rows)
        log.info("imported %d channels for category %r (terms: %r)", n, category, terms)
        return n
    except asyncio.TimeoutError:
        log.error("timeout importing category %r", category)
        return 0
    except Exception:
        log.exception("failed to import category %r", category)
        return 0


async def import_categories(categories: list[str], term_map: dict[str, list[str]] | None = None) -> dict:
    """Import channels for multiple categories sequentially."""
    term_map = term_map or {}
    results = {}
    total = 0
    for cat in categories:
        n = await import_category(cat, term_map.get(cat))
        results[cat] = n
        total += n
    await db.set_import_state(categories)
    return {"total": total, "per_category": results}
