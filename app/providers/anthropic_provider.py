from __future__ import annotations

import json
import os
from typing import Callable
from urllib.request import Request, urlopen


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-5",
        opener: Callable = urlopen,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.opener = opener

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def complete(self, *, system: str, user: str) -> str:
        if not self.available():
            raise RuntimeError("ANTHROPIC_API_KEY가 없습니다")
        payload = {
            "model": self.model,
            "max_tokens": 1200,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with self.opener(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        blocks = body.get("content") or []
        return "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict)
        )
