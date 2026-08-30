from __future__ import annotations

import logging
import re
import time

from urllib.parse import parse_qs, unquote_plus, urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .exposure import (
    compact_text,
    is_cafe_article_url,
    keyword_tool_query,
    keywordstool_volume,
    naver_search_url,
    parse_qc_count,
    same_search_query,
    strip_parenthetical,
    volume_from_result_cells,
)

FIND_KEYWORD_TOOL_BOX_JS = r"""
const compact = (s) => (s || '').replace(/\s+/g, '');
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = window.getComputedStyle(el);
  return r.width > 40 && r.height > 18
    && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
};
const inHeader = (el) => {
  if (el.closest && el.closest('header, nav, [role="banner"]')) return true;
  const r = el.getBoundingClientRect();
  return el.tagName === 'INPUT' && r.top < 90;
};
const hintOf = (el) => compact(
  (el.getAttribute('placeholder') || '')
  + (el.getAttribute('aria-placeholder') || '')
  + (el.getAttribute('aria-label') || '')
);
const isHint = (el) => hintOf(el).includes('한줄에하나씩');
const boxes = Array.from(document.querySelectorAll('textarea, input')).filter(visible);

const hinted = boxes.find((el) => isHint(el) && !inHeader(el));
if (hinted) return hinted;

const lookup = Array.from(document.querySelectorAll('button, a, [role="button"]')).find((el) => {
  return compact(el.innerText) === '조회하기' && visible(el);
});
if (lookup) {
  let p = lookup.parentElement;
  for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
    const tas = Array.from(p.querySelectorAll('textarea')).filter((el) => visible(el) && !inHeader(el));
    const match = tas.find(isHint) || tas[0];
    if (match) return match;
  }
}

const sections = Array.from(document.querySelectorAll('*')).filter((el) => {
  const t = compact(el.innerText);
  return t.includes('연관키워드조회기준') && t.length < 1200;
});
sections.sort((a, b) => compact(a.innerText).length - compact(b.innerText).length);
for (const section of sections) {
  const tas = Array.from(section.querySelectorAll('textarea')).filter((el) => visible(el) && !inHeader(el));
  if (tas.length) return tas[0];
}

const textareas = boxes.filter((el) => el.tagName === 'TEXTAREA' && !inHeader(el));
textareas.sort((a, b) => (b.offsetHeight * b.offsetWidth) - (a.offsetHeight * a.offsetWidth));
if (textareas.length) return textareas[0];
return null;
"""

IS_HEADER_SEARCH_JS = r"""
const el = arguments[0];
if (!el) return true;
if (el.closest && el.closest('header, nav, [role="banner"]')) return true;
const ph = (el.getAttribute('placeholder') || '').replace(/\s+/g, '');
if (ph.includes('한줄에하나씩')) return false;
if (el.tagName === 'TEXTAREA') return false;
const r = el.getBoundingClientRect();
return el.tagName === 'INPUT' && r.top < 90;
"""

FILL_KEYWORD_TOOL_BOX_JS = r"""
const el = arguments[0];
const val = arguments[1];
el.scrollIntoView({block: 'center'});
el.focus();
el.click();
const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const desc = Object.getOwnPropertyDescriptor(proto, 'value');
const setter = desc && desc.set;
const tracker = el._valueTracker;
if (tracker) tracker.setValue('');
if (setter) setter.call(el, val);
else el.value = val;
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return el.value || '';
"""

CLICK_KEYWORD_LOOKUP_JS = r"""
const compact = (s) => (s || '').replace(/\s+/g, '');
const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const btn = buttons.find((el) => {
  if (compact(el.innerText) !== '조회하기') return false;
  if (el.disabled) return false;
  if (el.getAttribute('aria-disabled') === 'true') return false;
  if ((el.className || '').includes('disabled')) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
});
if (!btn) return false;
btn.click();
return true;
"""

