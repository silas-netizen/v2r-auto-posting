import logging
import random
import threading
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from v2r_auto.affiliate_api import (
    AffiliateApiError,
    AffiliateApiPublisher,
    AffiliateDailyPending,
    CAFE_DELAYS,
    CCCANG_DAILY_HEAD,
    CCCANG_OLD_BOARD,
    _content_json,
)
from v2r_auto.content import parse_article
from v2r_auto.daily_posts import assign_daily_posts, load_daily_posts
from v2r_auto.models import DailyPost, JobStatus
from v2r_auto.runner import AffiliateRunner, assign_next_affiliate_daily_schedule
from v2r_auto.sheet import load_affiliate_jobs
from v2r_auto.state import JobStateStore


def write_affiliate_csv(tmp_path: Path, completion_url: str = "") -> Path:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크\n"
        '"갓비움 후기","제목 :\n제목입니다\n본문 :\n본문입니다\n댓글1:\n댓글입니다\n대댓글1:\n답글입니다",양평맘,writer,질문형,'
        f"{completion_url}\n",
        encoding="utf-8-sig",
    )
    return path


def test_loads_compact_affiliate_sheet_row(tmp_path: Path) -> None:
    jobs = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)

    job = jobs[0]
    assert job.article.tag == "갓비움후기"
    assert job.cafe == "양평맘"
    assert job.article_type == "질문형"
    assert job.comments[0].children[0].label == "대댓글1"


def test_completion_link_skips_affiliate_row(tmp_path: Path) -> None:
    jobs = load_affiliate_jobs(
        write_affiliate_csv(tmp_path, "https://v2r.example/article"),
        selected_row_number=2,
    )

    assert jobs[0].status == JobStatus.SKIPPED


def test_cccang_revision_board_comes_from_optional_j_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cccang-board.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,"
        "말머리,계정유형,이미지 없음,게시판명\n"
        '"키워드","제목 : 제목\n본문 : 본문",씨씨앙,writer,'
        "질문형,,,,Y,자유수다방\n",
        encoding="utf-8-sig",
    )

    job = load_affiliate_jobs(path, selected_row_number=2)[0]

    assert job.revision_board == "자유수다방"
    assert job.validate() == []


def test_cccang_uses_legacy_g_column_when_keyword_is_blank(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cccang-keyword-fallback.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,"
        "말머리,계정유형,이미지 없음,게시판명\n"
        ',"제목 : 제목\n본문 : 본문",씨씨앙,writer,질문형,,'
        "팍시 다이어트,비실명,Y,\n",
        encoding="utf-8-sig",
    )

    job = load_affiliate_jobs(path, selected_row_number=2)[0]

    assert job.keyword == "팍시 다이어트"
    assert job.prefix == "팍시 다이어트"
    assert job.validate() == []


def test_cccang_rejects_unknown_revision_board(tmp_path: Path) -> None:
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    job.cafe = "씨씨앙"
    job.revision_board = "임의 게시판"

    assert "J열 게시판명" in " ".join(job.validate())


def test_failure_text_in_completion_column_is_retried(tmp_path: Path) -> None:
    jobs = load_affiliate_jobs(
        write_affiliate_csv(
            tmp_path,
            "실패: 사진 첨부에 실패하여 사진 없는 글 등록을 중단했습니다",
        ),
        selected_row_number=2,
    )

    assert jobs[0].status == JobStatus.PENDING
    assert jobs[0].completion_url == ""


def test_missing_account_and_type_marks_affiliate_row_skipped(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        (
            "키워드,본문,카페명,작성계정,원고유형,완료 링크\n"
            '"키워드","제목 : 제목\n본문 : 본문",양평맘,,질문형,\n'
            '"정상 키워드","제목 : 정상 제목\n본문 : 정상 본문",씨씨앙,writer,후기형,\n'
        ),
        encoding="utf-8-sig",
    )

    jobs = load_affiliate_jobs(path)

    assert len(jobs) == 2
    assert jobs[0].status == JobStatus.SKIPPED
    assert jobs[1].row_number == 3
    assert jobs[1].keyword == "정상 키워드"


