from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.request import Request, urlopen

from ..commands import parse_korean_command
from ..models import ALLOWED_TASKS, TaskSpec


class SlackChannel:
    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        allowed_channel_ids: list[str] | None = None,
        opener: Callable = urlopen,
    ):
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        raw_ids = os.environ.get("SLACK_ALLOWED_CHANNEL_IDS", "")
        self.allowed_channel_ids = set(
            allowed_channel_ids or [item for item in raw_ids.split(",") if item]
        )
        self.opener = opener

    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def parse_event(self, event: dict[str, Any]) -> TaskSpec | None:
        inner = event.get("event") or event
        channel = str(inner.get("channel") or "")
        text = str(inner.get("text") or "").strip()
        if not text:
            return None
        if self.allowed_channel_ids and channel not in self.allowed_channel_ids:
            raise PermissionError("허용되지 않은 Slack 채널입니다")
        spec = parse_korean_command(text)
        if spec is None or spec.task not in ALLOWED_TASKS:
            raise ValueError("허용된 작업 명령만 실행합니다")
        return spec

    def send(self, text: str) -> None:
        if not self.enabled():
            return
        request = Request(
            self.webhook_url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        self.opener(request, timeout=10)