SEARCH_BOX_SELECTORS = (
    "#query",
    "#nx_query",
    'input[name="query"]',
    "input.search_input",
)
AUTOCOMPLETE_ROOTS = (
    "#autoFrame",
    ".autoCompleteContainer",
    ".atcmp_container",
    ".kwd_lst",
    "ul.lst_keyword",
    ".nx_list_auto",
    "._keyword_list",
    ".auto_list",
)
AUTOCOMPLETE_ITEMS = (
    "li a",
    "li",
    ".item._item",
    ".atcmp_keyword",
    "a",
)
INTEGRATED_TAB_TEXTS = {"통합", "통합검색"}
CAFE_IFRAME_SELECTORS = (
    "iframe#cafe_main",
    "iframe[name='cafe_main']",
    "iframe#cafe_content",
)
COMMENT_READY_SELECTORS = (
    ".CommentItem",
    ".text_comment",
)
COMMENT_SHELL_SELECTORS = (
    ".CommentBox",
    ".comment_list",
    ".box-reply",
    "#cmt_list",
)
EXTRA_WAIT_AFTER_COMMENT_BOX = 8.0
COMMENTS_READY_JS = r"""
const nodes = document.querySelectorAll('.CommentItem, .text_comment');
for (const el of nodes) {
  const text = ((el.innerText || el.textContent || '') + '').trim();
  if (!text) continue;
  const compact = text.replace(/\s+/g, '');
  if (compact === '댓글0') continue;
  return true;
}
return false;
"""
COMMENT_SHELL_JS = r"""
return !!(
  document.querySelector('.CommentBox')
  || document.querySelector('.comment_list')
  || document.querySelector('.box-reply')
  || document.querySelector('#cmt_list')
);
"""
ARTICLE_TEXT_SELECTORS = (
    "h3.title",
    "h3.title_text",
    ".title_text",
    ".title_area",
    "#title_area",
    ".article_header",
    ".article_container",
    ".se-main-container",
    ".ContentRenderer",
    ".article_viewer",
    "#tbody",
    ".se-component-content",
    ".text_comment",
    ".CommentItem",
    ".comment_text",
    ".box-reply .comment",
)
COLLECT_CAFE_POST_TEXT_JS = (
    """
const parts = [];
const push = (value) => {
  const text = (value || '').trim();
  if (text) parts.push(text);
};
const sels = [
"""
    + ",\n".join(f"  {selector!r}" for selector in ARTICLE_TEXT_SELECTORS)
    + """
];
for (const sel of sels) {
  document.querySelectorAll(sel).forEach((el) => {
    push(el.innerText || el.textContent || '');
  });
}
return parts.join('\\n');
"""
)
NAVER_LOGIN_COOKIES = {"NID_AUT", "NID_SES"}
ADS_HOME_URL = "https://ads.naver.com/"
VISIBLE_CAFE_LINKS_JS = r"""
const visible = (el) => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const st = window.getComputedStyle(el);
  if (r.width < 2 || r.height < 2) return false;
  if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) return false;
  let p = el;
  while (p && p !== document.documentElement) {
    const ps = window.getComputedStyle(p);
    if (ps.display === 'none' || ps.visibility === 'hidden' || Number(ps.opacity) === 0) return false;
    p = p.parentElement;
  }
  return true;
};
const root = document.querySelector('#main_pack');
if (!root) return [];
return Array.from(root.querySelectorAll('a[href*="cafe.naver.com"]'))
  .filter(visible)
  .map((a) => a.href || '')
  .filter(Boolean);
"""
INTEGRATED_WHERE = {"", "nexearch"}
RESULT_SCROLL_ROUNDS = 2


def is_naver_logged_in_cookies(cookies) -> bool:
    names = {str(item.get("name") or "") for item in cookies or []}
    return bool(names & NAVER_LOGIN_COOKIES)


def is_ads_center_url(url: str) -> bool:
    text = (url or "").lower()
    return "ads.naver.com" in text or "searchad.naver.com" in text


def is_keyword_tool_url(url: str) -> bool:
    text = (url or "").lower()
    if not is_ads_center_url(text):
        return False
    return "keyword-planner" in text or "/sa/tool/keyword" in text


