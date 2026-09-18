from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import JobRecord, JobStatus, TaskSpec


KST = ZoneInfo("Asia/Seoul")


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    task TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cafe TEXT NOT NULL,
                    board TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_cache (
                    key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executor_lease (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    owner TEXT NOT NULL,
                    until TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS photo_combos (
                    source_sha256 TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    combo_index INTEGER NOT NULL,
                    job_id INTEGER,
                    path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source_sha256, slot)
                );
                CREATE TABLE IF NOT EXISTS source_publications (
                    source_key TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL DEFAULT '',
                    cafe TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, row_number, content_hash)
                );
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(KST)

    @staticmethod
    def _stamp(moment: datetime | None = None) -> str:
        return (moment or JobStore._now()).isoformat(timespec="seconds")

    def enqueue(
        self,
        spec: TaskSpec,
        *,
        idempotency_key: str = "",
    ) -> JobRecord:
        spec.validate()
        key = idempotency_key or uuid.uuid4().hex
        now = self._stamp()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing:
                return self._row_to_job(existing)
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    idempotency_key, task, payload_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    spec.task,
                    json.dumps(spec.to_payload(), ensure_ascii=False),
                    JobStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_job(row)

    def acquire(
        self,
        owner: str,
        *,
        lease_seconds: int = 900,
    ) -> JobRecord | None:
        now = self._now()
        deadline = self._stamp(now + timedelta(seconds=lease_seconds))
        stamp = self._stamp(now)
        with self._lock, self._connect() as connection:
            if not self._take_executor(connection, owner, deadline, stamp):
                return None
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.RUNNING.value, owner, deadline, stamp, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return self._row_to_job(updated)

    def _take_executor(
        self,
        connection: sqlite3.Connection,
        owner: str,
        until: str,
        now: str,
    ) -> bool:
        current = connection.execute(
            "SELECT owner, until FROM executor_lease WHERE id = 1"
        ).fetchone()
        if current is None:
            connection.execute(
                "INSERT INTO executor_lease (id, owner, until) VALUES (1, ?, ?)",
                (owner, until),
            )
            return True
        if current["owner"] in {"", owner} or current["until"] <= now:
            connection.execute(
                "UPDATE executor_lease SET owner = ?, until = ? WHERE id = 1",
                (owner, until),
            )
            return True
        return False

    def finish(
        self,
        job_id: int,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> JobRecord:
        stamp = self._stamp()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error = ?,
                    lease_owner = '', lease_until = '', updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    stamp,
                    job_id,
                ),
            )
            connection.execute(
                "UPDATE executor_lease SET owner = '', until = '' WHERE id = 1"
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def cancel_running(self, reason: str = "사용자 중지") -> int:
        stamp = self._stamp()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, lease_owner = '', lease_until = '',
                    updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    reason,
                    stamp,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )
            connection.execute(
                "UPDATE executor_lease SET owner = '', until = '' WHERE id = 1"
            )
            return cursor.rowcount

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def history_corpus(self) -> list[tuple[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT title, body FROM history ORDER BY id DESC"
            ).fetchall()
        return [(row["title"], row["body"]) for row in rows]

    def record_history(
        self,
        *,
        cafe: str,
        board: str,
        title: str,
        body: str,
        url: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO history (cafe, board, title, body, url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cafe, board, title, body, url, self._stamp()),
            )

    def publication_exists(
        self,
        *,
        source_key: str,
        row_number: int,
        content_hash: str,
    ) -> bool:
        if not source_key or not row_number or not content_hash:
            return False
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM source_publications
                WHERE source_key = ? AND row_number = ? AND content_hash = ?
                  AND status IN ('registered', 'published')
                """,
                (source_key, row_number, content_hash),
            ).fetchone()
        return row is not None

    def mark_publication(
        self,
        *,
        source_key: str,
        row_number: int,
        content_hash: str,
        status: str,
        url: str = "",
        account: str = "",
        cafe: str = "",
    ) -> None:
        if status not in {"registered", "published", "failed", "uncertain"}:
            raise ValueError(f"지원하지 않는 발행 상태입니다: {status}")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_publications (
                    source_key, row_number, content_hash, status, url,
                    account, cafe, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, row_number, content_hash) DO UPDATE SET
                    status = excluded.status,
                    url = excluded.url,
                    account = excluded.account,
                    cafe = excluded.cafe,
                    updated_at = excluded.updated_at
                """,
                (
                    source_key,
                    row_number,
                    content_hash,
                    status,
                    url,
                    account,
                    cafe,
                    self._stamp(),
                ),
            )

    def publication_count(self, source_key: str) -> int:
        with self._lock, self._connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_publications
                    WHERE source_key = ?
                      AND status IN ('registered', 'published')
                    """,
                    (source_key,),
                ).fetchone()[0]
            )

    def save_source(self, key: str, payload: Any, status: str = "ok") -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_cache (key, payload_json, synced_at, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    synced_at = excluded.synced_at,
                    status = excluded.status
                """,
                (
                    key,
                    json.dumps(payload, ensure_ascii=False),
                    self._stamp(),
                    status,
                ),
            )

    def load_source(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "payload": json.loads(row["payload_json"]),
            "synced_at": row["synced_at"],
            "status": row["status"],
        }

    def photo_combo(self, source_sha256: str, slot: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT combo_index FROM photo_combos
                WHERE source_sha256 = ? AND slot = ?
                """,
                (source_sha256, slot),
            ).fetchone()
            if row:
                return int(row["combo_index"])
            next_index = (
                connection.execute(
                    """
                    SELECT COALESCE(MAX(combo_index), -1) + 1
                    FROM photo_combos
                    WHERE source_sha256 = ?
                    """,
                    (source_sha256,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO photo_combos (source_sha256, slot, combo_index)
                VALUES (?, ?, ?)
                """,
                (source_sha256, slot, next_index),
            )
            return int(next_index)

    def _row_to_job(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=int(row["id"]),
            idempotency_key=row["idempotency_key"],
            task=row["task"],
            payload=json.loads(row["payload_json"]),
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            lease_owner=row["lease_owner"],
            result=json.loads(row["result_json"] or "{}"),
            error=row["error"],
        )
