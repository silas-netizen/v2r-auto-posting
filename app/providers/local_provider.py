from __future__ import annotations

import json
import os
from typing import Callable
from urllib.request import Request, urlopen


class LocalProvider:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        opener: Callable = urlopen,
    ):
        self.endpoint = endpoint or os.environ.get("V2R_LOCAL_MODEL_URL", "")
        self.opener = opener

    def available(self) -> bool:
        return bool(self.endpoint.strip())

    def complete(self, *, system: str, user: str) -> str:
        if not self.available():
            raise RuntimeError("V2R_LOCAL_MODEL_URL이 없습니다")
        payload = {"system": system, "user": user}
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with self.opener(request, timeout=30) as response:
            return response.read().decode("utf-8")
