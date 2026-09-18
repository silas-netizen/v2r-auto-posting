from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import ALLOWED_TASKS, TaskSpec


KST = ZoneInfo("Asia/Seoul")

TASK_PATTERNS = (
    ("inspect_failures", re.compile(r"(실패|미완성).*(점검|재시도|모아)")),
    ("sync_all_sources", re.compile(r"전체\s*(원본|시트).*(동기화|갱신)")),
    ("sync_sources", re.compile(r"(원본|시트).*(지금\s*)?(동기화|갱신)")),
    ("collect_daily", re.compile(r"일상\s*글.*(수집|가져와|크롤)")),
    ("open_login", re.compile(r"로그인\s*(창|세션|준비)")),
    ("stop", re.compile(r"(중지|멈춰|중단|취소)")),
    ("status", re.compile(r"(상태|현황|진행)")),
    ("publish_brand", re.compile(r"(브랜드|수정)\s*글")),
    ("publish_info", re.compile(r"정보성\s*글")),
    ("publish_batch", re.compile(r"(일괄|배치)\s*(발행|등록)")),
    ("publish_daily", re.compile(r"(일상\s*글|올려|발행|등록)")),
)

CLOCK = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?")
COUNT = re.compile(r"(?:일상\s*글|글)\s*(\d+)\s*개")
ACCOUNT_COUNT = re.compile(r"(?:아이디|계정)\s*(\d+)\s*개")
INTERVAL = re.compile(r"(\d+)\s*분\s*간격")
CAFE = re.compile(r"(씨씨앙|양평맘|쌍둥이맘|고요한 아침|글로시 마이|웨딩 노트|송도포털|헬씨 트리|러브 인썸|마이 웨딩 드림|태극마케팅센터|소나무마케팅센터)")
BRAND = re.compile(r"(우아덤|코숨핏|뉴더미스|장으뜸|팥순이)")


def _today() -> date:
    return datetime.now(KST).date()


def _clock_to_hhmm(period: str | None, hour: str, minute: str | None) -> str:
    value = int(hour)
    if period == "오후" and value < 12:
        value += 12
    if period == "오전" and value == 12:
        value = 0
    return f"{value:02d}:{int(minute or 0):02d}"


def _start_date(text: str) -> str:
    if "모레" in text:
        return (_today() + timedelta(days=2)).isoformat()
    if "내일" in text:
        return (_today() + timedelta(days=1)).isoformat()
    if "오늘" in text:
        return _today().isoformat()
    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return _today().isoformat()


def parse_korean_command(text: str) -> TaskSpec | None:
    """Parse an explicit Korean command without calling a model."""

    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("task") in ALLOWED_TASKS:
        spec = TaskSpec.from_payload(payload)
        spec.validate()
        return spec

    task = ""
    for name, pattern in TASK_PATTERNS:
        if pattern.search(stripped):
            task = name
            break
    if not task:
        return None

    clocks = CLOCK.findall(stripped)
    window_start = "09:00"
    window_end = "18:00"
    if len(clocks) >= 2:
        window_start = _clock_to_hhmm(*clocks[0])
        window_end = _clock_to_hhmm(*clocks[1])
    elif len(clocks) == 1:
        window_start = _clock_to_hhmm(*clocks[0])

    count_match = COUNT.search(stripped)
    account_match = ACCOUNT_COUNT.search(stripped)
    interval_match = INTERVAL.search(stripped)
    cafe_match = CAFE.search(stripped)
    brand_match = BRAND.search(stripped)
    account_mode = "auto" if "자동" in stripped or not re.search(r"아이디\s+\S+", stripped) else "manual"
    dry_run = not re.search(r"(실제|바로)\s*(발행|등록)", stripped)

    spec = TaskSpec(
        task=task,
        count=int(count_match.group(1)) if count_match else 0,
        account_mode=account_mode,
        account_count=int(account_match.group(1)) if account_match else 0,
        window_start=window_start,
        window_end=window_end,
        interval_minutes=int(interval_match.group(1)) if interval_match else 10,
        start_date=_start_date(stripped),
        cafe=cafe_match.group(1) if cafe_match else "",
        brand=brand_match.group(1) if brand_match else "",
        dry_run=dry_run,
        notes=stripped,
    )
    spec.validate()
    return spec


def describe_spec(spec: TaskSpec) -> str:
    if spec.task == "publish_daily":
        return (
            f"일상 글 {spec.count}개 / 계정 {spec.account_count}개 / "
            f"{spec.window_start}~{spec.window_end} / {spec.interval_minutes}분 간격"
        )
    if spec.task == "collect_daily":
        return "일상 글 공개 수집"
    if spec.task == "inspect_failures":
        return "최근 실패·미완성 점검"
    if spec.task.startswith("sync"):
        return "원고 원본 동기화"
    return spec.task