def test_blank_account_uses_h_column_account_type(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형\n"
        '"키워드","제목 : 제목\n본문 : 본문",씨씨앙,,질문형,,,비실명\n',
        encoding="utf-8-sig",
    )

    job = load_affiliate_jobs(path)[0]

    assert job.account == ""
    assert job.account_type == "비실명"
    assert job.status == JobStatus.PENDING


def test_image_disabled_reads_y_from_i_column(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음\n"
        '"키워드","제목 : 제목\n본문 : {키워드}",씨씨앙,writer,질문형,,,,Y\n',
        encoding="utf-8-sig",
    )

    assert load_affiliate_jobs(path)[0].image_disabled is True


def test_daily_posts_are_matched_to_cafe_without_reuse(tmp_path: Path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text(
        (
            "번호,제목,내용,카페\n"
            '1,분류,"제목 : 첫 일상\n본문 : 첫 본문",양평맘\n'
            '2,분류,"제목 : 둘 일상\n본문 : 둘 본문",양평맘\n'
            '3,분류,"제목 :\n본문 : 깨진 본문",양평맘\n'
        ),
        encoding="utf-8-sig",
    )
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]

    assign_daily_posts(jobs, load_daily_posts(path), random.Random(1))

    assert {job.daily_post.title for job in jobs if job.daily_post} == {"첫 일상", "둘 일상"}


def test_affiliate_daily_schedules_are_random_and_independent_per_cafe(
    tmp_path: Path,
) -> None:
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]
    jobs[0].cafe = "씨씨앙"
    jobs[1].cafe = "씨씨앙"
    jobs[2].cafe = "양평맘"
    now = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
    last_by_cafe: dict[str, datetime] = {}
    rng = random.Random(7)

    for job in jobs:
        assign_next_affiliate_daily_schedule(
            job,
            last_by_cafe,
            now=now,
            rng=rng,
        )

    assert timedelta(minutes=5) <= jobs[0].daily_scheduled_at - now <= timedelta(
        minutes=15
    )
    assert timedelta(minutes=5) <= (
        jobs[1].daily_scheduled_at - jobs[0].daily_scheduled_at
    ) <= timedelta(minutes=15)
    assert timedelta(minutes=5) <= jobs[2].daily_scheduled_at - now <= timedelta(
        minutes=15
    )


def test_cccang_daily_and_revision_both_allow_comments() -> None:
    assert CAFE_DELAYS == {"씨씨앙": 4, "양평맘": 20}
    assert CCCANG_OLD_BOARD["menu_id"] == 2458
    assert CCCANG_DAILY_HEAD == {
        "head_id": 1749,
        "head_name": "댓글 이벤트 X",
    }
    assert (
        AffiliateApiPublisher._write_options(enable_comment=True)[
            "enableComment"
        ]
        is True
    )
    assert (
        AffiliateApiPublisher._comment_permission(
            {
                "naver_cafe_article_destination": {
                    "write_options": {"enableComment": False}
                }
            }
        )
        is False
    )


def test_missing_join_model_is_retryable_account_failure() -> None:
    reason, retryable = AffiliateApiPublisher.classify_failure(
        RuntimeError(
            'code=81 reason=NOT_FOUND_MODEL '
            'extra={"model":"NaverJoinCafeAccount"}'
        )
    )

    assert reason == "실패: 카페 가입 연결정보 없음"
    assert retryable is True


