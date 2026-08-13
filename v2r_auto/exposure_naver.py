from __future__ import annotations

import logging
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .exposure import strip_parenthetical

SEARCH_BOX_SELECTORS = (
    "#query",
    "#nx_query",
    'input[name="query"]',
    "input.search_input",
)
AUTOCOMPLETE_SELECTORS = (
    ".item._item",
    "ul.item_list > li a",
    ".auto_list li a",
    ".auto_list li",
    ".atcmp_keyword",
    ".kwd_lst li a",
    ".nx_list_auto li a",
    "#autoFrame li a",
    ".lnb_item",
    "div.item a",
)
INTEGRATED_TAB_TEXTS = {"통합", "통합검색"}
CAFE_IFRAME_SELECTORS = (
    "iframe#cafe_main",
    "iframe[name='cafe_main']",
    "iframe#cafe_content",
)
NAVER_LOGIN_COOKIES = {"NID_AUT", "NID_SES"}


def is_naver_logged_in_cookies(cookies) -> bool:
    names = {str(item.get("name") or "") for item in cookies or []}
    return bool(names & NAVER_LOGIN_COOKIES)


class SeleniumNaverSearch:
    def __init__(self, browser, logger: logging.Logger, timeout: int = 15):
        self.browser = browser
        self.logger = logger
        self.timeout = timeout
        self._naver_handle: str | None = None

    def _driver(self):
        self._ensure_browser()
        if not self.browser.driver:
            raise RuntimeError("Chrome이 시작되지 않았습니다")
        return self.browser.driver

    def _ensure_browser(self) -> None:
        driver = self.browser.driver
        if driver:
            try:
                driver.window_handles
                return
            except Exception:
                self.logger.warning(
                    "크롬 창이 닫혀 다시 엽니다. 로그인이 풀렸으면 한 번만 다시 로그인하세요"
                )
                try:
                    driver.quit()
                except Exception:
                    pass
                self.browser.driver = None
                self._naver_handle = None
        self.browser.start()

    def prepare_login(self, wait_seconds: float = 300) -> None:
        driver = self._driver()
        if not self._has_naver_tab(driver):
            driver.get("https://www.naver.com/")
            self._naver_handle = driver.current_window_handle
        if self.is_logged_in():
            self.logger.info(
                "네이버 로그인이 이미 되어 있습니다. 프로그램을 끄기 전까지 유지됩니다"
            )
            return
        self.logger.info(
            "네이버에 한 번만 로그인하세요. 프로그램을 끄기 전까지 다시 묻지 않습니다"
        )
        deadline = time.time() + wait_seconds
        next_notice = time.time() + 20
        while time.time() < deadline:
            if self.is_logged_in():
                self.logger.info("네이버 로그인을 확인했습니다. 이 크롬 창은 닫지 마세요")
                return
            time.sleep(1.2)
            if time.time() >= next_notice:
                self.logger.info("크롬 창에서 네이버 로그인을 기다리는 중입니다")
                next_notice = time.time() + 20
        self.logger.warning(
            "아직 로그인이 확인되지 않았습니다. 크롬에서 로그인하면 그때부터 유지됩니다"
        )

    def require_login(self, wait_seconds: float = 90) -> None:
        self._driver()
        if self.is_logged_in():
            self.logger.info("네이버 로그인 유지 중")
            return
        self.logger.info("네이버 로그인이 필요합니다. 크롬 창에서 로그인하세요")
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.is_logged_in():
                self.logger.info("네이버 로그인을 확인했습니다")
                return
            time.sleep(1.2)
        raise RuntimeError(
            "네이버 로그인이 필요합니다. 크롬 창에서 로그인한 뒤 다시 시작하세요"
        )

    def is_logged_in(self) -> bool:
        driver = self._driver()
        try:
            current = driver.current_window_handle
        except Exception:
            current = None
        try:
            for handle in list(driver.window_handles):
                try:
                    driver.switch_to.window(handle)
                    if is_naver_logged_in_cookies(driver.get_cookies()):
                        return True
                except Exception:
                    continue
            return False
        finally:
            if current:
                try:
                    driver.switch_to.window(current)
                except Exception:
                    pass

    def search_integrated(self, keyword: str) -> str:
        driver = self._driver()
        query = strip_parenthetical(keyword)
        self._focus_naver_tab(driver)
        box = self._find_search_box(driver)
        box.click()
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
        box.send_keys(query)
        time.sleep(0.55)
        if not self._click_first_autocomplete(driver):
            box.send_keys(Keys.ENTER)
        WebDriverWait(driver, self.timeout).until(
            lambda item: item.find_elements(
                By.CSS_SELECTOR, "#main_pack, #content, #lnb"
            )
        )
        time.sleep(0.35)
        self._stay_on_integrated_tab(driver)
        self._scroll_to_end(driver)
        return driver.page_source

    def open_post_text(self, url: str) -> str:
        driver = self._driver()
        naver = self._focus_naver_tab(driver)
        extra_tab = False
        try:
            driver.switch_to.new_window("tab")
            extra_tab = True
        except Exception:
            extra_tab = False
        try:
            driver.get(url)
            WebDriverWait(driver, self.timeout).until(
                lambda item: item.find_elements(By.CSS_SELECTOR, "body")
            )
            time.sleep(0.45)
            chunks = [self._visible_text(driver)]
            for selector in CAFE_IFRAME_SELECTORS:
                frames = driver.find_elements(By.CSS_SELECTOR, selector)
                if not frames:
                    continue
                try:
                    driver.switch_to.frame(frames[0])
                    time.sleep(0.35)
                    driver.execute_script(
                        "window.scrollTo(0, document.body.scrollHeight);"
                    )
                    time.sleep(0.25)
                    chunks.append(self._visible_text(driver))
                finally:
                    driver.switch_to.default_content()
                break
            return "\n".join(part for part in chunks if part)
        finally:
            if extra_tab:
                try:
                    driver.close()
                except Exception:
                    pass
                try:
                    driver.switch_to.window(naver)
                except Exception:
                    self._naver_handle = None
                    self._focus_naver_tab(driver)
            else:
                self._focus_naver_tab(driver)

    def _has_naver_tab(self, driver) -> bool:
        return self._existing_naver_handle(driver) is not None

    def _focus_naver_tab(self, driver) -> str:
        handle = self._existing_naver_handle(driver)
        if handle:
            driver.switch_to.window(handle)
            self._naver_handle = handle
            url = (driver.current_url or "").lower()
            if not self._is_naver_search_or_home(url):
                driver.get("https://www.naver.com/")
            return driver.current_window_handle
        driver.get("https://www.naver.com/")
        self._naver_handle = driver.current_window_handle
        return self._naver_handle

    def _existing_naver_handle(self, driver) -> str | None:
        handles = list(driver.window_handles)
        if self._naver_handle in handles:
            return self._naver_handle
        for handle in handles:
            try:
                driver.switch_to.window(handle)
            except Exception:
                continue
            url = (driver.current_url or "").lower()
            if self._is_naver_search_or_home(url):
                return handle
        return None

    def _is_naver_search_or_home(self, url: str) -> bool:
        text = (url or "").lower()
        if "cafe.naver.com" in text or "nid.naver.com" in text:
            return False
        return (
            "search.naver.com" in text
            or "www.naver.com" in text
            or text.rstrip("/") in {"https://naver.com", "http://naver.com"}
        )

    def _find_search_box(self, driver):
        last_error = None
        for selector in SEARCH_BOX_SELECTORS:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except Exception as exc:
                    last_error = exc
        raise RuntimeError(f"네이버 검색창을 찾지 못했습니다: {last_error}")

    def _click_first_autocomplete(self, driver) -> bool:
        for selector in AUTOCOMPLETE_SELECTORS:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not element.is_displayed():
                        continue
                    driver.execute_script("arguments[0].click();", element)
                    self.logger.info("자동완성 첫 항목을 선택했습니다")
                    return True
                except Exception:
                    continue
        self.logger.info("자동완성이 없어 입력한 키워드 그대로 검색합니다")
        return False

    def _stay_on_integrated_tab(self, driver) -> None:
        current = (driver.current_url or "").lower()
        if "where=article" in current or "where=cafe" in current:
            self.logger.info("카페 탭이 열려 통합검색으로 돌아갑니다")
            for tab in driver.find_elements(
                By.CSS_SELECTOR, "#lnb a, .api_lnb_menu a, .lnb_tab a, a.tab"
            ):
                label = (tab.text or "").replace(" ", "")
                if label in INTEGRATED_TAB_TEXTS:
                    try:
                        tab.click()
                        time.sleep(0.4)
                    except Exception:
                        continue
                    break

    def _scroll_to_end(self, driver, rounds: int = 8) -> None:
        last_height = 0
        for _ in range(rounds):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.28)
            height = driver.execute_script("return document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height

    def _visible_text(self, driver) -> str:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""
