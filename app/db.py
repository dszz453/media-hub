"""SQLite 数据层：曲库、收藏、歌单、播放历史、搜索缓存。

刻意只用标准库 sqlite3，不引入 ORM —— 单文件数据库 + WAL 足够撑住个人自用场景，
也让镜像保持轻量。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .config import settings

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tracks (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT '',
    duration    INTEGER NOT NULL DEFAULT 0,
    source_url  TEXT NOT NULL DEFAULT '',
    audio_path  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'idle',   -- idle|queued|running|ready|failed
    error       TEXT NOT NULL DEFAULT '',
    progress    REAL NOT NULL DEFAULT 0,
    extra       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (platform, source_id)
);

CREATE INDEX IF NOT EXISTS idx_tracks_status  ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_created ON tracks(created_at DESC);

CREATE TABLE IF NOT EXISTS favorites (
    track_id   TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id  TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_played ON history(played_at DESC);

CREATE TABLE IF NOT EXISTS search_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_track_id(platform: str, source_id: str) -> str:
    """平台 + 平台内 ID 的稳定哈希，作为跨表主键。"""
    raw = f"{platform}:{source_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.ensure_dirs()
        conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = _connect()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def init_db() -> None:
    _connect()
    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO playlists (name, created_at) VALUES (?, ?)",
            ("默认歌单", now_iso()),
        )


# ----------------------------------------------------------------------
# tracks
# ----------------------------------------------------------------------
def upsert_track(data: dict[str, Any]) -> str:
    """按 (platform, source_id) 插入或更新，返回 track id。

    已存在时只覆盖"元信息"字段，绝不把已经 ready 的 audio_path 覆盖掉。
    """
    track_id = data.get("id") or make_track_id(data["platform"], data["source_id"])
    ts = now_iso()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO tracks (id, platform, source_id, title, author, cover, duration,
                                source_url, audio_path, status, error, progress, extra,
                                created_at, updated_at)
            VALUES (:id, :platform, :source_id, :title, :author, :cover, :duration,
                    :source_url, :audio_path, :status, :error, :progress, :extra, :ts, :ts)
            ON CONFLICT (platform, source_id) DO UPDATE SET
                title      = COALESCE(NULLIF(excluded.title, ''), tracks.title),
                author     = COALESCE(NULLIF(excluded.author, ''), tracks.author),
                cover      = COALESCE(NULLIF(excluded.cover, ''), tracks.cover),
                duration   = CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE tracks.duration END,
                source_url = COALESCE(NULLIF(excluded.source_url, ''), tracks.source_url),
                extra      = CASE WHEN excluded.extra NOT IN ('', '{}') THEN excluded.extra ELSE tracks.extra END,
                updated_at = excluded.updated_at
            """,
            {
                "id": track_id,
                "platform": data["platform"],
                "source_id": str(data["source_id"]),
                "title": data.get("title") or "",
                "author": data.get("author") or "",
                "cover": data.get("cover") or "",
                "duration": int(data.get("duration") or 0),
                "source_url": data.get("source_url") or "",
                "audio_path": data.get("audio_path") or "",
                "status": data.get("status") or "idle",
                "error": data.get("error") or "",
                "progress": float(data.get("progress") or 0),
                "extra": json.dumps(data.get("extra") or {}, ensure_ascii=False),
                "ts": ts,
            },
        )
        cur.execute("SELECT id FROM tracks WHERE platform=? AND source_id=?",
                    (data["platform"], str(data["source_id"])))
        row = cur.fetchone()
    return row["id"] if row else track_id


