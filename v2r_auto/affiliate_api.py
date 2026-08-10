from __future__ import annotations

import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import AffiliateJob, JobStatus


API_ROOT = "https://api-v2r.daboja.im"
COMMENT_ACCOUNTS = (
    "quilliant",
    "hunnede",
    "prtchht",
    "chocobbn",
    "chenallo",
    "colpith",
)
CAFE_BOARDS = {"씨씨앙": "자유 수다방", "양평맘": "이모저모 이야기"}
CAFE_DELAYS = {"씨씨앙": 4, "양평맘": 10}


class AffiliateApiError(RuntimeError):
    pass


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value or "", flags=re.IGNORECASE).casefold()


def _paragraph(line: str) -> dict[str, Any]:
    return {
        "id": f"SE-{uuid.uuid4()}",
        "nodes": [
            {
                "id": f"SE-{uuid.uuid4()}",
                "value": line,
                "@ctype": "textNode",
            }
        ],
        "@ctype": "paragraph",
    }


def _content_json(body: str) -> str:
    document = {
        "document": {
            "version": "2.9.0",
            "theme": "default",
            "language": "ko-KR",
            "id": uuid.uuid4().hex.upper()[:26],
            "di": {
                "dif": False,
                "dio": [{"dis": "N", "dia": {"t": 0, "p": 0, "st": 2827, "sk": 0}}],
            },
            "components": [
                {
                    "id": f"SE-{uuid.uuid4()}",
                    "layout": "default",
                    "value": [_paragraph(line) for line in body.splitlines()],
                    "@ctype": "text",
                }
            ],
            "documentId": "",
        }
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


class AffiliateApiPublisher:
    def __init__(self, browser, logger):
        self.browser = browser
        self.logger = logger
        self.authorization = ""

    def _capture_authorization(self) -> None:
        driver = self.browser.driver
        if driver is None:
            raise AffiliateApiError("Chrome이 열려 있지 않습니다")
        driver.get_log("performance")
        driver.get("https://v2r.daboja.im/nc/board?view=list")
        time.sleep(1)
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                request = message["params"]["request"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if message.get("method") != "Network.requestWillBeSent":
                continue
            if "api-v2r.daboja.im" not in request.get("url", ""):
                continue
            headers = request.get("headers", {})
            token = headers.get("Authorization") or headers.get("authorization")
            if token:
                self.authorization = token
        if not self.authorization:
            raise AffiliateApiError("V2R 로그인 정보를 확인하지 못했습니다. 다시 로그인하세요")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = API_ROOT + path
        if query:
            url += "?" + urlencode(query)
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(
            url,
            method=method,
            data=data,
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AffiliateApiError(
                f"V2R 요청 실패 ({exc.code}): {path} - {detail[:300]}"
            ) from exc
        return json.loads(raw) if raw else None

    @staticmethod
    def _field(item: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in item:
                return item[name]
        return None

    def _resolve_destination(self, job: AffiliateJob) -> dict[str, Any]:
        cafes = self._request("GET", "/naver_cafes/naver_join_cafes")
        cafe = next(
            (
                item
                for item in _walk_dicts(cafes)
                if self._field(item, "cafe_id", "cafeId")
                and _normalized(job.cafe)
                in _normalized(
                    str(
                        self._field(
                            item,
                            "cafe_name",
                            "cafeName",
                            "pc_cafe_name",
                            "mobile_cafe_name",
                            "name",
                        )
                        or ""
                    )
                )
            ),
            None,
        )
        if not cafe:
            raise AffiliateApiError(f"V2R에서 카페를 찾지 못했습니다: {job.cafe}")
        cafe_id = int(self._field(cafe, "cafe_id", "cafeId"))
        cafe_name = str(
            self._field(
                cafe,
                "cafe_name",
                "cafeName",
                "pc_cafe_name",
                "mobile_cafe_name",
                "name",
            )
        )

        accounts = self._request("GET", "/navers/accounts")
        if not any(
            str(self._field(item, "login_id", "naver_login_id", "loginId") or "")
            == job.account
            for item in _walk_dicts(accounts)
        ):
            raise AffiliateApiError(f"V2R에서 작성계정을 찾지 못했습니다: {job.account}")

        menus = self._request(
            "GET",
            "/naver_cafes/menus",
            query={"cafe_id": cafe_id, "naver_login_id": job.account},
        )
        board_name = CAFE_BOARDS[job.cafe]
        menu = next(
            (
                item
                for item in _walk_dicts(menus)
                if _normalized(str(self._field(item, "menu_name", "menuName") or ""))
                == _normalized(board_name)
            ),
            None,
        )
        if not menu:
            raise AffiliateApiError(f"V2R에서 게시판을 찾지 못했습니다: {board_name}")

        head_id = None
        head_name = None
        if job.prefix:
            heads = self._request(
                "GET",
                "/naver_cafes/heads",
                query={
                    "cafe_id": cafe_id,
                    "naver_login_id": job.account,
                    "menu_id": int(self._field(menu, "menu_id", "menuId")),
                },
            )
            head = next(
                (
                    item
                    for item in _walk_dicts(heads)
                    if _normalized(str(self._field(item, "head_name", "headName") or ""))
                    == _normalized(job.prefix)
                ),
                None,
            )
            if not head:
                raise AffiliateApiError(f"V2R에서 말머리를 찾지 못했습니다: {job.prefix}")
            head_id = self._field(head, "head_id", "headId")
            head_name = self._field(head, "head_name", "headName")

        return {
            "cafe_id": cafe_id,
            "cafe_name": cafe_name,
            "head_id": head_id,
            "head_name": head_name,
            "menu_id": int(self._field(menu, "menu_id", "menuId")),
            "menu_name": board_name,
            "naver_login_id": job.account,
            "target_view_count": 0,
            "use_comment_ai": True,
            "parent_id": None,
        }

    def _last_used(self, cafe_id: int) -> dict[str, str]:
        rows: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(50):
            query: dict[str, Any] = {
                "cafe_id": cafe_id,
                "days_ago": 180,
                "include_reserve": "true",
            }
            if token:
                query["next_token"] = token
            response = self._request(
                "GET", "/naver_cafe_articles/board_histories", query=query
            )
            rows.extend(response.get("histories", []))
            token = response.get("next_token")
            if not token:
                break
        last: dict[str, str] = {}
        for row in rows:
            account = str(row.get("naver_account_login_id") or "")
            used_at = str(row.get("created_at") or row.get("written_at") or "")
            if account and used_at > last.get(account, ""):
                last[account] = used_at
        return last

    def assign_accounts(self, jobs: list[AffiliateJob]) -> list[AffiliateJob]:
        """Fill blank D-column accounts from V2R's cafe-account status data."""
        self._capture_authorization()
        pending = [
            job
            for job in jobs
            if not job.account and job.status != JobStatus.SKIPPED
        ]
        if not pending:
            return []

        cafe_list = self._request("GET", "/naver_cafes/naver_join_cafes")
        global_data = self._request("GET", "/navers/accounts")
        global_accounts = {
            str(item["naver_login_id"]): item
            for item in _walk_dicts(global_data)
            if item.get("naver_login_id")
        }
        fixed = set(COMMENT_ACCOUNTS)
        pools: dict[tuple[str, str], list[str]] = {}

        for cafe_name in {job.cafe for job in pending}:
            cafe = next(
                (
                    item
                    for item in _walk_dicts(cafe_list)
                    if self._field(item, "cafe_id", "cafeId")
                    and _normalized(cafe_name)
                    in _normalized(
                        str(
                            self._field(
                                item,
                                "cafe_name",
                                "cafeName",
                                "pc_cafe_name",
                                "mobile_cafe_name",
                                "name",
                            )
                            or ""
                        )
                    )
                ),
                None,
            )
            if not cafe:
                for job in pending:
                    if job.cafe == cafe_name:
                        job.status = JobStatus.SKIPPED
                        job.message = f"V2R 카페 계정 현황에서 {cafe_name}을 찾지 못함"
                continue

            cafe_id = int(self._field(cafe, "cafe_id", "cafeId"))
            status_data = self._request(
                "GET", "/naver_cafes/naver_join_cafe", query={"cafe_id": cafe_id}
            )
            joined = {
                str(item["login_id"]): item
                for item in _walk_dicts(status_data)
                if item.get("login_id")
                and not item.get("force_drop")
                and not item.get("stop_cafe_member")
            }
            last_used = self._last_used(cafe_id)

            for account_type, real_name in (("실명", True), ("비실명", False)):
                eligible: list[str] = []
                for account in joined:
                    global_account = global_accounts.get(account)
                    info = (global_account or {}).get("my_info_v2") or {}
                    if (
                        not global_account
                        or account in fixed
                        or info.get("is_real_name") is not real_name
                        or global_account.get("is_block")
                        or global_account.get("is_login_fail")
                    ):
                        continue
                    eligible.append(account)
                pools[(cafe_name, account_type)] = sorted(
                    eligible,
                    key=lambda account: (
                        account in last_used,
                        last_used.get(account, ""),
                        account,
                    ),
                )
                self.logger.info(
                    "%s %s 사용가능 작성계정: %s개",
                    cafe_name,
                    account_type,
                    len(eligible),
                )

        indexes = {key: 0 for key in pools}
        assigned: list[AffiliateJob] = []
        for job in pending:
            pool = pools.get((job.cafe, job.account_type), [])
            if not pool:
                job.status = JobStatus.SKIPPED
                job.message = (
                    f"{job.cafe}에 사용가능한 {job.account_type} 작성계정이 없음"
                )
                continue
            index = indexes[(job.cafe, job.account_type)]
            job.account = pool[index % len(pool)]
            indexes[(job.cafe, job.account_type)] = index + 1
            assigned.append(job)
            self.logger.info(
                "행 %s 작성계정 자동 배정: %s (%s / %s)",
                job.row_number,
                job.account,
                job.cafe,
                job.account_type,
            )
        return assigned

    def _member(self, cafe_id: int, account: str) -> dict[str, str]:
        cafe = self._request(
            "GET",
            "/naver_cafes/naver_join_cafe",
            query={"cafe_id": cafe_id},
        )
        candidate = next(
            (
                item for item in _walk_dicts(cafe)
                if self._field(item, "login_id", "naver_login_id") == account
                and self._field(item, "member_key", "memberKey")
            ),
            None,
        )
        if not candidate:
            raise AffiliateApiError(f"답글 연결 정보를 찾지 못했습니다: {account}")
        return {
            "member_key": str(self._field(candidate, "member_key", "memberKey")),
            "naver_login_id": account,
            "nick": str(
                self._field(candidate, "nick_name", "nick", "nickname", "naver_nick_name")
                or ""
            ),
        }

    @staticmethod
    def _write_options() -> dict[str, Any]:
        return {
            "enableComment": True,
            "externalOpen": True,
            "enableScrap": True,
            "enableCopy": False,
            "useAutoSource": True,
            "useCcl": True,
            "cclTypes": ["ATTRIBUTION", "NONCOMMERCIAL", "NO_DERIVATIVE"],
            "open": False,
            "naverOpen": True,
        }

    def _create_source(
        self,
        title: str,
        body: str,
        tags: list[str],
        destination: dict[str, Any],
        comments: list[dict[str, Any]],
        parent_source_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "tag_list": tags,
            "title": title,
            "content_json": _content_json(body),
            "cafe_write_options": self._write_options(),
            "comments": comments,
            "destination": destination,
            "likes": [],
        }
        if parent_source_id:
            payload["parent_source_id"] = parent_source_id
        response = self._request(
            "POST", "/naver_cafe_articles/naver_cafe_article_source", payload
        )
        source = response.get("naver_cafe_article_source", {})
        source_id = source.get("source_id")
        if not source_id:
            raise AffiliateApiError("V2R 등록 결과에서 글 번호를 찾지 못했습니다")
        return str(source_id)

    def _wait_for_written_at(self, source_id: str, cafe_id: int) -> datetime:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            history = self._request(
                "GET",
                "/naver_cafe_articles/board_histories",
                query={"cafe_id": cafe_id, "days_ago": 1, "include_reserve": "true"},
            )
            item = next(
                (
                    row
                    for row in history.get("histories", [])
                    if row.get("source_id") == source_id
                ),
                None,
            )
            if item and item.get("status") == "DONE" and item.get("written_at"):
                return datetime.fromisoformat(str(item["written_at"]).replace("Z", "+00:00"))
            time.sleep(2)
        raise AffiliateApiError("일상 글 등록 완료 시간을 확인하지 못했습니다")

    @staticmethod
    def _comment(
        account: str,
        text: str,
        start_at: datetime,
        *,
        root_start_at: datetime | None = None,
        reply_member: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "contents": text,
            "naver_login_id": account,
            "start_at": start_at.isoformat().replace("+00:00", "Z"),
            "repeat_count": 0,
            "interval_seconds": 0,
        }
        if root_start_at:
            item["root_start_at"] = root_start_at.isoformat().replace("+00:00", "Z")
        if reply_member:
            item["reply_member"] = reply_member
        return item

    def _comments(
        self,
        job: AffiliateJob,
        start_at: datetime,
        cafe_id: int,
    ) -> list[dict[str, Any]]:
        special_label = "대대댓글2" if job.article_type == "후기형" else "대대대댓글2"
        labels = ("댓글1", "댓글2", special_label, "댓글3", "댓글4", "댓글5")
        accounts = random.SystemRandom().sample(COMMENT_ACCOUNTS, len(COMMENT_ACCOUNTS))
        account = dict(zip(labels, accounts))
        by_label: dict[str, Any] = {}

        def collect(nodes):
            for node in nodes:
                by_label[node.label] = node
                collect(node.children)

        collect(job.comments)
        required = (
            "댓글1", "대댓글1", "댓글2", "대댓글2", "대대댓글2", "대대대댓글2",
            "댓글3", "대댓글3", "댓글4", "대댓글4", "댓글5", "대댓글5",
        )
        missing = [label for label in required if label not in by_label]
        if missing:
            raise AffiliateApiError("원고 댓글이 부족합니다: " + ", ".join(missing))

        author_member = self._member(cafe_id, job.account)
        question_deep_account = account["댓글2"]
        review_deep_account = account.get("대대댓글2", "")
        deep_account = (
            review_deep_account if job.article_type == "후기형" else question_deep_account
        )
        deep_member = self._member(cafe_id, deep_account)

        def root(label: str, root_minute: int, reply_label: str, reply_minute: int):
            root_at = start_at + timedelta(minutes=root_minute)
            item = self._comment(
                account[label], by_label[label].text, root_at
            )
            item["comments"] = [
                self._comment(
                    job.account,
                    by_label[reply_label].text,
                    start_at + timedelta(minutes=reply_minute),
                    root_start_at=root_at,
                )
            ]
            return item

        comment2_at = start_at + timedelta(minutes=6)
        comment2 = self._comment(
            account["댓글2"], by_label["댓글2"].text, comment2_at
        )
        comment2["comments"] = [
            self._comment(
                job.account,
                by_label["대댓글2"].text,
                start_at + timedelta(minutes=16),
                root_start_at=comment2_at,
            ),
            self._comment(
                deep_account,
                by_label["대대댓글2"].text,
                start_at + timedelta(minutes=26),
                root_start_at=comment2_at,
                reply_member=author_member,
            ),
            self._comment(
                job.account if job.article_type == "후기형" else account["대대대댓글2"],
                by_label["대대대댓글2"].text,
                start_at + timedelta(minutes=36),
                root_start_at=comment2_at,
                reply_member=deep_member,
            ),
        ]
        return [
            root("댓글1", 5, "대댓글1", 15),
            comment2,
            root("댓글3", 7, "대댓글3", 17),
            root("댓글4", 8, "대댓글4", 18),
            root("댓글5", 9, "대댓글5", 19),
        ]

    def _verify(self, source_id: str, job: AffiliateJob, start_at: datetime) -> None:
        detail = self._request(
            "GET", "/naver_cafe_articles/article", query={"source_id": source_id}
        )
        source = detail["naver_cafe_article_source"]
        destination = detail["naver_cafe_article_destination"]
        comments = detail["naver_cafe_article_source_comments"]
        document = json.loads(detail["naver_cafe_article_source_detail"]["body"])
        body_lines = [
            paragraph["nodes"][0]["value"]
            for component in document["document"]["components"]
            for paragraph in component["value"]
        ]
        if source["title"] != job.title or source["tag_list"] != job.tags:
            raise AffiliateApiError("등록 후 제목 또는 태그 검증에 실패했습니다")
        if body_lines != job.body.splitlines():
            raise AffiliateApiError("등록 후 본문 문단 검증에 실패했습니다")
        if datetime.fromisoformat(destination["start_at"].replace("Z", "+00:00")) != start_at:
            raise AffiliateApiError("등록 후 수정 예약 시간 검증에 실패했습니다")
        if not 80 <= int(destination["target_view_count"]) <= 100:
            raise AffiliateApiError("등록 후 조회수 설정 검증에 실패했습니다")
        roots = sum(comment.get("parent_comment_id") is None for comment in comments)
        replies = len(comments) - roots
        if len(comments) != 12 or roots != 5 or replies != 7:
            raise AffiliateApiError("등록 후 댓글 구조 검증에 실패했습니다")

    def publish(self, job: AffiliateJob, dry_run: bool) -> str:
        if job.daily_post is None:
            raise AffiliateApiError("배정된 일상 글이 없습니다")
        self._capture_authorization()
        destination = self._resolve_destination(job)
        if dry_run:
            self.logger.info(
                "행 %s API 검증 완료: %s / %s",
                job.row_number,
                destination["cafe_name"],
                destination["menu_name"],
            )
            return ""

        daily_destination = dict(destination)
        daily_destination["start_at"] = None
        daily_source_id = self._create_source(
            job.daily_post.title,
            job.daily_post.body,
            [],
            daily_destination,
            [],
        )
        job.daily_post_url = (
            f"https://v2r.daboja.im/nc/articleDetail/{daily_source_id}"
        )
        written_at = self._wait_for_written_at(daily_source_id, destination["cafe_id"])
        revision_at = written_at + timedelta(hours=CAFE_DELAYS[job.cafe])

        revision_destination = dict(destination)
        revision_destination["start_at"] = revision_at.isoformat().replace("+00:00", "Z")
        revision_destination["target_view_count"] = random.SystemRandom().randint(80, 100)
        comments = self._comments(job, revision_at, destination["cafe_id"])
        revision_source_id = self._create_source(
            job.title,
            job.body,
            job.tags,
            revision_destination,
            comments,
            parent_source_id=daily_source_id,
        )
        self._verify(revision_source_id, job, revision_at)
        self.logger.info(
            "행 %s 수정 예약 API 검증 완료: 댓글 12개 / 조회수 %s",
            job.row_number,
            revision_destination["target_view_count"],
        )
        return f"https://v2r.daboja.im/nc/articleDetail/{revision_source_id}"