def test_affiliate_revision_uses_planned_daily_time_without_waiting(
    tmp_path: Path,
) -> None:
    class PairPublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("test"))
            self.created = []

        def _capture_authorization(self) -> None:
            return None

        def _resolve_destination(self, job):
            return {
                "cafe_id": 25016228,
                "cafe_name": "씨씨앙",
                "menu_id": 328,
                "menu_name": "자유 수다방",
                "naver_login_id": job.account,
                "target_view_count": 0,
                "use_comment_ai": True,
                "parent_id": None,
            }

        def _retarget_destination(
            self,
            destination,
            *,
            menu_id,
            menu_name,
            head_name=None,
            head_id=None,
        ):
            result = dict(destination)
            result.update(
                {
                    "menu_id": menu_id,
                    "menu_name": menu_name,
                    "head_id": head_id if head_name else None,
                    "head_name": head_name,
                }
            )
            return result

        def _create_source(
            self,
            title,
            body,
            tags,
            destination,
            comments,
            parent_source_id=None,
            content_json=None,
            recovery_statuses=("DONE",),
            enable_comment=True,
        ):
            self.created.append(
                {
                    "destination": dict(destination),
                    "parent": parent_source_id,
                    "enable_comment": enable_comment,
                }
            )
            return f"source-{len(self.created)}"

        def _verify_destination_settings(self, source_id, **kwargs):
            return None

        def _comments(self, job, start_at, cafe_id, comment_accounts=None):
            return []

        def _prepare_revision_content(self, job, destination):
            return _content_json(job.body)

        def _verify(self, source_id, job, start_at, **kwargs):
            return None

    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    job.cafe = "씨씨앙"
    job.daily_post = DailyPost(2, "씨씨앙", "일상", "내용")
    job.daily_scheduled_at = datetime(
        2026,
        8,
        13,
        9,
        10,
        tzinfo=timezone.utc,
    )
    publisher = PairPublisher()

    publisher.publish(job, dry_run=False)

    assert len(publisher.created) == 2
    assert publisher.created[0]["destination"]["start_at"] == (
        "2026-08-13T09:10:00Z"
    )
    assert publisher.created[0]["destination"]["menu_id"] == 2458
    assert publisher.created[0]["destination"]["head_id"] == 1749
    assert publisher.created[0]["enable_comment"] is True
    assert publisher.created[1]["destination"]["start_at"] == (
        "2026-08-13T13:10:00Z"
    )
    assert publisher.created[1]["destination"]["menu_id"] == 2458
    assert publisher.created[1]["destination"]["head_id"] is None
    assert publisher.created[1]["parent"] == "source-1"
    assert publisher.created[1]["enable_comment"] is True

    job.revision_board = "자유수다방"
    moved_publisher = PairPublisher()
    moved_publisher.publish(job, dry_run=False)

    assert moved_publisher.created[0]["destination"]["menu_id"] == 2458
    assert moved_publisher.created[1]["destination"]["menu_id"] == 328
    assert moved_publisher.created[1]["destination"]["head_id"] is None


def test_image_failure_happens_before_any_daily_reservation(
    tmp_path: Path,
) -> None:
    class ImageFailurePublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("image-before-daily-test"))
            self.created = 0

        def _capture_authorization(self):
            return None

        def _resolve_destination(self, job):
            return {
                "cafe_id": 22788814,
                "cafe_name": "양평맘",
                "head_id": None,
                "head_name": None,
                "menu_id": 14,
                "menu_name": "이모저모 이야기💕",
                "naver_login_id": job.account,
                "target_view_count": 0,
                "use_comment_ai": True,
                "parent_id": None,
            }

        def _prepare_revision_content(self, job, destination):
            raise AffiliateApiError("사진 첨부 실패")

        def _create_source(self, *args, **kwargs):
            self.created += 1
            return "must-not-be-created"

    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    job.daily_post = DailyPost(2, "양평맘", "일상", "내용")
    job.daily_scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    publisher = ImageFailurePublisher()

    with pytest.raises(AffiliateApiError, match="사진 첨부 실패"):
        publisher.publish(job, dry_run=False)

    assert publisher.created == 0


def test_api_content_preserves_blank_lines_as_paragraphs() -> None:
    document = json.loads(_content_json("첫 줄\n\n둘째 줄"))
    paragraphs = document["document"]["components"][0]["value"]

    assert [item["nodes"][0]["value"] for item in paragraphs] == [
        "첫 줄",
        "",
        "둘째 줄",
    ]


