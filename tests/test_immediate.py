import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook

from v2r_auto.immediate_api import (
    MANAGER_ACCOUNTS,
    ImmediateApiPublisher,
    SELF_COMMENT_ACCOUNTS,
)
from v2r_auto.immediate_inputs import (
    load_brand_immediate_jobs,
    load_daily_excel_jobs,
)
from v2r_auto.models import JobStatus
from v2r_auto.history import HistoryStore
from v2r_auto.runner import ImmediateRunner, assign_immediate_schedules


def test_load_brand_sheet_with_board_and_optional_comments(tmp_path: Path) -> None:
    path = tmp_path / "brand.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명\n"
        '"키 워드","제목 : 제목\n본문 : 본문\n댓글1: 질문",고요한아침,,질문형,,,비실명,,극복후기\n'
        '"일상","제목 : 일상 제목\n본문 : 일상 본문",고요한아침,writer,,,,비실명,Y,오늘의 한 끼\n',
        encoding="utf-8-sig",
    )

    jobs = load_brand_immediate_jobs(path, brand="팥순이")

    assert len(jobs) == 2
    assert jobs[0].board == "극복후기"
    assert jobs[0].brand == "팥순이"
    assert jobs[0].comments
    assert jobs[1].comments == []
    assert jobs[1].image_disabled is True


def test_load_daily_excel_a_to_d(tmp_path: Path) -> None:
    path = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "일상"
    sheet.append(["카페명", "게시판명", "각색제목", "각색본문"])
    sheet.append(["고요한아침", "가입인사", "안녕하세요", "반갑습니다"])
    workbook.save(path)

    jobs = load_daily_excel_jobs(path)

    assert len(jobs) == 1
    assert jobs[0].source_kind == "daily"
    assert jobs[0].title == "안녕하세요"
    assert jobs[0].body == "반갑습니다"
    assert jobs[0].comments == []


class FakeImmediatePublisher(ImmediateApiPublisher):
    def _capture_authorization(self) -> None:
        return None

    def _request(self, method, path, payload=None, query=None):
        if path == "/naver_cafes/naver_join_cafes":
            return {
                "naver_join_cafes": [
                    {"cafe_id": 14567700, "pc_cafe_name": "고요한 아침"}
                ]
            }
        if path == "/navers/accounts":
            accounts = [
                {
                    "naver_login_id": "writer-a",
                    "my_info_v2": {"is_real_name": True},
                },
                {
                    "naver_login_id": "writer-b",
                    "my_info_v2": {"is_real_name": True},
                },
                {
                    "naver_login_id": "writer-alias",
                    "my_info_v2": {"is_real_name": False},
                },
                *[
                    {
                        "naver_login_id": account,
                        "my_info_v2": {"is_real_name": True},
                    }
                    for account in MANAGER_ACCOUNTS
                ],
                *[
                    {
                        "naver_login_id": account,
                        "my_info_v2": {"is_real_name": False},
                    }
                    for account in SELF_COMMENT_ACCOUNTS
                ],
            ]
            return {"accounts": accounts}
        if path == "/naver_cafes/naver_join_cafe":
            return {
                "naver_join_cafe": [
                    {"login_id": "writer-a"},
                    {"login_id": "writer-b"},
                    {"login_id": "writer-alias"},
                    *[{"login_id": account} for account in MANAGER_ACCOUNTS],
                    *[{"login_id": account} for account in SELF_COMMENT_ACCOUNTS],
                ]
            }
        if path == "/naver_cafes/board_histories" or path == "/naver_cafe_articles/board_histories":
            return {"histories": []}
        if path == "/naver_cafes/menus":
            return {
                "cafe_menus": [
                    {"menuId": 34, "menuName": "가입인사🌱", "writable": True},
                ]
            }
        raise AssertionError((method, path, query))


def test_prepare_jobs_matches_live_ids_and_rotates_all_writers(tmp_path: Path) -> None:
    path = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["카페명", "게시판명", "각색제목", "각색본문"])
    for index in range(3):
        sheet.append(["고요한아침", "가입인사", f"제목{index}", f"본문{index}"])
    workbook.save(path)
    jobs = load_daily_excel_jobs(path)
    publisher = FakeImmediatePublisher(None, logging.getLogger("test"))

    publisher.prepare_jobs(jobs)

    assert [job.account for job in jobs] == ["writer-a", "writer-b", "writer-a"]
    assert not ({job.account for job in jobs} & MANAGER_ACCOUNTS)
    assert "writer-alias" not in {job.account for job in jobs}
    assert all(job.cafe_id == 14567700 for job in jobs)
    assert all(job.menu_id == 34 for job in jobs)
    assert all(job.status == JobStatus.PENDING for job in jobs)


