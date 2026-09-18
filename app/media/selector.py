from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def select_photo_combo(source_sha256: str, files: list[Path], index: int) -> Path | None:
    if not files:
        return None
    return files[index % len(files)]
