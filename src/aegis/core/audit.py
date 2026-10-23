"""Append-only, tamper-evident audit log (SQLite + SHA-256 hash chain).

Every record stores the hash of the previous record, so editing or deleting any row
breaks the chain and is detected by :meth:`AuditLog.verify`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.core.redaction import redact

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq       INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: str
    actor: str
    action: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


def _digest(seq: int, ts: str, actor: str, action: str, payload: str, prev_hash: str) -> str:
    material = json.dumps([seq, ts, actor, action, payload, prev_hash], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def append(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> AuditRecord:
        safe_payload = json.dumps(redact(payload or {}), sort_keys=True, default=str)
        ts = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev_hash = (row[0] + 1, row[1]) if row else (1, GENESIS_HASH)
            digest = _digest(seq, ts, actor, action, safe_payload, prev_hash)
            self._conn.execute(
                "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, ts, actor, action, safe_payload, prev_hash, digest),
            )
        return AuditRecord(seq, ts, actor, action, json.loads(safe_payload), prev_hash, digest)

    def records(self, action: str | None = None) -> list[AuditRecord]:
        query = "SELECT seq, ts, actor, action, payload, prev_hash, hash FROM audit"
        params: tuple[str, ...] = ()
        if action:
            query += " WHERE action = ?"
            params = (action,)
        rows = self._conn.execute(query + " ORDER BY seq", params).fetchall()
        return [AuditRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows]

    def verify(self) -> tuple[bool, str]:
        """Re-compute the chain. Returns (ok, message)."""
        prev = GENESIS_HASH
        expected_seq = 1
        rows = self._conn.execute(
            "SELECT seq, ts, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq"
        ).fetchall()
        for seq, ts, actor, action, payload, prev_hash, digest in rows:
            if seq != expected_seq:
                return False, f"gap in sequence at {expected_seq} (found {seq})"
            if prev_hash != prev:
                return False, f"broken link at seq {seq}"
            if _digest(seq, ts, actor, action, payload, prev_hash) != digest:
                return False, f"hash mismatch at seq {seq}"
            prev = digest
            expected_seq += 1
        return True, f"ok ({expected_seq - 1} records)"

    def close(self) -> None:
        self._conn.close()
