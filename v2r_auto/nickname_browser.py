from __future__ import annotations

import json
import time
from typing import Any, Callable

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from .browser import AutomationError, V2RBrowser
from .nickname_exclude import (
    DEFAULT_CAFE_URL,
    FLOWMOA_MEMBERSHIP_URL,
    NAVER_LOGIN_URL,
    CafeTarget,
    ExcludeSyncResult,
    NicknameExcludeError,
    build_sync_result,
    cafe_id_from_page,
    cafe_search_url,
    cafe_search_url_modern,
    cookies_show_naver_login,
    join_nicknames,
    nicknames_from_html,
    nicknames_from_json,
    page_is_missing,
    page_requires_naver_login,
    parse_cafe_address,
    payload_has_articles,
    require_cafe_id,
    require_keywords,
    search_api_urls,
    split_nicknames,
)


FETCH_SCRIPT = """
const url = arguments[0];
const done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', headers: {Accept: 'application/json,text/html,*/*'}})
  .then(async (response) => {
    const text = await response.text();
    done({ok: response.ok, status: response.status, text: text});
  })
  .catch((error) => done({ok: false, status: 0, text: String(error)}));
"""


class NicknameExcludeSession:
    def __init__(self, browser: V2RBrowser, cafe: CafeTarget | None = None):
        self.browser = browser
        self.logger = browser.logger
        self.cafe = cafe or parse_cafe_address(DEFAULT_CAFE_URL)
        self.cafe_handle: str | None = None
        self.flowmoa_handle: str | None = None

    def open_login_windows(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        browser = self.browser
        browser.start()
        assert browser.driver
        self.cafe_handle = browser.driver.current_window_handle
        self.wait_for_naver_login(should_stop=should_stop)
        self._open_cafe_home()
        browser.driver.switch_to.new_window("tab")
        self.flowmoa_handle = browser.driver.current_window_handle
        browser._navigate(FLOWMOA_MEMBERSHIP_URL, self.flowmoa_handle)
        self.logger.info("신고기 창을 열었습니다")

    def wait_for_naver_login(
        self,
        should_stop: Callable[[], bool] | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.browser.ensure_browser()
        assert self.browser.driver
        if self._naver_logged_in():
            self.logger.info("네이버 로그인이 되어 있습니다")
            return
        self._switch(self.cafe_handle)
        self.cafe_handle = self.browser.driver.current_window_handle
        self.browser._navigate(NAVER_LOGIN_URL, self.cafe_handle)
        self.logger.info(
            "네이버 로그인 창을 먼저 열었습니다. "
            "이 크롬에서 로그인하면 카페가 열립니다"
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                raise NicknameExcludeError("확인을 중지했습니다")
            if self._naver_logged_in():
                self.logger.info("네이버 로그인을 확인했습니다")
                return
            time.sleep(1)
        raise NicknameExcludeError(
            "네이버 로그인을 기다렸지만 확인하지 못했습니다. "
            "이 프로그램 크롬에서 로그인한 뒤 다시 시작해 주세요"
        )

    def _naver_logged_in(self) -> bool:
        assert self.browser.driver
        names = [cookie.get("name", "") for cookie in self.browser.driver.get_cookies()]
        if cookies_show_naver_login(names):
            return True
        url = self.browser.driver.current_url or ""
        if page_requires_naver_login(self._page_html(), url):
            return False
        current = url.casefold()
        return "cafe.naver.com" in current and "nid.naver.com" not in current

    def _open_cafe_home(self) -> None:
        self._switch(self.cafe_handle)
        self.browser._navigate(self.cafe.home_url, self.cafe_handle)
        self.cafe_handle = self.browser.driver.current_window_handle
        self._resolve_cafe_id()
        self.logger.info("카페를 열었습니다: %s", self.cafe.home_url)

    def _resolve_cafe_id(self) -> None:
        if self.cafe.cafe_id:
            return
        found = cafe_id_from_page(self._page_html(), self.browser.driver.current_url)
        if not found:
            raise NicknameExcludeError(
                "카페 번호를 찾지 못했습니다. 카페 주소를 다시 확인해 주세요"
            )
        self.cafe = self.cafe.with_cafe_id(found)

    def _switch(self, handle: str | None) -> None:
        self.browser.ensure_browser()
        assert self.browser.driver
        self.browser._switch_to_handle(handle)

    def _fetch(self, url: str) -> dict[str, Any]:
        assert self.browser.driver
        raw = self.browser.driver.execute_async_script(FETCH_SCRIPT, url)
        if not isinstance(raw, dict):
            return {"ok": False, "status": 0, "text": ""}
        return raw

    def _page_html(self) -> str:
        assert self.browser.driver
        return self.browser.driver.page_source or ""

    def _require_cafe_login(self) -> None:
        assert self.browser.driver
        html = self._page_html()
        url = self.browser.driver.current_url
        if page_requires_naver_login(html, url):
            raise NicknameExcludeError(
                "네이버 로그인 화면이 열렸습니다. "
                "이 프로그램 크롬에서 네이버에 로그인한 뒤 다시 시작해 주세요"
            )

    def collect_cafe_nicknames(
        self,
        keywords: list[str],
        should_stop: Callable[[], bool] | None = None,
    ) -> list[str]:
        self.browser.ensure_browser()
        assert self.browser.driver
        self.wait_for_naver_login(should_stop=should_stop)
        if self.cafe_handle is None:
            self.cafe_handle = self.browser.driver.current_window_handle
        self._open_cafe_home()
        self._require_cafe_login()
        found: list[str] = []
        for keyword in keywords:
            if should_stop and should_stop():
                raise NicknameExcludeError("확인을 중지했습니다")
            found.extend(self._search_one_keyword(keyword, should_stop))
        nicknames = split_nicknames("\n".join(found))
        self.logger.info("카페에서 닉네임 %s개를 모았습니다", len(nicknames))
        return nicknames

    def _search_one_keyword(
        self,
        keyword: str,
        should_stop: Callable[[], bool] | None,
    ) -> list[str]:
        require_cafe_id(self.cafe)
        self.logger.info("카페 글 검색창에서 '%s'를 찾습니다", keyword)
        # 카페 글 검색만 연다. 글쓰기 화면은 쓰지 않는다.
        self.browser._navigate(cafe_search_url(keyword, self.cafe), self.cafe_handle)
        time.sleep(1.2)
        assert self.browser.driver
        current = (self.browser.driver.current_url or "").casefold()
        html = self._page_html()
        if (
            "articlewrite" in current
            or "/write" in current
            or page_is_missing(html, current)
        ):
            self.logger.info("글 검색 화면이 아니라서 카페 검색 주소로 다시 엽니다")
            self.browser._navigate(
                cafe_search_url_modern(keyword, self.cafe),
                self.cafe_handle,
            )
            time.sleep(1.2)
        self._require_cafe_login()
        found: list[str] = []
        page = 1
        empty_pages = 0
        while page <= 80:
            if should_stop and should_stop():
                raise NicknameExcludeError("확인을 중지했습니다")
            page_nicks = self._search_page(keyword, page)
            if page_nicks:
                found.extend(page_nicks)
                empty_pages = 0
                self.logger.info(
                    "'%s' %s페이지에서 닉네임 %s개",
                    keyword,
                    page,
                    len(page_nicks),
                )
            else:
                empty_pages += 1
                if page > 1 and empty_pages >= 1:
                    break
                if page == 1:
                    html_nicks = nicknames_from_html(self._page_html())
                    found.extend(html_nicks)
                    break
            page += 1
            time.sleep(0.4)
        return found

    def _search_page(self, keyword: str, page: int) -> list[str]:
        for url in search_api_urls(keyword, page, self.cafe):
            result = self._fetch(url)
            if not result.get("ok"):
                continue
            text = str(result.get("text") or "")
            payload: Any
            try:
                payload = json.loads(text) if text else None
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                if page > 1 and not payload_has_articles(payload):
                    return []
                nicks = nicknames_from_json(payload)
                if nicks:
                    return nicks
            html_nicks = nicknames_from_html(text)
            if html_nicks:
                return html_nicks
        if page == 1:
            return nicknames_from_html(self._page_html())
        return []

    def login_flowmoa(self, user: str, password: str) -> None:
        if not user or not password:
            raise NicknameExcludeError("신고기 아이디와 비밀번호를 넣어 주세요")
        self.browser.ensure_browser()
        assert self.browser.driver
        self.browser._navigate(FLOWMOA_MEMBERSHIP_URL, self.flowmoa_handle)
        self.flowmoa_handle = self.browser.driver.current_window_handle
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self._membership_ready():
                self.logger.info("신고기 제외 닉네임 화면을 열었습니다")
                return
            if self._fill_flowmoa_login(user, password):
                time.sleep(1.5)
                continue
            time.sleep(0.8)
        if self._membership_ready():
            return
        raise NicknameExcludeError(
            "신고기 사이트에 들어가지 못했습니다. "
            "크롬에서 보안 확인과 로그인을 마친 뒤 다시 시작해 주세요"
        )

    def _fill_flowmoa_login(self, user: str, password: str) -> bool:
        assert self.browser.driver
        user_boxes = [
            element
            for element in self.browser.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'], input[type='email'], input[name*='id'], input[name*='user']",
            )
            if element.is_displayed()
        ]
        password_boxes = [
            element
            for element in self.browser.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='password']",
            )
            if element.is_displayed()
        ]
        if not user_boxes or not password_boxes:
            return False
        user_box = user_boxes[0]
        password_box = password_boxes[0]
        user_box.click()
        user_box.send_keys(Keys.CONTROL, "a")
        user_box.send_keys(user)
        password_box.click()
        password_box.send_keys(Keys.CONTROL, "a")
        password_box.send_keys(password)
        buttons = [
            element
            for element in self.browser.driver.find_elements(
                By.CSS_SELECTOR,
                "button, input[type='submit']",
            )
            if element.is_displayed()
        ]
        if buttons:
            buttons[0].click()
        else:
            password_box.send_keys(Keys.ENTER)
        self.logger.info("신고기 로그인을 시도했습니다")
        return True

    def _membership_ready(self) -> bool:
        html = self._page_html()
        return "제외" in html and "닉네임" in html

    def read_exclude_nicknames(self) -> list[str]:
        field = self._exclude_field()
        value = field.get_attribute("value") or field.text or ""
        return split_nicknames(value)

    def write_exclude_nicknames(self, nicknames: list[str]) -> list[str]:
        field = self._exclude_field()
        text = join_nicknames(nicknames)
        field.click()
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(text)
        self._click_save()
        time.sleep(1.2)
        saved = self.read_exclude_nicknames()
        if split_nicknames(text) and not saved:
            raise NicknameExcludeError("제외 닉네임을 저장했는지 확인하지 못했습니다")
        self.logger.info("신고기 제외 닉네임 %s개를 저장했습니다", len(saved or split_nicknames(text)))
        return saved or split_nicknames(text)

    def _exclude_field(self):
        assert self.browser.driver
        self._switch(self.flowmoa_handle)
        html = self._page_html()
        if "제외" not in html:
            self.browser._navigate(FLOWMOA_MEMBERSHIP_URL, self.flowmoa_handle)
            time.sleep(1)
        selectors = (
            "textarea[name*='nick']",
            "textarea[id*='nick']",
            "input[name*='nick']",
            "textarea[name*='except']",
            "textarea[name*='exclude']",
            "textarea",
        )
        for selector in selectors:
            for element in self.browser.driver.find_elements(By.CSS_SELECTOR, selector):
                if not element.is_displayed():
                    continue
                blob = " ".join(
                    [
                        element.get_attribute("name") or "",
                        element.get_attribute("id") or "",
                        element.get_attribute("placeholder") or "",
                    ]
                )
                if any(token in blob for token in ("nick", "닉", "except", "exclude", "제외")):
                    return element
        # last fallback: the textarea nearest the 제외 닉네임 label
        try:
            label = self.browser.driver.find_element(
                By.XPATH,
                "//*[contains(text(),'제외') and contains(text(),'닉네임')]",
            )
            nearby = label.find_elements(
                By.XPATH,
                "./following::textarea[1] | ./following::input[1]",
            )
            if nearby:
                return nearby[0]
        except Exception:
            pass
        raise NicknameExcludeError("신고기 화면에서 제외 닉네임 칸을 찾지 못했습니다")

    def _click_save(self) -> None:
        assert self.browser.driver
        texts = ("저장", "수정", "확인", "등록", "Save")
        for text in texts:
            buttons = self.browser.driver.find_elements(
                By.XPATH,
                "//*[self::button or self::a or @type='submit']"
                f"[contains(normalize-space(), '{text}')]",
            )
            for button in buttons:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    return
        field = self._exclude_field()
        field.send_keys(Keys.TAB)
        field.send_keys(Keys.ENTER)

    def sync(
        self,
        keywords_text: str,
        user: str,
        password: str,
        should_stop: Callable[[], bool] | None = None,
        cafe_url: str = "",
    ) -> ExcludeSyncResult:
        if cafe_url:
            self.cafe = parse_cafe_address(cafe_url)
        keywords = require_keywords(keywords_text)
        found = self.collect_cafe_nicknames(keywords, should_stop=should_stop)
        self.login_flowmoa(user, password)
        existing = self.read_exclude_nicknames()
        plan = build_sync_result(keywords, found, existing)
        if plan.added:
            plan.saved = self.write_exclude_nicknames(plan.saved)
        else:
            plan.saved = existing
            self.logger.info("새로 넣을 닉네임이 없습니다")
        return plan


def open_login_windows(
    browser: V2RBrowser,
    cafe: CafeTarget | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    NicknameExcludeSession(browser, cafe).open_login_windows(should_stop=should_stop)
