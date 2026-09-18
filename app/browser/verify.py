from __future__ import annotations


def completion_ok(url: str, expected_host: str = "v2r") -> bool:
    text = (url or "").strip()
    if not text:
        return False
    return expected_host in text and "http" in text
