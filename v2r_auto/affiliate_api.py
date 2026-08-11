from __future__ import annotations

import json
import random
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cafe_catalog import CafeCatalogEntry, CafeCatalogService
from .images import (
    GoogleDriveImageResolver,
    PLACEHOLDER_PATTERN,
    strip_placeholders,
)
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
CAFE_DELAYS = {"씨씨앙": 4, "양평맘": 10}
CAFE_DESTINATIONS = {
    "씨씨앙": {
        "cafe_id": 25016228,
        "cafe_name": "국내1위 다이어트 커뮤니티 씨씨앙(식단,운동,후기,헬스,체험단)",
        "menu_id": 328,
        "menu_name": "자유 수다방",
    },
    "양평맘": {
        "cafe_id": 22788814,
        "cafe_name": "양평 맘`s 전원 Story",
        "menu_id": 14,
        "menu_name": "이모저모 이야기💕",
    },
}


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


def _text_component(lines: list[str]) -> dict[str, Any]:
    return {
        "id": f"SE-{uuid.uuid4()}",
        "layout": "default",
        "value": [_paragraph(line) for line in lines],
        "@ctype": "text",
    }


def _content_json(
    body: str,
    image_components: dict[int, dict[str, Any]] | None = None,
) -> str:
    image_components = image_components or {}
    components: list[dict[str, Any]] = []
    pending_lines: list[str] = []
    occurrence = 0

    def flush_text() -> None:
        if pending_lines:
            components.append(_text_component(list(pending_lines)))
            pending_lines.clear()

    for line in body.splitlines():
        matches = list(PLACEHOLDER_PATTERN.finditer(line))
        if not matches:
            pending_lines.append(line)
            continue
        cleaned_line = PLACEHOLDER_PATTERN.sub("", line)
        uploaded: list[dict[str, Any]] = []
        for _match in matches:
            component = image_components.get(occurrence)
            occurrence += 1
            if component:
                uploaded.append(deepcopy(component))
        if not uploaded:
            pending_lines.append(cleaned_line)
            continue
        if cleaned_line:
            pending_lines.append(cleaned_line)
        flush_text()
        components.extend(uploaded)
        if not cleaned_line:
            # Keep the original marker line as an empty paragraph so surrounding
            # paragraph spacing remains unchanged after inserting the image.
            pending_lines.append("")
    flush_text()
    if not components:
        components.append(_text_component([""]))

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
            "components": components,
            "documentId": "",
        }
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


