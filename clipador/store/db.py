"""Banco SQLite: contrato entre módulos.

Tabelas: streams, segments, chat_messages, moments, clips, posts.
Todos os timestamps são epoch UTC em segundos (float), para alinhar chat e vídeo.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer    TEXT NOT NULL,            -- handle do streamers.yaml
    platform    TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT NOT NULL DEFAULT 'live',   -- live | ended | failed
    dir         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id   INTEGER NOT NULL REFERENCES streams(id),
    idx         INTEGER NOT NULL,
    path        TEXT NOT NULL,
    start_ts    REAL NOT NULL,            -- epoch do primeiro frame (estimado)
    end_ts      REAL NOT NULL,
    duration    REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'recorded',  -- recorded | detecting | detected | failed | deleted
    transcript_path TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_stream ON segments(stream_id, idx);
CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id   INTEGER NOT NULL REFERENCES streams(id),
    ts          REAL NOT NULL,
    username    TEXT,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_stream_ts ON chat_messages(stream_id, ts);

CREATE TABLE IF NOT EXISTS moments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id   INTEGER NOT NULL REFERENCES streams(id),
    segment_id  INTEGER REFERENCES segments(id),
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    peak_ts     REAL NOT NULL,
    score       REAL NOT NULL,
    reasons     TEXT NOT NULL,            -- JSON: {chat:..,audio:..,transcript:..,keywords:..,llm:..,notes:[..]}
    transcript  TEXT,
    words       TEXT,                     -- JSON [[start_ts,end_ts,word],...] absolutos
    status      TEXT NOT NULL DEFAULT 'new',  -- new | waiting_media | editing | clipped | rejected | dupe | failed
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_status ON moments(status);
CREATE INDEX IF NOT EXISTS idx_moments_stream ON moments(stream_id, start_ts);

CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id   INTEGER NOT NULL REFERENCES moments(id),
    path        TEXT NOT NULL,
    cover_path  TEXT,
    post_path   TEXT,
    duration    REAL,
    title       TEXT,
    copy_json   TEXT,                     -- JSON com títulos/descrições/hashtags por rede
    status      TEXT NOT NULL DEFAULT 'ready',   -- ready | approved | rejected | posted
    review_note TEXT,
    created_at  REAL NOT NULL,
    reviewed_at REAL
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     INTEGER NOT NULL REFERENCES clips(id),
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | posting | posted | failed | manual
    scheduled_at REAL,
    posted_at   REAL,
    url         TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     INTEGER NOT NULL REFERENCES clips(id),
    moment_id   INTEGER NOT NULL REFERENCES moments(id),
    decision    TEXT NOT NULL,            -- approved | rejected | recut | reedit | auto | restored
    reason      TEXT,
    score       REAL,
    reasons     TEXT,                     -- JSON dos componentes da detecção (para calibrar depois)
    chosen_title TEXT,
    start_delta REAL,
    end_delta   REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_clip ON reviews(clip_id);
"""

# colunas adicionadas depois da Fase 1 (ALTER TABLE idempotente)
MIGRATIONS = [
    ("clips", "music_path", "TEXT"),
    ("clips", "chosen_title", "TEXT"),
    ("clips", "render_json", "TEXT"),     # layout, zoom, margem da legenda (variação)
    ("moments", "recut_count", "INTEGER NOT NULL DEFAULT 0"),
]


