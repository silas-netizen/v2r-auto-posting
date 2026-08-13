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


class SeleniumNaverSearch:
    def __init__(self, browser, logger: logging.Logger, timeout: int = 15):
        self.browser = browser
        self.logger = logger
        self.timeout = timeout

    def _driver(self):
        self.browser.start()
        if not self.browser.driver:
            raise RuntimeError("Chrome이 시작되지 않았습니다")
        return self.browser.driver

    def search_integrated(self, keyword: str) -> str:
        driver = self._driver()
        query = strip_parenthetical(keyword)
        driver.get("https://www.naver.com/")
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
            return
        # Never click 카페. Only confirm 통합 is selected if the URL already is.
        return

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
