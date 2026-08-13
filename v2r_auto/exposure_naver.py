from __future__ import annotations

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .exposure import naver_search_url


def fetch_naver_html(browser, url: str, timeout: int = 20) -> str:
    browser.start()
    if not browser.driver:
        raise RuntimeError("Chrome이 시작되지 않았습니다")
    browser.driver.get(url or naver_search_url(""))
    WebDriverWait(browser.driver, timeout).until(
        lambda driver: driver.find_elements(By.CSS_SELECTOR, "#main_pack, #content, body")
    )
    time.sleep(0.8)
    return browser.driver.page_source
