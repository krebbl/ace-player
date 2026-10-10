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


async def all_channels(category: str | None = None):
    db = await _conn()
    sql = "SELECT content_id, name, category FROM channels"
    params: list = []
    if category:
        sql += " WHERE category = ? COLLATE NOCASE"
        params.append(category)
    sql += " ORDER BY name COLLATE NOCASE"
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    return [{"content_id": r[0], "name": r[1], "category": r[2]} for r in rows]


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


async def add_channel(content_id: str, name: str):
    now = int(time.time())
    db = await _conn()
    await db.execute(
        """INSERT INTO channels(content_id, name, category, source, last_seen)
           VALUES (?, ?, 'Custom', 'Manual', ?)
           ON CONFLICT(content_id) DO UPDATE SET
               name=excluded.name,
               last_seen=excluded.last_seen""",
        (content_id, name, now),
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
