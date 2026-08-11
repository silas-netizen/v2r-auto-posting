import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook

from v2r_auto.immediate_api import ImmediateApiPublisher, SELF_COMMENT_ACCOUNTS
from v2r_auto.immediate_inputs import (
    load_brand_immediate_jobs,
    load_daily_excel_jobs,
)
from v2r_auto.models import JobStatus
from v2r_auto.runner import assign_immediate_schedules


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
                    "my_info_v2": {"is_real_name": False},
                },
                {
                    "naver_login_id": "writer-b",
                    "my_info_v2": {"is_real_name": False},
                },
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
    assert all(job.cafe_id == 14567700 for job in jobs)
    assert all(job.menu_id == 34 for job in jobs)
    assert all(job.status == JobStatus.PENDING for job in jobs)


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
