"""SQLite persistence for installation tasks.

A single row in ``install_tasks`` is the durable record of one installation.
The firmware blob (max 8 MiB) is stored inline so that, after a process exit,
the original package can be re-sent byte-for-byte without trusting any client
to re-upload it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS install_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    state               TEXT NOT NULL,
    model               TEXT NOT NULL,
    version             INTEGER NOT NULL,
    sha256              TEXT NOT NULL,
    firmware            BLOB NOT NULL,
    manifest_bytes      BLOB NOT NULL,
    signature           TEXT NOT NULL,
    reject_reason       TEXT,
    device_response     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

# States that still own the single-installation slot. A new installation is
# rejected while any task is in one of these states.
OPEN_STATES = ("PENDING", "DISPATCHED", "VERIFYING")
TERMINAL_STATES = ("SUCCESS", "REJECTED")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("database not connected")
        return self._db

    async def create_task(
        self,
        *,
        state: str,
        model: str,
        version: int,
        sha256: str,
        firmware: bytes,
        manifest_bytes: bytes,
        signature: str,
    ) -> int:
        cur = await self.db.execute(
            """
            INSERT INTO install_tasks
                (state, model, version, sha256, firmware, manifest_bytes, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (state, model, version, sha256, firmware, manifest_bytes, signature),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def mark_rejected(self, task_id: int, reason: str) -> None:
        await self.db.execute(
            """
            UPDATE install_tasks
               SET state = 'REJECTED', reject_reason = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE id = ?
            """,
            (reason, task_id),
        )
        await self.db.commit()

    async def update_state(
        self, task_id: int, state: str, device_response: str | None = None
    ) -> None:
        await self.db.execute(
            """
            UPDATE install_tasks
               SET state = ?,
                   device_response = COALESCE(?, device_response),
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE id = ?
            """,
            (state, device_response, task_id),
        )
        await self.db.commit()

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM install_tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def get_open_task(self) -> dict[str, Any] | None:
        """Return the task currently owning the installation slot, if any."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        async with self.db.execute(
            f"SELECT * FROM install_tasks WHERE state IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            OPEN_STATES,
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM install_tasks ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
