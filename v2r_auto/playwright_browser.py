from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .content import ParsedArticle
from .models import PostJob


V2R_LIST_URL = "https://v2r.daboja.im/nc/board?view=list"
V2R_SE_ONE_URL = "https://v2r.daboja.im/nc/seone"
AFFILIATE_CAFE_DELAYS = {
    "씨씨앙": 4,
    "양평맘": 20,
    "쌍둥이맘 모여라": 22,
}
AFFILIATE_CAFE_SEARCH_TERMS = {
    "씨씨앙": "씨씨앙",
    "양평맘": "양평",
    "쌍둥이맘 모여라": "쌍둥이맘",
}
SE_ONE_SELECTION_INDEX = {"카페": 0, "계정": 1, "게시판": 2, "말머리": 3}


class AutomationError(RuntimeError):
    """A browser-visible automation failure with a user-facing message."""


@dataclass(slots=True)
class PlaywrightBrowserConfig:
    profile_dir: Path
    download_dir: Path
    timeout_seconds: int = 20
    headless: bool = False
    channel: str | None = None


class PlaywrightBrowser:
    """Synchronous, UI-only V2R browser backed by a persistent Chromium context.

    The object is intentionally thread-bound: every method must be called from
    the same thread that called :meth:`start`.
    """

    def __init__(
        self,
        config: PlaywrightBrowserConfig,
        logger: logging.Logger,
        *,
        playwright_factory: Callable[[], Any] = sync_playwright,
    ):
        self.config = config
        self.logger = logger
        self._playwright_factory = playwright_factory
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.v2r_page: Page | None = None
        self.google_page: Page | None = None
        self._last_article_title = ""

    @property
    def timeout_ms(self) -> int:
        return self.config.timeout_seconds * 1000

    @property
    def page(self) -> Page:
        if self.v2r_page is None:
            raise AutomationError("브라우저가 시작되지 않았습니다")
        return self.v2r_page

    def start(self) -> None:
        """Launch Chromium once, reusing ``profile_dir`` across process runs."""
        started_at = time.monotonic()
        # region agent log
        open(os.environ.get("V2R_DEBUG_LOG", "/opt/cursor/logs/debug.log"), "a").write(json.dumps({"hypothesisId":"H3,H4","location":"playwright_browser.py:PlaywrightBrowser.start:entry","message":"Persistent browser start requested","data":{"profileRole":self.config.profile_dir.name,"contextExists":self.context is not None,"headless":self.config.headless,"channelConfigured":bool(self.config.channel)},"timestamp":time.time_ns()//1_000_000})+"\n")
        # endregion
        if self.context is not None:
            return
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        manager = self._playwright_factory()
        self._playwright = manager.start()
        launch_options: dict[str, Any] = {
            "user_data_dir": str(self.config.profile_dir.resolve()),
            "headless": self.config.headless,
            "accept_downloads": True,
            "downloads_path": str(self.config.download_dir.resolve()),
            "no_viewport": True,
            "args": ["--start-maximized", "--disable-notifications"],
        }
        if self.config.channel:
            launch_options["channel"] = self.config.channel
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self.v2r_page = self.context.pages[0] if self.context.pages else self._new_page()
        if self.context.pages:
            self._prepare_page(self.v2r_page)
        # region agent log
        open(os.environ.get("V2R_DEBUG_LOG", "/opt/cursor/logs/debug.log"), "a").write(json.dumps({"hypothesisId":"H3","location":"playwright_browser.py:PlaywrightBrowser.start:exit","message":"Persistent browser launch completed","data":{"profileRole":self.config.profile_dir.name,"pageCount":len(self.context.pages),"elapsedMs":round((time.monotonic()-started_at)*1000)},"timestamp":time.time_ns()//1_000_000})+"\n")
        # endregion
        self.logger.info("지속 프로필로 Chromium을 시작했습니다")

    def close(self) -> None:
        """Close the owned context and Playwright runtime; safe to call twice."""
        context, runtime = self.context, self._playwright
        self.context = None
        self._playwright = None
        self.v2r_page = None
        self.google_page = None
        try:
            if context is not None:
                context.close()
        finally:
            if runtime is not None:
                runtime.stop()

    def _new_page(self) -> Page:
        if self.context is None:
            raise AutomationError("브라우저가 시작되지 않았습니다")
        page = self.context.new_page()
        self._prepare_page(page)
        return page

    @staticmethod
    def _prepare_page(page: Page) -> None:
        page.on("dialog", lambda dialog: dialog.dismiss())

    def _navigate(self, page: Page, url: str) -> None:
        started_at = time.monotonic()
        target = "v2r" if url == V2R_LIST_URL else "sheet"
        # region agent log
        open(os.environ.get("V2R_DEBUG_LOG", "/opt/cursor/logs/debug.log"), "a").write(json.dumps({"hypothesisId":"H1,H2,H4,H5","location":"playwright_browser.py:PlaywrightBrowser._navigate:before","message":"Browser navigation starting","data":{"profileRole":self.config.profile_dir.name,"target":target},"timestamp":time.time_ns()//1_000_000})+"\n")
        # endregion
        page.bring_to_front()
        response = page.goto(url, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        # region agent log
        open(os.environ.get("V2R_DEBUG_LOG", "/opt/cursor/logs/debug.log"), "a").write(json.dumps({"hypothesisId":"H1,H4,H5","location":"playwright_browser.py:PlaywrightBrowser._navigate:after","message":"Browser navigation completed","data":{"profileRole":self.config.profile_dir.name,"target":target,"status":status if isinstance(status,int) else None,"elapsedMs":round((time.monotonic()-started_at)*1000)},"timestamp":time.time_ns()//1_000_000})+"\n")
        # endregion

    def open_login_window(self, sheet_url: str = "") -> None:
        """Open V2R and, optionally, Sheets in separate persistent tabs."""
        self.start()
        self._navigate(self.page, V2R_LIST_URL)
        if sheet_url:
            if self.google_page is None or self.google_page.is_closed():
                self.google_page = self._new_page()
            self._navigate(self.google_page, sheet_url)
            self.logger.info("Google Sheets 로그인 확인 탭을 열었습니다")
        self.logger.info("로그인 준비 창을 열었습니다. Google과 V2R 로그인을 확인하세요")

    def open_sheet_login_window(self, sheet_url: str) -> None:
        """Open only the Sheet login/lobby in the coordinator context."""
        self.start()
        if self.google_page is None or self.google_page.is_closed():
            self.google_page = self.page
        self._navigate(self.google_page, sheet_url)
        self.logger.info("Google Sheets 로그인 확인 창을 열었습니다")

    def _login_state(self) -> str | None:
        page = self.page
        password = page.locator("input[type='password']")
        if password.count() and password.first.is_visible():
            return "login"
        source = page.content()
        if "/nc/board" in page.url and any(
            text in source for text in ("글쓰기", "게시글", "카페명")
        ):
            return "authenticated"
        return None

    def ensure_v2r_login(self, email: str = "", password: str = "") -> None:
        """Verify a login completed manually; credentials are never submitted."""
        del email, password
        self.start()
        state = self._login_state()
        if state == "authenticated":
            self.logger.info("기존 V2R 로그인 세션을 사용합니다")
            return
        if state == "login":
            raise AutomationError(
                "V2R 로그인이 필요합니다. 로그인 준비 창에서 직접 로그인한 뒤 다시 실행하세요"
            )
        self._navigate(self.page, V2R_LIST_URL)
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            state = self._login_state()
            if state == "authenticated":
                self.logger.info("기존 V2R 로그인 세션을 사용합니다")
                return
            if state == "login":
                raise AutomationError(
                    "V2R 로그인이 필요합니다. 로그인 준비 창에서 직접 로그인한 뒤 다시 실행하세요"
                )
            self.page.wait_for_timeout(250)
        raise AutomationError("V2R 로그인 화면 또는 게시글 목록을 확인하지 못했습니다")

    def verify_current_v2r_login(self) -> None:
        """Inspect the current worker page without issuing another navigation."""
        self.start()
        state = self._login_state()
        if state == "authenticated":
            self.logger.info("현재 V2R 로그인 세션을 확인했습니다")
            return
        if state == "login":
            raise AutomationError(
                "V2R 로그인이 필요합니다. 로그인 준비 창에서 직접 로그인한 뒤 다시 실행하세요"
            )
        raise AutomationError("현재 창에서 V2R 게시글 목록을 확인하지 못했습니다")

    @staticmethod
    def _sheet_export_url(sheet_url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
        if not match:
            raise AutomationError("올바른 Google Sheets 주소가 아닙니다")
        parsed = urlparse(sheet_url)
        gid = parse_qs(parsed.query).get("gid", ["0"])[0]
        if parsed.fragment.startswith("gid="):
            gid = parsed.fragment.split("=", 1)[1].split("&", 1)[0]
        return (
            f"https://docs.google.com/spreadsheets/d/{match.group(1)}"
            f"/export?format=csv&gid={gid}"
        )

    @staticmethod
    def _csv_has_headers(path: Path, required_headers: set[str]) -> bool:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                headers = next(csv.reader(stream), [])
        except (OSError, UnicodeError, csv.Error):
            return False
        return required_headers.issubset(
            {header.strip() for header in headers if header}
        )

    def _sheets_page(self) -> Page:
        self.start()
        if self.google_page is None or self.google_page.is_closed():
            self.google_page = self._new_page()
        return self.google_page

    def download_sheet(
        self,
        sheet_url: str,
        *,
        required_headers: set[str] | None = None,
    ) -> Path:
        """Download a Sheet export through Chromium's authenticated session."""
        page = self._sheets_page()
        export_url = self._sheet_export_url(sheet_url)
        self.logger.info("Google Sheets 데이터를 내려받습니다")
        try:
            with page.expect_download(timeout=self.timeout_ms) as pending:
                try:
                    page.goto(export_url, wait_until="commit")
                except Exception as exc:
                    # Chromium reports an aborted navigation when a response is
                    # converted into a download. Only suppress that known case.
                    if "ERR_ABORTED" not in str(exc):
                        raise
            download = pending.value
        except PlaywrightTimeoutError as exc:
            if "accounts.google.com" in page.url:
                raise AutomationError(
                    "Google 로그인이 필요합니다. 로그인 준비 창에서 로그인한 뒤 다시 실행하세요"
                ) from exc
            raise AutomationError(
                "시트를 내려받지 못했습니다. 공유 권한 또는 Google 로그인을 확인하세요"
            ) from exc
        filename = Path(download.suggested_filename).name or "sheet.csv"
        destination = self.config.download_dir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(destination)
        if required_headers and not self._csv_has_headers(destination, required_headers):
            raise AutomationError(
                "시트 CSV는 내려받았지만 필수 헤더를 확인하지 못했습니다: "
                + ", ".join(sorted(required_headers))
            )
        self.logger.info("Google Sheets 다운로드 완료: %s", destination.name)
        return destination

    @staticmethod
    def _sheet_range_url(sheet_url: str, column: str, row_number: int) -> str:
        parsed = urlparse(sheet_url)
        gid = parse_qs(parsed.query).get("gid", ["0"])[0]
        if parsed.fragment.startswith("gid="):
            gid = parsed.fragment.split("=", 1)[1].split("&", 1)[0]
        query = f"?{parsed.query}" if parsed.query else ""
        return (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}{query}"
            f"#gid={gid}&range={column}{row_number}"
        )

    def update_completion_link(
        self,
        sheet_url: str,
        row_number: int,
        completion_url: str,
    ) -> None:
        if not completion_url:
            raise AutomationError("F열에 입력할 완료 링크가 없습니다")
        self.update_sheet_cell(sheet_url, "F", row_number, completion_url)

    def update_sheet_cell(
        self,
        sheet_url: str,
        column: str,
        row_number: int,
        value: str,
        *,
        max_attempts: int = 3,
        verify_checks: int = 10,
    ) -> None:
        """Select a Sheet range, type a value, and verify it via CSV export."""
        column = column.upper()
        if not re.fullmatch(r"[A-Z]+", column) or row_number < 1:
            raise AutomationError(f"올바르지 않은 시트 셀입니다: {column}{row_number}")
        page = self._sheets_page()
        cell = f"{column}{row_number}"
        last_error: Exception | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    self._navigate(
                        page, self._sheet_range_url(sheet_url, column, row_number)
                    )
                    editor = page.locator("#waffle-rich-text-editor")
                    visible_editor = next(
                        (
                            editor.nth(index)
                            for index in range(editor.count())
                            if editor.nth(index).is_visible()
                            and editor.nth(index).is_enabled()
                        ),
                        None,
                    )
                    if visible_editor is not None:
                        visible_editor.click()
                        visible_editor.press("Control+A")
                        visible_editor.fill(value)
                        visible_editor.press("Enter")
                    else:
                        page.keyboard.type(value)
                        page.keyboard.press("Enter")
                    self._verify_sheet_cell(
                        sheet_url, column, row_number, value, checks=verify_checks
                    )
                    self.logger.info("시트 %s에 값을 입력했습니다", cell)
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt < max_attempts:
                        page.wait_for_timeout(attempt * 1000)
            raise AutomationError(
                f"시트 {cell} 저장에 {max_attempts}회 실패했습니다: {last_error}"
            ) from last_error
        finally:
            if self.v2r_page is not None and not self.v2r_page.is_closed():
                self.v2r_page.bring_to_front()

    def _verify_sheet_cell(
        self,
        sheet_url: str,
        column: str,
        row_number: int,
        expected: str,
        *,
        checks: int = 10,
    ) -> None:
        column_index = 0
        for letter in column:
            column_index = column_index * 26 + ord(letter) - ord("A") + 1
        column_index -= 1
        export_url = self._sheet_export_url(sheet_url)
        for _ in range(checks):
            with urlopen(
                f"{export_url}&cache={time.time_ns()}", timeout=20
            ) as response:
                rows = list(
                    csv.reader(io.StringIO(response.read().decode("utf-8-sig")))
                )
            if (
                len(rows) >= row_number
                and len(rows[row_number - 1]) > column_index
                and rows[row_number - 1][column_index] == expected
            ):
                return
            time.sleep(0.5)
        raise AutomationError(
            f"시트 {column}{row_number} 저장값을 다시 확인하지 못했습니다"
        )

    @staticmethod
    def _normalize_option_text(value: str) -> str:
        return re.sub(r"\s+", "", value or "").casefold()

    @staticmethod
    def _normalize_board_text(value: str) -> str:
        return re.sub(
            r"[^0-9a-z가-힣]+", "", value or "", flags=re.IGNORECASE
        ).casefold()

    @classmethod
    def _option_text_matches(cls, label: str, value: str, option_text: str) -> bool:
        wanted = cls._normalize_option_text(value)
        candidate = cls._normalize_option_text(option_text)
        if candidate == wanted:
            return True
        if label == "계정":
            return any(
                cls._normalize_option_text(token) == wanted
                for token in re.findall(r"[0-9A-Za-z_-]+", option_text)
            )
        if label == "게시판":
            return cls._normalize_board_text(option_text) == cls._normalize_board_text(
                value
            )
        return label == "카페" and bool(wanted) and wanted in candidate

    @staticmethod
    def _visible(locator: Locator, *, enabled: bool = False) -> list[Locator]:
        result = []
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible() and (not enabled or item.is_enabled()):
                result.append(item)
        return result

    def click_text(self, texts: tuple[str, ...], exact_only: bool = False) -> None:
        """Click the first visible button/link matching one of ``texts``."""
        for exact in (True, False):
            if exact_only and not exact:
                break
            for text in texts:
                escaped = text.replace("\\", "\\\\").replace('"', '\\"')
                selector = (
                    f'button:text-is("{escaped}"), a:text-is("{escaped}"), '
                    f'[role="button"]:text-is("{escaped}")'
                    if exact
                    else (
                        f'button:has-text("{escaped}"), a:has-text("{escaped}"), '
                        f'[role="button"]:has-text("{escaped}")'
                    )
                )
                candidates = self._visible(self.page.locator(selector), enabled=True)
                if candidates:
                    candidates[0].scroll_into_view_if_needed()
                    candidates[0].click()
                    return
        raise AutomationError(f"버튼을 찾지 못했습니다: {' / '.join(texts)}")

    def field_near_label(self, label: str) -> Locator | None:
        escaped = label.replace("'", "\\'")
        locator = self.page.locator(
            "xpath="
            f"//*[self::label or self::div or self::span]"
            f"[contains(normalize-space(), '{escaped}')]"
            "/following::*[self::input or self::textarea][1]"
        )
        return next(iter(self._visible(locator)), None)

    def fill_input(
        self, label: str, value: str, fallback_css: str | None = None
    ) -> None:
        """Fill a visible input near a label, with an optional CSS fallback."""
        element = self.field_near_label(label)
        if element is None and fallback_css:
            element = next(
                iter(self._visible(self.page.locator(fallback_css), enabled=True)),
                None,
            )
        if element is None:
            raise AutomationError(f"입력란을 찾지 못했습니다: {label}")
        element.click()
        element.fill(value)

    def _select_option_at(self, label: str, value: str, index: int) -> None:
        if not value:
            return
        selections = self._visible(self.page.locator(".n-base-selection"))
        if len(selections) <= index:
            raise AutomationError(f"SE-ONE {label} 선택칸을 찾지 못했습니다")
        selection = selections[index]
        selection.scroll_into_view_if_needed()
        selection.click()
        search_value = (
            AFFILIATE_CAFE_SEARCH_TERMS.get(value, value)
            if label == "카페"
            else value
        )
        fields = self._visible(selection.locator("input"), enabled=True)
        if not fields:
            fields = self._visible(
                self.page.locator(
                    ".n-base-select-menu input, .n-base-selection input"
                ),
                enabled=True,
            )
        if fields:
            fields[-1].fill(search_value)
        options = self.page.locator(".n-base-select-option")
        try:
            options.first.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            if fields:
                fields[-1].press("ArrowDown")
                fields[-1].press("Enter")
                return
            raise AutomationError(f"SE-ONE {label} 목록이 준비되지 않았습니다") from exc
        for option in self._visible(options, enabled=True):
            if self._option_text_matches(label, value, option.inner_text()):
                option.click()
                if label in {"카페", "계정"}:
                    self.page.wait_for_timeout(1000)
                return
        shown = [
            option.inner_text().strip().replace("\n", " / ")
            for option in self._visible(options)
            if option.inner_text().strip()
        ]
        raise AutomationError(
            f"SE-ONE {label} 목록에서 '{value}' 항목을 찾지 못했습니다"
            + (f" / 표시 항목: {', '.join(shown[:10])}" if shown else "")
        )

    def select_option(self, label: str, value: str) -> None:
        """Select a value from the indexed Naive UI controls used by SE-ONE."""
        try:
            index = SE_ONE_SELECTION_INDEX[label]
        except KeyError as exc:
            raise AutomationError(f"지원하지 않는 선택란입니다: {label}") from exc
        self._select_option_at(label, value, index)

    def fill_editor(self, body: str) -> None:
        """Replace the visible SmartEditor/contenteditable body with plain text."""
        selectors = (
            ".se-module-text .se-text-paragraph",
            "iframe[title*='스마트 에디터']",
            "[contenteditable='true']:not([title='Channel chat'])",
            ".ProseMirror",
            ".ql-editor",
        )
        editor: Locator | None = None
        for selector in selectors:
            editor = next(
                iter(self._visible(self.page.locator(selector), enabled=True)), None
            )
            if editor is not None:
                break
        if editor is None:
            raise AutomationError("본문 편집기가 준비되지 않았습니다")
        if editor.evaluate("element => element.tagName.toLowerCase()") == "iframe":
            frame = editor.content_frame
            if frame is None:
                raise AutomationError("본문 편집기 프레임에 접근하지 못했습니다")
            target = frame.locator("body")
        else:
            target = editor
        target.click()
        target.press("Control+A")
        target.fill(body)

    def open_se_one_writer(self) -> None:
        """Navigate to the direct SE-ONE new-post screen and validate its controls."""
        self.start()
        self._navigate(self.page, V2R_SE_ONE_URL)
        try:
            self.page.locator(".n-base-selection").nth(3).wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise AutomationError(
                "SE-ONE 글쓰기 화면은 열렸지만 카페·계정·게시판 입력칸을 찾지 못했습니다"
            ) from exc

    def inspect_se_one_form(self) -> dict[str, object]:
        """Return visible form metadata without modifying or publishing the draft."""
        self.open_se_one_writer()
        controls = self.page.locator(
            "input, textarea, button, iframe, [contenteditable='true'], "
            "[role='combobox'], [class*='editor'], [class*='Editor'], "
            "[class*='ProseMirror']"
        ).evaluate_all(
            """items => items.filter(item => item.offsetParent !== null).map(
                (item, order) => ({
                    order,
                    tag: item.tagName.toLowerCase(),
                    type: item.getAttribute('type') || '',
                    placeholder: item.getAttribute('placeholder') || '',
                    name: item.getAttribute('name') || '',
                    role: item.getAttribute('role') || '',
                    ariaLabel: item.getAttribute('aria-label') || '',
                    text: (item.innerText || item.textContent || '').trim().slice(0, 80)
                })
            )"""
        )
        return {"url": self.page.url, "title": self.page.title(), "controls": controls}

    def fill_post(self, job: PostJob, dry_run: bool) -> None:
        """Open SE-ONE, fill a post, and optionally register it."""
        self.open_se_one_writer()
        self.select_option("카페", job.cafe)
        if job.account:
            self.select_option("계정", job.account)
        self.select_option("게시판", job.board)
        self.fill_input(
            "제목",
            job.title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self.fill_editor(job.body)
        if job.tags:
            self.fill_input(
                "태그", ", ".join(job.tags), "input[placeholder*='태그']"
            )
        if job.publish_at:
            self.logger.warning(
                "행 %s 예약시간 '%s'은 별도 set_schedule 호출이 필요합니다",
                job.row_number,
                job.publish_at,
            )
        if dry_run:
            self.logger.info("행 %s 입력 검증 완료(저장하지 않음)", job.row_number)
            return
        job.post_url = self.submit_registration()

    def open_writer(self) -> None:
        """Semantic web-publisher wrapper for a fresh SE-ONE writer."""
        self.open_se_one_writer()

    def first_available_account(
        self,
        cafe: str,
        board: str,
        account_type: str,
    ) -> str:
        """Return the first enabled account offered by the visible account menu."""
        accounts = self.available_accounts(cafe, board, account_type)
        return accounts[0] if accounts else ""

    def available_accounts(
        self,
        cafe: str,
        board: str,
        account_type: str,
    ) -> list[str]:
        """Return enabled account IDs offered by the visible account menu.

        SE-ONE enables the board only after account selection, so ``board`` is
        accepted as destination context but cannot be applied before this
        lookup.
        """
        del board
        self.open_se_one_writer()
        self.select_option("카페", cafe)
        selections = self._visible(self.page.locator(".n-base-selection"))
        if len(selections) <= SE_ONE_SELECTION_INDEX["계정"]:
            raise AutomationError("SE-ONE 계정 선택칸을 찾지 못했습니다")
        selections[SE_ONE_SELECTION_INDEX["계정"]].click()
        options = self.page.locator(".n-base-select-option")
        try:
            options.first.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise AutomationError("SE-ONE 계정 목록이 준비되지 않았습니다") from exc
        wanted_type = self._normalize_option_text(account_type)
        visible = self._visible(options, enabled=True)
        if wanted_type:
            typed = [
                option
                for option in visible
                if wanted_type
                in self._normalize_option_text(option.inner_text())
            ]
            if typed:
                visible = typed
        accounts: list[str] = []
        for option in visible:
            raw_value = (
                option.get_attribute("data-value")
                or option.get_attribute("value")
                or ""
            ).strip()
            if raw_value:
                if raw_value not in accounts:
                    accounts.append(raw_value)
                continue
            tokens = re.findall(r"[0-9A-Za-z_-]+", option.inner_text())
            if tokens and tokens[-1] not in accounts:
                accounts.append(tokens[-1])
        return accounts

    def select_destination(
        self,
        cafe: str,
        account: str,
        board: str,
        prefix: str = "",
    ) -> None:
        """Select cafe, account, board, and optional prefix in dependency order."""
        self.select_option("카페", cafe)
        self.select_option("계정", account)
        self.select_option("게시판", board)
        if prefix:
            self.select_option("말머리", prefix)

    def select_publish_mode(self, mode: str) -> None:
        """Choose ``scheduled`` or ``immediate`` using visible UI controls."""
        labels = {
            "scheduled": ("예약 발행", "예약 등록", "예약"),
            "immediate": ("즉시 발행", "즉시 등록", "즉시"),
        }
        try:
            texts = labels[mode]
        except KeyError as exc:
            raise AutomationError(f"지원하지 않는 발행 방식입니다: {mode}") from exc
        self.click_text(texts, exact_only=True)

    def fill_article(
        self,
        title: str,
        body: str,
        tags: Sequence[str],
    ) -> None:
        """Fill title, body, and optional tags without submitting."""
        self._last_article_title = title
        self.fill_input(
            "제목",
            title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self.fill_editor(body)
        if tags:
            self.fill_input(
                "태그", ", ".join(tags), "input[placeholder*='태그']"
            )

    def upload_images(self, paths: Sequence[str]) -> None:
        """Attach prepared images through SmartEditor's visible image control."""
        image_paths = [str(Path(path).resolve()) for path in paths]
        if not image_paths:
            return
        missing = [path for path in image_paths if not Path(path).is_file()]
        if missing:
            raise AutomationError(
                "첨부할 이미지 파일을 찾지 못했습니다: " + Path(missing[0]).name
            )
        file_inputs = self.page.locator("input[type='file'][accept*='image']")
        if file_inputs.count():
            file_inputs.last.set_input_files(image_paths)
        else:
            buttons = self._visible(
                self.page.locator(
                    ".se-toolbar-item-image button, "
                    "button[aria-label*='사진'], button[aria-label*='이미지']"
                ),
                enabled=True,
            )
            if not buttons:
                raise AutomationError("SmartEditor 이미지 첨부 버튼을 찾지 못했습니다")
            try:
                with self.page.expect_file_chooser(timeout=self.timeout_ms) as chooser:
                    buttons[0].click()
                chooser.value.set_files(image_paths)
            except PlaywrightTimeoutError as exc:
                raise AutomationError(
                    "이미지 파일 선택창을 확인하지 못했습니다"
                ) from exc
        self.page.wait_for_timeout(min(10000, 1000 + len(image_paths) * 750))
        self.logger.info("SmartEditor 이미지 %s개를 첨부했습니다", len(image_paths))

    def submit_registration(self) -> str:
        """Click the exact registration button and return the resulting URL."""
        self.click_text(("등록",), exact_only=True)

        def completed() -> bool:
            return (
                "/nc/board" in self.page.url
                or "/nc/articleDetail/" in self.page.url
                or any(
                    text in self.page.content()
                    for text in ("등록되었습니다", "발행되었습니다", "작성 완료")
                )
            )

        self._wait_until(
            completed, "저장 후 완료 신호를 확인하지 못했습니다. 목록에서 결과를 확인하세요"
        )
        if "/nc/articleDetail/" not in self.page.url:
            if not self._last_article_title:
                raise AutomationError(
                    "등록은 완료됐지만 게시글 제목을 확인하지 못했습니다"
                )
            link = self.page.get_by_role(
                "link",
                name=self._last_article_title,
                exact=True,
            )
            try:
                link.first.click()
                self.page.wait_for_url(
                    re.compile(r"/nc/articleDetail/"),
                    timeout=self.timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                raise AutomationError(
                    "등록은 완료됐지만 완료 게시글 링크를 확인하지 못했습니다"
                ) from exc
        return self.page.url

    def register(self) -> str:
        """Semantic web-publisher wrapper for UI registration."""
        return self.submit_registration()

    def set_schedule(self, scheduled_at: datetime) -> None:
        """Set the sole visible ``datetime-local`` input using native DOM events."""
        inputs = self._visible(
            self.page.locator("input[type='datetime-local']"), enabled=True
        )
        if len(inputs) != 1:
            raise AutomationError(
                "예약 시간 입력란을 하나만 찾지 못했습니다. 화면 형식을 확인하세요"
            )
        local_scheduled_at = (
            scheduled_at.astimezone()
            if scheduled_at.tzinfo is not None
            else scheduled_at
        )
        value = local_scheduled_at.strftime("%Y-%m-%dT%H:%M")
        inputs[0].evaluate(
            """(input, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(input, value);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )

    def set_revision_schedule(
        self, cafe: str, *, now: datetime | None = None
    ) -> datetime:
        """Apply the existing cafe-specific revision delay and return its time."""
        if cafe not in AFFILIATE_CAFE_DELAYS:
            raise AutomationError(f"수정 글 예약 시간을 알 수 없는 카페입니다: {cafe}")
        scheduled_at = (now or datetime.now()) + timedelta(
            hours=AFFILIATE_CAFE_DELAYS[cafe]
        )
        self.set_schedule(scheduled_at)
        return scheduled_at

    def open_revision_reservation(self) -> None:
        self.click_text(("수정 글 예약",), exact_only=True)
        self.page.wait_for_load_state("domcontentloaded")

    def open_article(self, completion_url: str) -> None:
        """Navigate the V2R tab to a published article URL."""
        if not completion_url:
            raise AutomationError("열 게시글 주소가 없습니다")
        self.start()
        self._navigate(self.page, completion_url)

    def reserve_revision(self, scheduled_at: datetime) -> None:
        """Open the revision reservation form and set its visible schedule."""
        self.open_revision_reservation()
        self.set_schedule(scheduled_at)

    def fill_revision_article(self, article: ParsedArticle) -> None:
        self.fill_input(
            "제목",
            article.title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self.fill_editor(article.body)
        if article.tag:
            self.fill_input("태그", article.tag, "input[placeholder*='태그']")

    def fill_comment_input(self, content: str) -> None:
        """Replace the last visible non-editor comment input."""
        candidates = [
            item
            for item in self._visible(
                self.page.locator("textarea, [contenteditable='true']"),
                enabled=True,
            )
            if "ProseMirror" not in (item.get_attribute("class") or "")
        ]
        if not candidates:
            raise AutomationError("댓글 입력창을 찾지 못했습니다")
        candidates[-1].click()
        candidates[-1].fill(content)

    def select_comment_account(self, account: str) -> None:
        """Select the fifth SE-ONE selector, used by revision comments."""
        self._select_option_at("계정", account, 4)

    def click_reply_for(self, parent_text: str) -> None:
        """Click ``답글쓰기`` inside the nearest ancestor of a matching comment."""
        escaped = parent_text.replace("\\", "\\\\").replace('"', '\\"')
        parents = self._visible(
            self.page.locator(f'text="{escaped}"')
        )
        for parent in parents:
            ancestor = parent
            for _ in range(6):
                buttons = self._visible(
                    ancestor.locator(
                        "button:text-is('답글쓰기'), "
                        "[role='button']:text-is('답글쓰기')"
                    ),
                    enabled=True,
                )
                if buttons:
                    buttons[0].click()
                    return
                ancestor = ancestor.locator("xpath=..")
        raise AutomationError(
            f"댓글의 답글쓰기 버튼을 찾지 못했습니다: {parent_text[:30]}"
        )

    def reserve_comment(
        self,
        text: str | None = None,
        *,
        parent_text: str | None = None,
        scheduled_at: datetime | None = None,
    ) -> None:
        """Fill and reserve one root comment or reply through visible controls."""
        if text is None:
            # Backward-compatible final-click primitive.
            self.click_text(("예약",), exact_only=True)
            return
        if parent_text:
            self.click_reply_for(parent_text)
        self.fill_comment_input(text)
        if scheduled_at is not None:
            self.set_schedule(scheduled_at)
        self.click_text(("예약",), exact_only=True)

    def save_screenshot(self, path: Path, *, full_page: bool = True) -> None:
        """Save a PNG/JPEG screenshot, creating parent directories as needed."""
        self.start()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=full_page)

    def _wait_until(self, predicate: Callable[[], bool], message: str) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            self.page.wait_for_timeout(250)
        raise AutomationError(message)

    # Compatibility names used by the existing browser-facing publisher code.
    _click_text = click_text
    _field_near_label = field_near_label
    _fill_input = fill_input
    _select_option = select_option
    _fill_editor = fill_editor
    _submit_registration = submit_registration
    _set_revision_schedule = set_revision_schedule
    _fill_comment_input = fill_comment_input
    _select_comment_account = select_comment_account
    _click_reply_for = click_reply_for
    def _reserve_comment(self) -> None:
        self.reserve_comment()


V2RPlaywrightBrowser = PlaywrightBrowser
BrowserConfig = PlaywrightBrowserConfig