def test_api_comments_keep_five_roots_and_seven_replies(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    # Expand the small fixture to the production 12-label pattern.
    job.article = parse_article(
        "키워드",
        "제목 : 제목\n본문 : 본문\n"
        + "\n".join(
            [
                "댓글1: c1", "대댓글1: r1",
                "댓글2: c2", "대댓글2: r2", "대대댓글2: rr2", "대대대댓글2: rrr2",
                "댓글3: c3", "대댓글3: r3",
                "댓글4: c4", "대댓글4: r4",
                "댓글5: c5", "대댓글5: r5",
            ]
        ),
    )
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))
    publisher._member = lambda cafe_id, account: {  # type: ignore[method-assign]
        "member_key": f"key-{account}",
        "naver_login_id": account,
        "nick": account,
    }

    comments = publisher._comments(job, datetime.now(timezone.utc), 1)

    assert len(comments) == 5
    assert sum(len(root["comments"]) for root in comments) == 7
    comment2 = comments[1]
    assert comment2["comments"][1]["naver_login_id"] == comment2["naver_login_id"]
    assert comment2["comments"][2]["naver_login_id"] != job.account


def test_api_assignment_uses_actual_real_name_type(tmp_path: Path) -> None:
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]
    jobs[0].account = ""
    jobs[0].account_type = "실명"
    jobs[1].account = ""
    jobs[1].account_type = "비실명"
    jobs[2].account = "real-id"
    jobs[2].account_type = "비실명"

    class FakeAllocator(AffiliateApiPublisher):
        def _capture_authorization(self) -> None:
            return None

        def _request(self, method, path, payload=None, query=None):
            if path == "/naver_cafes/naver_join_cafes":
                return {"cafes": [{"cafe_id": 1, "pc_cafe_name": "양평맘"}]}
            if path == "/navers/accounts":
                return {
                    "accounts": [
                        {
                            "naver_login_id": "real-id",
                            "is_block": False,
                            "is_login_fail": False,
                            "my_info_v2": {"is_real_name": True},
                        },
                        {
                            "naver_login_id": "alias-id",
                            "is_block": False,
                            "is_login_fail": False,
                            "my_info_v2": {"is_real_name": False},
                        },
                    ]
                }
            if path == "/naver_cafes/naver_join_cafe":
                return {
                    "naver_accounts": [
                        {
                            "login_id": "real-id",
                            "member_key": "member-real",
                            "force_drop": False,
                        },
                        {
                            "login_id": "alias-id",
                            "member_key": "member-alias",
                            "force_drop": False,
                        },
                    ]
                }
            if path == "/naver_cafe_articles/board_histories":
                return {"histories": [], "next_token": None}
            raise AssertionError(path)

    allocator = FakeAllocator(None, logging.getLogger("test"))
    assigned = allocator.assign_accounts(jobs)

    assert [job.account for job in assigned] == ["real-id", "alias-id"]
    assert jobs[2].account == "real-id"
    assert jobs[2].account_type == "실명"


def test_comment_time_collision_moves_one_minute() -> None:
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))
    start = datetime(2026, 8, 10, 9, 0, 15, tzinfo=timezone.utc)

    first = publisher._comment("account", "첫 댓글", start)
    second = publisher._comment("account", "둘째 댓글", start)

    first_at = datetime.fromisoformat(first["start_at"].replace("Z", "+00:00"))
    second_at = datetime.fromisoformat(second["start_at"].replace("Z", "+00:00"))
    assert second_at == first_at + timedelta(minutes=1)


def test_comment_bundle_collision_shifts_all_roots_in_source_order(
    tmp_path: Path,
) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    job.article = parse_article(
        "키워드",
        "제목 : 제목\n본문 : 본문\n"
        + "\n".join(
            [
                "댓글1: c1", "대댓글1: r1",
                "댓글2: c2", "대댓글2: r2", "대대댓글2: rr2", "대대대댓글2: rrr2",
                "댓글3: c3", "대댓글3: r3",
                "댓글4: c4", "대댓글4: r4",
                "댓글5: c5", "대댓글5: r5",
            ]
        ),
    )
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))
    publisher._member = lambda cafe_id, account: {  # type: ignore[method-assign]
        "member_key": f"key-{account}",
        "naver_login_id": account,
        "nick": account,
    }
    start = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    occupied = (start + timedelta(minutes=5)).replace(second=0, microsecond=0)
    for account in (
        "quilliant",
        "hunnede",
        "prtchht",
        "chocobbn",
        "chenallo",
        "colpith",
    ):
        publisher.comment_slots[account] = {occupied}

    comments = publisher._comments(job, start, 1)
    root_times = [
        datetime.fromisoformat(comment["start_at"].replace("Z", "+00:00"))
        for comment in comments
    ]

    assert [comment["contents"] for comment in comments] == [
        "c1", "c2", "c3", "c4", "c5"
    ]
    assert root_times == [
        start + timedelta(minutes=6),
        start + timedelta(minutes=7),
        start + timedelta(minutes=8),
        start + timedelta(minutes=9),
        start + timedelta(minutes=10),
    ]


