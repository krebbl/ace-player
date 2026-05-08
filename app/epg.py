"""EPG ingestion from XMLTV sources.

Downloads gzipped XMLTV, parses channels and programmes, matches against
our local channel catalog by normalised name, and stores the result in SQLite.
"""

import gzip
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO

import httpx

import db

log = logging.getLogger("epg")

DEFAULT_SOURCES = [
    "http://epg.one/epg2.xml.gz",
]
FETCH_TIMEOUT_S = 60
XMLTV_TIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*([+-]\d{4})$")


def _parse_xmltv_time(ts: str) -> int:
    m = XMLTV_TIME_RE.match(ts)
    if not m:
        return 0
    year, month, day, hour, minute, second, tz = m.groups()
    tz_hours = int(tz[:3])
    tz_mins = int(tz[0] + tz[3:])
    dt = datetime(
        int(year), int(month), int(day),
        int(hour), int(minute), int(second),
        tzinfo=timezone.utc,
    )
    from datetime import timedelta
    dt = dt - timedelta(hours=tz_hours, minutes=tz_mins)
    return int(dt.timestamp())


def _normalize(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[\s\-\_\.]+", "", n)
    n = re.sub(r"(hd|sd|fhd|uhd|4k|1080p|720p)$", "", n)
    n = re.sub(r"\+1$", "", n)
    return n


async def _fetch_sources(sources: list[str]) -> bytes:
    combined = BytesIO()
    async with httpx.AsyncClient(http2=False, timeout=FETCH_TIMEOUT_S) as client:
        for url in sources:
            try:
                r = await client.get(url, follow_redirects=True)
                r.raise_for_status()
                data = gzip.decompress(r.content) if url.endswith(".gz") else r.content
                combined.write(data)
                log.info("fetched EPG source %s (%d bytes)", url, len(data))
            except Exception:
                log.exception("failed to fetch EPG source %s", url)
    return combined.getvalue()


def _parse_xmltv(raw: bytes) -> tuple[dict, dict]:
    root = ET.fromstring(raw)
    channels = {}
    programmes = {}
    for el in root.findall("channel"):
        xmltv_id = el.get("id", "")
        if not xmltv_id:
            continue
        display = el.findtext("display-name") or xmltv_id
        channels[xmltv_id] = {
            "xmltv_id": xmltv_id,
            "display_name": display,
            "norm": _normalize(display),
        }
    for el in root.findall("programme"):
        ch_id = el.get("channel", "")
        start = _parse_xmltv_time(el.get("start", ""))
        stop = _parse_xmltv_time(el.get("stop", ""))
        if not ch_id or not start or not stop:
            continue
        title = el.findtext("title") or ""
        desc = el.findtext("desc") or ""
        cat_el = el.find("category")
        category = cat_el.text if cat_el is not None else ""
        programmes.setdefault(ch_id, []).append({
            "start": start,
            "stop": stop,
            "title": title.strip(),
            "description": desc.strip(),
            "category": category.strip(),
        })
    return channels, programmes


async def fetch_and_store(sources: list[str] | None = None) -> dict:
    sources = sources or DEFAULT_SOURCES
    log.info("starting EPG fetch from %d source(s)", len(sources))
    raw = await _fetch_sources(sources)
    if not raw:
        return {"error": "no data fetched", "matched": 0, "programs": 0}

    epg_channels, epg_programs = _parse_xmltv(raw)
    log.info("parsed %d EPG channels, %d programme entries", len(epg_channels), sum(len(v) for v in epg_programs.values()))

    our_channels = await db.search(page=1, page_size=10000)
    our_norm = { _normalize(ch["name"]): ch for ch in our_channels }

    matched_channels = []
    ch_id_map = {}
    for xmltv_id, ch_info in epg_channels.items():
        norm = ch_info["norm"]
        if norm in our_norm:
            our = our_norm[norm]
            matched_channels.append({
                "xmltv_id": xmltv_id,
                "display_name": ch_info["display_name"],
                "matched_channel_name": our["name"],
                "ace_content_id": our["content_id"],
            })
            ch_id_map[xmltv_id] = our["content_id"]

    log.info("matched %d channels", len(matched_channels))

    await db.epg_clear()
    stored_ch = await db.epg_store_channels(matched_channels)
    ch_id_to_db = { ch["xmltv_id"]: ch["id"] for ch in stored_ch }

    all_programs = []
    now = int(time.time())
    week = 7 * 86400
    for xmltv_id, progs in epg_programs.items():
        if xmltv_id not in ch_id_to_db:
            continue
        db_id = ch_id_to_db[xmltv_id]
        for p in progs:
            if p["stop"] < now - 3600 or p["start"] > now + week:
                continue
            all_programs.append({
                "channel_id": db_id,
                "start": p["start"],
                "stop": p["stop"],
                "title": p["title"],
                "description": p["description"],
                "category": p["category"],
            })

    n_progs = await db.epg_store_programs(all_programs)
    await db.epg_cleanup_old()
    await db.epg_set_last_fetch(now)

    log.info("stored %d programs for %d channels", n_progs, len(stored_ch))
    return {"matched": len(stored_ch), "programs": n_progs}
