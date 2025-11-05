# db_backend.py
import os, json, time

CACHE_TTL = 3 * 24 * 3600  # 3 ngày
USE_TURSO = bool(os.getenv("LIBSQL_URL"))

if not USE_TURSO:
    import sqlite3
    DB_PATH = os.path.join(os.getcwd(), "data", "orders.db")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
else:
    # ⚠️ Dùng client ĐỒNG BỘ
    from libsql_client import create_client_sync
    _client_instance = None

    def _get_client():
        """Khởi tạo libsql client (sync) khi cần."""
        global _client_instance
        if _client_instance is None:
            _client_instance = create_client_sync(
                url=os.environ["LIBSQL_URL"],
                auth_token=os.environ.get("LIBSQL_AUTH_TOKEN")
            )
        return _client_instance



def _now() -> int:
    return int(time.time())


def db_init():
    if USE_TURSO:
        _get_client().execute("""
        CREATE TABLE IF NOT EXISTS product_cache (
            cache_key TEXT PRIMARY KEY,
            items_json TEXT NOT NULL,
            meta_json  TEXT,
            ts INTEGER NOT NULL
        )
        """)
    else:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("""
            CREATE TABLE IF NOT EXISTS product_cache (
                cache_key TEXT PRIMARY KEY,
                items_json TEXT NOT NULL,
                meta_json  TEXT,
                ts INTEGER NOT NULL
            )
            """)
            try:
                con.execute("ALTER TABLE product_cache ADD COLUMN meta_json TEXT")
            except Exception:
                pass
            con.commit()
        finally:
            con.close()


def db_upsert(cache_key: str, items: list, ts: int | None = None, meta: dict | None = None):
    if not cache_key or not items:
        return
    ts = ts or _now()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    items_json = json.dumps(items, ensure_ascii=False)

    if USE_TURSO:
        _get_client().execute(
            "INSERT INTO product_cache(cache_key,items_json,meta_json,ts) "
            "VALUES(:k,:i,:m,:t) "
            "ON CONFLICT(cache_key) DO UPDATE "
            "SET items_json=:i, meta_json=:m, ts=:t",
            {"k": cache_key, "i": items_json, "m": meta_json, "t": ts}
        )
    else:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(
                "INSERT INTO product_cache(cache_key,items_json,meta_json,ts) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE "
                "SET items_json=excluded.items_json, meta_json=excluded.meta_json, ts=excluded.ts",
                (cache_key, items_json, meta_json, ts)
            )
            con.commit()
        finally:
            con.close()


def db_get(cache_key: str):
    """Trả (items, meta) hoặc (None, None)."""
    if not cache_key:
        return None, None

    cutoff = _now() - CACHE_TTL

    if USE_TURSO:
        rs = _get_client().execute(
            "SELECT items_json, meta_json, ts FROM product_cache WHERE cache_key = :k",
            {"k": cache_key}
        )
        row = rs.rows[0] if rs.rows else None
        if not row:
            return None, None

        items_json, meta_json, ts = row[0], row[1], int(row[2])
        if ts < cutoff:
            _get_client().execute("DELETE FROM product_cache WHERE cache_key = :k", {"k": cache_key})
            return None, None
        return (
            json.loads(items_json) if items_json else None,
            json.loads(meta_json) if meta_json else None
        )

    else:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute(
                "SELECT items_json, meta_json, ts FROM product_cache WHERE cache_key=?",
                (cache_key,)
            )
            row = cur.fetchone()
            if not row:
                return None, None
            items_json, meta_json, ts = row
            if _now() - int(ts) > CACHE_TTL:
                try:
                    con.execute("DELETE FROM product_cache WHERE cache_key=?", (cache_key,))
                    con.commit()
                except Exception:
                    pass
                return None, None
            return (
                json.loads(items_json) if items_json else None,
                json.loads(meta_json) if meta_json else None
            )
        finally:
            con.close()


def db_list_spx_keys(limit: int = 50):
    cutoff = _now() - CACHE_TTL

    if USE_TURSO:
        rs = _get_client().execute(
            "SELECT cache_key FROM product_cache "
            "WHERE cache_key LIKE 'SPXVN%' AND ts >= :cut "
            "ORDER BY ts DESC LIMIT :lim",
            {"cut": cutoff, "lim": limit}
        )
        return [r[0] for r in rs.rows]
    else:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute(
                "SELECT cache_key FROM product_cache "
                "WHERE cache_key LIKE 'SPXVN%' AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (cutoff, limit)
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            con.close()


def db_purge_expired():
    cutoff = _now() - CACHE_TTL

    if USE_TURSO:
        _get_client().execute("DELETE FROM product_cache WHERE ts < :cut", {"cut": cutoff})
    else:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("DELETE FROM product_cache WHERE ts < ?", (cutoff,))
            con.commit()
        finally:
            con.close()
