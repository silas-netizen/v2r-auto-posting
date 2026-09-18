from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..commands import parse_korean_command
from ..models import ALLOWED_TASKS, TaskSpec


class TelegramChannel:
    def __init__(
        self,
        *,
        token: str | None = None,
        allowed_chat_ids: list[str] | None = None,
        opener: Callable = urlopen,
    ):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        self.allowed_chat_ids = set(allowed_chat_ids or [item for item in raw_ids.split(",") if item])
        self.opener = opener

    def enabled(self) -> bool:
        return bool(self.token and self.allowed_chat_ids)

    def parse_update(self, update: dict[str, Any]) -> TaskSpec | None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = str(message.get("text") or "").strip()
        if not text:
            return None
        if chat_id not in self.allowed_chat_ids:
            raise PermissionError("허용되지 않은 Telegram 대화입니다")
        spec = parse_korean_command(text)
        if spec is None or spec.task not in ALLOWED_TASKS:
            raise ValueError("허용된 작업 명령만 실행합니다")
        return spec

    def send(self, chat_id: str, text: str) -> None:
        if not self.enabled():
            return
        payload = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            method="POST",
        )
        self.opener(request, timeout=10)

    def poll(self, offset: int = 0) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        query = urlencode({"timeout": 0, "offset": offset})
        request = Request(
            f"https://api.telegram.org/bot{self.token}/getUpdates?{query}"
        )
        with self.opener(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
        return list(body.get("result") or [])
