from __future__ import annotations

import inspect
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from v2r_auto.models import PostJob
from v2r_auto.playwright_browser import (
    AutomationError,
    PlaywrightBrowser,
    PlaywrightBrowserConfig,
)


class LocatorItems:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0]


def make_browser(tmp_path: Path, page: MagicMock | None = None) -> PlaywrightBrowser:
    page = page or MagicMock()
    page.is_closed.return_value = False
    browser = PlaywrightBrowser(
        PlaywrightBrowserConfig(
            profile_dir=tmp_path / "profile",
            download_dir=tmp_path / "downloads",
            timeout_seconds=1,
            headless=True,
            channel=None,
        ),
        logging.getLogger("playwright-browser-test"),
    )
    browser.context = MagicMock()
    browser.v2r_page = page
    return browser


def test_start_uses_owned_persistent_chromium_context(tmp_path: Path) -> None:
    page = MagicMock()
    context = MagicMock()
    context.pages = [page]
    runtime = MagicMock()
    runtime.chromium.launch_persistent_context.return_value = context
    manager = MagicMock()
    manager.start.return_value = runtime
    browser = PlaywrightBrowser(
        PlaywrightBrowserConfig(
            tmp_path / "profile",
            tmp_path / "downloads",
            headless=True,
            channel=None,
        ),
        logging.getLogger("persistent-test"),
        playwright_factory=lambda: manager,
    )

    browser.start()

    options = runtime.chromium.launch_persistent_context.call_args.kwargs
    assert options["user_data_dir"] == str((tmp_path / "profile").resolve())
    assert options["downloads_path"] == str((tmp_path / "downloads").resolve())
    assert options["accept_downloads"] is True
    assert options["headless"] is True
    assert browser.page is page

    browser.close()
    context.close.assert_called_once_with()
    runtime.stop.assert_called_once_with()
    browser.close()  # idempotent


def test_module_has_no_selenium_or_v2r_api_publisher_dependency() -> None:
    source = inspect.getsource(inspect.getmodule(PlaywrightBrowser))

    assert "selenium" not in source
    assert "affiliate_api" not in source
    assert "immediate_api" not in source


def test_ensure_v2r_login_only_accepts_existing_manual_session(
    tmp_path: Path,
) -> None:
    page = MagicMock()
    page.url = V2R_BOARD_URL = "https://v2r.daboja.im/nc/board?view=list"
    page.content.return_value = "<button>글쓰기</button>"
    password = MagicMock()
    password.count.return_value = 0
    page.locator.return_value = password
    browser = make_browser(tmp_path, page)
    browser._navigate = MagicMock()

    browser.ensure_v2r_login("ignored@example.com", "ignored-password")

    browser._navigate.assert_not_called()

    password.count.return_value = 1
    password.first.is_visible.return_value = True
    page.content.return_value = "<input type=password>"
    with pytest.raises(AutomationError, match="직접 로그인"):
        browser.ensure_v2r_login("still-ignored", "still-ignored")
    browser._navigate.assert_not_called()


def test_current_login_verification_never_navigates(tmp_path: Path) -> None:
    page = MagicMock()
    page.url = "https://v2r.daboja.im/nc/board?view=list"
    page.content.return_value = "<button>글쓰기</button>"
    password = MagicMock()
    password.count.return_value = 0
    page.locator.return_value = password
    browser = make_browser(tmp_path, page)
    browser._navigate = MagicMock()

    browser.verify_current_v2r_login()

    browser._navigate.assert_not_called()


def test_coordinator_login_window_opens_only_sheet(tmp_path: Path) -> None:
    page = MagicMock()
    browser = make_browser(tmp_path, page)
    browser._navigate = MagicMock()
    sheet_url = "https://docs.google.com/spreadsheets/d/example"

    browser.open_sheet_login_window(sheet_url)

    assert browser.google_page is page
    browser._navigate.assert_called_once_with(page, sheet_url)


