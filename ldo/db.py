"""SQLite 存储层。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  msg_id     INTEGER PRIMARY KEY,
  posted_at  TEXT,
  author     TEXT,
  topic      TEXT,
  text       TEXT,
  links      TEXT,
  has_media  INTEGER DEFAULT 0,
  simhash    TEXT,
  prescore   INTEGER DEFAULT 0,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
  msg_id     INTEGER,
  version    TEXT,
  model      TEXT,
  label      TEXT,
  score      REAL,
  tags       TEXT,
  reason     TEXT,
  extracted  TEXT,
  created_at TEXT,
  PRIMARY KEY (msg_id, version)
);

CREATE TABLE IF NOT EXISTS pushes (
  msg_id    INTEGER,
  channel   TEXT,
  pushed_at TEXT,
  PRIMARY KEY (msg_id, channel)
);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_verdicts_score ON verdicts(score);
CREATE INDEX IF NOT EXISTS idx_messages_posted ON messages(posted_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def upsert_message(conn: sqlite3.Connection, m: dict) -> bool:
    """返回 True 表示新插入。"""
    cur = conn.execute(
        """INSERT INTO messages(msg_id,posted_at,author,topic,text,links,has_media,simhash,prescore,fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(msg_id) DO NOTHING""",
        (
            m["msg_id"],
            m.get("posted_at"),
            m.get("author"),
            m.get("topic"),
            m.get("text"),
            json.dumps(m.get("links") or [], ensure_ascii=False),
            int(bool(m.get("has_media"))),
            m.get("simhash"),
            int(m.get("prescore") or 0),
            now_iso(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def save_verdict(conn: sqlite3.Connection, msg_id: int, version: str, v: dict, model: str) -> None:
    conn.execute(
        """INSERT INTO verdicts(msg_id,version,model,label,score,tags,reason,extracted,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(msg_id,version) DO UPDATE SET
             model=excluded.model,label=excluded.label,score=excluded.score,
             tags=excluded.tags,reason=excluded.reason,extracted=excluded.extracted,
             created_at=excluded.created_at""",
        (
            msg_id,
            version,
            model,
            v.get("label", "unknown"),
            float(v.get("score") or 0.0),
            json.dumps(v.get("tags") or [], ensure_ascii=False),
            v.get("reason", ""),
            json.dumps(v.get("extracted") or {}, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.commit()


def pending_for_llm(conn: sqlite3.Connection, version: str, min_prescore: int, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.* FROM messages m
           LEFT JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           WHERE v.msg_id IS NULL AND m.prescore >= ?
           ORDER BY m.msg_id ASC LIMIT ?""",
        (version, min_prescore, limit),
    ).fetchall()


def unpushed_above(conn: sqlite3.Connection, version: str, threshold: float, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.*, v.label, v.score, v.reason, v.extracted, v.tags
           FROM messages m
           JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           LEFT JOIN pushes p ON p.msg_id=m.msg_id AND p.channel='push'
           WHERE v.score >= ? AND p.msg_id IS NULL
           ORDER BY v.score DESC, m.msg_id ASC LIMIT ?""",
        (version, threshold, limit),
    ).fetchall()


def digest_rows(conn: sqlite3.Connection, version: str, lo: float, hi: float, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.*, v.label, v.score, v.reason, v.extracted
           FROM messages m JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           WHERE v.score >= ? AND v.score < ? AND m.posted_at >= ?
           ORDER BY v.score DESC LIMIT 100""",
        (version, lo, hi, since_iso),
    ).fetchall()


def mark_pushed(conn: sqlite3.Connection, msg_id: int, channel: str) -> None:
    conn.execute(
        "INSERT INTO pushes(msg_id,channel,pushed_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
        (msg_id, channel, now_iso()),
    )
    conn.commit()
