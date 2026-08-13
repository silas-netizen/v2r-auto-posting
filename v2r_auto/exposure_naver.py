from __future__ import annotations

import logging
import time

from urllib.parse import parse_qs, unquote_plus, urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .exposure import compact_text, keywordstool_volume, naver_search_url, parse_qc_count, same_search_query, strip_parenthetical

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
        self._ads_handle: str | None = None
        self._volume_unavailable = False

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
                self._volume_unavailable = False
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
        if not self._click_spacing_autocomplete(driver, query):
            box.send_keys(Keys.ENTER)
        self._wait_for_integrated_results(driver, query)
        return driver.page_source

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
                    "검색광고 키워드 도구에 로그인하면 검색량을 채울 수 있습니다. 노출상태와 카페는 그대로 반영합니다"
                )
                return None
            payload = self._fetch_keywordstool_json(driver, keyword)
            volume = keywordstool_volume(payload or {}, keyword)
            if volume is not None:
                return volume
            volume = self._scrape_keyword_tool(driver, keyword)
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

    def _open_keyword_tool_tab(self) -> None:
        driver = self._driver()
        try:
            if self._existing_ads_handle(driver):
                return
            driver.switch_to.new_window("tab")
            driver.get("https://manage.searchad.naver.com/")
            self._ads_handle = driver.current_window_handle
            self.logger.info(
                "검색량 반영을 위해 검색광고 창을 열었습니다. 네이버 검색 로그인과 별도로, 이 탭에서 검색광고에도 로그인하세요"
            )
        except Exception as exc:
            self.logger.warning("검색광고 창을 열지 못했습니다: %s", exc)
        finally:
            self._focus_naver_tab(driver)

    def _existing_ads_handle(self, driver) -> str | None:
        handles = list(driver.window_handles)
        if self._ads_handle in handles:
            return self._ads_handle
        for handle in handles:
            try:
                driver.switch_to.window(handle)
            except Exception:
                continue
            url = (driver.current_url or "").lower()
            if "searchad.naver.com" in url:
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
                driver.get("https://manage.searchad.naver.com/")
                time.sleep(1.2)
                self._ads_handle = driver.current_window_handle
                handle = self._ads_handle
            except Exception:
                return False
        else:
            driver.switch_to.window(handle)
        url = (driver.current_url or "").lower()
        if self._ads_login_required(url):
            return False
        if "keyword" not in url and "planner" not in url:
            for path in (
                "https://manage.searchad.naver.com/tool/keyword-planner",
                "https://searchad.naver.com/ncc/tool/keyword-planner",
            ):
                try:
                    driver.get(path)
                    time.sleep(1.2)
                    url = (driver.current_url or "").lower()
                    if self._ads_login_required(url):
                        return False
                    if "keyword" in url or "planner" in url or "searchad.naver.com" in url:
                        break
                except Exception:
                    continue
        return (
            "searchad.naver.com" in (driver.current_url or "").lower()
            and not self._ads_login_required(driver.current_url or "")
        )

    def _fetch_keywordstool_json(self, driver, keyword: str) -> dict | None:
        script = """
        const keyword = arguments[0];
        const done = arguments[1];
        const origin = location.origin;
        const match = location.pathname.match(/customers\\/(\\d+)/);
        const customerId = match ? match[1] : '';
        const token = localStorage.getItem('nccToken')
          || localStorage.getItem('token')
          || sessionStorage.getItem('nccToken')
          || '';
        const urls = [
          origin + '/keywordstool?hintKeywords=' + encodeURIComponent(keyword) + '&showDetail=1',
          origin + '/ncc/keywordstool?hintKeywords=' + encodeURIComponent(keyword) + '&showDetail=1'
        ];
        if (customerId) {
          urls.push(origin + '/customers/' + customerId + '/keywordstool?hintKeywords=' + encodeURIComponent(keyword) + '&showDetail=1');
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
        box = None
        for selector in (
            "textarea",
            'input[placeholder*="키워드"]',
            'input[type="text"]',
            '[contenteditable="true"]',
        ):
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if element.is_displayed():
                        box = element
                        break
                except Exception:
                    continue
            if box:
                break
        if box is None:
            return None
        box.click()
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
        box.send_keys(keyword)
        clicked = False
        for element in driver.find_elements(By.CSS_SELECTOR, "button, a.btn, input[type='button']"):
            label = (element.text or element.get_attribute("value") or "").replace(" ", "")
            if "조회" in label or "검색" in label:
                try:
                    if element.is_displayed():
                        element.click()
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            box.send_keys(Keys.ENTER)
        time.sleep(1.8)
        return self._volume_from_keyword_table(driver, keyword)

    def _volume_from_keyword_table(self, driver, keyword: str) -> int | None:
        want = compact_text(keyword)
        rows = driver.find_elements(By.CSS_SELECTOR, "tr, [role='row']")
        for row in rows:
            cells = [
                cell.text.strip()
                for cell in row.find_elements(By.CSS_SELECTOR, "th,td,[role='cell'],[role='gridcell']")
            ]
            if not cells:
                cells = [part.strip() for part in (row.text or "").split("\n") if part.strip()]
            if len(cells) < 2:
                continue
            names = [compact_text(cell.split("\n")[0]) for cell in cells]
            if want not in names:
                continue
            start = names.index(want) + 1
            numbers = [
                parse_qc_count(cell.split("\n")[-1]) for cell in cells[start:]
            ]
            numbers = [item for item in numbers if item is not None]
            if numbers:
                return sum(numbers[:2])
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

    def _wait_for_integrated_results(self, driver, query: str) -> None:
        WebDriverWait(driver, self.timeout).until(
            lambda item: item.find_elements(
                By.CSS_SELECTOR, "#main_pack, #content, #lnb"
            )
        )
        time.sleep(0.35)
        self._stay_on_integrated_tab(driver)
        actual = self._actual_query(driver)
        if actual and not same_search_query(query, actual):
            self.logger.info(
                "검색어가 달라져 다시 검색합니다: %s → %s", actual, query
            )
            driver.get(naver_search_url(query))
            WebDriverWait(driver, self.timeout).until(
                lambda item: item.find_elements(
                    By.CSS_SELECTOR, "#main_pack, #content, #lnb"
                )
            )
            time.sleep(0.35)
            self._stay_on_integrated_tab(driver)
        self._scroll_to_end(driver)
        time.sleep(0.45)
        self._scroll_to_end(driver, rounds=4)

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
