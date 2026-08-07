"""Application configuration loading.

Configuration is layered: values from a YAML file are used as defaults, and any
``${ENV_VAR}`` placeholders are expanded from the process environment so that
secrets never need to live in the config file itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class TargetConfig:
    """Configuration for a single publish target."""

    name: str
    type: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    posts_dir: str = "posts"
    targets: list[TargetConfig] = field(default_factory=list)
    schedule_cron: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        data = _expand_env(data or {})
        targets = [
            TargetConfig(
                name=str(t.get("name", t.get("type", "target"))),
                type=str(t["type"]),
                enabled=bool(t.get("enabled", True)),
                options={k: v for k, v in t.items() if k not in {"name", "type", "enabled"}},
            )
            for t in data.get("targets", [])
        ]
        return cls(
            posts_dir=str(data.get("posts_dir", "posts")),
            targets=targets,
            schedule_cron=data.get("schedule_cron"),
        )

    @classmethod
    def load(cls, path: str | None) -> AppConfig:
        if not path:
            return cls(targets=[TargetConfig(name="console", type="dryrun")])
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls.from_dict(data)

    def enabled_targets(self) -> list[TargetConfig]:
        return [t for t in self.targets if t.enabled]
