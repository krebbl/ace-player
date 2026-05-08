import os
import time
import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/data/catalog.db")

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    content_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT,
    source     TEXT,
    last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_channels_category ON channels(category);

CREATE TABLE IF NOT EXISTS search_cache (
    source     TEXT NOT NULL,
    query      TEXT NOT NULL,
    last_run   INTEGER NOT NULL,
    last_count INTEGER NOT NULL,
    last_error TEXT,
    PRIMARY KEY (source, query)
);
CREATE INDEX IF NOT EXISTS idx_search_cache_run ON search_cache(last_run);

CREATE TABLE IF NOT EXISTS import_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS epg_channels (
    id                   INTEGER PRIMARY KEY,
    xmltv_id             TEXT UNIQUE NOT NULL,
    display_name         TEXT,
    matched_channel_name TEXT,
    ace_content_id       TEXT REFERENCES channels(content_id)
);

CREATE TABLE IF NOT EXISTS epg_programs (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL REFERENCES epg_channels(id),
    start       INTEGER NOT NULL,
    stop        INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    category    TEXT
);
CREATE INDEX IF NOT EXISTS idx_epg_programs_ch_time ON epg_programs(channel_id, start, stop);

CREATE TABLE IF NOT EXISTS epg_config (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


async def init():
    global _db
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    await _db.executescript(SCHEMA)
    await _db.commit()


async def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("db not initialized")
    return _db


async def upsert_channels(rows):
    if not rows:
        return 0
    now = int(time.time())
    db = await _conn()
    await db.executemany(
        """INSERT INTO channels(content_id, name, category, source, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(content_id) DO UPDATE SET
               name=excluded.name,
               category=COALESCE(excluded.category, channels.category),
               source=excluded.source,
               last_seen=excluded.last_seen""",
        [(r["content_id"], r["name"], r.get("category"), r["source"], now) for r in rows],
    )
    await db.commit()
    return len(rows)


async def is_query_cached(source: str, query: str, ttl_s: int) -> bool:
    cutoff = int(time.time()) - ttl_s
    db = await _conn()
    cur = await db.execute(
        "SELECT last_run FROM search_cache WHERE source=? AND query=? AND last_error IS NULL",
        (source, query.lower()),
    )
    row = await cur.fetchone()
    return bool(row and row[0] >= cutoff)


async def record_query_run(source: str, query: str, count: int, error: str | None = None):
    db = await _conn()
    await db.execute(
        """INSERT INTO search_cache(source, query, last_run, last_count, last_error)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source, query) DO UPDATE SET
               last_run=excluded.last_run,
               last_count=excluded.last_count,
               last_error=excluded.last_error""",
        (source, query.lower(), int(time.time()), count, error),
    )
    await db.commit()


async def search(q=None, category=None, page=1, page_size=50):
    where, params = [], []
    if q:
        where.append("name LIKE ? COLLATE NOCASE")
        params.append(f"%{q}%")
    if category:
        where.append("category = ?")
        params.append(category)
    sql = "SELECT content_id, name, category, source, last_seen FROM channels"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen DESC, name COLLATE NOCASE LIMIT ? OFFSET ?"
    params.extend([page_size, (page - 1) * page_size])
    db = await _conn()
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    return [
        {"content_id": r[0], "name": r[1], "category": r[2], "source": r[3], "last_seen": r[4]}
        for r in rows
    ]


async def categories():
    db = await _conn()
    cur = await db.execute(
        "SELECT category, COUNT(*) FROM channels WHERE category IS NOT NULL "
        "GROUP BY category ORDER BY category"
    )
    rows = await cur.fetchall()
    return [{"name": r[0], "count": r[1]} for r in rows]


async def recent_searches(limit: int = 10):
    db = await _conn()
    cur = await db.execute(
        """SELECT source, query, last_run, last_count, last_error
             FROM search_cache
             ORDER BY last_run DESC LIMIT ?""",
        (limit,),
    )
    rows = await cur.fetchall()
    return [
        {"source": r[0], "query": r[1], "last_run": r[2], "last_count": r[3], "last_error": r[4]}
        for r in rows
    ]


async def channel_count():
    db = await _conn()
    cur = await db.execute("SELECT COUNT(*) FROM channels")
    (n,) = await cur.fetchone()
    return n


async def get_import_state():
    db = await _conn()
    cur = await db.execute("SELECT value FROM import_state WHERE key = 'imported'")
    row = await cur.fetchone()
    if row is None:
        return {"imported": False, "categories": []}
    import json
    data = json.loads(row[0])
    return {"imported": True, "categories": data.get("categories", [])}


async def set_import_state(categories):
    import json
    db = await _conn()
    await db.execute(
        "INSERT OR REPLACE INTO import_state(key, value) VALUES (?, ?)",
        ("imported", json.dumps({"categories": categories})),
    )
    await db.commit()


async def imported_categories():
    db = await _conn()
    cur = await db.execute(
        "SELECT category, COUNT(*) FROM channels WHERE category IS NOT NULL "
        "GROUP BY category ORDER BY category"
    )
    rows = await cur.fetchall()
    return [{"name": r[0], "count": r[1]} for r in rows]


async def epg_clear():
    db = await _conn()
    await db.execute("DELETE FROM epg_programs")
    await db.execute("DELETE FROM epg_channels")
    await db.commit()


async def epg_store_channels(channels):
    if not channels:
        return []
    db = await _conn()
    ids = []
    for ch in channels:
        cur = await db.execute(
            "INSERT OR IGNORE INTO epg_channels(xmltv_id, display_name, matched_channel_name, ace_content_id) VALUES (?, ?, ?, ?)",
            (ch["xmltv_id"], ch.get("display_name"), ch.get("matched_channel_name"), ch.get("ace_content_id")),
        )
        if cur.lastrowid:
            ids.append(cur.lastrowid)
    await db.commit()
    cur = await db.execute(
        "SELECT id, xmltv_id FROM epg_channels WHERE xmltv_id IN ({})".format(",".join("?" for _ in channels)),
        [ch["xmltv_id"] for ch in channels],
    )
    rows = await cur.fetchall()
    return [{"id": r[0], "xmltv_id": r[1]} for r in rows]


async def epg_store_programs(programs):
    if not programs:
        return 0
    db = await _conn()
    await db.executemany(
        "INSERT OR REPLACE INTO epg_programs(channel_id, start, stop, title, description, category) VALUES (?, ?, ?, ?, ?, ?)",
        [(p["channel_id"], p["start"], p["stop"], p["title"], p.get("description"), p.get("category")) for p in programs],
    )
    await db.commit()
    return len(programs)


async def epg_cleanup_old():
    cutoff = int(time.time()) - 3600
    db = await _conn()
    await db.execute("DELETE FROM epg_programs WHERE stop < ?", (cutoff,))
    await db.commit()


async def epg_set_last_fetch(ts):
    db = await _conn()
    await db.execute("INSERT OR REPLACE INTO epg_config(key, value) VALUES (?, ?)", ("last_fetch", str(ts)))
    await db.commit()


async def epg_get_last_fetch():
    db = await _conn()
    cur = await db.execute("SELECT value FROM epg_config WHERE key = 'last_fetch'")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def epg_now_for_all():
    now = int(time.time())
    db = await _conn()
    cur = await db.execute(
        """SELECT DISTINCT c.ace_content_id, ch.name,
                  p.title, p.start, p.stop, p.description, p.category,
                  np.title as next_title, np.start as next_start, np.stop as next_stop
           FROM epg_channels c
           JOIN channels ch ON ch.content_id = c.ace_content_id
           LEFT JOIN epg_programs p ON p.channel_id = c.id AND p.start <= ? AND p.stop > ?
           LEFT JOIN epg_programs np ON np.channel_id = c.id AND np.start = (
               SELECT MIN(np2.start) FROM epg_programs np2 WHERE np2.channel_id = c.id AND np2.start > ?
           )
           ORDER BY ch.name COLLATE NOCASE""",
        (now, now, now),
    )
    rows = await cur.fetchall()
    return [
        {
            "content_id": r[0],
            "channel_name": r[1],
            "title": r[2],
            "start": r[3],
            "stop": r[4],
            "description": r[5],
            "category": r[6],
            "next_title": r[7],
            "next_start": r[8],
            "next_stop": r[9],
        }
        for r in rows
    ]


async def epg_for_channel(ace_content_id, hours=24):
    now = int(time.time())
    cutoff = now + hours * 3600
    db = await _conn()
    cur = await db.execute(
        """SELECT c.xmltv_id, c.display_name,
                  p.title, p.start, p.stop, p.description, p.category
           FROM epg_channels c
           LEFT JOIN epg_programs p ON p.channel_id = c.id AND p.start >= ? AND p.stop <= ?
           WHERE c.ace_content_id = ?
           ORDER BY p.start""",
        (now, cutoff, ace_content_id),
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    return {
        "xmltv_id": rows[0][0],
        "display_name": rows[0][1],
        "programs": [
            {
                "title": r[2],
                "start": r[3],
                "stop": r[4],
                "description": r[5],
                "category": r[6],
            }
            for r in rows if r[2] is not None
        ],
    }
