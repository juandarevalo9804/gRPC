"""SQLite persistence for one RUBI hospital node."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SEED_BEDS = (
    ("BOG-CENTRAL-UCI-01", "Hospital Central", "UCI"),
    ("BOG-CENTRAL-UCI-02", "Hospital Central", "UCI"),
    ("BOG-CENTRAL-GEN-01", "Hospital Central", "GENERAL"),
    ("BOG-NORTE-UCI-01", "Hospital Norte", "UCI"),
    ("BOG-NORTE-GEN-01", "Hospital Norte", "GENERAL"),
    ("BOG-NORTE-GEN-02", "Hospital Norte", "GENERAL"),
    ("BOG-SUR-UCI-01", "Hospital Sur", "UCI"),
    ("BOG-SUR-GEN-01", "Hospital Sur", "GENERAL"),
    ("BOG-SUR-GEN-02", "Hospital Sur", "GENERAL"),
)


class Store:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS beds (
                    id TEXT PRIMARY KEY,
                    hospital TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'LIBRE',
                    version INTEGER NOT NULL DEFAULT 0,
                    reservation_request TEXT
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS locks (
                    bed_id TEXT PRIMARY KEY REFERENCES beds(id),
                    coordinator_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            count = connection.execute("SELECT COUNT(*) FROM beds").fetchone()[0]
            if count == 0:
                connection.executemany(
                    "INSERT INTO beds (id, hospital, type) VALUES (?, ?, ?)",
                    SEED_BEDS,
                )

    def list_beds(self, hospital: str = "", since_version: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM beds"
        conditions: list[str] = []
        parameters: list[Any] = []
        if hospital:
            conditions.append("hospital = ?")
            parameters.append(hospital)
        if since_version is not None:
            conditions.append("version > ?")
            parameters.append(since_version)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY hospital, id"
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def get_bed(self, bed_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM beds WHERE id = ?", (bed_id,)).fetchone()
            return dict(row) if row else None

    def try_lock(self, bed_id: str, coordinator_id: str, request_id: str, lease_ms: int) -> tuple[bool, str]:
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bed = connection.execute("SELECT status FROM beds WHERE id = ?", (bed_id,)).fetchone()
            if bed is None:
                return False, "La cama no existe en este nodo."
            if bed["status"] != "LIBRE":
                return False, "La cama ya está reservada."
            lock = connection.execute(
                "SELECT coordinator_id, request_id, expires_at FROM locks WHERE bed_id = ?",
                (bed_id,),
            ).fetchone()
            if lock and lock["expires_at"] > now:
                if lock["request_id"] == request_id and lock["coordinator_id"] == coordinator_id:
                    return True, "Bloqueo ya concedido a esta solicitud."
                return False, "La cama tiene un bloqueo vigente."
            connection.execute("DELETE FROM locks WHERE bed_id = ?", (bed_id,))
            connection.execute(
                "INSERT INTO locks (bed_id, coordinator_id, request_id, expires_at) VALUES (?, ?, ?, ?)",
                (bed_id, coordinator_id, request_id, now + max(lease_ms, 1000) / 1000),
            )
            return True, "Bloqueo concedido."

    def release_lock(self, bed_id: str, request_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM locks WHERE bed_id = ? AND request_id = ?", (bed_id, request_id)
            )
            return cursor.rowcount > 0

    def confirm_reservation(self, bed_id: str, request_id: str, version: int) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bed = connection.execute("SELECT * FROM beds WHERE id = ?", (bed_id,)).fetchone()
            if bed is None:
                return False
            if bed["status"] == "RESERVADA" and bed["reservation_request"] == request_id:
                return True
            lock = connection.execute(
                "SELECT request_id FROM locks WHERE bed_id = ?", (bed_id,)
            ).fetchone()
            if bed["status"] != "LIBRE" or not lock or lock["request_id"] != request_id:
                return False
            connection.execute(
                "UPDATE beds SET status = 'RESERVADA', version = ?, reservation_request = ? WHERE id = ?",
                (max(version, bed["version"] + 1), request_id, bed_id),
            )
            connection.execute("DELETE FROM locks WHERE bed_id = ?", (bed_id,))
            return True

    def apply_replica(self, bed: dict[str, Any]) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT version FROM beds WHERE id = ?", (bed["id"],)).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO beds (id, hospital, type, status, version) VALUES (?, ?, ?, ?, ?)",
                    (bed["id"], bed["hospital"], bed["tipo"], bed["estado"], bed["version"]),
                )
                return True
            if bed["version"] <= current["version"]:
                return False
            connection.execute(
                "UPDATE beds SET hospital = ?, type = ?, status = ?, version = ?, reservation_request = NULL WHERE id = ?",
                (bed["hospital"], bed["tipo"], bed["estado"], bed["version"], bed["id"]),
            )
            connection.execute("DELETE FROM locks WHERE bed_id = ?", (bed["id"],))
            return True