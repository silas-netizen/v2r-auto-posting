from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import AffiliateJob


class AnotherInstanceRunningError(RuntimeError):
    pass


class InstanceLock(AbstractContextManager):
    """Keep only one affiliate worker open for a data directory."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AnotherInstanceRunningError(
                "V2R 제휴 프로그램이 이미 실행 중입니다"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


class JobStateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS affiliate_jobs (
                job_key TEXT PRIMARY KEY,
                sheet_url TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                stage TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '',
                daily_source_id TEXT NOT NULL DEFAULT '',
                daily_scheduled_at TEXT NOT NULL DEFAULT '',
                revision_source_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(affiliate_jobs)"
            ).fetchall()
        }
        if "daily_scheduled_at" not in columns:
            self.connection.execute(
                """
                ALTER TABLE affiliate_jobs
                ADD COLUMN daily_scheduled_at TEXT NOT NULL DEFAULT ''
                """
            )
        self.connection.commit()
        self.cleanup()

    @staticmethod
    def identity(sheet_url: str, job: AffiliateJob) -> tuple[str, str]:
        content = "\n".join(
            (
                job.keyword,
                job.title,
                job.body,
                job.cafe,
                job.article_type,
                job.brand,
                job.revision_board,
                "image-disabled" if job.image_disabled else "image-enabled",
            )
        )
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        raw_key = f"{sheet_url}\n{job.row_number}\n{content_hash}"
        return hashlib.sha256(raw_key.encode()).hexdigest(), content_hash

    def load_or_create(self, sheet_url: str, job: AffiliateJob) -> dict[str, Any]:
        job_key, content_hash = self.identity(sheet_url, job)
        now = datetime.now().isoformat()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO affiliate_jobs (
                job_key, sheet_url, row_number, content_hash, stage,
                account, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (job_key, sheet_url, job.row_number, content_hash, job.account, now, now),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM affiliate_jobs WHERE job_key = ?", (job_key,)
        ).fetchone()
        assert row is not None
        return dict(row)

    def update(self, job_key: str, stage: str | None = None, **values: Any) -> None:
        fields = dict(values)
        if stage:
            fields["stage"] = stage
        fields["updated_at"] = datetime.now().isoformat()
        if stage == "COMPLETED":
            fields["completed_at"] = fields["updated_at"]
        names = list(fields)
        sql = ", ".join(f"{name} = ?" for name in names)
        self.connection.execute(
            f"UPDATE affiliate_jobs SET {sql} WHERE job_key = ?",
            [fields[name] for name in names] + [job_key],
        )
        self.connection.commit()

    def increment_attempt(self, job_key: str) -> None:
        self.connection.execute(
            """
            UPDATE affiliate_jobs
            SET attempts = attempts + 1, updated_at = ?
            WHERE job_key = ?
            """,
            (datetime.now().isoformat(), job_key),
        )
        self.connection.commit()

    def reset_sources(self, job_key: str, account: str, error: str) -> None:
        self.update(
            job_key,
            stage="ACCOUNT_ASSIGNED",
            account=account,
            daily_source_id="",
            daily_scheduled_at="",
            revision_source_id="",
            last_error=error,
        )

    def cleanup(self) -> None:
        cutoff = (datetime.now() - timedelta(days=90)).isoformat()
        self.connection.execute(
            """
            UPDATE affiliate_jobs
            SET last_error = ''
            WHERE completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoff,),
        )
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self.connection.close()