def ads_account_id(url: str) -> str:
    text = url or ""
    match = re.search(r"ad-accounts/(\d+)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"customers/(\d+)", text, flags=re.I)
    return match.group(1) if match else ""


def find_cafe_frame(driver):
    for selector in CAFE_IFRAME_SELECTORS:
        frames = driver.find_elements(By.CSS_SELECTOR, selector)
        if frames:
            return frames[0]
    return None


def comments_ready(driver) -> bool:
    try:
        return bool(driver.execute_script(COMMENTS_READY_JS))
    except Exception:
        for selector in COMMENT_READY_SELECTORS:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    text = (element.text or "").strip()
                except Exception:
                    text = ""
                if text and compact_text(text) != "댓글0":
                    return True
        return False


def comment_shell_present(driver) -> bool:
    try:
        return bool(driver.execute_script(COMMENT_SHELL_JS))
    except Exception:
        return any(
            driver.find_elements(By.CSS_SELECTOR, selector)
            for selector in COMMENT_SHELL_SELECTORS
        )


def document_text(driver) -> str:
    try:
        text = driver.execute_script(
            "return (document.body && (document.body.innerText"
            " || document.body.textContent)) || '';"
        )
        if text:
            return str(text)
    except Exception:
        pass
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def collect_cafe_post_text(driver) -> str:
    """제목·본문·댓글 칸만. 카페 껍데기/인기글은 식별어로 보지 않는다."""
    try:
        text = driver.execute_script(COLLECT_CAFE_POST_TEXT_JS)
        if text:
            return str(text)
    except Exception:
        pass
    return ""


def wait_for_cafe_frame(driver, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = find_cafe_frame(driver)
        if frame is not None:
            return frame
        time.sleep(0.2)
    return find_cafe_frame(driver)


def wait_for_comments(
    driver,
    timeout: float,
    extra_after_box: float = EXTRA_WAIT_AFTER_COMMENT_BOX,
) -> bool:
    deadline = time.time() + timeout
    box_deadline = None
    while True:
        if comments_ready(driver):
            time.sleep(0.2)
            return True
        now = time.time()
        if box_deadline is None and comment_shell_present(driver):
            box_deadline = now + extra_after_box
        if box_deadline is not None and now >= box_deadline:
            break
        if box_deadline is None and now >= deadline:
            break
        time.sleep(0.2)
    return comments_ready(driver)


def collect_frame_tree_text(driver, depth: int = 0) -> str:
    parts = [collect_cafe_post_text(driver)]
    if depth >= 2:
        return "\n".join(part for part in parts if part)
    for frame in driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
        try:
            driver.switch_to.frame(frame)
            parts.append(collect_frame_tree_text(driver, depth + 1))
        except Exception:
            continue
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                driver.switch_to.default_content()
    return "\n".join(part for part in parts if part)


def is_integrated_search_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if "search.naver.com" not in host:
        return False
    where = (parse_qs(parsed.query).get("where") or [""])[0].casefold()
    return where in INTEGRATED_WHERE


def read_opened_cafe_article(driver, timeout: float = 15) -> str:
    """Read title/body/comments of an already opened cafe post. No extra clicks."""
    frame = wait_for_cafe_frame(driver, timeout)
    if frame is None:
        wait_for_comments(driver, timeout)
        return collect_cafe_post_text(driver)
    try:
        driver.switch_to.frame(frame)
        wait_for_comments(driver, timeout)
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        time.sleep(0.25)
        return collect_frame_tree_text(driver)
    finally:
        driver.switch_to.default_content()


class SeleniumNaverSearch:
    def __init__(self, browser, logger: logging.Logger, timeout: int = 15):
        self.browser = browser
        self.logger = logger
        self.timeout = timeout
        self._naver_handle: str | None = None
        self._ads_handle: str | None = None
        self._sheet_handle: str | None = None
        self._volume_unavailable = False
        self._last_visible_cafe_urls: list[str] = []

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
                self._ads_handle = None
                self._sheet_handle = None
                self._volume_unavailable = False
                self._last_visible_cafe_urls = []
        self.browser.start()

    def prepare_login(self, wait_seconds: float = 300, sheet_url: str = "") -> None:
        try:
            driver = self._driver()
            if not self._has_naver_tab(driver):
                driver.get("https://www.naver.com/")
                self._naver_handle = driver.current_window_handle
            if self.is_logged_in():
                self.logger.info(
                    "네이버 로그인이 이미 되어 있습니다. 프로그램을 끄기 전까지 유지됩니다"
                )
                self._open_keyword_tool_tab()
                return
            self.logger.info(
                "네이버에 한 번만 로그인하세요. 프로그램을 끄기 전까지 다시 묻지 않습니다"
            )
            deadline = time.time() + wait_seconds
            next_notice = time.time() + 20
            while time.time() < deadline:
                if self.is_logged_in():
                    self.logger.info("네이버 로그인을 확인했습니다. 이 크롬 창은 닫지 마세요")
                    self._open_keyword_tool_tab()
                    return
                time.sleep(1.2)
                if time.time() >= next_notice:
                    self.logger.info("크롬 창에서 네이버 로그인을 기다리는 중입니다")
                    next_notice = time.time() + 20
            self.logger.warning(
                "아직 로그인이 확인되지 않았습니다. 크롬에서 로그인하면 그때부터 유지됩니다"
            )
            self._open_keyword_tool_tab()
        finally:
            if sheet_url:
                try:
                    self._open_sheet_tab(sheet_url)
                except Exception as exc:
                    self.logger.warning("구글 시트 탭을 열지 못했습니다: %s", exc)

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
        self._last_visible_cafe_urls = []
        self._focus_naver_tab(driver)
        box = self._find_search_box(driver)
        box.click()
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
        box.send_keys(query)
        time.sleep(0.55)
        if not self._click_spacing_autocomplete(driver, query):
            box.send_keys(Keys.ENTER)
        if not self._wait_for_integrated_results(driver, query):
            self.logger.warning(
                "통검 결과칸을 확인하지 못해 이번 키워드는 우리 글로 보지 않습니다"
            )
            return ""
        self._last_visible_cafe_urls = self._collect_visible_cafe_urls(driver)
        return driver.page_source

    def visible_cafe_article_urls(self) -> list[str]:
        return list(self._last_visible_cafe_urls or [])

    def _collect_visible_cafe_urls(self, driver) -> list[str]:
        try:
            hrefs = driver.execute_script(VISIBLE_CAFE_LINKS_JS) or []
        except Exception as exc:
            self.logger.warning("통검에서 보이는 카페 글을 가리지 못했습니다: %s", exc)
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            text = str(href or "").strip()
            if not text or not is_cafe_article_url(text):
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            urls.append(text)
        return urls

    def lookup_search_volume(self, keyword: str) -> int | None:
        if self._volume_unavailable:
            return None
        driver = self._driver()
        naver = None
        try:
            naver = driver.current_window_handle
        except Exception:
            naver = self._naver_handle
        try:
            if not self._focus_keyword_tool(driver):
                self._volume_unavailable = True
                self.logger.warning(
                    "광고주센터 키워드 도구로 들어가면 검색량을 채울 수 있습니다. 노출상태와 카페는 그대로 반영합니다"
                )
                return None
            volume = self._scrape_keyword_tool(driver, keyword)
            if volume is not None:
                return volume
            payload = self._fetch_keywordstool_json(driver, keyword)
            volume = keywordstool_volume(payload or {}, keyword)
            if volume is not None:
                return volume
            self.logger.warning("검색량을 찾지 못했습니다: %s", keyword)
            return None
        except Exception as exc:
            self.logger.error("검색량 조회 실패 (%s): %s", keyword, exc)
            return None
        finally:
            if naver:
                try:
                    driver.switch_to.window(naver)
                except Exception:
                    self._focus_naver_tab(driver)

    def _open_sheet_tab(self, sheet_url: str) -> None:
        driver = self._driver()
        try:
            handle = self._existing_sheet_handle(driver)
            if handle:
                driver.switch_to.window(handle)
            else:
                driver.switch_to.new_window("tab")
                driver.get(sheet_url)
                self._sheet_handle = driver.current_window_handle
            self.browser.google_handle = self._sheet_handle or driver.current_window_handle
            self.logger.info(
                "구글 시트 탭을 열었습니다. 링크가 막혀 있으면 이 탭에서 구글 로그인하세요"
            )
        finally:
            self._focus_naver_tab(driver)

    def _existing_sheet_handle(self, driver) -> str | None:
        handles = list(driver.window_handles)
        if self._sheet_handle in handles:
            return self._sheet_handle
        for handle in handles:
            try:
                driver.switch_to.window(handle)
            except Exception:
                continue
            url = (driver.current_url or "").lower()
            if "docs.google.com/spreadsheets" in url:
                self._sheet_handle = handle
                return handle
        return None

    def _open_keyword_tool_tab(self) -> None:
        driver = self._driver()
        try:
            if not self._existing_ads_handle(driver):
                driver.switch_to.new_window("tab")
                driver.get(ADS_HOME_URL)
                time.sleep(1.4)
                self._ads_handle = driver.current_window_handle
            self.logger.info(
                "검색량 반영을 위해 광고주센터 창을 열었습니다. 이 탭에서 로그인되어 있으면 키워드 도구로 들어갑니다"
            )
            if self._ads_login_required(driver.current_url or ""):
                self.logger.warning(
                    "광고주센터 로그인이 필요합니다. 이 탭에서 로그인한 뒤 검사를 시작하면 검색량을 채웁니다"
                )
                return
            if self._enter_keyword_tool(driver):
                self.logger.info("키워드 도구 화면입니다. 검사할 때 여기서 검색량을 읽습니다")
            else:
                self.logger.warning(
                    "광고주센터 메인은 열렸습니다. 검사 중에 왼쪽 메뉴 도구 → 키워드 도구로 다시 들어갑니다"
                )
        except Exception as exc:
            self.logger.warning("광고주센터 창을 열지 못했습니다: %s", exc)
        finally:
            self._focus_naver_tab(driver)

    def _existing_ads_handle(self, driver) -> str | None:
        handles = list(driver.window_handles)
        if self._ads_handle in handles:
            try:
                driver.switch_to.window(self._ads_handle)
                if is_ads_center_url(driver.current_url or ""):
                    return self._ads_handle
            except Exception:
                pass
        for handle in handles:
            try:
                driver.switch_to.window(handle)
            except Exception:
                continue
            if is_ads_center_url(driver.current_url or ""):
                self._ads_handle = handle
                return handle
        return None

    def _ads_login_required(self, url: str) -> bool:
        text = (url or "").lower()
        if "nid.naver.com" in text or "nidlogin" in text:
            return True
        if "/login" in text:
            return True
        return False

    def _focus_keyword_tool(self, driver) -> bool:
        handle = self._existing_ads_handle(driver)
        if not handle:
            try:
                driver.switch_to.new_window("tab")
                driver.get(ADS_HOME_URL)
                time.sleep(1.4)
                self._ads_handle = driver.current_window_handle
            except Exception:
                return False
        else:
            driver.switch_to.window(handle)
        if self._ads_login_required(driver.current_url or ""):
            return False
        return self._enter_keyword_tool(driver)

    def _enter_keyword_tool(self, driver) -> bool:
        account = ads_account_id(driver.current_url or "")
        if self._is_ads_not_found(driver):
            self.logger.info("없는 주소라 광고주센터 메인으로 돌아갑니다")
            self._open_ads_dashboard(driver, account)
        if self._is_keyword_tool_page(driver):
            return True
        for path in self._keyword_tool_urls(account):
            try:
                driver.get(path)
                time.sleep(1.2)
            except Exception:
                continue
            if self._ads_login_required(driver.current_url or ""):
                return False
            if self._is_ads_not_found(driver):
                continue
            if self._is_keyword_tool_page(driver):
                return True
        if self._click_ads_menu(driver, "도구"):
            time.sleep(0.7)
        if self._click_keyword_tool_entry(driver):
            time.sleep(1.2)
            if self._is_keyword_tool_page(driver):
                return True
        return self._is_keyword_tool_page(driver)

    def _open_ads_dashboard(self, driver, account: str) -> None:
        if account:
            driver.get(f"https://ads.naver.com/manage/ad-accounts/{account}/dashboard")
        else:
            driver.get(ADS_HOME_URL)
        time.sleep(1.2)

    def _keyword_tool_urls(self, account: str) -> list[str]:
        urls: list[str] = []
        if account:
            urls.append(
                f"https://ads.naver.com/manage/ad-accounts/{account}/sa/tool/keyword-planner"
            )
        urls.extend(
            [
                "https://manage.searchad.naver.com/tool/keyword-planner",
                "https://searchad.naver.com/ncc/tool/keyword-planner",
            ]
        )
        return urls

    def _is_ads_not_found(self, driver) -> bool:
        try:
            heading = compact_text(self._visible_text(driver)[:1500])
        except Exception:
            heading = ""
        return "페이지를찾을수없습니다" in heading

    def _is_keyword_tool_page(self, driver) -> bool:
        if self._is_ads_not_found(driver):
            return False
        if is_keyword_tool_url(driver.current_url or ""):
            return True
        try:
            heading = compact_text(self._visible_text(driver)[:2500])
        except Exception:
            heading = ""
        if "키워드도구" not in heading:
            return False
        for selector in ("textarea", 'input[placeholder*="키워드"]'):
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if element.is_displayed():
                        return True
                except Exception:
                    continue
        return False

    def _click_ads_menu(self, driver, label: str) -> bool:
        want = compact_text(label)
        candidates = []
        for element in driver.find_elements(
            By.CSS_SELECTOR,
            "a, button, [role='button'], [role='menuitem'], nav span, aside span, li, p",
        ):
            try:
                if not element.is_displayed():
                    continue
                text = compact_text(element.text)
                if text == want:
                    candidates.append(element)
            except Exception:
                continue
        for element in candidates:
            try:
                driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                continue
        return False

    def _click_keyword_tool_entry(self, driver) -> bool:
        if self._click_ads_menu(driver, "키워드 도구"):
            return True
        for element in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
            try:
                href = (element.get_attribute("href") or "").lower()
                text = compact_text(element.text)
                if "키워드도구" in text or (
                    "keyword" in href and ("tool" in href or "planner" in href)
                ):
                    if element.is_displayed():
                        driver.execute_script("arguments[0].click();", element)
                        return True
            except Exception:
                continue
        return False

    def _fetch_keywordstool_json(self, driver, keyword: str) -> dict | None:
        script = """
        const keyword = arguments[0];
        const done = arguments[1];
        const origin = location.origin;
        const path = location.pathname || '';
        const customerMatch = path.match(/customers\\/(\\d+)/);
        const accountMatch = path.match(/ad-accounts\\/(\\d+)/);
        const customerId = customerMatch ? customerMatch[1] : '';
        const accountId = accountMatch ? accountMatch[1] : '';
        const token = localStorage.getItem('nccToken')
          || localStorage.getItem('token')
          || sessionStorage.getItem('nccToken')
          || '';
        const qs = 'hintKeywords=' + encodeURIComponent(keyword) + '&showDetail=1';
        const urls = [
          origin + '/keywordstool?' + qs,
          origin + '/ncc/keywordstool?' + qs
        ];
        if (customerId) {
          urls.push(origin + '/customers/' + customerId + '/keywordstool?' + qs);
        }
        if (accountId) {
          urls.push(origin + '/manage/ad-accounts/' + accountId + '/keywordstool?' + qs);
        }
        const looksUseful = (data) => {
          if (!data || typeof data !== 'object') return false;
          if (data.keywordList || data.relKeywordList) return true;
          if (data.data && (Array.isArray(data.data) || data.data.keywordList || data.data.relKeywordList)) return true;
          return false;
        };
        (async () => {
          for (const url of urls) {
            try {
              const headers = {Accept: 'application/json'};
              if (token) {
                headers.Authorization = token.indexOf('Bearer') === 0 ? token : ('Bearer ' + token);
              }
              const resp = await fetch(url, {credentials: 'include', headers});
              if (!resp.ok) continue;
              const data = await resp.json();
              if (looksUseful(data)) {
                done(data);
                return;
              }
            } catch (e) {}
          }
          done(null);
        })();
        """
        try:
            driver.set_script_timeout(20)
            payload = driver.execute_async_script(script, keyword)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _scrape_keyword_tool(self, driver, keyword: str) -> int | None:
        query = keyword_tool_query(keyword)
        if not query:
            return None
        self._check_keyword_hint_box(driver)
        box = self._find_keyword_tool_box(driver)
        if box is None or self._is_ads_header_search(driver, box):
            self.logger.warning("키워드 도구 입력칸을 찾지 못했습니다")
            return None
        self._fill_keyword_tool_box(driver, box, query)
        if not self._click_keyword_lookup(driver, retries=8):
            if not self._is_ads_header_search(driver, box):
                box.send_keys(Keys.ENTER)
        time.sleep(2.4)
        volume = self._volume_from_keyword_table(driver, query)
        if volume is not None:
            self.logger.info("키워드 도구 검색량(PC+모바일): %s = %s", query, volume)
        return volume

    def _check_keyword_hint_box(self, driver) -> None:
        script = """
        const wraps = Array.from(document.querySelectorAll('label, .ant-checkbox-wrapper'));
        for (const wrap of wraps) {
          const text = (wrap.innerText || '').replace(/\\s+/g, '');
          if (text !== '키워드') continue;
          const box = wrap.querySelector('input[type="checkbox"]');
          if (box && !box.checked) {
            wrap.click();
            return true;
          }
          return false;
        }
        return false;
        """
        try:
            driver.execute_script(script)
        except Exception:
            return

    def _find_keyword_tool_box(self, driver):
        try:
            box = driver.execute_script(FIND_KEYWORD_TOOL_BOX_JS)
        except Exception:
            box = None
        if box is not None and not self._is_ads_header_search(driver, box):
            return box
        for xpath in (
            "//textarea[contains(@placeholder, '한줄에 하나씩')]",
            "//textarea[contains(@placeholder, '한 줄에 하나씩')]",
            "//*[contains(normalize-space(), '연관키워드 조회 기준')]//textarea",
        ):
            found = driver.find_elements(By.XPATH, xpath)
            for element in found:
                try:
                    if element.is_displayed() and not self._is_ads_header_search(driver, element):
                        return element
                except Exception:
                    continue
        return None

    def _is_ads_header_search(self, driver, box) -> bool:
        try:
            return bool(driver.execute_script(IS_HEADER_SEARCH_JS, box))
        except Exception:
            return False

    def _fill_keyword_tool_box(self, driver, box, query: str) -> None:
        filled = ""
        try:
            filled = str(driver.execute_script(FILL_KEYWORD_TOOL_BOX_JS, box, query) or "")
        except Exception:
            filled = ""
        if compact_text(filled) == compact_text(query):
            return
        try:
            box.click()
            box.send_keys(Keys.CONTROL, "a")
            box.send_keys(Keys.BACKSPACE)
            box.send_keys(query)
        except Exception:
            return

    def _click_keyword_lookup(self, driver, retries: int = 1) -> bool:
        for _ in range(max(1, retries)):
            try:
                if driver.execute_script(CLICK_KEYWORD_LOOKUP_JS):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _volume_from_keyword_table(self, driver, keyword: str) -> int | None:
        rows = driver.find_elements(
            By.CSS_SELECTOR, "tr, [role='row'], .ant-table-row"
        )
        for row in rows:
            cells = [
                cell.text.strip()
                for cell in row.find_elements(
                    By.CSS_SELECTOR, "th,td,[role='cell'],[role='gridcell'],.ant-table-cell"
                )
            ]
            if not cells:
                cells = [part.strip() for part in (row.text or "").split("\n") if part.strip()]
            volume = volume_from_result_cells(cells, keyword)
            if volume is not None:
                return volume
        return None

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
            return read_opened_cafe_article(driver, self.timeout)
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

    def _click_spacing_autocomplete(self, driver, query: str) -> bool:
        for root_selector in AUTOCOMPLETE_ROOTS:
            roots = driver.find_elements(By.CSS_SELECTOR, root_selector)
            for root in roots:
                try:
                    if not root.is_displayed():
                        continue
                except Exception:
                    continue
                for item_selector in AUTOCOMPLETE_ITEMS:
                    for element in root.find_elements(By.CSS_SELECTOR, item_selector):
                        try:
                            if not element.is_displayed():
                                continue
                            suggestion = (element.text or "").strip()
                            if not suggestion:
                                continue
                            if compact_text(suggestion) != compact_text(query):
                                self.logger.info(
                                    "자동완성 첫 항목이 다른 검색어라 입력한 키워드 그대로 검색합니다: %s",
                                    suggestion.split("\n")[0][:40],
                                )
                                return False
                            driver.execute_script("arguments[0].click();", element)
                            self.logger.info("자동완성에서 띄어쓰기만 다른 항목을 선택했습니다")
                            return True
                        except Exception:
                            continue
        self.logger.info("자동완성이 없어 입력한 키워드 그대로 검색합니다")
        return False

    def _wait_for_integrated_results(self, driver, query: str) -> bool:
        try:
            WebDriverWait(driver, self.timeout).until(
                lambda item: item.find_elements(By.CSS_SELECTOR, "#lnb, #main_pack")
            )
        except Exception:
            return False
        time.sleep(0.35)
        self._stay_on_integrated_tab(driver, query)
        actual = self._actual_query(driver)
        if actual and not same_search_query(query, actual):
            self.logger.info(
                "검색어가 달라져 다시 검색합니다: %s → %s", actual, query
            )
            driver.get(naver_search_url(query))
            time.sleep(0.35)
            self._stay_on_integrated_tab(driver, query)
        if not is_integrated_search_url(driver.current_url or ""):
            return False
        try:
            WebDriverWait(driver, self.timeout).until(
                lambda item: item.find_elements(By.CSS_SELECTOR, "#main_pack")
            )
        except Exception:
            return False
        if not driver.find_elements(By.CSS_SELECTOR, "#main_pack"):
            return False
        self._scroll_result_column(driver)
        return True

    def _actual_query(self, driver) -> str:
        parsed = urlparse(driver.current_url or "")
        values = parse_qs(parsed.query).get("query") or []
        if values:
            return unquote_plus(values[0])
        try:
            box = self._find_search_box(driver)
            return (box.get_attribute("value") or "").strip()
        except Exception:
            return ""

    def _stay_on_integrated_tab(self, driver, query: str) -> None:
        if is_integrated_search_url(driver.current_url or ""):
            return
        self.logger.info("통합검색이 아니라 다른 탭이라 통합검색으로 돌아갑니다")
        for tab in driver.find_elements(
            By.CSS_SELECTOR, "#lnb a, .api_lnb_menu a, .lnb_tab a, a.tab"
        ):
            label = (tab.text or "").replace(" ", "")
            href = (tab.get_attribute("href") or "").lower()
            if label in INTEGRATED_TAB_TEXTS or "where=nexearch" in href:
                try:
                    tab.click()
                    time.sleep(0.4)
                except Exception:
                    continue
                break
        if is_integrated_search_url(driver.current_url or ""):
            return
        driver.get(naver_search_url(query))
        time.sleep(0.4)

    def _scroll_result_column(self, driver, rounds: int = RESULT_SCROLL_ROUNDS) -> None:
        last_height = 0
        for _ in range(max(1, rounds)):
            try:
                height = driver.execute_script(
                    """
                    const root = document.querySelector('#main_pack');
                    if (!root) return 0;
                    const bottom = root.getBoundingClientRect().bottom + window.scrollY;
                    window.scrollTo(0, bottom);
                    return document.body.scrollHeight;
                    """
                )
            except Exception:
                return
            time.sleep(0.28)
            if height == last_height:
                break
            last_height = height

    def _visible_text(self, driver) -> str:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""
