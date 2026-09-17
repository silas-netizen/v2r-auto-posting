from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

from v2r_auto.content import parse_article
from v2r_auto.models import AffiliateJob, DailyPost, ImmediateJob
from v2r_auto import web_publish
from v2r_auto.web_publish import WebPublisher, assign_first_available_accounts


class FakeBrowser:
    def __init__(self, available_account: str = "ui-writer") -> None:
        self.available_account = available_account
        self.operations: list[tuple] = []
        self.urls = iter(
            (
                "https://v2r.example/nc/articleDetail/daily",
                "https://v2r.example/nc/articleDetail/revision",
                "https://v2r.example/nc/articleDetail/immediate",
            )
        )

    def first_available_account(self, cafe, board, account_type):
        self.operations.append(("first_account", cafe, board, account_type))
        return self.available_account

    def open_writer(self):
        self.operations.append(("open_writer",))

    def select_destination(self, cafe, account, board, prefix):
        self.operations.append(("destination", cafe, account, board, prefix))

    def select_publish_mode(self, mode):
        self.operations.append(("mode", mode))

    def set_schedule(self, scheduled_at):
        self.operations.append(("schedule", scheduled_at))

    def fill_article(self, title, body, tags):
        self.operations.append(("article", title, body, tuple(tags)))

    def upload_images(self, paths):
        self.operations.append(("images", tuple(paths)))

    def register(self):
        url = next(self.urls)
        self.operations.append(("register", url))
        return url

    def open_article(self, completion_url):
        self.operations.append(("open_article", completion_url))

    def reserve_revision(self, scheduled_at):
        self.operations.append(("reserve_revision", scheduled_at))

    def reserve_comment(self, text, *, parent_text, scheduled_at):
        self.operations.append(
            ("reserve_comment", text, parent_text, scheduled_at)
        )


def test_affiliate_ui_sequence_covers_daily_revision_and_comments() -> None:
    article = parse_article(
        "키워드",
        "제목 : 수정 제목\n본문 : 수정 본문\n댓글1: 첫 댓글\n대댓글1: 첫 답글",
    )
    daily_at = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    revision_at = datetime(2026, 9, 17, 23, 0, tzinfo=timezone.utc)
    job = AffiliateJob(
        row_number=2,
        keyword="키워드",
        article=article,
        cafe="양평맘",
        account="sheet-writer",
        article_type="질문형",
        daily_post=DailyPost(7, "양평맘", "일상 제목", "일상 본문"),
        daily_scheduled_at=daily_at,
    )
    browser = FakeBrowser()

    result = WebPublisher(browser).publish_affiliate(job)

    daily_url = "https://v2r.example/nc/articleDetail/daily"
    revision_url = "https://v2r.example/nc/articleDetail/revision"
    assert result == revision_url
    assert browser.operations == [
        ("open_writer",),
        ("destination", "양평맘", "sheet-writer", "이모저모 이야기", ""),
        ("mode", "scheduled"),
        ("schedule", daily_at),
        ("article", "일상 제목", "일상 본문", ()),
        ("register", daily_url),
        ("open_article", daily_url),
        ("reserve_revision", revision_at),
        ("article", "수정 제목", "수정 본문", ("키워드",)),
        ("register", revision_url),
        ("open_article", revision_url),
        (
            "reserve_comment",
            "첫 댓글",
            None,
            datetime(2026, 9, 17, 23, 5, tzinfo=timezone.utc),
        ),
        (
            "reserve_comment",
            "첫 답글",
            "첫 댓글",
            datetime(2026, 9, 17, 23, 6, tzinfo=timezone.utc),
        ),
    ]


def test_immediate_ui_supports_scheduled_mode_and_completion_url() -> None:
    scheduled_at = datetime(2026, 9, 18, 4, 30, tzinfo=timezone.utc)
    job = ImmediateJob(
        row_number=3,
        article=parse_article(
            "태그",
            "제목 : 예약 제목\n본문 : 예약 본문\n댓글1: 예약 댓글",
        ),
        cafe="헬씨트리",
        board="건강 게시판",
        account="",
        account_type="실명",
        canonical_cafe_name="헬씨 트리",
        canonical_board_name="건강 이야기",
        canonical_head_name="공지",
        scheduled_at=scheduled_at,
    )
    browser = FakeBrowser()

    result = WebPublisher(browser).publish_immediate(job)

    completion_url = "https://v2r.example/nc/articleDetail/daily"
    assert result == completion_url
    assert job.account == "ui-writer"
    assert browser.operations == [
        ("first_account", "헬씨트리", "건강 게시판", "실명"),
        ("open_writer",),
        ("destination", "헬씨 트리", "ui-writer", "건강 이야기", "공지"),
        ("mode", "scheduled"),
        ("schedule", scheduled_at),
        ("article", "예약 제목", "예약 본문", ("태그",)),
        ("register", completion_url),
        ("open_article", completion_url),
        (
            "reserve_comment",
            "예약 댓글",
            None,
            datetime(2026, 9, 18, 4, 35, tzinfo=timezone.utc),
        ),
    ]


def test_immediate_dry_run_fills_ui_without_registering() -> None:
    job = ImmediateJob(
        row_number=4,
        article=parse_article("태그", "제목 : 즉시 제목\n본문 : 즉시 본문"),
        cafe="송도포털",
        board="자유게시판",
        account="sheet-account",
        publish_immediately=True,
    )
    browser = FakeBrowser()

    result = WebPublisher(browser).publish_immediate(job, dry_run=True)

    assert result == ""
    assert browser.operations == [
        ("open_writer",),
        ("destination", "송도포털", "sheet-account", "자유게시판", ""),
        ("mode", "immediate"),
        ("article", "즉시 제목", "즉시 본문", ("태그",)),
    ]


def test_account_fallback_preserves_explicit_sheet_accounts() -> None:
    explicit = ImmediateJob(
        row_number=2,
        article=parse_article("명시", "제목 : 명시\n본문 : 본문"),
        cafe="카페",
        board="게시판",
        account="sheet-account",
    )
    blank = ImmediateJob(
        row_number=3,
        article=parse_article("자동", "제목 : 자동\n본문 : 본문"),
        cafe="카페",
        board="게시판",
        account="",
        account_type="비실명",
    )
    browser = FakeBrowser("first-visible")

    assigned = assign_first_available_accounts([explicit, blank], browser)

    assert explicit.account == "sheet-account"
    assert blank.account == "first-visible"
    assert assigned == [blank]
    assert browser.operations == [
        ("first_account", "카페", "게시판", "비실명")
    ]


def test_web_publish_module_has_no_http_client_or_api_host() -> None:
    source = inspect.getsource(web_publish).casefold()

    assert "api-v2r" not in source
    for forbidden in (
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "http.client",
        "urlopen",
    ):
        assert forbidden not in source


def test_prepared_images_use_browser_file_upload_and_remove_markers() -> None:
    job = ImmediateJob(
        row_number=5,
        article=parse_article(
            "태그",
            "제목 : 사진 글\n본문 : 첫 문단\n{사진1}\n마지막 문단",
        ),
        cafe="헬씨 트리",
        board="자유게시판",
        account="writer",
        publish_immediately=True,
        prepared_images=[
            SimpleNamespace(local_path="/prepared/photo-1.jpg"),
        ],
    )
    browser = FakeBrowser()

    WebPublisher(browser).publish_immediate(job, dry_run=True)

    article = next(item for item in browser.operations if item[0] == "article")
    assert "{사진1}" not in article[2]
    assert ("images", ("/prepared/photo-1.jpg",)) in browser.operations
