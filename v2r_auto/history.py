from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import PostJob


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, dict[str, str]] = {}
        if path.exists():
            try:
                self._items = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._items = {}

    @staticmethod
    def key(job: PostJob) -> str:
        payload = "\n".join((job.cafe, job.board, job.title, job.body))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def contains(self, job: PostJob) -> bool:
        return self.key(job) in self._items

    def record(self, job: PostJob) -> None:
        self._items[self.key(job)] = {
            "title": job.title,
            "cafe": job.cafe,
            "board": job.board,
            "url": job.post_url,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
