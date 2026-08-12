from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import PostJob


class HistoryCorruptedError(RuntimeError):
    pass


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, dict[str, str]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise HistoryCorruptedError(
                    f"중복 이력 파일을 읽지 못했습니다. 파일을 확인하세요: {path}"
                ) from exc
            if not isinstance(loaded, dict):
                raise HistoryCorruptedError(
                    f"중복 이력 파일 형식이 올바르지 않습니다: {path}"
                )
            self._items = loaded

    @staticmethod
    def key(job: PostJob) -> str:
        parts = [job.cafe, job.board, job.title, job.body]
        if getattr(job, "source_kind", "") == "account_test":
            parts.append(job.account)
        payload = "\n".join(parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def contains(self, job: PostJob) -> bool:
        return self.key(job) in self._items

    def get(self, job: PostJob) -> dict[str, str] | None:
        record = self._items.get(self.key(job))
        return dict(record) if record else None

    def record(self, job: PostJob) -> None:
        self._items[self.key(job)] = {
            "title": job.title,
            "cafe": job.cafe,
            "board": job.board,
            "url": job.post_url,
        }
        self._save()

    def remove_urls(self, urls: set[str]) -> int:
        if not urls:
            return 0
        keys = [
            key
            for key, record in self._items.items()
            if record.get("url") in urls
        ]
        for key in keys:
            self._items.pop(key, None)
        if keys:
            self._save()
        return len(keys)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
