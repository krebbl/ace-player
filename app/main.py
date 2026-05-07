import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import acestream
import db
import hls
from scraper import search_live

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

ID_RE = re.compile(r"^[a-fA-F0-9]{40}$")
SEG_RE = re.compile(r"^seg_\d{5}\.ts$")


def _check_id(content_id: str):
    if not ID_RE.match(content_id):
        raise HTTPException(status_code=400, detail="invalid content id")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await hls.manager.start()
    log.info("startup complete")
    yield
    log.info("shutting down")
    await hls.manager.shutdown()
    if db._db:
        await db._db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/api/health")
async def api_health():
    engine_ok = await acestream.health()
    return {
        "engine": "ok" if engine_ok else "down",
        "channels": await db.channel_count(),
        "recent_searches": await db.recent_searches(),
        "active_streams": hls.manager.active(),
    }


@app.get("/api/categories")
async def api_categories():
    return await db.categories()


@app.get("/api/streams")
async def api_streams(q: str | None = None, category: str | None = None, page: int = 1):
    """Search the catalog. If `q` is given, fan out to upstream sources first
    (skipping any that have a fresh cache entry for this query)."""
    page = max(1, page)
    upstream = None
    if q and q.strip():
        upstream = await search_live(q.strip())
    rows = await db.search(q=q, category=category, page=page)
    return {"page": page, "results": rows, "upstream": upstream}


@app.post("/api/refresh")
async def api_refresh(q: str | None = None):
    """Force a fresh upstream fan-out for the given query (cache-busting).
    Calling without a query is a no-op since browsing is cache-only now."""
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="query parameter `q` is required")
    return await search_live(q.strip(), force=True)


@app.post("/api/stop/{content_id}")
async def api_stop(content_id: str):
    _check_id(content_id)
    await hls.manager.stop(content_id)
    return {"ok": True}


def _cors(headers: dict | None = None) -> dict:
    h = {"Access-Control-Allow-Origin": "*"}
    if headers:
        h.update(headers)
    return h


@app.get("/hls/{content_id}/index.m3u8")
async def hls_playlist(content_id: str):
    _check_id(content_id)
    try:
        await hls.manager.ensure(content_id)
    except Exception as e:
        log.exception("ensure failed for %s", content_id)
        raise HTTPException(status_code=502, detail=f"engine error: {e}")
    ok = await hls.manager.wait_for_playlist(content_id, timeout=30.0)
    if not ok:
        raise HTTPException(status_code=504, detail="ffmpeg did not produce a playlist in time")
    hls.manager.touch(content_id)
    hls.manager.mark_playlist_updated(content_id)
    return FileResponse(
        hls.manager.playlist_path(content_id),
        media_type="application/vnd.apple.mpegurl",
        headers=_cors({"Cache-Control": "no-store"}),
    )


async def _is_segment_complete(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        st1 = os.stat(path)
        await asyncio.sleep(0.1)
        st2 = os.stat(path)
        return st1.st_size == st2.st_size and st1.st_size > 0
    except OSError:
        return False


@app.get("/hls/{content_id}/{segment}")
async def hls_segment(content_id: str, segment: str):
    _check_id(content_id)
    if not SEG_RE.match(segment):
        raise HTTPException(status_code=400, detail="invalid segment name")
    path = os.path.join(hls.manager.stream_dir(content_id), segment)
    if not await _is_segment_complete(path):
        raise HTTPException(status_code=404, detail="segment not ready")
    hls.manager.touch(content_id)
    return FileResponse(
        path,
        media_type="video/mp2t",
        headers=_cors({"Cache-Control": "max-age=10"}),
    )


# Static UI is mounted last so /api and /hls take precedence.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
