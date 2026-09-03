from __future__ import annotations

import time
from typing import Any, Callable, Iterator
from urllib.parse import quote

from .browser import V2RBrowser
from .cafe_posts import (
    NAVER_LOGIN_URL,
    CafeBoardTarget,
    CafeComment,
    CafePostError,
    CafePostRow,
    apply_intro_cafe_name,
    article_ids_from_html,
    article_ids_from_payload,
    article_url,
    cafe_id_from_text,
    cafe_name_from_info_payload,
    cafe_name_from_intro_html,
    cafe_name_from_intro_text,
    clean_intro_cafe_name,
    comments_from_payload,
    cookies_show_naver_login,
    last_page_from_payload,
    loads_maybe_json,
    post_from_payload,
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
SCRAPE_POST_JS = r"""
const text = (el) => (el && (el.innerText || el.textContent) || '').trim();
const pick = (sels) => {
  for (const sel of sels) {
    const el = document.querySelector(sel);
    if (el && text(el)) return text(el);
  }
  return '';
};
const comments = [];
document.querySelectorAll('.CommentItem, .comment_list li, li.CommentItem').forEach((el) => {
  const nick = text(el.querySelector('.comment_nickname, .nickname, .nick, .nick_area'));
  const body = text(el.querySelector('.text_comment, .comment_text, .comment, .text_content'));
  if (nick || body) comments.push({nickname: nick, text: body});
});
return {
  cafe: pick(['.cafe_name', '.cafe-name', 'h1.d-none']),
  board: pick(['.link_board', '.board_name', '.ArticleHead .board']),
  author: pick(['.nick_box .nickname', '.nickname', '.nick', '.writer .nick']),
  title: pick(['h3.title_text', 'h3.title', '.title_text', '.title_area']),
  body: pick(['.se-main-container', '.article_viewer', '.ContentRenderer', '#tbody', '.article_container']),
  comments,
};
"""
MAX_LIST_PAGES = 400
ARTICLE_LIST_TEMPLATES = (
    "https://apis.naver.com/cafe-web/cafe-boardlist-api/v1/cafes/{cafe_id}/menus/{menu_id}/articles?page={page}&pageSize=50&sortBy=TIME&viewType=L",
    "https://apis.naver.com/cafe-web/cafe2/ArticleList.json?search.clubid={cafe_id}&search.menuid={menu_id}&search.page={page}&search.perPage=50",
)
ARTICLE_DETAIL_TEMPLATES = (
    "https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/{cafe_id}/articles/{article_id}?useCafeId=true",
    "https://apis.naver.com/cafe-web/cafe-articleapi/v2/cafes/{cafe_id}/articles/{article_id}",
)
COMMENT_TEMPLATES = (
    "https://apis.naver.com/cafe-web/cafe-articleapi/v2/cafes/{cafe_id}/articles/{article_id}/comments/pages?requestFrom=A&orderBy=asc&page={page}",
)
INTRO_API_TEMPLATES = (
    "https://apis.naver.com/cafe-web/cafe2/CafeGateInfo.json?clubid={cafe_id}",
    "https://apis.naver.com/cafe-web/cafe2/CafeGateInfo.json?cluburl={slug}",
)
INTRO_PAGE_TEMPLATES = (
    "https://cafe.naver.com/CafeProfileView.nhn?clubid={cafe_id}",
    "https://cafe.naver.com/MyCafeIntro.nhn?clubid={cafe_id}",
)
INTRO_NAME_JS = r"""
const text = (el) => (el && (el.innerText || el.textContent) || '').trim();
const table = document.querySelector('table.tbl_cafe_info, table.cafe_info, table[class*="cafe_info"]');
if (table) {
  const named = table.querySelector('strong.cafe_name, .cafe_name');
  if (named && text(named)) return {name: text(named), html: table.outerHTML};
  for (const tr of table.querySelectorAll('tr')) {
    const label = text(tr.querySelector('th'));
    const value = text(tr.querySelector('td'));
    if (/^카페\s*이름$/.test(label) && value) return {name: value, html: tr.outerHTML};
  }
  return {name: '', html: table.outerHTML};
}
for (const row of document.querySelectorAll('tr, li, .info_item, dl > div')) {
  const label = text(row.querySelector('th, dt, .tit, .label'));
  const value = text(row.querySelector('td, dd, .val, .value'));
  if (/^카페\s*이름$/.test(label) && value) return {name: value, html: row.outerHTML || ''};
}
return {name: '', html: document.documentElement ? document.documentElement.outerHTML : ''};
"""


class CafePostSession:
    def __init__(self, browser: V2RBrowser, target: CafeBoardTarget):
        self.browser = browser
        self.logger = browser.logger
        self.target = target
        self.handle: str | None = None
        self.intro_cafe_name = ""
        self.rows: list[CafePostRow] = []
        self._found_count = 0

    def open_login_window(self, should_stop: Callable[[], bool] | None = None) -> None:
        self.browser.start()
        assert self.browser.driver
        self.handle = self.browser.driver.current_window_handle
        self.wait_for_naver_login(should_stop=should_stop)
        self._open_cafe_home()

    def wait_for_naver_login(
        self,
        should_stop: Callable[[], bool] | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.browser.start()
        assert self.browser.driver
        if self._logged_in():
            self.logger.info("네이버 로그인이 되어 있습니다")
            return
        self._switch()
        self.handle = self.browser.driver.current_window_handle
        self.browser._navigate(NAVER_LOGIN_URL, self.handle)
        self.logger.info(
            "네이버 로그인 창을 열었습니다. 이 크롬에서 로그인하면 수집을 시작할 수 있습니다"
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                raise CafePostError("로그인을 중지했습니다")
            if self._logged_in():
                self.logger.info("네이버 로그인을 확인했습니다")
                return
            time.sleep(1)
        raise CafePostError(
            "네이버 로그인을 기다렸지만 확인하지 못했습니다. "
            "이 프로그램 크롬에서 로그인한 뒤 다시 시작해 주세요"
        )

    def collect(
        self,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[CafePostRow]:
        self.wait_for_naver_login(should_stop=should_stop)
        self._open_cafe_home()
        self._load_intro_cafe_name()
        self.rows = []
        self._found_count = 0
        for article_id in self._iter_article_ids(should_stop):
            if should_stop and should_stop():
                break
            if progress:
                progress(len(self.rows), max(self._found_count, 1))
            try:
                row = self._read_article(article_id)
            except Exception as exc:
                self.logger.error("글 %s 읽기 실패: %s", article_id, exc)
                continue
            if not row.title and not row.body:
                self.logger.info("글 %s는 비어 있어 건너뜁니다", article_id)
                continue
            if self.intro_cafe_name:
                row.cafe_name = self.intro_cafe_name
            self.rows.append(row)
            if progress:
                progress(len(self.rows), max(self._found_count, len(self.rows)))
            time.sleep(0.2)
        apply_intro_cafe_name(self.rows, self.intro_cafe_name)
        if should_stop and should_stop():
            self.logger.info("중지를 눌러 여기까지 모은 글만 저장합니다")
        elif not self.rows:
            raise CafePostError("모을 글을 찾지 못했습니다. 카페·게시판 주소를 확인해 주세요")
        return self.rows

    def _iter_article_ids(
        self, should_stop: Callable[[], bool] | None
    ) -> Iterator[int]:
        cafe_id = self._require_cafe_id()
        menu_id = int(self.target.menu_id or 0)
        scope = "전체 게시글" if menu_id == 0 else f"게시판 {menu_id}"
        self.logger.info("%s 목록을 읽으면서 글을 모읍니다", scope)
        seen: set[int] = set()
        last_page = MAX_LIST_PAGES
        for page in range(1, MAX_LIST_PAGES + 1):
            if should_stop and should_stop():
                return
            if page > last_page:
                return
            payload, html = self._fetch_list_page(cafe_id, menu_id, page)
            ids = article_ids_from_payload(payload) if payload is not None else []
            if not ids:
                ids = article_ids_from_html(html)
            fresh = [item for item in ids if item not in seen]
            if not fresh:
                return
            for item in fresh:
                seen.add(item)
            self._found_count = len(seen)
            self.logger.info("목록 %s페이지에서 글 %s개를 더 찾았습니다", page, len(fresh))
            guessed = last_page_from_payload(payload) if payload is not None else None
            if guessed:
                last_page = min(last_page, guessed)
            for item in fresh:
                if should_stop and should_stop():
                    return
                yield item

    def _fetch_list_page(
        self, cafe_id: int, menu_id: int, page: int
    ) -> tuple[Any, str]:
        for template in ARTICLE_LIST_TEMPLATES:
            url = template.format(cafe_id=cafe_id, menu_id=menu_id, page=page)
            result = self._fetch(url)
            text = str(result.get("text") or "")
            payload = loads_maybe_json(text)
            if payload is not None and article_ids_from_payload(payload):
                return payload, text
        list_url = (
            f"{self.target.home_url}?iframe_url=/ArticleList.nhn"
            f"?search.clubid={cafe_id}&search.menuid={menu_id}"
            f"&search.boardtype=L&search.page={page}"
        )
        self._switch()
        self.browser._navigate(list_url, self.handle)
        time.sleep(0.8)
        html = self._page_html()
        return loads_maybe_json(html), html

    def _read_article(self, article_id: int) -> CafePostRow:
        cafe_id = self._require_cafe_id()
        payload = None
        for template in ARTICLE_DETAIL_TEMPLATES:
            result = self._fetch(template.format(cafe_id=cafe_id, article_id=article_id))
            payload = loads_maybe_json(str(result.get("text") or ""))
            if payload is not None:
                break
        row = post_from_payload(payload) if payload is not None else CafePostRow("", "", "", "", "")
        comments = list(row.comments)
        if not comments:
            comments = self._read_comments(cafe_id, article_id)
        if not row.title or not row.body or not comments:
            scraped = self._scrape_opened_article(article_id)
            row = CafePostRow(
                cafe_name=row.cafe_name or scraped.cafe_name,
                board_name=row.board_name or scraped.board_name,
                author=row.author or scraped.author,
                title=row.title or scraped.title,
                body=row.body or scraped.body,
                comments=comments or scraped.comments,
            )
        else:
            row.comments = comments
        if self.intro_cafe_name:
            row.cafe_name = self.intro_cafe_name
        return row

    def _load_intro_cafe_name(self) -> None:
        cafe_id = self.target.cafe_id
        slug = self.target.slug
        page_urls: list[str] = []
        api_urls: list[str] = []
        if cafe_id:
            page_urls.extend(
                template.format(cafe_id=cafe_id) for template in INTRO_PAGE_TEMPLATES
            )
            api_urls.append(INTRO_API_TEMPLATES[0].format(cafe_id=cafe_id, slug=slug))
        if slug:
            api_urls.append(INTRO_API_TEMPLATES[1].format(cafe_id=cafe_id or 0, slug=slug))
        for url in page_urls:
            result = self._fetch(url)
            name = cafe_name_from_intro_html(str(result.get("text") or ""))
            if name:
                self.intro_cafe_name = name
                self.logger.info("카페소개에서 카페 이름을 읽었습니다: %s", name)
                return
        name = self._scrape_intro_page()
        if name:
            self.intro_cafe_name = name
            self.logger.info("카페소개에서 카페 이름을 읽었습니다: %s", name)
            return
        for url in api_urls:
            result = self._fetch(url)
            name = self._name_from_intro_response(str(result.get("text") or ""))
            if name:
                self.intro_cafe_name = name
                self.logger.info("카페소개에서 카페 이름을 읽었습니다: %s", name)
                return
        self.logger.warning(
            "카페소개에서 카페 이름을 찾지 못했습니다. 글에서 읽은 이름을 씁니다"
        )

    def _name_from_intro_response(self, text: str) -> str:
        payload = loads_maybe_json(text)
        if payload is not None:
            name = cafe_name_from_info_payload(payload)
            if name:
                return name
        return cafe_name_from_intro_html(text) or cafe_name_from_intro_text(text)

    def _scrape_intro_page(self) -> str:
        cafe_id = self.target.cafe_id
        if not cafe_id:
            return ""
        iframe = quote(f"/CafeProfileView.nhn?clubid={cafe_id}", safe="")
        intro_url = f"{self.target.home_url}?iframe_url={iframe}"
        self._switch()
        self.browser._navigate(intro_url, self.handle)
        time.sleep(1.0)
        html, raw = self._intro_page_bits()
        name = clean_intro_cafe_name(str(raw.get("name") or ""))
        if name:
            return name
        return (
            cafe_name_from_intro_html(html)
            or cafe_name_from_intro_text(str(raw.get("html") or html))
        )

    def _intro_page_bits(self) -> tuple[str, dict[str, Any]]:
        assert self.browser.driver
        driver = self.browser.driver
        switched = False
        try:
            for frame in driver.find_elements(
                "css selector", "iframe#cafe_main, iframe[name='cafe_main']"
            ):
                driver.switch_to.frame(frame)
                switched = True
                break
        except Exception:
            pass
        raw: dict[str, Any] = {}
        html = ""
        try:
            try:
                raw = driver.execute_script(INTRO_NAME_JS) or {}
            except Exception:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            html = str(raw.get("html") or "") or self._page_html()
        finally:
            if switched:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
        return html, raw

    def _read_comments(self, cafe_id: int, article_id: int) -> list[CafeComment]:
        found: list[CafeComment] = []
        for page in range(1, 51):
            got = False
            for template in COMMENT_TEMPLATES:
                result = self._fetch(
                    template.format(cafe_id=cafe_id, article_id=article_id, page=page)
                )
                payload = loads_maybe_json(str(result.get("text") or ""))
                items = comments_from_payload(payload) if payload is not None else []
                if items:
                    found.extend(items)
                    got = True
                    break
            if not got:
                break
        return found

    def _scrape_opened_article(self, article_id: int) -> CafePostRow:
        assert self.browser.driver
        self._switch()
        self.browser._navigate(article_url(self.target, article_id), self.handle)
        time.sleep(1.2)
        driver = self.browser.driver
        try:
            for frame in driver.find_elements("css selector", "iframe#cafe_main, iframe[name='cafe_main']"):
                driver.switch_to.frame(frame)
                break
        except Exception:
            pass
        try:
            raw = driver.execute_script(SCRAPE_POST_JS) or {}
        except Exception:
            raw = {}
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
        comments = [
            CafeComment(
                nickname=str(item.get("nickname") or ""),
                text=str(item.get("text") or ""),
            )
            for item in (raw.get("comments") or [])
            if isinstance(item, dict)
        ]
        return CafePostRow(
            cafe_name=str(raw.get("cafe") or ""),
            board_name=str(raw.get("board") or ""),
            author=str(raw.get("author") or ""),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            comments=comments,
        )

    def _open_cafe_home(self) -> None:
        self.browser.start()
        assert self.browser.driver
        if self.handle is None:
            self.handle = self.browser.driver.current_window_handle
        self._switch()
        self.browser._navigate(self.target.home_url, self.handle)
        self.handle = self.browser.driver.current_window_handle
        if not self.target.cafe_id:
            found = cafe_id_from_text(self._page_html()) or cafe_id_from_text(
                self.browser.driver.current_url or ""
            )
            if not found:
                raise CafePostError(
                    "카페 번호를 찾지 못했습니다. 카페 주소를 다시 확인해 주세요"
                )
            self.target = self.target.with_cafe_id(found)
        self.logger.info("카페를 열었습니다: %s", self.target.home_url)

    def _require_cafe_id(self) -> int:
        if not self.target.cafe_id:
            raise CafePostError(
                "카페 번호를 찾지 못했습니다. 카페 주소를 다시 확인해 주세요"
            )
        return int(self.target.cafe_id)

    def _logged_in(self) -> bool:
        assert self.browser.driver
        names = [cookie.get("name", "") for cookie in self.browser.driver.get_cookies()]
        return cookies_show_naver_login(names)

    def _fetch(self, url: str) -> dict[str, Any]:
        assert self.browser.driver
        raw = self.browser.driver.execute_async_script(FETCH_SCRIPT, url)
        if not isinstance(raw, dict):
            return {"ok": False, "status": 0, "text": ""}
        return raw

    def _page_html(self) -> str:
        assert self.browser.driver
        return self.browser.driver.page_source or ""

    def _switch(self) -> None:
        self.browser.start()
        assert self.browser.driver
        self.browser._switch_to_handle(self.handle)