def test_prepare_jobs_unions_menus_from_all_healthy_accounts(tmp_path: Path) -> None:
    path = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["카페명", "게시판명", "각색제목", "각색본문"])
    sheet.append(["고요한아침", "신혼 가전 후기", "제목", "본문"])
    workbook.save(path)
    jobs = load_daily_excel_jobs(path)

    class PartialMenuPublisher(FakeImmediatePublisher):
        def _request(self, method, request_path, payload=None, query=None):
            if request_path == "/naver_cafes/menus":
                if query["naver_login_id"] == "writer-a":
                    return {
                        "cafe_menus": [
                            {"menuId": 1, "menuName": "🪻가입인사", "writable": True}
                        ]
                    }
                return {
                    "cafe_menus": [
                        {
                            "menuId": 29,
                            "menuName": "🌸신혼 가전 후기",
                            "writable": True,
                        }
                    ]
                }
            return super()._request(method, request_path, payload, query)

    publisher = PartialMenuPublisher(None, logging.getLogger("test"))
    publisher.prepare_jobs(jobs)

    assert jobs[0].menu_id == 29
    assert jobs[0].canonical_board_name == "🌸신혼 가전 후기"
    assert jobs[0].account == "writer-b"


def test_brand_job_can_still_select_non_real_name_account(tmp_path: Path) -> None:
    path = tmp_path / "brand.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명\n"
        '"키워드","제목 : 제목\n본문 : 본문",고요한아침,,,,,비실명,,가입인사\n',
        encoding="utf-8-sig",
    )
    job = load_brand_immediate_jobs(path, brand="팥순이")[0]
    publisher = FakeImmediatePublisher(None, logging.getLogger("test"))

    publisher.prepare_jobs([job])

    assert job.account == "writer-alias"


def test_schedules_accumulate_five_to_fifteen_minutes_per_cafe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["카페명", "게시판명", "각색제목", "각색본문"])
    for index in range(4):
        sheet.append(["고요한아침", "가입인사", f"제목{index}", f"본문{index}"])
    workbook.save(path)
    jobs = load_daily_excel_jobs(path)
    jobs[0].cafe_id = 1
    jobs[1].cafe_id = 1
    jobs[2].cafe_id = 2
    jobs[3].cafe_id = 31670256

    class FixedRandom:
        values = iter((5, 15, 7))

        def randint(self, _minimum, _maximum):
            return next(self.values)

    now = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    assign_immediate_schedules(jobs, now=now, rng=FixedRandom())

    assert jobs[0].scheduled_at == now + timedelta(minutes=5)
    assert jobs[1].scheduled_at == now + timedelta(minutes=20)
    assert jobs[2].scheduled_at == now + timedelta(minutes=7)
    assert jobs[3].scheduled_at is None


def test_duplicate_skip_reason_is_written_to_live_log(
    tmp_path: Path,
    caplog,
) -> None:
    path = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["카페명", "게시판명", "각색제목", "각색본문"])
    sheet.append(["고요한아침", "가입인사", "제목", "본문"])
    workbook.save(path)
    job = load_daily_excel_jobs(path)[0]
    history_path = tmp_path / "history.json"
    HistoryStore(history_path).record(job)

    class DuplicateBrowser:
        def ensure_v2r_login(self, _email, _password):
            return None

        def prepare_immediate_jobs(self, jobs):
            jobs[0].cafe_id = 14567700
            jobs[0].menu_id = 34
            jobs[0].account = "writer"
            jobs[0].canonical_cafe_name = "고요한 아침"
            jobs[0].canonical_board_name = "가입인사"

    runner = ImmediateRunner(
        browser=DuplicateBrowser(),
        history_path=history_path,
        report_dir=tmp_path,
        logger=logging.getLogger("duplicate-log-test"),
    )
    with caplog.at_level(logging.INFO, logger="duplicate-log-test"):
        result, _report = runner.run(
            [job],
            dry_run=False,
            stop_event=threading.Event(),
            progress=lambda _current, _total: None,
        )

    assert result.jobs[0].status == JobStatus.SKIPPED
    assert "행 2 건너뜀: 이전에 발행한 동일 글" in caplog.text
