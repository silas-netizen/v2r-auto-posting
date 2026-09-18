from __future__ import annotations

from pathlib import Path


def describe_image(path: Path) -> dict[str, str]:
    return {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "size": str(path.stat().st_size) if path.exists() else "0",
    }
