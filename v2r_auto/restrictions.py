from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RESTRICTION_DAYS = 30


class AccountRestrictionStore:
    """Persist account-level writing restrictions discovered from article failures."""

    def __init__(self, path: Path, logger):
        self.path = path
        self.logger = logger
        self.data: dict[str, Any] = {"accounts": {}, "observed_sources": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data["accounts"] = loaded.get("accounts") or {}
                    self.data["observed_sources"] = (
                        loaded.get("observed_sources") or {}
                    )
            except (OSError, json.JSONDecodeError):
                self.logger.exception("계정 제한 이력 파일을 읽지 못했습니다")

    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def refresh(self, now: datetime | None = None) -> set[str]:
        current = self._now(now)
        expired: list[str] = []
        for account, record in self.data["accounts"].items():
            blocked_until = datetime.fromisoformat(record["blocked_until"])
            if blocked_until <= current:
                expired.append(account)
        for account in expired:
            record = self.data["accounts"].pop(account)
            self.logger.info(
                "%s 계정 30일 자동 제외 종료: %s",
                account,
                record["blocked_until"],
            )
        if expired:
            self.save()
        return set(self.data["accounts"])

    def observe_code_27000(
        self,
        *,
        source_id: str,
        account: str,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        if not source_id or not account:
            return False
        observed = self.data["observed_sources"]
        if source_id in observed:
            return False
        current = self._now(now)
        blocked_until = current + timedelta(days=RESTRICTION_DAYS)
        observed[source_id] = {
            "account": account,
            "detected_at": current.isoformat(),
            "reason": reason,
        }
        existing = self.data["accounts"].get(account)
        if existing:
            existing_until = datetime.fromisoformat(existing["blocked_until"])
            blocked_until = max(blocked_until, existing_until)
        self.data["accounts"][account] = {
            "detected_at": current.isoformat(),
            "blocked_until": blocked_until.isoformat(),
            "reason": reason,
            "source_id": source_id,
        }
        self.save()
        self.logger.warning(
            "%s 계정 코드 27000 감지: %s까지 본문 작성에서 자동 제외",
            account,
            blocked_until.astimezone().strftime("%Y-%m-%d %H:%M"),
        )
        return True

    def blocked_accounts(self, now: datetime | None = None) -> set[str]:
        return self.refresh(now)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