def test_missing_member_grades_refresh_by_cafe_before_publish(
    tmp_path: Path,
) -> None:
    jobs = [
        load_affiliate_jobs(
            write_affiliate_csv(tmp_path),
            selected_row_number=2,
        )[0]
        for _ in range(3)
    ]
    jobs[0].cafe = "씨씨앙"
    jobs[0].account = "oaxastera"
    jobs[1].cafe = "씨씨앙"
    jobs[1].account = "already-ready"
    jobs[2].cafe = "양평맘"
    jobs[2].account = "yang-missing"

    class GradeRefreshPublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("grade-refresh-test"))
            self.authorization = "token"
            self.refreshed: set[tuple[int, str]] = set()
            self.put_payloads: list[dict] = []

        def _request(self, method, path, payload=None, query=None, **kwargs):
            assert path in {
                "/naver_cafes/naver_join_cafe",
                "/naver_cafes/naver_join_cafe/sync/account",
            }
            if method == "PUT":
                self.put_payloads.append(payload)
                self.refreshed.add(
                    (int(payload["cafe_id"]), payload["naver_login_id"])
                )
                return {"naver_account": payload}
            cafe_id = int(query["cafe_id"])
            accounts = (
                ("oaxastera", "already-ready")
                if cafe_id == 25016228
                else ("yang-missing",)
            )
            return {
                "naver_join_cafe": {
                    "cafe_id": cafe_id,
                    "naver_accounts": [
                        {
                            "login_id": account,
                            "member_key": f"key-{account}",
                            "level_info": {
                                "member_level": (
                                    128
                                    if (cafe_id, account) in self.refreshed
                                    or account == "already-ready"
                                    else 1
                                ),
                                "member_level_icon_url": (
                                    "Lv2 회원"
                                    if (cafe_id, account) in self.refreshed
                                    or account == "already-ready"
                                    else ""
                                ),
                            },
                        }
                        for account in accounts
                    ],
                }
            }

    publisher = GradeRefreshPublisher()
    failed = publisher.refresh_assigned_account_grades(jobs)

    assert failed == []
    assert publisher.put_payloads == [
        {"cafe_id": 25016228, "naver_login_id": "oaxastera"},
        {"cafe_id": 22788814, "naver_login_id": "yang-missing"},
    ]
    assert all(job.status == JobStatus.PENDING for job in jobs)


def test_grade_refresh_failure_only_blocks_affected_account(
    tmp_path: Path,
) -> None:
    failed_job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    failed_job.cafe = "씨씨앙"
    failed_job.account = "oaxastera"
    healthy_job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    healthy_job.cafe = "씨씨앙"
    healthy_job.account = "healthy"

    class FailingGradeRefreshPublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("grade-failure-test"))
            self.authorization = "token"

        def _request(self, method, path, payload=None, query=None, **kwargs):
            if method == "PUT":
                raise AffiliateApiError("등급 조회 실패")
            return {
                "naver_join_cafe": {
                    "cafe_id": 25016228,
                    "naver_accounts": [
                        {
                            "login_id": "oaxastera",
                            "member_key": "key-oaxastera",
                            "level_info": {"member_level_name": ""},
                        },
                        {
                            "login_id": "healthy",
                            "member_key": "key-healthy",
                            "level_info": {"member_level_name": "새싹"},
                        },
                    ],
                }
            }

    failed = FailingGradeRefreshPublisher().refresh_assigned_account_grades(
        [failed_job, healthy_job]
    )

    assert failed == [failed_job]
    assert failed_job.status == JobStatus.FAILED
    assert "oaxastera" in failed_job.message
    assert healthy_job.status == JobStatus.PENDING


