from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from .models import Account


MANAGER_GRADES = {"m", "매니저", "manager"}
ENGLISH_ONLY = __import__("re").compile(r"^[a-zA-Z]+$")


def is_excluded(account: Account) -> bool:
    if account.excluded:
        return True
    shade = (account.shade or "").strip().casefold()
    if shade in {"gray", "grey", "회색", "음영"}:
        return True
    return False


def is_manager(account: Account) -> bool:
    return (account.grade or "").strip().casefold() in MANAGER_GRADES


def needs_korean_nickname(account: Account) -> bool:
    nickname = (account.nickname or "").strip()
    return bool(nickname) and ENGLISH_ONLY.fullmatch(nickname) is not None


def eligible_accounts(
    accounts: Iterable[Account],
    *,
    work_type: str,
    linked: str = "V2R",
) -> list[Account]:
    wanted_work = work_type.strip()
    wanted_link = linked.strip().casefold()
    selected: list[Account] = []
    for account in accounts:
        if is_excluded(account) or is_manager(account):
            continue
        if account.work_type.strip() != wanted_work:
            continue
        if account.linked.strip().casefold() != wanted_link:
            continue
        selected.append(account)
    return selected


def assign_accounts(
    accounts: Iterable[Account],
    *,
    work_type: str,
    account_count: int,
    explicit: list[str] | None = None,
    linked: str = "V2R",
) -> list[Account]:
    pool = eligible_accounts(accounts, work_type=work_type, linked=linked)
    if explicit:
        wanted = [item.strip() for item in explicit if item.strip()]
        by_id = {item.login_id: item for item in pool}
        missing = [item for item in wanted if item not in by_id]
        if missing:
            raise ValueError("조건에 맞는 지정 계정이 부족합니다: " + ", ".join(missing))
        selected = [by_id[item] for item in wanted]
    else:
        if account_count <= 0:
            raise ValueError("자동 배정할 아이디 수를 지정하세요")
        if len(pool) < account_count:
            raise ValueError(
                f"조건에 맞는 계정이 부족합니다: 필요 {account_count}개, 사용 가능 {len(pool)}개"
            )
        selected = pool[:account_count]
    return selected


def nickname_change_targets(accounts: Iterable[Account]) -> list[dict[str, str]]:
    return [
        {
            **asdict(account),
            "suggested": "한글+숫자 최대 2자리",
        }
        for account in accounts
        if needs_korean_nickname(account)
    ]