def update_track(track_id: str, **fields: Any) -> None:
    allowed = {"title", "author", "cover", "duration", "source_url", "audio_path",
               "status", "error", "progress", "extra"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "extra" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(now_iso())
    params.append(track_id)
    with cursor() as cur:
        cur.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id=?", params)


def get_track(track_id: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM tracks WHERE id=?", (track_id,))
        row = cur.fetchone()
    return _track_row(row) if row else None


def list_tracks(status: str | None = None, platform: str | None = None,
                limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tracks WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if platform:
        sql += " AND platform=?"
        params.append(platform)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_track_row(r) for r in rows]


def _track_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["extra"] = json.loads(item.get("extra") or "{}")
    except json.JSONDecodeError:
        item["extra"] = {}
    item["favorited"] = is_favorite(item["id"])
    return item


# ----------------------------------------------------------------------
# favorites
# ----------------------------------------------------------------------
def is_favorite(track_id: str) -> bool:
    with cursor() as cur:
        cur.execute("SELECT 1 FROM favorites WHERE track_id=?", (track_id,))
        return cur.fetchone() is not None


def add_favorite(track_id: str) -> bool:
    with cursor() as cur:
        cur.execute("INSERT OR IGNORE INTO favorites (track_id, created_at) VALUES (?, ?)",
                    (track_id, now_iso()))
        return cur.rowcount > 0


def remove_favorite(track_id: str) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM favorites WHERE track_id=?", (track_id,))
        return cur.rowcount > 0


def list_favorites() -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """SELECT t.* FROM favorites f JOIN tracks t ON t.id = f.track_id
               ORDER BY f.created_at DESC"""
        )
        rows = cur.fetchall()
    return [_track_row(r) for r in rows]


# ----------------------------------------------------------------------
# playlists
# ----------------------------------------------------------------------
def create_playlist(name: str) -> int:
    with cursor() as cur:
        cur.execute("INSERT OR IGNORE INTO playlists (name, created_at) VALUES (?, ?)",
                    (name, now_iso()))
        cur.execute("SELECT id FROM playlists WHERE name=?", (name,))
        return int(cur.fetchone()["id"])


def list_playlists() -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """SELECT p.id, p.name, p.created_at, COUNT(i.id) AS count
               FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id = p.id
               GROUP BY p.id ORDER BY p.id"""
        )
        return [dict(r) for r in cur.fetchall()]


def delete_playlist(playlist_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))


def add_to_playlist(playlist_id: int, track_id: str) -> None:
    with cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM playlist_items WHERE playlist_id=?",
                    (playlist_id,))
        pos = int(cur.fetchone()["pos"])
        cur.execute(
            """INSERT OR IGNORE INTO playlist_items (playlist_id, track_id, position, created_at)
               VALUES (?, ?, ?, ?)""",
            (playlist_id, track_id, pos, now_iso()),
        )


def remove_from_playlist(playlist_id: int, track_id: str) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM playlist_items WHERE playlist_id=? AND track_id=?",
                    (playlist_id, track_id))


def playlist_tracks(playlist_id: int) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """SELECT t.* FROM playlist_items i JOIN tracks t ON t.id = i.track_id
               WHERE i.playlist_id=? ORDER BY i.position""",
            (playlist_id,),
        )
        rows = cur.fetchall()
    return [_track_row(r) for r in rows]


# ----------------------------------------------------------------------
# history
# ----------------------------------------------------------------------
def add_history(track_id: str) -> None:
    with cursor() as cur:
        cur.execute("INSERT INTO history (track_id, played_at) VALUES (?, ?)",
                    (track_id, now_iso()))
        cur.execute(
            """DELETE FROM history WHERE id NOT IN
               (SELECT id FROM history ORDER BY played_at DESC LIMIT 500)"""
        )


def list_history(limit: int = 100) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """SELECT t.*, MAX(h.played_at) AS played_at
               FROM history h JOIN tracks t ON t.id = h.track_id
               GROUP BY t.id ORDER BY played_at DESC LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
    return [_track_row(r) for r in rows]


# ----------------------------------------------------------------------
# search cache
# ----------------------------------------------------------------------
def cache_get(key: str) -> Any | None:
    if settings.search_cache_ttl <= 0:
        return None
    with cursor() as cur:
        cur.execute("SELECT payload, created_at FROM search_cache WHERE key=?", (key,))
        row = cur.fetchone()
    if not row:
        return None
    if time.time() - row["created_at"] > settings.search_cache_ttl:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def cache_set(key: str, payload: Any) -> None:
    if settings.search_cache_ttl <= 0:
        return
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO search_cache (key, payload, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        cur.execute("DELETE FROM search_cache WHERE created_at < ?",
                    (time.time() - 86400 * 7,))


def cache_clear(prefix: str | None = None) -> int:
    with cursor() as cur:
        if prefix:
            cur.execute("DELETE FROM search_cache WHERE key LIKE ?", (f"{prefix}%",))
        else:
            cur.execute("DELETE FROM search_cache")
        return cur.rowcount


# ----------------------------------------------------------------------
def stats() -> dict[str, Any]:
    with cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS c FROM tracks GROUP BY status")
        by_status = {r["status"]: r["c"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS c FROM favorites")
        favs = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM history")
        hist = cur.fetchone()["c"]
    ready = by_status.get("ready", 0)
    total_bytes = 0
    for item in list_tracks(status="ready", limit=10000):
        try:
            total_bytes += (settings.cache_dir / item["audio_path"]).stat().st_size
        except OSError:
            pass
    return {
        "tracks": sum(by_status.values()),
        "by_status": by_status,
        "favorites": favs,
        "history": hist,
        "cache_bytes": total_bytes,
    }


def iter_ready_tracks() -> Iterable[dict[str, Any]]:
    yield from list_tracks(status="ready", limit=100000)
