from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.accounts import assign_accounts, eligible_accounts, nickname_change_targets
from app.browser.comments import plan_comment_tree
from app.commands import parse_korean_command
from app.duplicate import compare_manuscripts, highest_match
from app.models import Account
from app.scheduler import KST, plan_slots, window_bounds
from app.sources import EXCLUDED_DOCUMENT_IDS, SourceError, SourceRef, visualization_csv_url


def test_parses_daily_publish_command_without_a_model() -> None:
    spec = parse_korean_command(
        "내일 오전 9시부터 오후 6시까지만 일상 글 20개 올려줘. "
        "아이디 5개 자동으로 쓰고 10분 간격으로 해줘."
    )
    assert spec is not None
    assert spec.task == "publish_daily"
    assert spec.count == 20
    assert spec.account_count == 5
    assert spec.window_start == "09:00"
    assert spec.window_end == "18:00"
    assert spec.interval_minutes == 10
    assert spec.start_date == (datetime.now(KST).date() + timedelta(days=1)).isoformat()
    assert spec.dry_run is True


def test_parses_collect_and_failure_commands() -> None:
    assert parse_korean_command("일상 글 수집해줘").task == "collect_daily"
    assert parse_korean_command("최근 7일 실패 글 점검해줘").task == "inspect_failures"
    assert parse_korean_command("전체 원본 지금 동기화").task == "sync_all_sources"
    assert parse_korean_command("작업 중지").task == "stop"


def test_window_carries_to_next_morning() -> None:
    first = datetime(2026, 9, 19, 17, 55, tzinfo=KST)
    slots = plan_slots(
        count=2,
        start_date="2026-09-19",
        window_start="09:00",
        window_end="18:00",
        interval_minutes=10,
        first_at=first,
    )
    assert slots[0] == first
    assert slots[1] == datetime(2026, 9, 20, 9, 0, tzinfo=KST)


def test_window_supports_midnight_wrap() -> None:
    start, end = window_bounds(date(2026, 9, 19), "23:00", "02:00")
    assert start == datetime(2026, 9, 19, 23, 0, tzinfo=KST)
    assert end == datetime(2026, 9, 20, 2, 0, tzinfo=KST)


def test_duplicate_uses_normalized_exact_and_dice() -> None:
    exact = compare_manuscripts("오늘 날씨!", "본문 입니다.", "오늘 날씨", "본문입니다")
    assert exact.exact is True
    similar = compare_manuscripts("아침 산책", "공원에서 걸었다", "아침 산택", "공원에서 걷는다")
    assert similar.exact is False
    assert similar.score > 0.4
    assert highest_match("새 글", "새 본문", []).score == 0


def test_accounts_exclude_gray_manager_and_do_not_backfill() -> None:
    accounts = [
        Account("ok1", "자사 카페", "V2R"),
        Account("ok2", "자사 카페", "V2R"),
        Account("gray", "자사 카페", "V2R", shade="회색"),
        Account("mgr", "자사 카페", "V2R", grade="매니저"),
        Account("eng", "자사 카페", "V2R", nickname="englishonly"),
        Account("aff", "제휴 작업", "V2R"),
    ]
    eligible = eligible_accounts(accounts, work_type="자사 카페")
    assert [item.login_id for item in eligible] == ["ok1", "ok2", "eng"]
    try:
        assign_accounts(accounts, work_type="자사 카페", account_count=4)
        raise AssertionError("부족 계정을 채웠습니다")
    except ValueError as exc:
        assert "부족" in str(exc)
    targets = nickname_change_targets(accounts)
    assert targets[0]["login_id"] == "eng"


def test_excluded_sheet_is_blocked() -> None:
    ref = SourceRef(
        name="blocked",
        url="https://docs.google.com/spreadsheets/d/1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT/edit",
        gid="0",
    )
    assert ref.document_id in EXCLUDED_DOCUMENT_IDS
    try:
        visualization_csv_url(ref)
        raise AssertionError("제외 원본을 읽었습니다")
    except SourceError as exc:
        assert "제외" in str(exc)


def test_comment_tree_has_twelve_nodes() -> None:
    tree = plan_comment_tree(datetime(2026, 9, 19, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")))
    assert len(tree) == 12
    assert tree[0]["depth"] == 0
    assert tree[-1]["depth"] == 3