class AffiliateApiPublisher:
    def __init__(self, browser, logger):
        self.browser = browser
        self.logger = logger
        self.authorization = ""
        self.account_pools: dict[tuple[str, str], list[str]] = {}
        self.account_indexes: dict[tuple[str, str], int] = {}
        self.blocked_accounts: set[str] = set()
        self.used_accounts: set[str] = set()
        self.comment_slots: dict[str, set[datetime]] = {}
        self.image_resolver = (
            GoogleDriveImageResolver(
                self.browser.config.download_dir,
                logger,
            )
            if self.browser is not None and getattr(self.browser, "config", None)
            else None
        )

    def load_cafe_catalog(self) -> list[CafeCatalogEntry]:
        self._capture_authorization()
        return CafeCatalogService(self._request, self.logger).load()

    def _capture_authorization(self) -> None:
        if self.authorization:
            return
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
        *,
        retry_auth: bool = True,
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
        delays = (10, 30, 120, 300)
        for attempt in range(5):
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                break
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if (
                    retry_auth
                    and exc.code == 403
                    and "TOKEN_ERROR" in detail
                    and self.browser is not None
                ):
                    self.authorization = ""
                    self._capture_authorization()
                    return self._request(
                        method,
                        path,
                        payload,
                        query,
                        retry_auth=False,
                    )
                if (exc.code == 429 or exc.code >= 500) and attempt < 4:
                    delay = delays[attempt]
                    self.logger.warning(
                        "V2R 일시 오류 %s: %s초 후 재시도 (%s/5)",
                        exc.code,
                        delay,
                        attempt + 2,
                    )
                    time.sleep(delay)
                    continue
                raise AffiliateApiError(
                    f"V2R 요청 실패 ({exc.code}): {path} - {detail[:300]}"
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 4:
                    delay = delays[attempt]
                    self.logger.warning(
                        "네트워크 오류: %s초 후 재시도 (%s/5)",
                        delay,
                        attempt + 2,
                    )
                    time.sleep(delay)
                    continue
                raise AffiliateApiError(f"V2R 네트워크 요청 실패: {path}") from exc
        else:
            raise AffiliateApiError(f"V2R 요청 재시도 실패: {path}")
        return json.loads(raw) if raw else None

    @staticmethod
    def _field(item: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in item:
                return item[name]
        return None

    def _resolve_destination(self, job: AffiliateJob) -> dict[str, Any]:
        config = CAFE_DESTINATIONS.get(job.cafe)
        if not config:
            raise AffiliateApiError(f"지원하지 않는 제휴 카페입니다: {job.cafe}")
        cafe_id = int(config["cafe_id"])
        cafe_name = str(config["cafe_name"])

        accounts = self._request("GET", "/navers/accounts")
        if not any(
            str(self._field(item, "login_id", "naver_login_id", "loginId") or "")
            == job.account
            for item in _walk_dicts(accounts)
        ):
            raise AffiliateApiError(f"V2R에서 작성계정을 찾지 못했습니다: {job.account}")

        board_name = str(config["menu_name"])
        menu_id = int(config["menu_id"])

        head_id = None
        head_name = None
        if job.prefix:
            heads = self._request(
                "GET",
                "/naver_cafes/heads",
                query={
                    "cafe_id": cafe_id,
                    "naver_login_id": job.account,
                    "menu_id": menu_id,
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
            "menu_id": menu_id,
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
        target_jobs = [
            job
            for job in jobs
            if job.status == JobStatus.PENDING
            and job.account_type in {"실명", "비실명"}
        ]
        if not target_jobs:
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

        for cafe_name in {job.cafe for job in target_jobs}:
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

        self.account_pools = pools
        self.account_indexes = {key: 0 for key in pools}
        self.used_accounts.update(
            job.account for job in jobs if job.account and job.status == JobStatus.PENDING
        )
        assigned: list[AffiliateJob] = []
        for job in pending:
            pool = pools.get((job.cafe, job.account_type), [])
            if not pool:
                job.status = JobStatus.SKIPPED
                job.message = (
                    f"{job.cafe}에 사용가능한 {job.account_type} 작성계정이 없음"
                )
                continue
            job.account = self._pick_account(job.cafe, job.account_type)
            if not job.account:
                job.status = JobStatus.SKIPPED
                job.message = (
                    f"{job.cafe}에 사용가능한 {job.account_type} 작성계정이 없음"
                )
                continue
            assigned.append(job)
            self.logger.info(
                "행 %s 작성계정 자동 배정: %s (%s / %s)",
                job.row_number,
                job.account,
                job.cafe,
                job.account_type,
            )
        return assigned

    def _pick_account(self, cafe: str, account_type: str) -> str:
        key = (cafe, account_type)
        pool = self.account_pools.get(key, [])
        if not pool:
            return ""
        start = self.account_indexes.get(key, 0)
        for prefer_unused in (True, False):
            for offset in range(len(pool)):
                account = pool[(start + offset) % len(pool)]
                if account in self.blocked_accounts:
                    continue
                if prefer_unused and account in self.used_accounts:
                    continue
                self.account_indexes[key] = start + offset + 1
                self.used_accounts.add(account)
                return account
        return ""

    def replace_failed_account(self, job: AffiliateJob) -> str:
        if job.account:
            self.blocked_accounts.add(job.account)
        replacement = self._pick_account(job.cafe, job.account_type)
        if replacement:
            job.account = replacement
        return replacement

    @staticmethod
    def classify_failure(error: Exception) -> tuple[str, bool]:
        text = str(error)
        if "33007" in text or "등급" in text:
            return "실패: 등급 미달", True
        if "NOT_LOGIN" in text or "session not found" in text:
            return "실패: 네이버 로그인 세션 없음", True
        if "NAVER_LOGIN_FAIL" in text or "NID_SES" in text:
            return "실패: 네이버 로그인 실패", True
        if "등록 완료 시간을 확인하지 못했습니다" in text:
            return "실패: 일상 글 발행 시간 초과", True
        return f"실패: {text[:150]}", False

    def cleanup_stale_sources(self, cafe_names: set[str]) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for cafe_name in cafe_names:
            config = CAFE_DESTINATIONS.get(cafe_name)
            if not config:
                continue
            history = self._request(
                "GET",
                "/naver_cafe_articles/board_histories",
                query={
                    "cafe_id": config["cafe_id"],
                    "days_ago": 1,
                    "include_reserve": "true",
                },
            )
            for item in history.get("histories", []):
                status = item.get("status")
                created_raw = item.get("created_at")
                if not created_raw:
                    continue
                created_at = datetime.fromisoformat(
                    str(created_raw).replace("Z", "+00:00")
                )
                stale_reserved = (
                    status == "RESERVED"
                    and item.get("parent_source_id") is None
                    and item.get("write_executor") is None
                    and created_at < cutoff
                )
                if status == "FAIL" or stale_reserved:
                    self._delete_source(str(item["source_id"]))

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
        content_json: str | None = None,
        recovery_statuses: tuple[str, ...] = ("DONE",),
    ) -> str:
        payload: dict[str, Any] = {
            "tag_list": tags,
            "title": title,
            "content_json": content_json or _content_json(body),
            "cafe_write_options": self._write_options(),
            "comments": comments,
            "destination": destination,
            "likes": [],
        }
        if parent_source_id:
            payload["parent_source_id"] = parent_source_id
        requested_at = datetime.now(timezone.utc)
        try:
            response = self._request(
                "POST", "/naver_cafe_articles/naver_cafe_article_source", payload
            )
        except AffiliateApiError:
            recent = self._find_recent_source(
                int(destination["cafe_id"]),
                str(destination["naver_login_id"]),
                title,
                requested_at,
                parent_source_id,
            )
            if recent and recent.get("status") in recovery_statuses:
                return str(recent["source_id"])
            if recent:
                self._delete_source(str(recent["source_id"]))
            raise
        source = response.get("naver_cafe_article_source", {})
        source_id = source.get("source_id")
        if not source_id:
            raise AffiliateApiError("V2R 등록 결과에서 글 번호를 찾지 못했습니다")
        return str(source_id)

    def _find_recent_source(
        self,
        cafe_id: int,
        account: str,
        title: str,
        requested_at: datetime,
        parent_source_id: str | None,
    ) -> dict[str, Any] | None:
        threshold = requested_at - timedelta(seconds=15)
        for _ in range(8):
            history = self._request(
                "GET",
                "/naver_cafe_articles/board_histories",
                query={"cafe_id": cafe_id, "days_ago": 1, "include_reserve": "true"},
            )
            item = next(
                (
                    row
                    for row in history.get("histories", [])
                    if row.get("naver_account_login_id") == account
                    and row.get("title") == title
                    and row.get("parent_source_id") == parent_source_id
                    and datetime.fromisoformat(
                        str(row.get("created_at")).replace("Z", "+00:00")
                    )
                    >= threshold
                ),
                None,
            )
            if item:
                return item
            time.sleep(0.5)
        return None

    def _delete_source(self, source_id: str) -> None:
        try:
            self._request(
                "POST",
                "/naver_cafe_articles/article/delete",
                {"source_id": source_id},
            )
            self.logger.info("실패 찌꺼기 글 삭제: %s", source_id)
        except Exception:
            self.logger.exception("실패 찌꺼기 글 삭제 실패: %s", source_id)

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
            if item and item.get("status") == "FAIL":
                reason = str(item.get("fail_reason") or "원인 불명")
                raise AffiliateApiError(f"일상 글 발행 실패: {reason}")
            time.sleep(2)
        raise AffiliateApiError("일상 글 등록 완료 시간을 확인하지 못했습니다")

    def _comment(
        self,
        account: str,
        text: str,
        start_at: datetime,
        *,
        root_start_at: datetime | None = None,
        reply_member: dict[str, str] | None = None,
        manage_slot: bool = True,
    ) -> dict[str, Any]:
        if manage_slot:
            minute = start_at.replace(second=0, microsecond=0)
            occupied = self.comment_slots.setdefault(account, set())
            while minute in occupied:
                start_at += timedelta(minutes=1)
                minute = start_at.replace(second=0, microsecond=0)
            occupied.add(minute)
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
        comment_accounts: tuple[str, ...] = COMMENT_ACCOUNTS,
    ) -> list[dict[str, Any]]:
        special_label = "대대댓글2" if job.article_type == "후기형" else "대대대댓글2"
        labels = ("댓글1", "댓글2", special_label, "댓글3", "댓글4", "댓글5")
        if len(comment_accounts) != len(labels):
            raise AffiliateApiError("댓글 작성계정은 정확히 6개여야 합니다")
        accounts = random.SystemRandom().sample(
            comment_accounts,
            len(comment_accounts),
        )
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

        bundle_slots = [
            (account["댓글1"], 5),
            (job.account, 15),
            (account["댓글2"], 6),
            (job.account, 16),
            (deep_account, 26),
            (
                job.account
                if job.article_type == "후기형"
                else account["대대대댓글2"],
                36,
            ),
            (account["댓글3"], 7),
            (job.account, 17),
            (account["댓글4"], 8),
            (job.account, 18),
            (account["댓글5"], 9),
            (job.account, 19),
        ]
        bundle_shift = 0
        for candidate_shift in range(24 * 60):
            proposed: set[tuple[str, datetime]] = set()
            conflict = False
            for slot_account, minute_offset in bundle_slots:
                minute = (
                    start_at + timedelta(minutes=minute_offset + candidate_shift)
                ).replace(second=0, microsecond=0)
                key = (slot_account, minute)
                if (
                    minute in self.comment_slots.setdefault(slot_account, set())
                    or key in proposed
                ):
                    conflict = True
                    break
                proposed.add(key)
            if not conflict:
                bundle_shift = candidate_shift
                for slot_account, minute in proposed:
                    self.comment_slots[slot_account].add(minute)
                break
        else:
            raise AffiliateApiError("댓글 예약 충돌을 피할 시간을 찾지 못했습니다")

        shifted_start_at = start_at + timedelta(minutes=bundle_shift)
        if bundle_shift:
            self.logger.info(
                "행 %s 댓글 전체를 %s분 이동해 원고 순서를 유지합니다",
                job.row_number,
                bundle_shift,
            )

        def root(label: str, root_minute: int, reply_label: str, reply_minute: int):
            proposed_root_at = shifted_start_at + timedelta(minutes=root_minute)
            item = self._comment(
                account[label],
                by_label[label].text,
                proposed_root_at,
                manage_slot=False,
            )
            root_at = datetime.fromisoformat(item["start_at"].replace("Z", "+00:00"))
            item["comments"] = [
                self._comment(
                    job.account,
                    by_label[reply_label].text,
                    shifted_start_at + timedelta(minutes=reply_minute),
                    root_start_at=root_at,
                    manage_slot=False,
                )
            ]
            return item

        proposed_comment2_at = shifted_start_at + timedelta(minutes=6)
        comment2 = self._comment(
            account["댓글2"],
            by_label["댓글2"].text,
            proposed_comment2_at,
            manage_slot=False,
        )
        comment2_at = datetime.fromisoformat(
            comment2["start_at"].replace("Z", "+00:00")
        )
        comment2["comments"] = [
            self._comment(
                job.account,
                by_label["대댓글2"].text,
                shifted_start_at + timedelta(minutes=16),
                root_start_at=comment2_at,
                manage_slot=False,
            ),
            self._comment(
                deep_account,
                by_label["대대댓글2"].text,
                shifted_start_at + timedelta(minutes=26),
                root_start_at=comment2_at,
                reply_member=author_member,
                manage_slot=False,
            ),
            self._comment(
                job.account if job.article_type == "후기형" else account["대대대댓글2"],
                by_label["대대대댓글2"].text,
                shifted_start_at + timedelta(minutes=36),
                root_start_at=comment2_at,
                reply_member=deep_member,
                manage_slot=False,
            ),
        ]
        return [
            root("댓글1", 5, "대댓글1", 15),
            comment2,
            root("댓글3", 7, "대댓글3", 17),
            root("댓글4", 8, "대댓글4", 18),
            root("댓글5", 9, "대댓글5", 19),
        ]

    def _prepare_revision_content(
        self,
        job: AffiliateJob,
        destination: dict[str, Any],
    ) -> str:
        clean_body = strip_placeholders(job.body)
        job.prepared_image_count = 0
        if job.image_disabled:
            self.logger.info(
                "행 %s I열 이미지 없음=Y: 중괄호를 지우고 이미지 없이 진행",
                job.row_number,
            )
            return _content_json(clean_body)
        if not self.image_resolver or not job.brand:
            if PLACEHOLDER_PATTERN.search(job.body):
                self.logger.warning(
                    "행 %s 브랜드를 확인하지 못해 중괄호를 지우고 이미지 없이 진행",
                    job.row_number,
                )
            return _content_json(clean_body)

        try:
            resolved = self.image_resolver.resolve(job)
        except Exception as exc:
            self.logger.warning(
                "행 %s Google Drive 이미지 확인 실패로 이미지 없이 계속 발행: %s",
                job.row_number,
                exc,
            )
            return _content_json(clean_body)
        if not resolved:
            return _content_json(clean_body)
        try:
            uploaded = self.browser.upload_affiliate_images(
                job,
                str(destination["menu_name"]),
                [item.local_path for item in resolved],
            )
        except Exception as exc:
            self.logger.warning(
                "행 %s 이미지 업로드 실패로 이미지 없이 계속 발행: %s",
                job.row_number,
                exc,
            )
            return _content_json(clean_body)

        components = {
            item.occurrence: component
            for item, component in zip(resolved, uploaded)
            if component
        }
        job.prepared_image_count = len(components)
        self.logger.info(
            "행 %s 수정 본문 이미지 준비 완료: %s개",
            job.row_number,
            job.prepared_image_count,
        )
        return _content_json(job.body, components)

    def _verify(self, source_id: str, job: AffiliateJob, start_at: datetime) -> None:
        detail = self._request(
            "GET", "/naver_cafe_articles/article", query={"source_id": source_id}
        )
        source = detail["naver_cafe_article_source"]
        destination = detail["naver_cafe_article_destination"]
        comments = detail["naver_cafe_article_source_comments"]
        document = json.loads(detail["naver_cafe_article_source_detail"]["body"])
        body_lines = [
            "".join(str(node.get("value") or "") for node in paragraph.get("nodes", []))
            for component in document["document"]["components"]
            if component.get("@ctype") == "text"
            for paragraph in component.get("value", [])
        ]
        if source["title"] != job.title or source["tag_list"] != job.tags:
            raise AffiliateApiError("등록 후 제목 또는 태그 검증에 실패했습니다")
        if body_lines != strip_placeholders(job.body).splitlines():
            raise AffiliateApiError("등록 후 본문 문단 검증에 실패했습니다")
        if any(PLACEHOLDER_PATTERN.search(line) for line in body_lines):
            raise AffiliateApiError("등록 후 본문에 중괄호 표시가 남아 있습니다")
        media_count = sum(
            component.get("@ctype") in {"image", "imageGroup", "imageStrip"}
            for component in document["document"]["components"]
        )
        if job.prepared_image_count and media_count < job.prepared_image_count:
            raise AffiliateApiError("등록 후 본문 이미지 개수 검증에 실패했습니다")
        if datetime.fromisoformat(destination["start_at"].replace("Z", "+00:00")) != start_at:
            raise AffiliateApiError("등록 후 수정 예약 시간 검증에 실패했습니다")
        if not 80 <= int(destination["target_view_count"]) <= 100:
            raise AffiliateApiError("등록 후 조회수 설정 검증에 실패했습니다")
        roots = sum(comment.get("parent_comment_id") is None for comment in comments)
        replies = len(comments) - roots
        if len(comments) != 12 or roots != 5 or replies != 7:
            raise AffiliateApiError("등록 후 댓글 구조 검증에 실패했습니다")

        by_label: dict[str, Any] = {}

        def collect(nodes):
            for node in nodes:
                by_label[node.label] = node
                collect(node.children)

        collect(job.comments)
        root_comments = sorted(
            (
                comment
                for comment in comments
                if comment.get("parent_comment_id") is None
            ),
            key=lambda comment: datetime.fromisoformat(
                str(comment["start_at"]).replace("Z", "+00:00")
            ),
        )
        expected_root_labels = ["댓글1", "댓글2", "댓글3", "댓글4", "댓글5"]
        if [comment.get("contents") for comment in root_comments] != [
            by_label[label].text for label in expected_root_labels
        ]:
            raise AffiliateApiError("등록 후 댓글1~5 원고 순서 검증에 실패했습니다")

        expected_reply_labels = {
            "댓글1": ["대댓글1"],
            "댓글2": ["대댓글2", "대대댓글2", "대대대댓글2"],
            "댓글3": ["대댓글3"],
            "댓글4": ["대댓글4"],
            "댓글5": ["대댓글5"],
        }
        for root_label, root_comment in zip(
            expected_root_labels,
            root_comments,
        ):
            children = sorted(
                (
                    comment
                    for comment in comments
                    if comment.get("parent_comment_id")
                    == root_comment.get("comment_id")
                ),
                key=lambda comment: datetime.fromisoformat(
                    str(comment["start_at"]).replace("Z", "+00:00")
                ),
            )
            if [comment.get("contents") for comment in children] != [
                by_label[label].text
                for label in expected_reply_labels[root_label]
            ]:
                raise AffiliateApiError(
                    f"등록 후 {root_label} 답글 순서 검증에 실패했습니다"
                )

    def publish(
        self,
        job: AffiliateJob,
        dry_run: bool,
        resume: dict[str, Any] | None = None,
        checkpoint=None,
    ) -> str:
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

        resume = resume or {}
        revision_source_id = str(resume.get("revision_source_id") or "")
        if revision_source_id:
            detail = self._request(
                "GET",
                "/naver_cafe_articles/article",
                query={"source_id": revision_source_id},
            )
            revision_at = datetime.fromisoformat(
                detail["naver_cafe_article_destination"]["start_at"].replace(
                    "Z", "+00:00"
                )
            )
            self._verify(revision_source_id, job, revision_at)
            if checkpoint:
                checkpoint("VERIFIED", revision_source_id=revision_source_id)
            return f"https://v2r.daboja.im/nc/articleDetail/{revision_source_id}"

        daily_source_id = str(resume.get("daily_source_id") or "")
        if not daily_source_id:
            daily_destination = dict(destination)
            daily_destination["start_at"] = None
            daily_source_id = self._create_source(
                job.daily_post.title,
                job.daily_post.body,
                [],
                daily_destination,
                [],
            )
            if checkpoint:
                checkpoint("DAILY_CREATED", daily_source_id=daily_source_id)
        job.daily_post_url = (
            f"https://v2r.daboja.im/nc/articleDetail/{daily_source_id}"
        )
        try:
            written_at = self._wait_for_written_at(
                daily_source_id, destination["cafe_id"]
            )
            if checkpoint:
                checkpoint("DAILY_DONE", daily_source_id=daily_source_id)
            revision_at = written_at + timedelta(hours=CAFE_DELAYS[job.cafe])

            revision_destination = dict(destination)
            revision_destination["start_at"] = revision_at.isoformat().replace(
                "+00:00", "Z"
            )
            revision_destination["target_view_count"] = random.SystemRandom().randint(
                80, 100
            )
            comments = self._comments(job, revision_at, destination["cafe_id"])
            revision_content_json = self._prepare_revision_content(
                job,
                revision_destination,
            )
            revision_source_id = self._create_source(
                job.title,
                strip_placeholders(job.body),
                job.tags,
                revision_destination,
                comments,
                parent_source_id=daily_source_id,
                content_json=revision_content_json,
            )
            if checkpoint:
                checkpoint(
                    "REVISION_CREATED",
                    daily_source_id=daily_source_id,
                    revision_source_id=revision_source_id,
                )
            self._verify(revision_source_id, job, revision_at)
            if checkpoint:
                checkpoint(
                    "VERIFIED",
                    daily_source_id=daily_source_id,
                    revision_source_id=revision_source_id,
                )
        except Exception:
            if revision_source_id:
                self._delete_source(revision_source_id)
            self._delete_source(daily_source_id)
            raise
        self.logger.info(
            "행 %s 수정 예약 API 검증 완료: 댓글 12개 / 조회수 %s",
            job.row_number,
            revision_destination["target_view_count"],
        )
        return f"https://v2r.daboja.im/nc/articleDetail/{revision_source_id}"