def test_download_sheet_uses_browser_download_and_validates_headers(
    tmp_path: Path,
) -> None:
    page = MagicMock()
    page.url = "https://docs.google.com/spreadsheets/"
    download = MagicMock()
    download.suggested_filename = "jobs.csv"

    def save_as(path: Path) -> None:
        Path(path).write_text("제목,본문\nhello,world\n", encoding="utf-8")

    download.save_as.side_effect = save_as
    pending = MagicMock()
    pending.__enter__.return_value.value = download
    page.expect_download.return_value = pending
    browser = make_browser(tmp_path, page)
    browser.google_page = page

    result = browser.download_sheet(
        "https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=7",
        required_headers={"제목", "본문"},
    )

    assert result == tmp_path / "downloads" / "jobs.csv"
    assert result.read_text(encoding="utf-8").startswith("제목,본문")
    page.goto.assert_called_once_with(
        "https://docs.google.com/spreadsheets/d/sheet-id/export?format=csv&gid=7",
        wait_until="commit",
    )


def test_completion_wrapper_targets_column_f(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    browser.update_sheet_cell = MagicMock()

    browser.update_completion_link("https://example.test/sheet", 12, "https://post")

    browser.update_sheet_cell.assert_called_once_with(
        "https://example.test/sheet", "F", 12, "https://post"
    )
    with pytest.raises(AutomationError, match="완료 링크"):
        browser.update_completion_link("https://example.test/sheet", 12, "")


def test_fill_post_composes_ui_primitives_and_registration(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    browser.open_se_one_writer = MagicMock()
    browser.select_option = MagicMock()
    browser.fill_input = MagicMock()
    browser.fill_editor = MagicMock()
    browser.submit_registration = MagicMock(return_value="https://v2r/post/1")
    job = PostJob(
        row_number=2,
        keyword="keyword",
        title="title",
        body="body",
        cafe="cafe",
        board="board",
        account="account",
        tags=["one", "two"],
    )

    browser.fill_post(job, dry_run=False)

    assert browser.select_option.call_args_list == [
        call("카페", "cafe"),
        call("계정", "account"),
        call("게시판", "board"),
    ]
    browser.fill_editor.assert_called_once_with("body")
    assert browser.fill_input.call_args_list[-1] == call(
        "태그", "one, two", "input[placeholder*='태그']"
    )
    assert job.post_url == "https://v2r/post/1"


def test_registration_and_schedule_use_ui_state(tmp_path: Path) -> None:
    page = MagicMock()
    page.url = "https://v2r.daboja.im/nc/articleDetail/123"
    page.content.return_value = ""
    browser = make_browser(tmp_path, page)
    browser.click_text = MagicMock()

    assert browser.submit_registration() == page.url
    browser.click_text.assert_called_once_with(("등록",), exact_only=True)

    datetime_input = MagicMock()
    datetime_input.is_visible.return_value = True
    datetime_input.is_enabled.return_value = True
    page.locator.return_value = LocatorItems([datetime_input])
    scheduled = browser.set_revision_schedule(
        "씨씨앙", now=datetime(2026, 9, 17, 2, 30)
    )

    assert scheduled == datetime(2026, 9, 17, 6, 30)
    assert datetime_input.evaluate.call_args.args[1] == "2026-09-17T06:30"


def test_comment_account_selector_uses_fifth_dropdown(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    browser._select_option_at = MagicMock()

    browser.select_comment_account("writer-id")

    browser._select_option_at.assert_called_once_with("계정", "writer-id", 4)


def test_web_publisher_semantic_contract_and_wrappers(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    expected_methods = {
        "first_available_account",
        "open_writer",
        "select_destination",
        "select_publish_mode",
        "set_schedule",
        "fill_article",
        "upload_images",
        "register",
        "open_article",
        "reserve_revision",
        "reserve_comment",
    }
    assert all(callable(getattr(browser, name, None)) for name in expected_methods)

    browser.select_option = MagicMock()
    browser.select_destination("cafe", "account", "board", "prefix")
    assert browser.select_option.call_args_list == [
        call("카페", "cafe"),
        call("계정", "account"),
        call("게시판", "board"),
        call("말머리", "prefix"),
    ]

    browser.click_reply_for = MagicMock()
    browser.fill_comment_input = MagicMock()
    browser.set_schedule = MagicMock()
    browser.click_text = MagicMock()
    scheduled_at = datetime(2026, 9, 17, 7, 45)
    browser.reserve_comment(
        "reply", parent_text="parent", scheduled_at=scheduled_at
    )
    browser.click_reply_for.assert_called_once_with("parent")
    browser.fill_comment_input.assert_called_once_with("reply")
    browser.set_schedule.assert_called_once_with(scheduled_at)
    browser.click_text.assert_called_once_with(("예약",), exact_only=True)

