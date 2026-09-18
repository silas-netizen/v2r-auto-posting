from __future__ import annotations

import json
from typing import Any

from .anthropic_provider import AnthropicProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider


class ModelRouter:
    """Optional model router. Clear commands never enter this class."""

    def __init__(
        self,
        *,
        anthropic: AnthropicProvider | None = None,
        openai: OpenAIProvider | None = None,
        local: LocalProvider | None = None,
    ):
        self.providers = {
            "claude": anthropic or AnthropicProvider(),
            "gpt": openai or OpenAIProvider(),
            "local": local or LocalProvider(),
        }

    def available(self, name: str) -> bool:
        provider = self.providers.get(name)
        return bool(provider and provider.available())

    def complete_json(self, name: str, *, system: str, user: str) -> list[dict[str, Any]]:
        payload = self.complete_object(name, system=system, user=user)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return [item for item in payload["items"] if isinstance(item, dict)]
        raise ValueError("모델이 JSON 배열을 반환하지 않았습니다")

    def complete_object(self, name: str, *, system: str, user: str) -> Any:
        provider = self.providers[name]
        if not provider.available():
            raise RuntimeError(f"{name} 모델이 설정되지 않았습니다")
        text = provider.complete(system=system, user=user)
        return json.loads(_extract_json(text))


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped
    start = stripped.find("{")
    array_start = stripped.find("[")
    if start == -1 or (array_start != -1 and array_start < start):
        start = array_start
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if start == -1 or end == -1:
        raise ValueError("모델 응답에서 JSON을 찾지 못했습니다")
    return stripped[start : end + 1]