class FakeAffiliateBrowser:
    def __init__(self) -> None:
        self.published = []

    def ensure_v2r_login(self, email: str, password: str) -> None:
        return None

    def start_affiliate_api_run(self, jobs=None) -> None:
        return None

    def assign_affiliate_accounts(self, jobs):
        return []

    def refresh_affiliate_account_grades(self, jobs):
        return []

    def publish_affiliate_revision(
        self,
        job,
        dry_run: bool,
        resume=None,
        checkpoint=None,
        wait_control=None,
    ) -> str:
        assert dry_run
        self.published.append(job)
        return ""


def test_saved_source_probes_run_in_parallel_with_unknowns_preserved() -> None:
    class ProbePublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("probe-simulation"))
            self.authorization = "token"

        def _request(self, method, path, payload=None, query=None, **kwargs):
            time.sleep(0.06)
            source_id = query["source_id"]
            if source_id == "deleted":
                raise AffiliateApiError(
                    "code=36 reason=DELETED_NAVER_CAFE_ARTICLE_SOURCE"
                )
            if source_id == "uncertain":
                raise AffiliateApiError("V2R 네트워크 요청 실패")
            return {"source_id": source_id}

    urls = {
        f"https://v2r.daboja.im/nc/articleDetail/source-{index}"
        for index in range(22)
    }
    deleted_url = "https://v2r.daboja.im/nc/articleDetail/deleted"
    uncertain_url = "https://v2r.daboja.im/nc/articleDetail/uncertain"
    urls.update({deleted_url, uncertain_url})

    started = time.monotonic()
    results = ProbePublisher().probe_source_urls(urls)
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert results[deleted_url] is True
    assert results[uncertain_url] is None
    assert sum(value is False for value in results.values()) == 22


def test_saved_source_probe_stops_after_first_full_outage_batch() -> None:
    class OutagePublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("outage-simulation"))
            self.authorization = "token"
            self.calls = 0

        def _request(self, method, path, payload=None, query=None, **kwargs):
            self.calls += 1
            time.sleep(0.03)
            raise AffiliateApiError("V2R 네트워크 요청 실패")

    urls = {
        f"https://v2r.daboja.im/nc/articleDetail/source-{index}"
        for index in range(100)
    }
    publisher = OutagePublisher()
    started = time.monotonic()
    results = publisher.probe_source_urls(urls)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert publisher.calls == 6
    assert len(results) == 100
    assert set(results.values()) == {None}


def test_written_status_uses_article_detail_not_removed_history_api() -> None:
    written_at = "2026-08-31T01:30:00Z"

    class DetailStatusPublisher(AffiliateApiPublisher):
        def __init__(self):
            super().__init__(None, logging.getLogger("detail-status-test"))
            self.paths = []

        def _request(self, method, path, payload=None, query=None, **kwargs):
            self.paths.append(path)
            return {
                "naver_cafe_article_source": {
                    "source_id": query["source_id"],
                },
                "naver_cafe_article_destination": {"status": "SUCCESS"},
                "naver_cafe_article_history": {
                    "status": "DONE",
                    "written_at": written_at,
                },
            }

    publisher = DetailStatusPublisher()
    result = publisher._wait_for_written_at("source-id", 31670254)

    assert result == datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc)
    assert publisher.paths == ["/naver_cafe_articles/article"]


def test_affiliate_runner_uses_single_revision_flow(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    browser = FakeAffiliateBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )

    result, report = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=True,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[
            DailyPost(row_number=2, cafe="양평맘", title="일상", body="내용"),
        ],
        source_sheet_url="https://docs.google.com/spreadsheets/d/example/edit?gid=0",
    )

    assert result.succeeded == 1
    assert browser.published == [job]
    assert report.exists()


