from __future__ import annotations

from pathlib import Path

import pytest

from v2r_auto.exposure import is_keyword_tool_placeholder, keyword_tool_query, volume_from_result_cells
from v2r_auto.exposure_naver import (
    CLICK_KEYWORD_LOOKUP_JS,
    FILL_KEYWORD_TOOL_BOX_JS,
    FIND_KEYWORD_TOOL_BOX_JS,
    SeleniumNaverSearch,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "keyword_planner.html"

OLD_BUGGY_FINDER_JS = r"""
const visible = (el) => {
  const r = el.getBoundingClientRect();
  return r.width > 40 && r.height > 12 && el.offsetParent !== null;
};
const boxes = Array.from(document.querySelectorAll('textarea, input[type="text"]')).filter(visible);
const hinted = boxes.find((el) => (el.getAttribute('placeholder') || '').includes('한 줄에 하나씩'));
if (hinted) return hinted;
const section = Array.from(document.querySelectorAll('*')).find((el) => {
  const text = (el.innerText || '').replace(/\s+/g, '');
  return text.includes('연관키워드조회기준') && text.length < 3500;
});
if (section) {
  const inner = Array.from(section.querySelectorAll('textarea, input[type="text"]')).filter(visible);
  if (inner.length) return inner[0];
}
return null;
"""


def test_keyword_tool_placeholder_matches_ads_center_copy() -> None:
    real = "한줄에 하나씩 입력하세요.\n(최대 5개까지)"
    assert is_keyword_tool_placeholder(real)
    assert is_keyword_tool_placeholder("한 줄에 하나씩 입력해 주세요")
    assert not is_keyword_tool_placeholder("검색")
    assert not ("한 줄에 하나씩" in real)


def _chrome():
    from selenium.webdriver import Chrome, ChromeOptions

    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    try:
        driver = Chrome(options=options)
    except Exception as exc:
        pytest.skip(f"Chrome을 열 수 없습니다: {exc}")
    return driver


def test_keyword_tool_box_ignores_header_and_clicks_lookup() -> None:
    driver = _chrome()
    try:
        driver.get(FIXTURE.as_uri())
        old_box = driver.execute_script(OLD_BUGGY_FINDER_JS)
        assert old_box.get_attribute("id") == "header-search"

        browser = type("Browser", (), {"driver": driver})()
        search = SeleniumNaverSearch(browser, __import__("logging").getLogger("test"))
        box = search._find_keyword_tool_box(driver)
        assert box is not None
        assert box.get_attribute("id") == "keyword-hints"
        assert search._is_ads_header_search(driver, driver.find_element("id", "header-search"))
        assert not search._is_ads_header_search(driver, box)

        query = keyword_tool_query("뱃살 다이어트 보조제")
        search._fill_keyword_tool_box(driver, box, query)
        assert driver.find_element("id", "header-search").get_attribute("value") == ""
        assert "뱃살다이어트보조제" in (box.get_attribute("value") or "")
        assert driver.find_element("id", "lookup").get_property("disabled") is False
        assert search._click_keyword_lookup(driver, retries=4)
        assert driver.execute_script("return window.__lookedUp") is True
        volume = search._volume_from_keyword_table(driver, query)
        assert volume == 130
        assert volume_from_result_cells(["뱃살다이어트보조제", "20", "110"], query) == 130
    finally:
        driver.quit()


def test_finder_js_returns_textarea_not_header() -> None:
    driver = _chrome()
    try:
        driver.get(FIXTURE.as_uri())
        found = driver.execute_script(FIND_KEYWORD_TOOL_BOX_JS)
        assert found.get_attribute("id") == "keyword-hints"
        filled = driver.execute_script(FILL_KEYWORD_TOOL_BOX_JS, found, "뱃살다이어트보조제")
        assert filled == "뱃살다이어트보조제"
        assert driver.execute_script(CLICK_KEYWORD_LOOKUP_JS) is True
        assert driver.execute_script("return window.__lookedUp") is True
    finally:
        driver.quit()
