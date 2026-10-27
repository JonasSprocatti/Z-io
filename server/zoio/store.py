"""Banco SQLite pequeno: eventos de movimento, inscrições de notificação e chaves."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    camera INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    dvr_time TEXT,
    received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_received ON events(received_at);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    user TEXT,
    cameras TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

EVENT_RETENTION_DAYS = 30


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- eventos ----

    def add_event(self, camera: int, kind: str, status: str, dvr_time: str | None) -> dict:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO events(camera, kind, status, dvr_time, received_at) VALUES (?,?,?,?,?)",
            (camera, kind, status, dvr_time, now),
        )
        self.db.commit()
        return {"id": cur.lastrowid, "camera": camera, "kind": kind, "status": status, "dvr_time": dvr_time, "received_at": now}

    def events(self, limit: int = 100, before_id: int | None = None, camera: int | None = None, status: str | None = "Start") -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if camera:
            sql += " AND camera = ?"
            args.append(camera)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        return [dict(r) for r in self.db.execute(sql, args)]

    def prune_events(self) -> None:
        self.db.execute("DELETE FROM events WHERE received_at < ?", (time.time() - EVENT_RETENTION_DAYS * 86400,))
        self.db.commit()

    # ---- notificações ----

    def save_subscription(self, sub: dict, user: str | None, cameras: list[int] | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO push_subscriptions(endpoint, data, user, cameras, created_at) VALUES (?,?,?,?,?)",
            (sub["endpoint"], json.dumps(sub), user, json.dumps(cameras) if cameras else None, time.time()),
        )
        self.db.commit()

    def delete_subscription(self, endpoint: str) -> None:
        self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self.db.commit()

    def subscriptions(self) -> list[dict]:
        rows = self.db.execute("SELECT data, cameras FROM push_subscriptions").fetchall()
        return [{"sub": json.loads(r["data"]), "cameras": json.loads(r["cameras"]) if r["cameras"] else None} for r in rows]

    # ---- chave/valor ----

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, value))
        self.db.commit()
