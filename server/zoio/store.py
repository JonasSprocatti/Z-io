"""Banco SQLite pequeno: eventos, aparelhos aprovados, sessões, notificações e chaves."""

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
    received_at REAL NOT NULL,
    detail TEXT
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
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    admin INTEGER NOT NULL DEFAULT 0,
    user TEXT,
    user_agent TEXT,
    created_at REAL NOT NULL,
    approved_at REAL,
    approved_by TEXT,
    last_seen REAL,
    gate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_device ON sessions(device_id);
"""

DEVICE_FIELDS = {"name", "status", "admin", "approved_at", "approved_by", "last_seen", "gate"}

GATE_KIND = "Portao"
GARAGE_KIND = "Garagem"
BELL_KIND = "Campainha"
PORTARIA_KINDS = (GATE_KIND, GARAGE_KIND, BELL_KIND)

EVENT_RETENTION_DAYS = 30


class Store:
    def __init__(self, path: str | Path):
        # timeout: a linha de comando (`zoio aparelhos`) pode escrever junto com o servidor
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        if "device_id" not in self._columns("push_subscriptions"):  # bancos da versão 0.1
            self.db.execute("ALTER TABLE push_subscriptions ADD COLUMN device_id TEXT")
        if "detail" not in self._columns("events"):  # bancos da versão 0.2
            self.db.execute("ALTER TABLE events ADD COLUMN detail TEXT")
        if "gate" not in self._columns("devices"):
            self.db.execute("ALTER TABLE devices ADD COLUMN gate INTEGER NOT NULL DEFAULT 0")
            # quem já administrava o Zóio pode abrir o portão
            self.db.execute("UPDATE devices SET gate = 1 WHERE admin = 1 AND status = 'approved'")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- eventos ----

    def add_event(self, camera: int, kind: str, status: str, dvr_time: str | None, detail: dict | None = None) -> dict:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO events(camera, kind, status, dvr_time, received_at, detail) VALUES (?,?,?,?,?,?)",
            (camera, kind, status, dvr_time, now, json.dumps(detail, ensure_ascii=False) if detail else None),
        )
        self.db.commit()
        return {"id": cur.lastrowid, "camera": camera, "kind": kind, "status": status, "dvr_time": dvr_time, "received_at": now, "detail": detail}

    def events(self, limit: int = 100, before_id: int | None = None, camera: int | None = None, status: str | None = "Start") -> list[dict]:
        """Eventos do mais novo ao mais antigo. Com `status`, os da portaria entram sempre."""
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if camera:
            sql += " AND camera = ?"
            args.append(camera)
        if status:
            sql += f" AND (status = ? OR kind IN ({','.join('?' * len(PORTARIA_KINDS))}))"
            args += [status, *PORTARIA_KINDS]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else None
        return rows

    def prune_events(self) -> None:
        self.db.execute("DELETE FROM events WHERE received_at < ?", (time.time() - EVENT_RETENTION_DAYS * 86400,))
        self.db.commit()

    # ---- notificações ----

    def save_subscription(self, sub: dict, user: str | None, cameras: list[int] | None, device_id: str | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO push_subscriptions(endpoint, data, user, cameras, created_at, device_id) VALUES (?,?,?,?,?,?)",
            (sub["endpoint"], json.dumps(sub), user, json.dumps(cameras) if cameras else None, time.time(), device_id),
        )
        self.db.commit()

    def delete_subscription(self, endpoint: str) -> None:
        self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self.db.commit()

    def delete_device_subscriptions(self, device_id: str) -> None:
        self.db.execute("DELETE FROM push_subscriptions WHERE device_id = ?", (device_id,))
        self.db.commit()

    def subscriptions(self, approved_only: bool = False, admins_only: bool = False, gate_only: bool = False) -> list[dict]:
        """Inscrições de push. Com a aprovação ligada, só as de aparelhos aprovados."""
        sql = (
            "SELECT s.data, s.cameras, s.device_id, d.status, d.admin, d.gate FROM push_subscriptions s "
            "LEFT JOIN devices d ON d.id = s.device_id"
        )
        rows = self.db.execute(sql).fetchall()
        out = []
        for r in rows:
            if (approved_only or admins_only or gate_only) and r["status"] != "approved":
                continue
            if (admins_only and not r["admin"]) or (gate_only and not r["gate"]):
                continue
            out.append({"sub": json.loads(r["data"]), "cameras": json.loads(r["cameras"]) if r["cameras"] else None, "device_id": r["device_id"]})
        return out

    # ---- aparelhos e sessões ----

    def add_device(self, device: dict) -> None:
        cols = ", ".join(device)
        marks = ", ".join("?" for _ in device)
        self.db.execute(f"INSERT INTO devices({cols}) VALUES ({marks})", tuple(device.values()))
        self.db.commit()

    def device(self, device_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def device_by_key(self, public_key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM devices WHERE public_key = ?", (public_key,)).fetchone()
        return dict(row) if row else None

    def devices(self, status: str | None = None) -> list[dict]:
        """Do mais novo para o mais antigo."""
        if status:
            rows = self.db.execute("SELECT * FROM devices WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            rows = self.db.execute("SELECT * FROM devices ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def update_device(self, device_id: str, **fields) -> None:
        unknown = set(fields) - DEVICE_FIELDS
        if unknown:
            raise ValueError(f"campos inválidos: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE devices SET {sets} WHERE id = ?", (*fields.values(), device_id))
        self.db.commit()

    def delete_device(self, device_id: str) -> None:
        self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.db.commit()

    def add_session(self, token_hash: str, device_id: str, created_at: float, expires_at: float) -> None:
        self.db.execute(
            "INSERT INTO sessions(token_hash, device_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token_hash, device_id, created_at, expires_at),
        )
        self.db.commit()

    def session(self, token_hash: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def extend_session(self, token_hash: str, expires_at: float) -> None:
        self.db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?", (expires_at, token_hash))
        self.db.commit()

    def delete_session(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        self.db.commit()

    def delete_sessions(self, device_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE device_id = ?", (device_id,))
        self.db.commit()

    def prune_sessions(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self.db.commit()

    # ---- chave/valor ----

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, value))
        self.db.commit()
