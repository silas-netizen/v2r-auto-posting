from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured = os.environ.get("V2R_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def data_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "data"


def config_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "config"


def logs_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs"


def media_cache_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "media-cache"


def browser_profile_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "browser-profile"


def sqlite_path(root: Path | None = None) -> Path:
    return data_dir(root) / "v2r.sqlite"
