from __future__ import annotations

import json
import os
from typing import Callable
from urllib.request import Request, urlopen


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4.1-mini",
        opener: Callable = urlopen,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.opener = opener

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def complete(self, *, system: str, user: str) -> str:
        if not self.available():
            raise RuntimeError("OPENAI_API_KEY가 없습니다")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with self.opener(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        return str(body["choices"][0]["message"]["content"])
