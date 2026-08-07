from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    UnexpectedAlertPresentException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .content import ParsedArticle
from .models import PostJob


V2R_LIST_URL = "https://v2r.daboja.im/nc/board?view=list"
V2R_SE_ONE_URL = "https://v2r.daboja.im/nc/seone"


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
        self.driver = webdriver.Chrome(options=options)
        self.v2r_handle = self.driver.current_window_handle

    def close(self) -> None:
        if self.driver:
            if not self.config.debugger_address:
                self.driver.quit()
            self.driver = None
            self.v2r_handle = None
            self.google_handle = None

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
        se_one_placeholders = {
            "카페": "카페 검색",
            "게시판": "게시판 검색",
            "말머리": "말머리 검색",
        }
        if label in se_one_placeholders:
            selectors = [f"input[placeholder='{se_one_placeholders[label]}']"]
        elif label == "계정":
            selectors = ["input[placeholder='닉네임 or 계정 검색']"]
        else:
            selectors = []
        for selector in selectors:
            inputs = [
                item
                for item in self.driver.find_elements(By.CSS_SELECTOR, selector)
                if item.is_displayed()
            ]
            if inputs:
                input_element = inputs[0]
                input_element.click()
                input_element.send_keys(Keys.CONTROL, "a")
                input_element.send_keys(value)
                break
        else:
            input_element = None

        if input_element is not None:
            value_literal = self._xpath_literal(value)
            option_xpath = (
                f"//*[@role='option' or self::li or self::div]"
                f"[normalize-space()={value_literal}]"
            )
            try:
                option = self.wait.until(EC.element_to_be_clickable((By.XPATH, option_xpath)))
                option.click()
                return
            except TimeoutException as exc:
                raise AutomationError(f"'{label}'에서 '{value}' 항목을 찾지 못했습니다") from exc

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

        value_literal = self._xpath_literal(value)
        option_xpath = (
            f"//*[@role='option' or self::li or self::div]"
            f"[normalize-space()={value_literal}]"
        )
        try:
            option = self.wait.until(EC.element_to_be_clickable((By.XPATH, option_xpath)))
            option.click()
        except TimeoutException as exc:
            raise AutomationError(f"'{label}'에서 '{value}' 항목을 찾지 못했습니다") from exc

    def _fill_editor(self, body: str) -> None:
        assert self.driver
        editors = [
            element
            for element in self.driver.find_elements(
                By.CSS_SELECTOR,
                "[contenteditable='true'], .ProseMirror, .ql-editor, .tox-edit-area iframe",
            )
            if element.is_displayed()
        ]
        if not editors:
            raise AutomationError("본문 편집기를 찾지 못했습니다")
        editor = editors[0]
        if editor.tag_name.lower() == "iframe":
            self.driver.switch_to.frame(editor)
            editor = self.driver.find_element(By.CSS_SELECTOR, "body")
        editor.click()
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(body)
        self.driver.switch_to.default_content()

    def open_se_one_writer(self) -> None:
        """Open the only supported new-post flow using V2R's direct SE-ONE URL."""
        self.start()
        assert self.driver
        self._navigate(V2R_SE_ONE_URL, self.v2r_handle)
        self.v2r_handle = self.driver.current_window_handle
        self.wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
        self.wait.until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "input[placeholder='카페 검색']")
            )
        )

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

        self._click_text(("등록",), exact_only=True)
        try:
            self.wait.until(
                lambda driver: "/nc/board" in driver.current_url
                or any(
                    word in driver.page_source
                    for word in ("등록되었습니다", "발행되었습니다", "작성 완료")
                )
            )
        except TimeoutException as exc:
            raise AutomationError(
                "저장 후 완료 신호를 확인하지 못했습니다. 목록에서 결과를 확인하세요"
            ) from exc
        job.post_url = self.driver.current_url

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