def test_affiliate_runner_processes_each_daily_revision_pair_before_next_job(
    tmp_path: Path,
) -> None:
    first = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    second = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    first.cafe = "씨씨앙"
    second.cafe = "양평맘"
    events: list[tuple[str, str]] = []

    class PairBrowser(FakeAffiliateBrowser):
        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            assert not dry_run
            events.append(("daily", job.cafe))
            if checkpoint:
                checkpoint(
                    "DAILY_CREATED",
                    daily_source_id=f"daily-{job.cafe}",
                    daily_scheduled_at=job.daily_scheduled_at.isoformat(),
                )
            events.append(("revision", job.cafe))
            return f"https://v2r.example/revision-{job.cafe}"

        def update_completion_link(self, sheet_url, row_number, url):
            return None

    browser = PairBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )

    runner.run(
        jobs=[first, second],
        email="",
        password="",
        dry_run=False,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[
            DailyPost(2, "씨씨앙", "씨씨앙 일상", "내용"),
            DailyPost(3, "양평맘", "양평맘 일상", "내용"),
        ],
        source_sheet_url="https://docs.google.com/spreadsheets/d/example/edit?gid=0",
    )

    assert events == [
        ("daily", "씨씨앙"),
        ("revision", "씨씨앙"),
        ("daily", "양평맘"),
        ("revision", "양평맘"),
    ]


def test_affiliate_runner_recalculates_past_schedule_without_source(
    tmp_path: Path,
) -> None:
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    sheet_url = "https://sheet.example"
    state_path = tmp_path / "jobs.db"
    store = JobStateStore(state_path)
    record = store.load_or_create(sheet_url, job)
    store.update(
        record["job_key"],
        daily_scheduled_at="2026-08-01T00:00:00Z",
    )
    store.close()
    observed: list[datetime] = []

    class ScheduleBrowser(FakeAffiliateBrowser):
        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            observed.append(job.daily_scheduled_at)
            return "https://v2r.example/revision"

        def update_completion_link(self, sheet_url, row_number, url):
            return None

    runner = AffiliateRunner(
        browser=ScheduleBrowser(),  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
        state_path=state_path,
    )
    before = datetime.now(timezone.utc)
    runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=False,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[DailyPost(2, "양평맘", "일상", "내용")],
        source_sheet_url=sheet_url,
    )

    assert len(observed) == 1
    assert observed[0] >= before + timedelta(minutes=5)


def test_affiliate_runner_pause_waits_before_new_api_work(tmp_path: Path) -> None:
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]
    pause_event = threading.Event()
    pause_event.set()
    events: list[str] = []

    class PauseBrowser(FakeAffiliateBrowser):
        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            assert not pause_event.is_set()
            events.append("pair")
            return "https://v2r.example/revision-1"

        def update_completion_link(self, sheet_url, row_number, url):
            return None

    threading.Timer(0.05, pause_event.clear).start()
    started = time.monotonic()
    runner = AffiliateRunner(
        browser=PauseBrowser(),  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )
    runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=False,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[DailyPost(2, "양평맘", "일상", "내용")],
        source_sheet_url="https://sheet.example",
        pause_event=pause_event,
    )

    assert time.monotonic() - started >= 0.04
    assert events == ["pair"]


def test_affiliate_daily_pending_keeps_reservation_for_next_run(
    tmp_path: Path,
) -> None:
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]

    class PendingBrowser(FakeAffiliateBrowser):
        def reserve_affiliate_daily(self, job, resume=None, checkpoint=None):
            if checkpoint:
                checkpoint("DAILY_CREATED", daily_source_id="daily-1")
            return "https://v2r.example/daily-1"

        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            raise AffiliateDailyPending("예약은 유지하고 다음 실행에서 확인")

    runner = AffiliateRunner(
        browser=PendingBrowser(),  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )
    result, _report = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=False,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[DailyPost(2, "양평맘", "일상", "내용")],
        source_sheet_url="https://sheet.example",
    )

    assert result.reserved == 1
    assert job.status == JobStatus.RESERVED


def test_affiliate_repeated_errors_do_not_pause_other_rows(
    tmp_path: Path,
) -> None:
    jobs = [
        load_affiliate_jobs(
            write_affiliate_csv(tmp_path),
            selected_row_number=2,
        )[0]
        for _ in range(6)
    ]

    class RepeatedFailureBrowser(FakeAffiliateBrowser):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            self.calls += 1
            raise RuntimeError("same failure")

        def classify_affiliate_failure(self, error):
            return ("실패: 동일 테스트 오류", False)

    browser = RepeatedFailureBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )
    result, _report = runner.run(
        jobs=jobs,
        email="",
        password="",
        dry_run=True,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[
            DailyPost(index, "양평맘", f"일상 {index}", "내용")
            for index in range(6)
        ],
        source_sheet_url="https://sheet.example",
    )

    assert browser.calls == 6
    assert result.failed == 6
    assert all(
        "전체 일시정지" not in job.message
        for job in jobs
    )


