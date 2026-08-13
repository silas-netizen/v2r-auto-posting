from __future__ import annotations

import csv
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    UnexpectedAlertPresentException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .content import ParsedArticle
from .images import strip_placeholders
from .models import AffiliateJob, JobStatus, PostJob


V2R_LIST_URL = "https://v2r.daboja.im/nc/board?view=list"
V2R_SE_ONE_URL = "https://v2r.daboja.im/nc/seone"
AFFILIATE_CAFE_DELAYS = {"씨씨앙": 4, "양평맘": 10}
AFFILIATE_CAFE_BOARDS = {"씨씨앙": "자유 수다방", "양평맘": "이모저모 이야기"}
AFFILIATE_CAFE_SEARCH_TERMS = {"씨씨앙": "씨씨앙", "양평맘": "양평"}
SE_ONE_SELECTION_INDEX = {"카페": 0, "계정": 1, "게시판": 2, "말머리": 3}


class AutomationError(RuntimeError):
    pass


@dataclass(slots=True)
class BrowserConfig:
    profile_dir: Path
    download_dir: Path
    timeout_seconds: int = 20
    debugger_address: str | None = None


class V2RBrowser:
    def __init__(self, config: BrowserConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.driver: webdriver.Chrome | None = None
        self.v2r_handle: str | None = None
        self.google_handle: str | None = None
        self._api_capture_active = False
        self._affiliate_publisher = None
        self._immediate_publisher = None

    def start(self) -> None:
        if self.driver:
            return
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        options = ChromeOptions()
        if self.config.debugger_address:
            options.debugger_address = self.config.debugger_address
            self.logger.info("이미 열린 Chrome에 연결합니다")
        else:
            options.add_argument(f"--user-data-dir={self.config.profile_dir}")
            options.add_argument("--start-maximized")
            options.add_argument("--disable-notifications")
            options.add_experimental_option(
                "prefs",
                {
                    "download.default_directory": str(self.config.download_dir.resolve()),
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "safebrowsing.enabled": True,
                },
            )
            self.logger.info("Chrome을 시작합니다")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        self.driver = webdriver.Chrome(options=options)
        self.v2r_handle = self.driver.current_window_handle

    def close(self) -> None:
        if self.driver:
            if not self.config.debugger_address:
                self.driver.quit()
            self.driver = None
            self.v2r_handle = None
            self.google_handle = None
            self._api_capture_active = False
            self._affiliate_publisher = None
            self._immediate_publisher = None

    @property
    def wait(self) -> WebDriverWait:
        if not self.driver:
            raise AutomationError("브라우저가 시작되지 않았습니다")
        return WebDriverWait(self.driver, self.config.timeout_seconds)

    def open_login_window(self, sheet_url: str = "") -> None:
        self.start()
        assert self.driver
        self._navigate(V2R_LIST_URL, self.v2r_handle)
        self.v2r_handle = self.driver.current_window_handle
        if sheet_url:
            self.driver.switch_to.new_window("tab")
            self.google_handle = self.driver.current_window_handle
            self._navigate(sheet_url, self.google_handle)
            self.logger.info("Google Sheets 로그인 확인 탭을 열었습니다")
        self.logger.info("로그인 준비 창을 열었습니다. Google과 V2R 로그인을 확인하세요")

    def _switch_to_handle(self, preferred: str | None = None) -> None:
        assert self.driver
        handles = self.driver.window_handles
        if not handles:
            raise AutomationError("열려 있는 Chrome 창이 없습니다")
        target = preferred if preferred in handles else handles[0]
        try:
            self.driver.switch_to.window(target)
        except NoSuchWindowException:
            self.driver.switch_to.window(self.driver.window_handles[0])

    def _navigate(self, url: str, preferred_handle: str | None = None) -> None:
        assert self.driver
        self._switch_to_handle(preferred_handle)
        try:
            self.driver.get(url)
        except UnexpectedAlertPresentException:
            try:
                self.driver.switch_to.alert.dismiss()
            except NoAlertPresentException:
                pass
            self.driver.get(url)

    @staticmethod
    def _sheet_export_url(sheet_url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
        if not match:
            raise AutomationError("올바른 Google Sheets 주소가 아닙니다")
        parsed = urlparse(sheet_url)
        gid = parse_qs(parsed.query).get("gid", ["0"])[0]
        if parsed.fragment.startswith("gid="):
            gid = parsed.fragment.split("=", 1)[1]
        return (
            f"https://docs.google.com/spreadsheets/d/{match.group(1)}"
            f"/export?format=csv&gid={gid}"
        )

    def download_sheet(self, sheet_url: str) -> Path:
        self.start()
        assert self.driver
        self._switch_to_handle(self.google_handle)
        self.google_handle = self.driver.current_window_handle
        before = {path: path.stat().st_mtime for path in self.config.download_dir.glob("*.csv")}
        self.logger.info("Google Sheets 데이터를 내려받습니다")
        request_started = time.time()
        self._navigate(self._sheet_export_url(sheet_url), self.google_handle)
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            candidates = sorted(
                self.config.download_dir.glob("*.csv"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for candidate in candidates:
                if candidate not in before or candidate.stat().st_mtime > before[candidate]:
                    related_download = candidate.with_suffix(candidate.suffix + ".crdownload")
                    if candidate.stat().st_mtime >= request_started and not related_download.exists():
                        self.logger.info("Google Sheets 다운로드 완료: %s", candidate.name)
                        return candidate
            time.sleep(0.5)
        if "accounts.google.com" in self.driver.current_url:
            raise AutomationError(
                "Google 로그인이 필요합니다. '로그인 준비'에서 로그인한 뒤 다시 실행하세요"
            )
        raise AutomationError(
            "시트를 내려받지 못했습니다. 공유 권한 또는 Google 로그인을 확인하세요"
        )

    def update_completion_link(
        self,
        sheet_url: str,
        row_number: int,
        completion_url: str,
    ) -> None:
        """Write the published revision URL into column F of the source Sheet."""
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
        """Write one value into an editable Google Sheet cell."""
        self.start()
        assert self.driver
        column = column.upper()
        if not re.fullmatch(r"[A-Z]+", column):
            raise AutomationError(f"올바르지 않은 시트 열입니다: {column}")
        parsed = urlparse(sheet_url)
        gid = parse_qs(parsed.query).get("gid", ["0"])[0]
        if parsed.fragment.startswith("gid="):
            gid = parsed.fragment.split("=", 1)[1].split("&", 1)[0]
        sheet_url_with_range = (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            f"?{parsed.query}#gid={gid}&range={column}{row_number}"
        )
        cell_label = f"{column}{row_number}"
        last_error: Exception | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    self._navigate(sheet_url_with_range, self.google_handle)
                    self.google_handle = self.driver.current_window_handle
                    self.wait.until(
                        lambda driver: driver.execute_script(
                            "return document.readyState"
                        )
                        == "complete"
                    )
                    self.wait.until(
                        EC.presence_of_element_located(
                            (By.ID, "waffle-rich-text-editor")
                        )
                    )
                    self.driver.execute_script("window.focus();")
                    editors = [
                        element
                        for element in self.driver.find_elements(
                            By.ID,
                            "waffle-rich-text-editor",
                        )
                        if element.is_displayed() and element.is_enabled()
                    ]
                    if editors:
                        editor = editors[0]
                        editor.click()
                        editor.send_keys(Keys.CONTROL, "a")
                        editor.send_keys(value)
                        editor.send_keys(Keys.ENTER)
                    else:
                        # Sheets keeps a hidden rich-text editor while a grid
                        # cell is selected. Send typing to its global active-cell
                        # keyboard handler instead of that hidden element.
                        ActionChains(self.driver).send_keys(value).send_keys(
                            Keys.ENTER
                        ).perform()
                    self._verify_sheet_cell(
                        sheet_url,
                        column,
                        row_number,
                        value,
                        checks=verify_checks,
                    )
                    self.logger.info("시트 %s에 값을 입력했습니다", cell_label)
                    return
                except Exception as exc:
                    last_error = exc
                    self.logger.warning(
                        "시트 %s 저장 재시도 (%s/%s): %s",
                        cell_label,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    time.sleep(attempt)
            raise AutomationError(
                f"시트 {cell_label} 저장에 {max_attempts}회 실패했습니다: "
                f"{last_error}"
            )
        finally:
            self._switch_to_handle(self.v2r_handle)

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
            column_index = column_index * 26 + (ord(letter) - ord("A") + 1)
        column_index -= 1
        export_url = self._sheet_export_url(sheet_url)
        for _ in range(checks):
            separator = "&" if "?" in export_url else "?"
            with urlopen(
                f"{export_url}{separator}cache={time.time_ns()}", timeout=20
            ) as response:
                rows = list(
                    csv.reader(
                        io.StringIO(response.read().decode("utf-8-sig"))
                    )
                )
            if len(rows) >= row_number and len(rows[row_number - 1]) > column_index:
                if rows[row_number - 1][column_index] == expected:
                    return
            time.sleep(0.5)
        raise AutomationError(
            f"시트 {column}{row_number} 저장값을 다시 확인하지 못했습니다"
        )

    def ensure_v2r_login(self, email: str, password: str) -> None:
        self.start()
        assert self.driver
        self._navigate(V2R_LIST_URL, self.v2r_handle)
        self.v2r_handle = self.driver.current_window_handle

        def login_state(driver):
            if driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
                return "login"
            if "/nc/board" in driver.current_url and any(
                text in driver.page_source for text in ("글쓰기", "게시글", "카페명")
            ):
                return "authenticated"
            return False

        try:
            state = self.wait.until(login_state)
        except TimeoutException as exc:
            raise AutomationError("V2R 로그인 화면 또는 게시글 목록을 확인하지 못했습니다") from exc
        if state == "authenticated":
            self.logger.info("기존 V2R 로그인 세션을 사용합니다")
            return
        password_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")

        if not email or not password:
            raise AutomationError("V2R 로그인이 필요하지만 아이디 또는 비밀번호가 비어 있습니다")
        email_input = self.driver.find_element(
            By.CSS_SELECTOR, "input[type='email'], input[name*='email'], input[type='text']"
        )
        email_input.clear()
        email_input.send_keys(email)
        password_input.clear()
        password_input.send_keys(password)
        self._click_text(("로그인", "Login"))
        try:
            self.wait.until(lambda driver: not driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
        except TimeoutException as exc:
            raise AutomationError("V2R 로그인에 실패했습니다. 계정 정보나 추가 인증을 확인하세요") from exc
        self.logger.info("V2R 로그인 완료")

    @staticmethod
    def _xpath_literal(value: str) -> str:
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
        parts = value.split("'")
        return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"

    @staticmethod
    def _normalize_option_text(value: str) -> str:
        return re.sub(r"\s+", "", value or "").casefold()

    @staticmethod
    def _normalize_board_text(value: str) -> str:
        return re.sub(r"[^0-9a-z가-힣]+", "", value or "", flags=re.IGNORECASE).casefold()

    @classmethod
    def _option_text_matches(cls, label: str, value: str, option_text: str) -> bool:
        wanted = cls._normalize_option_text(value)
        candidate = cls._normalize_option_text(option_text)
        if candidate == wanted:
            return True
        if label == "게시판":
            return cls._normalize_board_text(option_text) == cls._normalize_board_text(value)
        # Only the cafe display name contains extra branding. Board and account
        # values must remain exact apart from decorative board emoji.
        return label == "카페" and bool(wanted) and wanted in candidate

    def _select_first_dropdown_result(self, input_element, label: str, value: str) -> None:
        """Choose the first filtered V2R custom-menu result through the keyboard."""
        assert self.driver
        # V2R renders a custom result list asynchronously. A brief pause lets
        # the first filtered row become the keyboard-active item.
        time.sleep(0.3)
        input_element.send_keys(Keys.ARROW_DOWN)
        input_element.send_keys(Keys.ENTER)

    def _visible_se_one_selections(self):
        assert self.driver
        return [
            item
            for item in self.driver.find_elements(By.CSS_SELECTOR, ".n-base-selection")
            if item.is_displayed()
        ]

    def _se_one_option_matches(self, label: str, value: str, option_text: str) -> bool:
        if label == "계정":
            wanted = self._normalize_option_text(value)
            account_tokens = re.findall(r"[0-9A-Za-z_-]+", option_text)
            return any(
                self._normalize_option_text(token) == wanted
                for token in account_tokens
            )
        return self._option_text_matches(label, value, option_text)

    def _select_se_one_option(self, label: str, value: str, selection_index: int) -> None:
        assert self.driver
        selections = self._visible_se_one_selections()
        if len(selections) <= selection_index:
            raise AutomationError(f"SE-ONE {label} 선택칸을 찾지 못했습니다")
        selection = selections[selection_index]
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", selection
        )

        def visible_options():
            return [
                item
                for item in self.driver.find_elements(
                    By.CSS_SELECTOR, ".n-base-select-option"
                )
                if item.is_displayed()
            ]

        search_value = (
            AFFILIATE_CAFE_SEARCH_TERMS.get(value, value)
            if label == "카페"
            else value
        )

        def search_inputs():
            fields = [
                item
                for item in selection.find_elements(By.CSS_SELECTOR, "input")
                if item.is_displayed() and item.is_enabled()
            ]
            if fields:
                return fields
            return [
                item
                for item in self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".n-base-select-menu input, .n-base-selection input",
                )
                if item.is_displayed() and item.is_enabled()
            ]

        option_wait = WebDriverWait(
            self.driver,
            min(
                6,
                getattr(
                    getattr(self, "config", None),
                    "timeout_seconds",
                    20,
                ),
            ),
            poll_frequency=0.25,
        )
        options = []
        last_timeout: TimeoutException | None = None
        for attempt in range(3):
            if attempt:
                ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.25)
            selection.click()
            try:
                options = option_wait.until(lambda driver: visible_options())
                break
            except TimeoutException as exc:
                last_timeout = exc
        if not options:
            fields = search_inputs()
            if fields:
                try:
                    search_input = fields[-1]
                    search_input.send_keys(Keys.CONTROL, "a")
                    search_input.send_keys(search_value)
                    time.sleep(0.5)
                    search_input.send_keys(Keys.ARROW_DOWN)
                    search_input.send_keys(Keys.ENTER)
                    selected = WebDriverWait(
                        self.driver,
                        3,
                        poll_frequency=0.25,
                    ).until(
                        lambda driver: self._se_one_option_matches(
                            label,
                            value,
                            selection.text,
                        )
                    )
                    if selected:
                        if label in {"카페", "계정"}:
                            time.sleep(1)
                        return
                except Exception:
                    pass
            raise AutomationError(
                f"SE-ONE {label} 목록이 준비되지 않았습니다"
            ) from last_timeout

        def choose(option_element) -> None:
            option_element.click()
            try:
                WebDriverWait(
                    self.driver,
                    3,
                    poll_frequency=0.25,
                ).until(
                    lambda driver: self._se_one_option_matches(
                        label,
                        value,
                        selection.text,
                    )
                )
            except TimeoutException as exc:
                raise AutomationError(
                    f"SE-ONE {label} 선택 완료를 확인하지 못했습니다: {value}"
                ) from exc
            if label in {"카페", "계정"}:
                time.sleep(1)

        direct = next(
            (
                item
                for item in options
                if self._se_one_option_matches(label, value, item.text)
            ),
            None,
        )
        if direct is not None:
            choose(direct)
            return

        fields = search_inputs()
        if fields:
            try:
                search_input = fields[-1]
                search_input.send_keys(Keys.CONTROL, "a")
                search_input.send_keys(search_value)
            except Exception:
                self.logger.debug(
                    "SE-ONE %s 검색어 입력을 사용할 수 없어 표시 목록에서 찾습니다",
                    label,
                )

        def option():
            return next(
                (
                    item
                    for item in visible_options()
                    if self._se_one_option_matches(label, value, item.text)
                ),
                False,
            )

        try:
            selected_option = self.wait.until(lambda driver: option())
            choose(selected_option)
        except TimeoutException as exc:
            visible_options = [
                item.text.strip().replace("\n", " / ")
                for item in self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".n-base-select-option",
                )
                if item.is_displayed() and item.text.strip()
            ]
            raise AutomationError(
                f"SE-ONE {label} 목록에서 '{value}' 항목을 찾지 못했습니다"
                + (
                    f" / 표시 항목: {', '.join(visible_options[:10])}"
                    if visible_options
                    else " / 표시된 항목 없음"
                )
            ) from exc

    def _click_text(self, texts: tuple[str, ...], exact_only: bool = False) -> None:
        assert self.driver
        for text in texts:
            literal = self._xpath_literal(text)
            xpath = (
                "//*[self::button or self::a or @role='button']"
                f"[normalize-space()={literal}]"
            )
            elements = self.driver.find_elements(By.XPATH, xpath)
            for element in elements:
                if element.is_displayed() and element.is_enabled():
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                    element.click()
                    return
        if not exact_only:
            for text in texts:
                literal = self._xpath_literal(text)
                xpath = (
                    "//*[self::button or self::a or @role='button']"
                    f"[contains(normalize-space(), {literal})]"
                )
                elements = self.driver.find_elements(By.XPATH, xpath)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        element.click()
                        return
        raise AutomationError(f"버튼을 찾지 못했습니다: {' / '.join(texts)}")

    def _field_near_label(self, label: str):
        assert self.driver
        literal = self._xpath_literal(label)
        xpath = (
            f"//*[self::label or self::div or self::span][contains(normalize-space(), {literal})]"
            "/following::*[self::input or self::textarea][1]"
        )
        elements = self.driver.find_elements(By.XPATH, xpath)
        return next((element for element in elements if element.is_displayed()), None)

    def _fill_input(self, label: str, value: str, fallback_css: str | None = None) -> None:
        assert self.driver
        element = self._field_near_label(label)
        if element is None and fallback_css:
            visible = [
                item for item in self.driver.find_elements(By.CSS_SELECTOR, fallback_css) if item.is_displayed()
            ]
            element = visible[0] if visible else None
        if element is None:
            raise AutomationError(f"입력란을 찾지 못했습니다: {label}")
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(value)

    def _select_option(self, label: str, value: str) -> None:
        if not value:
            return
        assert self.driver
        if label in SE_ONE_SELECTION_INDEX:
            self._select_se_one_option(
                label,
                value,
                SE_ONE_SELECTION_INDEX[label],
            )
            return
        se_one_placeholders = {
            "카페": (
                "input[placeholder*='카페']",
                "input[name*='cafe' i]",
            ),
            "게시판": (
                "input[placeholder*='게시판']",
                "input[name*='board' i]",
            ),
            "말머리": (
                "input[placeholder*='말머리']",
            ),
        }
        if label in se_one_placeholders:
            selectors = list(se_one_placeholders[label])
        elif label == "계정":
            selectors = [
                "input[placeholder*='닉네임']",
                "input[placeholder*='계정']",
                "input[name*='account' i]",
            ]
        else:
            selectors = []
        input_element = None
        for selector in selectors:
            inputs = [
                item
                for item in self.driver.find_elements(By.CSS_SELECTOR, selector)
                if item.is_displayed()
            ]
            if inputs:
                input_element = inputs[0]
                break
        if input_element is not None:
            input_element.click()
            input_element.send_keys(Keys.CONTROL, "a")
            search_value = (
                AFFILIATE_CAFE_SEARCH_TERMS.get(value, value)
                if label == "카페"
                else value
            )
            input_element.send_keys(search_value)

        if input_element is not None:
            self._select_first_dropdown_result(input_element, label, value)
            return

        label_literal = self._xpath_literal(label)
        label_elements = self.driver.find_elements(
            By.XPATH,
            f"//*[self::label or self::div or self::span][contains(normalize-space(), {label_literal})]",
        )
        for label_element in label_elements:
            if not label_element.is_displayed():
                continue
            candidates = label_element.find_elements(
                By.XPATH,
                "./following::*[@role='combobox' or self::button or self::input][1]",
            )
            if candidates:
                candidates[0].click()
                break
        else:
            raise AutomationError(f"선택란을 찾지 못했습니다: {label}")

        selection_input = candidates[0]
        selection_input.send_keys(value)
        self._select_first_dropdown_result(selection_input, label, value)

    def _visible_seone_text_paragraphs(self):
        assert self.driver
        return [
            element
            for element in self.driver.find_elements(
                By.CSS_SELECTOR,
                ".se-module-text .se-text-paragraph",
            )
            if element.is_displayed()
            and element.rect.get("width", 0) > 20
            and element.rect.get("height", 0) > 10
            and element.rect.get("x", -1) >= 0
            and element.rect.get("y", -1) >= 0
        ]

    def _focus_seone_text_paragraph(self) -> None:
        assert self.driver
        try:
            paragraphs = self.wait.until(
                lambda driver: self._visible_seone_text_paragraphs()
            )
        except TimeoutException as exc:
            raise AutomationError(
                "사진을 넣을 본문 위치가 준비되지 않았습니다"
            ) from exc
        ActionChains(self.driver).move_to_element(paragraphs[-1]).click().perform()

    def _wait_for_seone_idle(self) -> None:
        assert self.driver
        try:
            self.wait.until(
                lambda driver: driver.execute_script(
                    """
                    const editor = window.SmartEditor &&
                        window.SmartEditor.getEditor('cafepc001');
                    return editor &&
                        typeof editor.isDocumentProcessing === 'function' &&
                        !editor.isDocumentProcessing();
                    """
                )
            )
        except TimeoutException as exc:
            raise AutomationError(
                "SmartEditor 사진 처리가 끝나지 않았습니다"
            ) from exc

    def _fill_editor(self, body: str) -> None:
        assert self.driver
        def visible_editor():
            native_paragraphs = self._visible_seone_text_paragraphs()
            if native_paragraphs:
                return native_paragraphs[0]
            smart_editor_iframes = [
                element
                for element in self.driver.find_elements(
                    By.CSS_SELECTOR, "iframe[title*='스마트 에디터']"
                )
                if element.is_displayed()
                and element.rect.get("width", 0) > 20
                and element.rect.get("height", 0) > 10
                and element.rect.get("x", -1) >= 0
                and element.rect.get("y", -1) >= 0
            ]
            editors = smart_editor_iframes or [
                element
                for element in self.driver.find_elements(
                    By.CSS_SELECTOR,
                    (
                        "[contenteditable='true'], .ProseMirror, .ql-editor, "
                        ".tox-edit-area iframe"
                    ),
                )
                if element.is_displayed()
                and element.get_attribute("title") != "Channel chat"
                and element.rect.get("width", 0) > 20
                and element.rect.get("height", 0) > 10
                and element.rect.get("x", -1) >= 0
                and element.rect.get("y", -1) >= 0
            ]
            return editors[0] if editors else False

        try:
            editor = self.wait.until(lambda driver: visible_editor())
        except TimeoutException as exc:
            raise AutomationError("본문 편집기가 준비되지 않았습니다") from exc
        try:
            if editor.tag_name.lower() == "iframe":
                self.driver.switch_to.frame(editor)
                editor = self.wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
                )
            actions = ActionChains(self.driver)
            actions.move_to_element(editor).click()
            actions.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL)
            actions.send_keys(body).perform()
        finally:
            self.driver.switch_to.default_content()

    def open_se_one_writer(self) -> None:
        """Open the only supported new-post flow using V2R's direct SE-ONE URL."""
        self.start()
        assert self.driver
        self._navigate(V2R_SE_ONE_URL, self.v2r_handle)
        self.v2r_handle = self.driver.current_window_handle
        self.wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
        try:
            self.wait.until(
                lambda driver: len(self._visible_se_one_selections()) >= 4
            )
        except TimeoutException as exc:
            visible_inputs = self.driver.execute_script(
                """
                return [...document.querySelectorAll('input, textarea')]
                    .filter(item => item.offsetParent !== null)
                    .map(item => item.placeholder || item.name || item.type)
                    .filter(Boolean)
                    .slice(0, 10);
                """
            )
            raise AutomationError(
                "SE-ONE 글쓰기 화면은 열렸지만 카페·계정·게시판 입력칸을 찾지 못했습니다. "
                f"표시된 입력칸: {', '.join(visible_inputs) or '없음'}"
            ) from exc

    def inspect_se_one_form(self) -> dict[str, object]:
        """Capture visible SE-ONE controls without filling or publishing anything."""
        self.start()
        assert self.driver
        self._navigate(V2R_SE_ONE_URL, self.v2r_handle)
        self.v2r_handle = self.driver.current_window_handle
        self.wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
        time.sleep(0.5)
        controls = self.driver.execute_script(
            """
            return [...document.querySelectorAll(
                'input, textarea, button, iframe, [contenteditable="true"], '
                '[role="combobox"], [class*="editor"], [class*="Editor"], '
                '[class*="ProseMirror"]'
            )]
                .filter(item => item.offsetParent !== null)
                .map((item, index) => {
                    const rect = item.getBoundingClientRect();
                    const label = item.labels && item.labels.length
                        ? [...item.labels].map(label => label.innerText.trim()).join(' | ')
                        : '';
                    return {
                        order: index,
                        tag: item.tagName.toLowerCase(),
                        type: item.getAttribute('type') || '',
                        placeholder: item.getAttribute('placeholder') || '',
                        name: item.getAttribute('name') || '',
                        role: item.getAttribute('role') || '',
                        ariaLabel: item.getAttribute('aria-label') || '',
                        label,
                        text: (item.innerText || item.textContent || '').trim().slice(0, 80),
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    };
                });
            """
        )
        return {
            "url": self.driver.current_url,
            "title": self.driver.title,
            "controls": controls,
        }

    @staticmethod
    def _request_payload_summary(post_data: str | None) -> dict[str, object]:
        if not post_data:
            return {}
        try:
            payload = json.loads(post_data)
        except json.JSONDecodeError:
            keys = [
                part.split("=", 1)[0]
                for part in post_data.split("&")
                if "=" in part
            ]
            return {"format": "text", "keys": keys, "length": len(post_data)}
        if isinstance(payload, dict):
            return {"format": "json", "keys": sorted(payload.keys())}
        return {"format": "json", "type": type(payload).__name__}

    def start_api_capture(self) -> None:
        """Start a safe network map capture without recording credentials or contents."""
        self.start()
        assert self.driver
        try:
            self.driver.get_log("performance")
        except Exception as exc:
            raise AutomationError(
                "Chrome 네트워크 기록을 시작하지 못했습니다. 프로그램을 다시 실행하세요"
            ) from exc
        self._api_capture_active = True
        self.logger.info("V2R 전체 API 확인 기록을 시작했습니다")

    def finish_api_capture(self) -> list[dict[str, object]]:
        """Return V2R request endpoints and payload key names captured since start."""
        if not self._api_capture_active:
            raise AutomationError("먼저 '전체 API 확인 시작'을 누르세요")
        assert self.driver
        try:
            entries = self.driver.get_log("performance")
        finally:
            self._api_capture_active = False

        requests: dict[str, dict[str, object]] = {}
        for entry in entries:
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            method = message.get("method")
            params = message.get("params", {})
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                url = str(request.get("url", ""))
                parsed = urlparse(url)
                if parsed.netloc != "api-v2r.daboja.im":
                    continue
                request_id = str(params.get("requestId", ""))
                requests[request_id] = {
                    "method": request.get("method", ""),
                    "host": parsed.netloc,
                    "path": parsed.path,
                    "query_keys": sorted(parse_qs(parsed.query).keys()),
                    "payload": self._request_payload_summary(request.get("postData")),
                }
            elif method == "Network.responseReceived":
                request_id = str(params.get("requestId", ""))
                if request_id in requests:
                    response = params.get("response", {})
                    requests[request_id]["status"] = response.get("status")
                    requests[request_id]["mime_type"] = response.get("mimeType", "")
        captured = list(requests.values())
        self.logger.info("V2R API 확인 기록 완료: %s건", len(captured))
        return captured

    def save_screenshot(self, path: Path) -> None:
        self.start()
        assert self.driver
        path.parent.mkdir(parents=True, exist_ok=True)
        self.driver.save_screenshot(str(path))

    def _fill_se_one_fields(self, job: PostJob) -> None:
        self._select_option("카페", job.cafe)
        # SE-ONE requires an account before it enables the board selector.
        if job.account:
            self._select_option("계정", job.account)
        self._select_option("게시판", job.board)
        self._fill_input(
            "제목",
            job.title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self._fill_editor(job.body)
        if job.tags:
            self._fill_input(
                "태그",
                ", ".join(job.tags),
                "input[placeholder*='태그']",
            )

    def _fill_affiliate_daily_fields(self, job: AffiliateJob) -> None:
        if job.daily_post is None:
            raise AutomationError("배정된 일상 글이 없습니다")
        self._select_option("카페", job.cafe)
        self._select_option("계정", job.account)
        try:
            board = AFFILIATE_CAFE_BOARDS[job.cafe]
        except KeyError as exc:
            raise AutomationError(f"제휴 카페 게시판을 알 수 없습니다: {job.cafe}") from exc
        self._select_option("게시판", board)
        self._fill_input(
            "제목",
            job.daily_post.title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self._fill_editor(job.daily_post.body)

    def fill_post(self, job: PostJob, dry_run: bool) -> None:
        self.open_se_one_writer()

        if job.publish_at:
            self.logger.warning(
                "행 %s 예약시간 '%s'은 화면 형식 확인이 필요해 즉시 발행으로 유지합니다",
                job.row_number,
                job.publish_at,
            )
        self._fill_se_one_fields(job)

        if dry_run:
            self.logger.info("행 %s 입력 검증 완료(저장하지 않음)", job.row_number)
            return

        job.post_url = self._submit_registration()

    def _submit_registration(self) -> str:
        self._click_text(("등록",), exact_only=True)
        try:
            self.wait.until(
                lambda driver: "/nc/board" in driver.current_url
                or "/nc/articleDetail/" in driver.current_url
                or any(
                    word in driver.page_source
                    for word in ("등록되었습니다", "발행되었습니다", "작성 완료")
                )
            )
        except TimeoutException as exc:
            raise AutomationError(
                "저장 후 완료 신호를 확인하지 못했습니다. 목록에서 결과를 확인하세요"
            ) from exc
        assert self.driver
        return self.driver.current_url

    def open_revision_reservation(self) -> None:
        """Open the revision editor for the source post currently being viewed."""
        self._click_text(("수정 글 예약",), exact_only=True)
        self.wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")

    def fill_revision_article(self, article: ParsedArticle) -> None:
        """Fill content only; publication timing remains unchanged by design."""
        self._fill_input(
            "제목",
            article.title,
            "input[placeholder*='제목'], textarea[placeholder*='제목']",
        )
        self._fill_editor(article.body)
        self._fill_input("태그", article.tag, "input[placeholder*='태그']")

    def _open_published_article(self, title: str) -> str:
        """Open the newly published daily post from its title if V2R returned to a list."""
        assert self.driver
        if "/articleDetail/" in self.driver.current_url:
            return self.driver.current_url
        title_literal = self._xpath_literal(title)
        article_link = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, f"//a[normalize-space()={title_literal}]")
            )
        )
        article_link.click()
        self.wait.until(lambda driver: "/articleDetail/" in driver.current_url)
        return self.driver.current_url

    def _set_revision_schedule(self, cafe: str) -> None:
        """Set the fixed revision time for each affiliate cafe."""
        assert self.driver
        hours = AFFILIATE_CAFE_DELAYS.get(cafe)
        if hours is None:
            raise AutomationError(f"수정 글 예약 시간을 알 수 없는 카페입니다: {cafe}")
        scheduled_at = datetime.now() + timedelta(hours=hours)
        datetime_inputs = [
            item
            for item in self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='datetime-local']"
            )
            if item.is_displayed()
        ]
        if len(datetime_inputs) != 1:
            raise AutomationError(
                "수정 글 예약 시간 입력란을 찾지 못했습니다. 화면 형식을 확인하세요"
            )
        value = scheduled_at.strftime("%Y-%m-%dT%H:%M")
        self.driver.execute_script(
            """
            const input = arguments[0];
            const value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(input, value);
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            datetime_inputs[0],
            value,
        )
        self.logger.info("%s 수정 글 예약 시간 설정: %s", cafe, scheduled_at.strftime("%m-%d %H:%M"))

    def _fill_comment_input(self, content: str) -> None:
        assert self.driver
        candidates = [
            item
            for item in self.driver.find_elements(
                By.CSS_SELECTOR, "textarea, [contenteditable='true']"
            )
            if item.is_displayed()
            and "ProseMirror" not in (item.get_attribute("class") or "")
        ]
        if not candidates:
            raise AutomationError("댓글 입력창을 찾지 못했습니다")
        input_element = candidates[-1]
        input_element.click()
        input_element.send_keys(Keys.CONTROL, "a")
        input_element.send_keys(content)

    def _select_comment_account(self, account: str) -> None:
        self._select_se_one_option("계정", account, selection_index=4)

    def _click_reply_for(self, parent_text: str) -> None:
        assert self.driver
        literal = self._xpath_literal(parent_text)
        parents = self.driver.find_elements(
            By.XPATH,
            f"//*[contains(normalize-space(), {literal})]",
        )
        for element in parents:
            if not element.is_displayed():
                continue
            ancestor = element
            for _ in range(6):
                buttons = ancestor.find_elements(
                    By.XPATH,
                    ".//*[self::button or @role='button'][normalize-space()='답글쓰기']",
                )
                for button in buttons:
                    if button.is_displayed() and button.is_enabled():
                        button.click()
                        return
                ancestor = ancestor.find_element(By.XPATH, "..")
        raise AutomationError(f"댓글의 답글쓰기 버튼을 찾지 못했습니다: {parent_text[:30]}")

    def _reserve_comment(self) -> None:
        self._click_text(("예약",), exact_only=True)

    def _publish_revision_comments(self, job: AffiliateJob) -> None:
        fixed_labels = (
            "댓글1",
            "댓글2",
            "대대댓글2",
            "댓글3",
            "댓글4",
            "댓글5",
        )
        fixed_accounts = (
            "quilliant",
            "hunnede",
            "prtchht",
            "chocobbn",
            "chenallo",
            "colpith",
        )
        account_map = dict(zip(fixed_labels, random.SystemRandom().sample(fixed_accounts, 6)))
        self.logger.info(
            "행 %s 댓글 고정 계정 순서를 무작위로 배정했습니다",
            job.row_number,
        )

        def publish_node(node, parent_text: str | None = None) -> None:
            if parent_text:
                self._click_reply_for(parent_text)
            account = account_map.get(node.label)
            if account:
                self._select_comment_account(account)
            self._fill_comment_input(node.text)
            self._reserve_comment()
            for child in node.children:
                publish_node(child, node.text)

        for comment in job.comments:
            publish_node(comment)

    def publish_affiliate_revision(
        self,
        job: AffiliateJob,
        dry_run: bool,
        resume=None,
        checkpoint=None,
    ) -> str:
        """Run the live-verified affiliate flow through V2R's own API."""
        return self._get_affiliate_publisher().publish(
            job,
            dry_run,
            resume=resume,
            checkpoint=checkpoint,
        )

    def _get_seone_document(self) -> dict:
        """Read the current SmartEditor document without publishing the draft."""
        assert self.driver
        script = """
            const done = arguments[arguments.length - 1];
            (async () => {
                try {
                    const smartEditor = window.SmartEditor;
                    const editor = smartEditor && (
                        smartEditor.getEditor('cafepc001') ||
                        Object.values(smartEditor._editors || {})[0]
                    );
                    if (editor && typeof editor.getDocumentData === 'function') {
                        const value = await editor.getDocumentData();
                        if (value) {
                            done({ok: true, value});
                            return;
                        }
                    }
                } catch (_) {}

                const container = document.querySelector('.seone-container');
                let instance = container && container.__vueParentComponent;
                const candidates = [];
                while (instance) {
                    candidates.push(
                        instance.setupState,
                        instance.ctx,
                        instance.proxy,
                        instance.provides
                    );
                    let provided = instance.provides;
                    while (provided) {
                        try {
                            for (const key of Reflect.ownKeys(provided)) {
                                candidates.push(provided[key]);
                            }
                        } catch (_) {}
                        provided = Object.getPrototypeOf(provided);
                    }
                    instance = instance.parent;
                }
                for (const candidate of candidates) {
                    if (!candidate) continue;
                    try {
                        let getter = candidate.seoneGetDocument;
                        if (
                            getter &&
                            typeof getter === 'object' &&
                            'value' in getter
                        ) {
                            getter = getter.value;
                        }
                        if (typeof getter !== 'function') continue;
                        const value = await getter.call(candidate);
                        if (value) {
                            done({ok: true, value});
                            return;
                        }
                    } catch (_) {}
                }
                done({ok: false, error: 'SE-ONE document getter not found'});
            })();
        """
        result = self.driver.execute_async_script(script)
        if not result or not result.get("ok") or not result.get("value"):
            detail = result.get("error") if isinstance(result, dict) else "응답 없음"
            raise AutomationError(f"SE-ONE 이미지 문서를 읽지 못했습니다: {detail}")
        return result["value"]

    @staticmethod
    def _media_components(document: dict) -> list[dict]:
        components = document.get("document", {}).get("components", [])
        return [
            component
            for component in components
            if component.get("@ctype") in {"image", "imageGroup", "imageStrip"}
        ]

    def _prepare_seone_image_editor(
        self,
        job: AffiliateJob,
        destination: dict,
    ) -> None:
        """Prepare SE-ONE with the exact destination already resolved by the API."""
        assert self.driver
        cafe_name = str(destination["cafe_name"])
        account = str(destination["naver_login_id"])
        menu_name = str(destination["menu_name"])
        self.logger.info(
            "행 %s API 목적지로 사진 화면 준비: 카페 %s / 게시판 %s",
            job.row_number,
            destination.get("cafe_id"),
            destination.get("menu_id"),
        )
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self.open_se_one_writer()
                self._select_option("카페", cafe_name)
                self._select_option("계정", account)
                self._select_option("게시판", menu_name)
                self._fill_input(
                    "제목",
                    job.title,
                    "input[placeholder*='제목'], textarea[placeholder*='제목']",
                )
                self.wait.until(
                    lambda driver: driver.find_elements(
                        By.CSS_SELECTOR, ".seone-container"
                    )
                )
                self._fill_editor(strip_placeholders(job.body))

                def editor_document_ready(_driver):
                    try:
                        return self._get_seone_document()
                    except AutomationError:
                        return False

                self.wait.until(editor_document_ready)
                return
            except (AutomationError, TimeoutException) as exc:
                last_error = exc
                if attempt == 3:
                    break
                self.logger.warning(
                    "행 %s 사진 화면 준비 재시도 (%s/3): %s",
                    job.row_number,
                    attempt + 1,
                    exc,
                )
                time.sleep(attempt)
        raise AutomationError(
            f"SE-ONE 사진 화면을 준비하지 못했습니다: {last_error}"
        ) from last_error

    def _seone_image_inputs(self):
        assert self.driver
        return [
            item
            for item in self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            if any(
                token in (item.get_attribute("accept") or "").casefold()
                for token in ("image", ".jpg", ".jpeg", ".png", ".gif", ".webp")
            )
        ]

    def _seone_photo_button(self):
        assert self.driver
        selectors = (
            ".se-toolbar-item-image button",
            "button.se-toolbar-option-image",
            "button[aria-label*='사진']",
            "button[aria-label*='이미지']",
            "button[title*='사진']",
            "button[title*='이미지']",
            "button[data-name*='image' i]",
            "[role='button'][data-name*='image' i]",
        )
        button = next(
            (
                element
                for selector in selectors
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector)
                if element.is_displayed() and element.is_enabled()
            ),
            None,
        )
        if button is not None:
            return button
        xpath = (
            "//*[contains(normalize-space(), '사진') or "
            "contains(normalize-space(), '이미지') or "
            "contains(@aria-label, '사진') or contains(@aria-label, '이미지')]"
            "/ancestor-or-self::*[self::button or @role='button'][1]"
        )
        return next(
            (
                element
                for element in self.driver.find_elements(By.XPATH, xpath)
                if element.is_displayed() and element.is_enabled()
            ),
            None,
        )

    def _upload_one_seone_image(
        self,
        image_path: Path,
    ) -> dict:
        assert self.driver
        existing_media_count = len(
            self._media_components(self._get_seone_document())
        )

        self._wait_for_seone_idle()
        self._focus_seone_text_paragraph()
        button = self._seone_photo_button()
        inputs = self._seone_image_inputs()
        if button is not None:
            existing_input_ids = {item.id for item in inputs}
            self.driver.execute_script("arguments[0].click();", button)
            time.sleep(0.5)
            current_inputs = self._seone_image_inputs()
            new_inputs = [
                item
                for item in current_inputs
                if item.id not in existing_input_ids
            ]
            reusable_inputs = [
                item
                for item in current_inputs
                if not item.get_attribute("value")
            ]
            inputs = new_inputs or reusable_inputs
            if not inputs:
                inputs = self.wait.until(
                    lambda _driver: [
                        item
                        for item in self._seone_image_inputs()
                        if item.id not in existing_input_ids
                        or not item.get_attribute("value")
                    ]
                )
        elif not inputs:
            raise AutomationError("SE-ONE 사진 첨부 버튼을 찾지 못했습니다")

        inputs[-1].send_keys(str(image_path.resolve()))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                media = self._media_components(self._get_seone_document())
                if len(media) > existing_media_count:
                    self._wait_for_seone_idle()
                    return media[-1]
            except AutomationError:
                pass
            time.sleep(0.5)
        raise AutomationError(f"SE-ONE 이미지 업로드 완료를 확인하지 못했습니다: {image_path.name}")

    def upload_affiliate_images(
        self,
        job: AffiliateJob,
        destination: dict,
        image_paths: list[Path],
    ) -> list[dict]:
        """Upload through V2R's SmartEditor, while article submission stays API-based."""
        self._prepare_seone_image_editor(job, destination)
        uploaded: list[dict] = []
        for index, image_path in enumerate(image_paths, start=1):
            self.logger.info(
                "행 %s 수정 본문 이미지 업로드 (%s/%s): %s",
                job.row_number,
                index,
                len(image_paths),
                image_path.name,
            )
            try:
                uploaded.append(self._upload_one_seone_image(image_path))
            except Exception as exc:
                raise AutomationError(
                    f"{image_path.name} 사진 첨부 실패: {exc}"
                ) from exc
        return uploaded

    def _get_affiliate_publisher(self):
        from .affiliate_api import AffiliateApiPublisher

        if self._affiliate_publisher is None:
            self._affiliate_publisher = AffiliateApiPublisher(self, self.logger)
        return self._affiliate_publisher

    def start_affiliate_api_run(self, jobs: list[AffiliateJob] | None = None) -> None:
        publisher = self._get_affiliate_publisher()
        publisher._capture_authorization()
        if jobs:
            publisher.cleanup_stale_sources(
                {job.cafe for job in jobs if job.status == JobStatus.PENDING}
            )

    def load_v2r_cafe_catalog(self):
        """Fetch the current cafe/menu catalog without creating any article."""
        return self._get_affiliate_publisher().load_cafe_catalog()

    def assign_affiliate_accounts(
        self, jobs: list[AffiliateJob]
    ) -> list[AffiliateJob]:
        return self._get_affiliate_publisher().assign_accounts(jobs)

    def classify_affiliate_failure(self, error: Exception) -> tuple[str, bool]:
        return self._get_affiliate_publisher().classify_failure(error)

    def replace_failed_affiliate_account(self, job: AffiliateJob) -> str:
        return self._get_affiliate_publisher().replace_failed_account(job)

    def _get_immediate_publisher(self):
        from .immediate_api import ImmediateApiPublisher

        if self._immediate_publisher is None:
            self._immediate_publisher = ImmediateApiPublisher(self, self.logger)
        return self._immediate_publisher

    def prepare_immediate_jobs(self, jobs) -> None:
        self._get_immediate_publisher().prepare_jobs(jobs)

    def publish_immediate(self, job, dry_run: bool) -> str:
        return self._get_immediate_publisher().publish(job, dry_run)

    def replace_failed_immediate_account(self, job) -> str:
        return self._get_immediate_publisher().replace_failed_account(job)

    def classify_immediate_failure(self, error: Exception) -> tuple[str, bool]:
        return self._get_immediate_publisher().classify_failure(error)

    def consume_failed_immediate_urls(self) -> set[str]:
        return self._get_immediate_publisher().consume_failed_source_urls()
