from __future__ import annotations

import json
import time
from typing import Any, Callable

from .browser import V2RBrowser
from .nickname_exclude import (
    DEFAULT_CAFE_URL,
    NAVER_LOGIN_URL,
    SEARCH_SCOPES,
    WRITER_SCOPE,
    CafeTarget,
    ExcludeSyncResult,
    NicknameExcludeError,
    SearchScope,
    article_ids_from_html,
    article_url,
    article_ids_from_json,
    build_sync_result,
    cafe_id_from_page,
    cafe_search_url,
    cafe_search_url_modern,
    cookies_show_naver_login,
    is_search_response,
    last_page_from_html,
    nicknames_from_search_html,
    nicknames_from_search_payload,
    search_articles_from_payload,
    page_is_missing,
    page_requires_naver_login,
    parse_cafe_address,
    require_cafe_id,
    require_keywords,
    search_api_urls,
    search_page_info,
    should_stop_search,
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
        self.logger.info(
            "카페 검색창에서 '%s'를 글 + 댓글, 댓글내용으로 찾습니다. 모든 페이지를 확인합니다",
            keyword,
        )
        self._require_cafe_login()
        found: list[str] = []
        for scope in SEARCH_SCOPES:
            nicks, _ids = self._search_hits(keyword, scope, should_stop)
            found.extend(nicks)
        nicknames = split_nicknames("\n".join(found))
        self.logger.info(
            "'%s' 글 + 댓글과 댓글내용을 합쳐 닉네임 %s개를 모았습니다",
            keyword,
            len(nicknames),
        )
        return nicknames

    def _search_one_keyword_scope(
        self,
        keyword: str,
        scope: SearchScope,
        should_stop: Callable[[], bool] | None,
    ) -> list[str]:
        nicks, _ids = self._search_hits(keyword, scope, should_stop)
        return nicks

    def collect_author_article_links(
        self,
        nicknames: list[str],
        should_stop: Callable[[], bool] | None = None,
    ) -> list[tuple[str, list[str]]]:
        require_cafe_id(self.cafe)
        self._require_cafe_login()
        rows: list[tuple[str, list[str]]] = []
        for nickname in split_nicknames("\n".join(nicknames)):
            if should_stop and should_stop():
                raise NicknameExcludeError("확인을 중지했습니다")
            self.logger.info(
                "'%s'를 글작성자로 검색해 글 링크를 모읍니다",
                nickname,
            )
            _nicks, article_ids = self._search_hits(
                nickname,
                WRITER_SCOPE,
                should_stop,
            )
            urls: list[str] = []
            seen: set[str] = set()
            for article_id in article_ids:
                url = article_url(self.cafe, article_id)
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
            rows.append((nickname, urls))
            self.logger.info(
                "'%s' 글작성자 검색에서 글 링크 %s개를 모았습니다",
                nickname,
                len(urls),
            )
        return rows

    def _search_hits(
        self,
        keyword: str,
        scope: SearchScope,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[list[str], list[str]]:
        self.logger.info(
            "카페 검색창에서 '%s'를 %s로 찾습니다. 모든 페이지를 확인합니다",
            keyword,
            scope.label,
        )
        found: list[str] = []
        seen_articles: set[str] = set()
        ordered_ids: list[str] = []
        page = 1
        last_page: int | None = None
        has_more = False
        empty_streak = 0
        while True:
            if should_stop and should_stop():
                raise NicknameExcludeError("확인을 중지했습니다")
            self._open_search_page(keyword, page, scope)
            page_nicks, article_ids, page_last, page_total, page_more = self._search_page(
                keyword,
                page,
                scope,
            )
            if page_last:
                last_page = page_last
            if page_more:
                has_more = True
            new_ids = [item for item in article_ids if item not in seen_articles]
            for item in new_ids:
                seen_articles.add(item)
                ordered_ids.append(item)
            if new_ids:
                found.extend(page_nicks)
                empty_streak = 0
                extra = f", 전체 {last_page}페이지" if last_page else ""
                if page_total:
                    extra += f", 글 {page_total}개"
                if scope.ta == "WRITER":
                    self.logger.info(
                        "'%s' %s %s페이지에서 글 %s개%s",
                        keyword,
                        scope.label,
                        page,
                        len(new_ids),
                        extra,
                    )
                else:
                    self.logger.info(
                        "'%s' %s %s페이지에서 닉네임 %s개%s",
                        keyword,
                        scope.label,
                        page,
                        len(split_nicknames("\n".join(page_nicks))) if page_nicks else 0,
                        extra,
                    )
            elif page_nicks and not article_ids and page == 1:
                found.extend(page_nicks)
                empty_streak = 0
                self.logger.info(
                    "'%s' %s %s페이지에서 닉네임 %s개",
                    keyword,
                    scope.label,
                    page,
                    len(split_nicknames("\n".join(page_nicks))),
                )
            else:
                empty_streak += 1
                self.logger.info(
                    "'%s' %s %s페이지에서 검색 결과가 더 없습니다",
                    keyword,
                    scope.label,
                    page,
                )
            if should_stop_search(page, last_page, has_more, empty_streak, len(new_ids)):
                if last_page is not None and page >= last_page:
                    self.logger.info(
                        "'%s' %s 검색 마지막 페이지입니다. %s페이지에서 멈춥니다",
                        keyword,
                        scope.label,
                        page,
                    )
                elif not new_ids:
                    self.logger.info(
                        "'%s' %s 검색 결과가 더 없어 %s페이지에서 멈춥니다",
                        keyword,
                        scope.label,
                        page,
                    )
                break
            has_more = False
            page += 1
            time.sleep(0.35)
        nicknames = split_nicknames("\n".join(found))
        self.logger.info(
            "'%s' %s 검색을 %s페이지까지 확인했고 닉네임 %s개, 글 %s개를 모았습니다",
            keyword,
            scope.label,
            page,
            len(nicknames),
            len(ordered_ids),
        )
        return nicknames, ordered_ids

    def _open_search_page(
        self,
        keyword: str,
        page: int,
        scope: SearchScope,
    ) -> None:
        url = cafe_search_url(keyword, self.cafe, page=page, scope=scope)
        self.browser._navigate(url, self.cafe_handle)
        time.sleep(0.8)
        assert self.browser.driver
        current = (self.browser.driver.current_url or "").casefold()
        html = self._page_html()
        if (
            "articlewrite" in current
            or "/write" in current
            or page_is_missing(html, current)
        ):
            self.browser._navigate(
                cafe_search_url_modern(keyword, self.cafe, page=page, scope=scope),
                self.cafe_handle,
            )
            time.sleep(0.8)

    def _search_page(
        self,
        keyword: str,
        page: int,
        scope: SearchScope,
    ) -> tuple[list[str], list[str], int | None, int | None, bool]:
        last_page: int | None = None
        total: int | None = None
        has_more = False
        for url in search_api_urls(keyword, page, self.cafe, scope=scope):
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
                info_last, info_total, info_more = search_page_info(payload)
                if info_last:
                    last_page = info_last
                if info_total is not None:
                    total = info_total
                if info_more:
                    has_more = True
                nicks = nicknames_from_search_payload(payload)
                articles = search_articles_from_payload(payload)
                ids = article_ids_from_json(
                    articles if articles or is_search_response(payload) else payload
                )
                if is_search_response(payload):
                    html_last = last_page_from_html(self._page_html())
                    if html_last and not last_page:
                        last_page = html_last
                    return nicks, ids, last_page, total, has_more
                if nicks or ids:
                    return nicks, ids, last_page, total, has_more
            html_nicks = nicknames_from_search_html(text)
            html_ids = article_ids_from_html(text)
            if html_nicks or html_ids:
                html_last = last_page_from_html(text)
                return html_nicks, html_ids, html_last or last_page, total, has_more
        page_html = self._page_html()
        html_last = last_page_from_html(page_html)
        return (
            nicknames_from_search_html(page_html),
            article_ids_from_html(page_html),
            html_last or last_page,
            total,
            has_more,
        )

    def sync(
        self,
        keywords_text: str,
        should_stop: Callable[[], bool] | None = None,
        cafe_url: str = "",
        collect_author_links: bool = False,
    ) -> ExcludeSyncResult:
        if cafe_url:
            self.cafe = parse_cafe_address(cafe_url)
        keywords = require_keywords(keywords_text)
        found = self.collect_cafe_nicknames(keywords, should_stop=should_stop)
        plan = build_sync_result(keywords, found, [])
        plan.saved = found
        self.logger.info("닉네임 %s개를 모두 모았습니다", len(found))
        if collect_author_links and found:
            plan.author_links = self.collect_author_article_links(
                found,
                should_stop=should_stop,
            )
            total = sum(len(urls) for _nick, urls in plan.author_links)
            self.logger.info("닉네임 %s명의 글 링크 %s개를 모았습니다", len(found), total)
        return plan


def open_login_windows(
    browser: V2RBrowser,
    cafe: CafeTarget | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    NicknameExcludeSession(browser, cafe).open_login_windows(should_stop=should_stop)