def test_affiliate_runner_retries_with_replacement_account(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    job.account_type = "실명"

    class RetryBrowser(FakeAffiliateBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.sheet_updates = []

        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("NAVER_LOGIN_FAIL")
            return ""

        def classify_affiliate_failure(self, error):
            return ("실패: 네이버 로그인 실패", True)

        def replace_failed_affiliate_account(self, job):
            job.account = "replacement"
            return job.account

        def update_sheet_cell(self, url, column, row, value):
            self.sheet_updates.append((column, row, value))

    browser = RetryBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )

    result, _ = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=True,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[
            DailyPost(row_number=2, cafe="양평맘", title="일상", body="내용"),
        ],
        source_sheet_url="https://docs.google.com/spreadsheets/d/example/edit?gid=0",
    )

    assert result.succeeded == 1
    assert browser.calls == 2
    assert browser.sheet_updates == [("D", 2, "replacement")]


def test_affiliate_runner_recreates_deleted_source_pair(
    tmp_path: Path,
) -> None:
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path),
        selected_row_number=2,
    )[0]

    class DeletedSourceBrowser(FakeAffiliateBrowser):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.resets = 0

        def publish_affiliate_revision(
            self,
            job,
            dry_run: bool,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ) -> str:
            self.calls += 1
            if self.calls == 1:
                resume.update(
                    {
                        "daily_source_id": "deleted-daily",
                        "revision_source_id": "deleted-revision",
                    }
                )
                raise RuntimeError(
                    "code=36 reason=DELETED_NAVER_CAFE_ARTICLE_SOURCE"
                )
            return "https://v2r.example/new-revision"

        def reset_deleted_affiliate_sources(self, job, resume):
            self.resets += 1
            resume.clear()
            job.daily_scheduled_at = None
            job.daily_written_at = None

    browser = DeletedSourceBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )
    result, _report = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=True,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[DailyPost(2, "양평맘", "일상", "내용")],
        source_sheet_url="https://sheet.example",
    )

    assert result.succeeded == 1
    assert browser.calls == 2
    assert browser.resets == 1
    assert job.daily_scheduled_at is not None


def test_completed_affiliate_source_is_probed_then_republished(
    tmp_path: Path,
) -> None:
    old_url = "https://v2r.daboja.im/nc/articleDetail/deleted-revision"
    job = load_affiliate_jobs(
        write_affiliate_csv(tmp_path, completion_url=old_url),
        selected_row_number=2,
    )[0]
    sheet_url = "https://sheet.example"
    state_path = tmp_path / "jobs.db"
    store = JobStateStore(state_path)
    record = store.load_or_create(sheet_url, job)
    store.update(
        record["job_key"],
        stage="COMPLETED",
        daily_source_id="deleted-daily",
        daily_scheduled_at="2026-08-24T10:00:00Z",
        revision_source_id="deleted-revision",
    )
    store.close()

    class CompletedDeletedBrowser(FakeAffiliateBrowser):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def probe_v2r_source_urls(self, urls):
            daily_url = (
                "https://v2r.daboja.im/nc/articleDetail/deleted-daily"
            )
            assert urls == {old_url, daily_url}
            return {old_url: False, daily_url: True}

        def publish_affiliate_revision(
            self,
            job,
            dry_run,
            resume=None,
            checkpoint=None,
            wait_control=None,
        ):
            assert not dry_run
            assert not resume.get("daily_source_id")
            self.calls += 1
            return "https://v2r.daboja.im/nc/articleDetail/new-revision"

        def update_completion_link(self, sheet_url, row_number, url):
            return None

    browser = CompletedDeletedBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("completed-deleted-test"),
        state_path=state_path,
    )
    result, _report = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=False,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[DailyPost(2, "양평맘", "일상", "내용")],
        source_sheet_url=sheet_url,
    )

    assert browser.calls == 1
    assert result.jobs[0].status == JobStatus.SUCCESS
    assert result.jobs[0].revision_url.endswith("new-revision")
