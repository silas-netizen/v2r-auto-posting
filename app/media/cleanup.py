from __future__ import annotations

from pathlib import Path


def cleanup_temp(directory: Path, *, keep: set[Path] | None = None) -> list[Path]:
    retained = {item.resolve() for item in keep or set()}
    removed: list[Path] = []
    if not directory.exists():
        return removed
    for path in directory.iterdir():
        if path.is_file() and path.resolve() not in retained:
            path.unlink()
            removed.append(path)
    return removed