class Database:
    """Wrapper fino sobre sqlite3, seguro para threads (uma conexão por thread)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, col, decl in MIGRATIONS:
            cols = {r["name"] for r in self.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # --- helpers -----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def insert(self, table: str, **values: Any) -> int:
        cols = ", ".join(values)
        qs = ", ".join("?" for _ in values)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(values.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **values: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])

    # --- streams -----------------------------------------------------------
    def create_stream(self, streamer: str, platform: str, url: str, directory: Path, title: str | None = None) -> int:
        return self.insert("streams", streamer=streamer, platform=platform, url=url, title=title,
                           started_at=time.time(), status="live", dir=str(directory))

    def end_stream(self, stream_id: int, status: str = "ended") -> None:
        self.update("streams", stream_id, ended_at=time.time(), status=status)

    def live_streams(self) -> list[dict]:
        return self.fetchall("SELECT * FROM streams WHERE status = 'live'")

    def get_stream(self, stream_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM streams WHERE id = ?", [stream_id])

    # --- segments ----------------------------------------------------------
    def add_segment(self, stream_id: int, idx: int, path: Path, start_ts: float, end_ts: float) -> int:
        return self.insert("segments", stream_id=stream_id, idx=idx, path=str(path), start_ts=start_ts,
                           end_ts=end_ts, duration=end_ts - start_ts, status="recorded", created_at=time.time())

    def get_segment(self, segment_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM segments WHERE id = ?", [segment_id])

    def next_segment_to_detect(self) -> dict | None:
        return self.fetchone("SELECT * FROM segments WHERE status = 'recorded' ORDER BY created_at LIMIT 1")

    def segments_covering(self, stream_id: int, start_ts: float, end_ts: float) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM segments WHERE stream_id = ? AND status != 'deleted' AND end_ts > ? AND start_ts < ? ORDER BY idx",
            [stream_id, start_ts, end_ts])

    def previous_segment(self, segment: dict) -> dict | None:
        return self.fetchone("SELECT * FROM segments WHERE stream_id = ? AND idx = ?",
                             [segment["stream_id"], segment["idx"] - 1])

    def old_processed_segments(self, older_than_ts: float) -> list[dict]:
        """Segmentos detectados antigos sem momento pendente que ainda precise deles."""
        return self.fetchall(
            """SELECT s.* FROM segments s
               WHERE s.status = 'detected' AND s.created_at < ?
                 AND NOT EXISTS (SELECT 1 FROM moments m WHERE m.stream_id = s.stream_id
                                 AND m.status IN ('new','waiting_media','editing')
                                 AND m.end_ts > s.start_ts - 5 AND m.start_ts < s.end_ts + 5)""",
            [older_than_ts])

    # --- chat --------------------------------------------------------------
    def add_chat_messages(self, rows: list[tuple[int, float, str | None, str]]) -> None:
        if not rows:
            return
        self.conn().executemany("INSERT INTO chat_messages (stream_id, ts, username, text) VALUES (?,?,?,?)", rows)

    def chat_between(self, stream_id: int, start_ts: float, end_ts: float) -> list[dict]:
        return self.fetchall(
            "SELECT ts, username, text FROM chat_messages WHERE stream_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
            [stream_id, start_ts, end_ts])

    def chat_count(self, stream_id: int) -> int:
        row = self.fetchone("SELECT COUNT(*) AS n FROM chat_messages WHERE stream_id = ?", [stream_id])
        return int(row["n"]) if row else 0

    # --- moments -----------------------------------------------------------
    def add_moment(self, stream_id: int, segment_id: int | None, start_ts: float, end_ts: float, peak_ts: float,
                   score: float, reasons: dict, transcript: str, words: list, status: str = "new") -> int:
        return self.insert("moments", stream_id=stream_id, segment_id=segment_id, start_ts=start_ts, end_ts=end_ts,
                           peak_ts=peak_ts, score=score, reasons=json.dumps(reasons, ensure_ascii=False),
                           transcript=transcript, words=json.dumps(words, ensure_ascii=False), status=status,
                           created_at=time.time())

    def moments_overlapping(self, stream_id: int, start_ts: float, end_ts: float) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM moments WHERE stream_id = ? AND status NOT IN ('rejected','dupe','failed') AND end_ts > ? AND start_ts < ?",
            [stream_id, start_ts, end_ts])

    def next_moment_to_edit(self) -> dict | None:
        return self.fetchone(
            "SELECT * FROM moments WHERE status IN ('new','waiting_media') ORDER BY score DESC, created_at LIMIT 1")

    def pending_moments(self) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM moments WHERE status IN ('new','waiting_media') ORDER BY score DESC, created_at")

    def get_moment(self, moment_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM moments WHERE id = ?", [moment_id])

    def moments(self, limit: int = 50) -> list[dict]:
        return self.fetchall("SELECT * FROM moments ORDER BY created_at DESC LIMIT ?", [limit])

    # --- clips -------------------------------------------------------------
    def add_clip(self, moment_id: int, path: Path, cover_path: Path | None, post_path: Path | None,
                 duration: float, title: str, copy_data: dict, music_path: Path | None = None,
                 render_info: dict | None = None) -> int:
        return self.insert("clips", moment_id=moment_id, path=str(path),
                           cover_path=str(cover_path) if cover_path else None,
                           post_path=str(post_path) if post_path else None, duration=duration, title=title,
                           copy_json=json.dumps(copy_data, ensure_ascii=False), status="ready", created_at=time.time(),
                           music_path=str(music_path) if music_path else None,
                           render_json=json.dumps(render_info or {}, ensure_ascii=False))

    def clips(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            return self.fetchall("SELECT * FROM clips WHERE status = ? ORDER BY created_at DESC LIMIT ?", [status, limit])
        return self.fetchall("SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", [limit])

    def set_clip_status(self, clip_id: int, status: str, note: str | None = None) -> None:
        self.update("clips", clip_id, status=status, review_note=note, reviewed_at=time.time())

    def get_clip(self, clip_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM clips WHERE id = ?", [clip_id])

    def clips_for_review(self, status: str | None = "ready", limit: int = 100) -> list[dict]:
        """Clipes com dados do momento e da stream (para a fila de aprovação)."""
        sql = """SELECT c.*, m.score, m.reasons, m.transcript, m.start_ts, m.end_ts, m.peak_ts, m.stream_id,
                        m.status AS moment_status, m.recut_count, s.streamer, s.platform, s.title AS stream_title
                 FROM clips c JOIN moments m ON m.id = c.moment_id JOIN streams s ON s.id = m.stream_id"""
        params: list = []
        if status and status != "all":
            sql += " WHERE c.status = ?"
            params.append(status)
        order = "m.score DESC, c.created_at DESC" if status == "ready" else "c.reviewed_at DESC, c.created_at DESC"
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        return self.fetchall(sql, params)

    def clip_counts(self) -> dict[str, int]:
        rows = self.fetchall("SELECT status, COUNT(*) AS n FROM clips GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    # --- reviews -----------------------------------------------------------
    def add_review(self, clip: dict, decision: str, reason: str | None = None, chosen_title: str | None = None,
                   start_delta: float | None = None, end_delta: float | None = None, moment: dict | None = None) -> int:
        moment = moment or self.get_moment(clip["moment_id"]) or {}
        return self.insert("reviews", clip_id=clip["id"], moment_id=clip["moment_id"], decision=decision, reason=reason,
                           score=moment.get("score"), reasons=moment.get("reasons"), chosen_title=chosen_title,
                           start_delta=start_delta, end_delta=end_delta, created_at=time.time())

    def review_stats(self) -> list[dict]:
        """Taxa de aprovação por faixa de nota (para calibrar min_score)."""
        return self.fetchall(
            """SELECT CASE WHEN score < 55 THEN '<55' WHEN score < 70 THEN '55-69' WHEN score < 85 THEN '70-84' ELSE '85+' END AS band,
                      SUM(decision IN ('approved','auto')) AS approved, SUM(decision = 'rejected') AS rejected,
                      SUM(decision IN ('recut','reedit')) AS recut, COUNT(*) AS total
               FROM reviews GROUP BY band ORDER BY MIN(score)""")

    def purgeable_clips(self, older_than_ts: float) -> list[dict]:
        return self.fetchall("SELECT * FROM clips WHERE status IN ('rejected','superseded') AND reviewed_at < ?", [older_than_ts])

    # --- manutenção ----------------------------------------------------------
    def stats(self) -> dict:
        out = {}
        for table in ("streams", "segments", "chat_messages", "moments", "clips", "posts"):
            out[table] = self.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        return out

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
