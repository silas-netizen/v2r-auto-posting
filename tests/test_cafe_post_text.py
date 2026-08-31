from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from v2r_auto.exposure import brand_found
from v2r_auto.exposure import STATUS_EXPOSED
from v2r_auto.exposure_naver import (
    SeleniumNaverSearch,
    collect_cafe_post_text,
    comment_shell_present,
    comments_ready,
    document_text,
    find_cafe_frame,
    read_opened_cafe_article,
    wait_for_comments,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
OUTER = FIXTURE_DIR / "cafe_outer.html"
INNER = FIXTURE_DIR / "cafe_main_comments.html"
OUTER_BRAND = FIXTURE_DIR / "cafe_outer_brand.html"
BODY_ONLY = FIXTURE_DIR / "cafe_body_only.html"
QUOTE = "저는 자연방패 항문세정제 쓰고 있어요."
SHELL_BRAND = "장으뜸 장어즙"


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


def _schedule_comment_inject(driver, delay_ms: int = 400) -> None:
    """Start the comment delay after the page is already open.

    A page-load setTimeout can fire during a slow Windows get()/iframe
    switch, which makes the early-read test see comments immediately.
    """
    script = (
        "setTimeout(function () {"
        "  if (window.__injectCafeComments) window.__injectCafeComments();"
        "}, arguments[0]);"
    )
    frame = find_cafe_frame(driver)
    if frame is None:
        driver.execute_script(script, delay_ms)
        return
    driver.switch_to.frame(frame)
    try:
        driver.execute_script(script, delay_ms)
    finally:
        driver.switch_to.default_content()


def test_cafe_article_reader_does_not_click_more_comments() -> None:
    source = inspect.getsource(read_opened_cafe_article)
    source += inspect.getsource(SeleniumNaverSearch.open_post_text)
    assert "더보기" not in source
    assert "send_keys" not in source
    assert "document_text" not in inspect.getsource(read_opened_cafe_article)
    assert "document_text" not in inspect.getsource(collect_cafe_post_text)


def test_early_cafe_main_body_text_misses_delayed_comments() -> None:
    driver = _chrome()
    try:
        driver.get(OUTER.as_uri())
        frame = find_cafe_frame(driver)
        assert frame is not None
        driver.switch_to.frame(frame)
        early = document_text(driver)
        assert QUOTE not in early
        assert comment_shell_present(driver)
        assert not comments_ready(driver)
        assert brand_found(early, ["자연방패 항문세정제"]) == ""
    finally:
        driver.quit()


def test_empty_comment_box_is_not_ready() -> None:
    driver = _chrome()
    try:
        driver.get(INNER.as_uri())
        assert comment_shell_present(driver)
        assert not comments_ready(driver)
        assert QUOTE not in document_text(driver)
    finally:
        driver.quit()


def test_read_opened_cafe_article_waits_past_empty_comment_box() -> None:
    driver = _chrome()
    try:
        driver.get(OUTER.as_uri())
        _schedule_comment_inject(driver)
        text = read_opened_cafe_article(driver, timeout=8)
        assert QUOTE in text
        assert brand_found(text, ["자연방패 항문세정제"]) == "자연방패 항문세정제"
        assert STATUS_EXPOSED == "노출완"
    finally:
        driver.quit()


def test_cafe_shell_brand_is_not_article_text() -> None:
    driver = _chrome()
    try:
        driver.get(OUTER_BRAND.as_uri())
        text = read_opened_cafe_article(driver, timeout=4)
        assert SHELL_BRAND not in text
        assert brand_found(text, [SHELL_BRAND]) == ""
    finally:
        driver.quit()


def test_body_only_brand_is_not_article_text() -> None:
    driver = _chrome()
    try:
        driver.get(BODY_ONLY.as_uri())
        text = collect_cafe_post_text(driver)
        assert SHELL_BRAND not in text
        assert brand_found(text, [SHELL_BRAND]) == ""
        assert collect_cafe_post_text(driver) == ""
    finally:
        driver.quit()


def test_collect_js_reads_comment_nodes() -> None:
    driver = _chrome()
    try:
        driver.get(INNER.as_uri())
        _schedule_comment_inject(driver)
        assert wait_for_comments(driver, 8)
        text = collect_cafe_post_text(driver)
        assert QUOTE in text
        assert brand_found(text, ["자연방패 항문세정제"]) == "자연방패 항문세정제"
    finally:
        driver.quit()
