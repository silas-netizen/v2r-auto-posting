from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
API_MIN_REQUEST_INTERVAL_SECONDS = 0.25
API_CACHE_TTL_SECONDS = 300.0
API_CACHEABLE_GET_PATHS = {
    "/navers/accounts",
    "/naver_cafes/naver_join_cafes",
    "/naver_cafes/menus",
    "/naver_cafes/heads",
}


class _ApiRequestGate:
    """Limit aggregate request bursts from all parallel browser workers."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.min_interval


_API_REQUEST_GATE = _ApiRequestGate(API_MIN_REQUEST_INTERVAL_SECONDS)
COMMENT_ACCOUNTS = (
    "quilliant",
    "hunnede",
    "prtchht",
    "chocobbn",
    "chenallo",
    "colpith",
)
CAFE_DELAYS = {
    "씨씨앙": 4,
    "양평맘": 20,
    "쌍둥이맘 모여라": 22,
}
CCCANG_CURRENT_BOARD = {"menu_id": 328, "menu_name": "자유 수다방"}
CCCANG_DAILY_HEAD = {"head_name": "일상"}
TWIN_MOMS_BOARD = {
    "menu_id": 664,
    "menu_name": "⭐가족업체 자유게시판",
}
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
    "쌍둥이맘 모여라": {
        "cafe_id": 10174516,
        "cafe_name": "쌍둥이맘 모여라",
        **TWIN_MOMS_BOARD,
    },
}


class AffiliateApiError(RuntimeError):
    pass


class AffiliateDailyPending(AffiliateApiError):
    pass


class AffiliateRunStopped(AffiliateApiError):
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


def _image_resource_ready(component: dict[str, Any]) -> bool:
    images: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("@ctype") == "image":
                images.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(component)
    return bool(images) and all(
        isinstance(image.get("src"), str)
        and bool(image["src"].strip())
        and isinstance(image.get("path"), str)
        and bool(image["path"].strip())
        and isinstance(image.get("fileName"), str)
        and bool(image["fileName"].strip())
        and int(image.get("fileSize") or 0) > 0
        for image in images
    )


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
        self._get_cache: dict[str, tuple[float, Any]] = {}
        self._get_cache_lock = threading.Lock()
        self._member_status_cache: dict[int, Any] = {}
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
        if self.browser is None:
            raise AffiliateApiError("Chrome이 열려 있지 않습니다")
        self.authorization = str(
            self.browser.capture_v2r_authorization() or ""
        )
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
        max_attempts: int = 3,
        request_timeout: float = 30,
    ) -> Any:
        url = API_ROOT + path
        if query:
            url += "?" + urlencode(query)
        if method == "GET" and path in API_CACHEABLE_GET_PATHS:
            with self._get_cache_lock:
                cached = self._get_cache.get(url)
                if cached and cached[0] > time.monotonic():
                    return deepcopy(cached[1])
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
        delays = (10, 30)
        for attempt in range(max_attempts):
            try:
                _API_REQUEST_GATE.wait()
                with urlopen(request, timeout=request_timeout) as response:
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
                        max_attempts=max_attempts,
                        request_timeout=request_timeout,
                    )
                if (
                    (exc.code == 429 or exc.code >= 500)
                    and attempt < max_attempts - 1
                ):
                    retry_after = (
                        (exc.headers or {}).get("Retry-After", "").strip()
                    )
                    delay = (
                        min(900, int(retry_after))
                        if retry_after.isdigit()
                        else delays[min(attempt, len(delays) - 1)]
                    )
                    self.logger.warning(
                        "V2R 일시 오류 %s: %s초 후 재시도 (%s/%s)",
                        exc.code,
                        delay,
                        attempt + 2,
                        max_attempts,
                    )
                    time.sleep(delay)
                    continue
                raise AffiliateApiError(
                    f"V2R 요청 실패 ({exc.code}): {path} - {detail[:300]}"
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < max_attempts - 1:
                    delay = delays[min(attempt, len(delays) - 1)]
                    self.logger.warning(
                        "네트워크 오류: %s초 후 재시도 (%s/%s)",
                        delay,
                        attempt + 2,
                        max_attempts,
                    )
                    time.sleep(delay)
                    continue
                raise AffiliateApiError(f"V2R 네트워크 요청 실패: {path}") from exc
        else:
            raise AffiliateApiError(f"V2R 요청 재시도 실패: {path}")
        result = json.loads(raw) if raw else None
        if method == "GET" and path in API_CACHEABLE_GET_PATHS:
            with self._get_cache_lock:
                self._get_cache[url] = (
                    time.monotonic() + API_CACHE_TTL_SECONDS,
                    deepcopy(result),
                )
        return result

    @staticmethod
    def _source_id_from_url(url: str) -> str:
        match = re.search(r"/nc/articleDetail/([0-9A-Za-z_-]+)", url or "")
        return match.group(1) if match else ""

    def _probe_source_url(
        self,
        url: str,
        request_timeout: float = 4,
    ) -> bool | None:
        """Return True for deleted, False for present, and None if uncertain."""
        source_id = self._source_id_from_url(url)
        if not source_id:
            return None
        try:
            self._request(
                "GET",
                "/naver_cafe_articles/article",
                query={"source_id": source_id},
                retry_auth=False,
                max_attempts=1,
                request_timeout=request_timeout,
            )
        except AffiliateApiError as exc:
            if "DELETED_NAVER_CAFE_ARTICLE_SOURCE" in str(exc):
                return True
            return None
        except Exception:
            return None
        return False

    def probe_source_urls(self, urls: set[str]) -> dict[str, bool | None]:
        """Check saved links concurrently without delaying normal API retries."""
        unique_urls = {url for url in urls if self._source_id_from_url(url)}
        if not unique_urls:
            return {}
        self._capture_authorization()
        worker_count = min(2, len(unique_urls))
        pending_urls = list(unique_urls)
        results: dict[str, bool | None] = {}
        deadline = time.monotonic() + 8
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for offset in range(0, len(pending_urls), worker_count):
                batch = pending_urls[offset : offset + worker_count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    results.update({url: None for url in batch})
                    results.update(
                        {
                            url: None
                            for url in pending_urls[offset + worker_count :]
                        }
                    )
                    break
                timeout = min(4.0, max(0.1, remaining))
                statuses = list(
                    executor.map(
                        lambda url: self._probe_source_url(url, timeout),
                        batch,
                    )
                )
                results.update(zip(batch, statuses))
                if all(status is None for status in statuses):
                    results.update(
                        {
                            url: None
                            for url in pending_urls[offset + worker_count :]
                        }
                    )
                    break
        return results

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
        if job.prefix and job.cafe != "씨씨앙":
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

    def _retarget_destination(
        self,
        destination: dict[str, Any],
        *,
        menu_id: int,
        menu_name: str,
        head_name: str | None = None,
        head_id: int | None = None,
    ) -> dict[str, Any]:
        result = dict(destination)
        result.update(
            {
                "menu_id": menu_id,
                "menu_name": menu_name,
                "head_id": None,
                "head_name": None,
            }
        )
        if not head_name:
            return result
        if head_id is not None:
            result["head_id"] = head_id
            result["head_name"] = head_name
            return result
        heads = self._request(
            "GET",
            "/naver_cafes/heads",
            query={
                "cafe_id": result["cafe_id"],
                "naver_login_id": result["naver_login_id"],
                "menu_id": menu_id,
            },
        )
        head = next(
            (
                item
                for item in _walk_dicts(heads)
                if _normalized(
                    str(self._field(item, "head_name", "headName") or "")
                )
                == _normalized(head_name)
            ),
            None,
        )
        if not head:
            raise AffiliateApiError(
                f"V2R에서 말머리를 찾지 못했습니다: {head_name}"
            )
        result["head_id"] = self._field(head, "head_id", "headId")
        result["head_name"] = self._field(head, "head_name", "headName")
        return result

    def _last_used(self, cafe_id: int) -> dict[str, str]:
        rows: list[dict[str, Any]] = []
        token: str | None = None
        # Recent usage is only a rotation hint. Limiting pagination prevents one
        # startup from scanning months of history and flooding the V2R server.
        for _ in range(5):
            query: dict[str, Any] = {
                "cafe_id": cafe_id,
                "days_ago": 30,
                "include_reserve": "true",
            }
            if token:
                query["next_token"] = token
            try:
                response = self._request(
                    "GET", "/naver_cafe_articles/board_histories", query=query
                )
            except AffiliateApiError as exc:
                if "(404)" not in str(exc):
                    raise
                self.logger.warning(
                    "V2R 최근 발행 이력 API를 사용할 수 없어 "
                    "계정 사용순서 확인을 생략합니다"
                )
                return {}
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
            and (
                bool(job.account)
                or job.account_type in {"실명", "비실명"}
            )
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
        eligible_any_by_cafe: dict[str, set[str]] = {}
        actual_type_by_account = {
            account: (
                "실명"
                if ((data.get("my_info_v2") or {}).get("is_real_name") is True)
                else "비실명"
                if ((data.get("my_info_v2") or {}).get("is_real_name") is False)
                else ""
            )
            for account, data in global_accounts.items()
        }

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
                and self._field(item, "member_key", "memberKey")
                and not item.get("force_drop")
                and not item.get("stop_cafe_member")
            }
            last_used = self._last_used(cafe_id)
            eligible_any: set[str] = set()

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
                    eligible_any.add(account)
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
            eligible_any_by_cafe[cafe_name] = eligible_any

        self.account_pools = pools
        self.account_indexes = {key: 0 for key in pools}
        self.used_accounts.update(
            job.account for job in jobs if job.account and job.status == JobStatus.PENDING
        )
        assigned: list[AffiliateJob] = []
        for job in target_jobs:
            if not job.account:
                continue
            previous = job.account
            effective_type = (
                actual_type_by_account.get(previous)
                or job.account_type
            )
            if effective_type in {"실명", "비실명"}:
                job.account_type = effective_type
            if previous in eligible_any_by_cafe.get(job.cafe, set()):
                continue
            self.blocked_accounts.add(previous)
            replacement = (
                self._pick_account(job.cafe, effective_type)
                if effective_type in {"실명", "비실명"}
                else ""
            )
            if not replacement:
                job.status = JobStatus.SKIPPED
                job.message = (
                    f"{job.cafe}에 가입 연결정보가 있는 "
                    f"{effective_type or '동일 유형'} 작성계정이 없음"
                )
                continue
            job.account = replacement
            assigned.append(job)
            self.logger.warning(
                "행 %s 가입 연결정보가 없는 지정계정 자동 교체: %s → %s",
                job.row_number,
                previous,
                replacement,
            )
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

    @classmethod
    def _account_rows(cls, payload: Any) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for item in _walk_dicts(payload):
            account = str(
                cls._field(item, "login_id", "naver_login_id", "loginId") or ""
            )
            if account and (
                "level_info" in item
                or "levelInfo" in item
                or cls._field(item, "member_key", "memberKey")
            ):
                rows[account] = item
        return rows

    @staticmethod
    def _member_grade_ready(item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        level = item.get("level_info") or item.get("levelInfo")
        if not isinstance(level, dict):
            return False
        label = str(
            level.get("member_level_name")
            or level.get("memberLevelName")
            or level.get("member_level_icon_url")
            or level.get("memberLevelIconUrl")
            or ""
        ).strip()
        member_level = level.get("member_level") or level.get("memberLevel")
        return bool(
            label
            or (
                isinstance(member_level, int)
                and member_level > 1
            )
        )

    def refresh_assigned_account_grades(
        self,
        jobs: list[Any],
    ) -> list[Any]:
        """Refresh missing member grades once per cafe/account before writing."""
        self._capture_authorization()
        pending_by_cafe: dict[tuple[str, int], list[Any]] = {}
        for job in jobs:
            if (
                job.status != JobStatus.PENDING
                or not job.account
                or getattr(job, "source_kind", "") == "account_test"
            ):
                continue
            config = CAFE_DESTINATIONS.get(job.cafe) or {}
            cafe_id = int(
                getattr(job, "cafe_id", 0)
                or config.get("cafe_id")
                or 0
            )
            if cafe_id:
                pending_by_cafe.setdefault((job.cafe, cafe_id), []).append(job)

        failed_jobs: list[Any] = []
        for (cafe_name, cafe_id), cafe_jobs in pending_by_cafe.items():
            assigned_accounts = {job.account for job in cafe_jobs}
            status = self._request(
                "GET",
                "/naver_cafes/naver_join_cafe",
                query={"cafe_id": cafe_id},
            )
            rows = self._account_rows(status)
            missing = sorted(
                account
                for account in assigned_accounts
                if not self._member_grade_ready(rows.get(account))
            )
            if not missing:
                self.logger.info(
                    "%s 발행계정 멤버등급 확인 완료: %s개",
                    cafe_name,
                    len(assigned_accounts),
                )
                continue

            self.logger.info(
                "%s 멤버등급 미확인 계정 API 갱신 시작: %s개",
                cafe_name,
                len(missing),
            )
            refresh_errors: dict[str, str] = {}
            for account in missing:
                try:
                    self._request(
                        "PUT",
                        "/naver_cafes/naver_join_cafe/sync/account",
                        {
                            "cafe_id": cafe_id,
                            "naver_login_id": account,
                        },
                    )
                    self.logger.info(
                        "%s 계정 멤버등급 API 갱신 요청 완료: %s",
                        cafe_name,
                        account,
                    )
                except Exception as exc:
                    refresh_errors[account] = str(exc)
                    self.logger.warning(
                        "%s 계정 멤버등급 API 갱신 실패: %s / %s",
                        cafe_name,
                        account,
                        exc,
                    )

            unresolved = set(missing) - set(refresh_errors)
            for attempt in range(3):
                if not unresolved:
                    break
                refreshed = self._request(
                    "GET",
                    "/naver_cafes/naver_join_cafe",
                    query={"cafe_id": cafe_id},
                )
                refreshed_rows = self._account_rows(refreshed)
                unresolved = {
                    account
                    for account in unresolved
                    if not self._member_grade_ready(refreshed_rows.get(account))
                }
                if unresolved and attempt < 2:
                    time.sleep(1)

            for account in unresolved:
                refresh_errors[account] = "갱신 후에도 멤버등급 정보가 표시되지 않음"

            for job in cafe_jobs:
                error = refresh_errors.get(job.account)
                if not error:
                    continue
                job.status = JobStatus.FAILED
                job.message = f"계정 멤버등급 갱신 실패: {job.account} / {error[:150]}"
                failed_jobs.append(job)
                self.logger.error(
                    "행 %s 발행 전 계정 멤버등급 확인 실패: %s",
                    job.row_number,
                    job.message,
                )

            refreshed_count = len(missing) - len(refresh_errors)
            self.logger.info(
                "%s 멤버등급 사전 갱신 완료: 성공 %s / 실패 %s",
                cafe_name,
                refreshed_count,
                len(refresh_errors),
            )
        return failed_jobs

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

    def reset_deleted_sources(
        self,
        job: AffiliateJob,
        resume: dict[str, Any],
    ) -> None:
        source_ids = {
            str(resume.get("daily_source_id") or ""),
            str(resume.get("revision_source_id") or ""),
        }
        for source_id in source_ids - {""}:
            self._delete_source(source_id)
        resume.clear()
        resume.update(
            {
                "stage": "ACCOUNT_ASSIGNED",
                "account": job.account,
                "daily_source_id": "",
                "daily_scheduled_at": "",
                "revision_source_id": "",
            }
        )
        job.daily_scheduled_at = None
        job.daily_written_at = None

    @staticmethod
    def classify_failure(error: Exception) -> tuple[str, bool]:
        text = str(error)
        if "NOT_FOUND_MODEL" in text and "NaverJoinCafeAccoun" in text:
            return "실패: 카페 가입 연결정보 없음", True
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
            try:
                history = self._request(
                    "GET",
                    "/naver_cafe_articles/board_histories",
                    query={
                        "cafe_id": config["cafe_id"],
                        "days_ago": 1,
                        "include_reserve": "true",
                    },
                )
            except AffiliateApiError as exc:
                if "(404)" not in str(exc):
                    raise
                self.logger.warning(
                    "%s V2R 이력 API를 사용할 수 없어 "
                    "실패 찌꺼기 사전 정리를 생략합니다",
                    cafe_name,
                )
                continue
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
        cafe = self._member_status_cache.get(cafe_id)
        if cafe is None:
            cafe = self._request(
                "GET",
                "/naver_cafes/naver_join_cafe",
                query={"cafe_id": cafe_id},
            )
            self._member_status_cache[cafe_id] = cafe
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
    def _write_options(*, enable_comment: bool = True) -> dict[str, Any]:
        return {
            "enableComment": enable_comment,
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
        enable_comment: bool = True,
    ) -> str:
        payload: dict[str, Any] = {
            "tag_list": tags,
            "title": title,
            "content_json": content_json or _content_json(body),
            "cafe_write_options": self._write_options(
                enable_comment=enable_comment
            ),
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
            try:
                history = self._request(
                    "GET",
                    "/naver_cafe_articles/board_histories",
                    query={
                        "cafe_id": cafe_id,
                        "days_ago": 1,
                        "include_reserve": "true",
                    },
                )
            except AffiliateApiError as exc:
                if "(404)" in str(exc):
                    return None
                raise
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

    def _wait_for_written_at(
        self,
        source_id: str,
        cafe_id: int,
        expected_start_at: datetime | None = None,
        wait_control=None,
    ) -> datetime:
        wait_seconds = 90.0
        if expected_start_at is not None:
            remaining = (
                expected_start_at - datetime.now(timezone.utc)
            ).total_seconds()
            wait_seconds = max(wait_seconds, remaining + 30 * 60)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if wait_control:
                wait_control()
            try:
                detail = self._request(
                    "GET",
                    "/naver_cafe_articles/article",
                    query={"source_id": source_id},
                )
            except AffiliateApiError as exc:
                if "(404)" in str(exc):
                    time.sleep(2)
                    continue
                raise
            source = detail.get("naver_cafe_article_source") or {}
            destination = detail.get("naver_cafe_article_destination") or {}
            article_history = detail.get("naver_cafe_article_history") or {}
            history_status = article_history.get("status")
            status = destination.get("status") or source.get("status")
            written_raw = (
                article_history.get("written_at")
                or source.get("written_at")
                or destination.get("written_at")
                or source.get("created_at")
            )
            if history_status in {"DONE", "SUCCESS"}:
                if written_raw:
                    return datetime.fromisoformat(
                        str(written_raw).replace("Z", "+00:00")
                    )
                return datetime.now(timezone.utc)
            if history_status == "FAIL" or status == "FAIL":
                reason = str(
                    article_history.get("fail_reason")
                    or article_history.get("reason")
                    or destination.get("fail_reason")
                    or source.get("fail_reason")
                    or "원인 불명"
                )
                raise AffiliateApiError(f"일상 글 발행 실패: {reason}")
            if not article_history and status == "DONE":
                return datetime.now(timezone.utc)
            time.sleep(2)
        raise AffiliateDailyPending(
            "일상 글이 예약시간 이후 30분 동안 예약대기 상태입니다. "
            "예약은 유지하며 다음 실행에서 다시 확인합니다"
        )

    @staticmethod
    def _comment_permission(detail: dict[str, Any]) -> bool | None:
        destination = detail.get("naver_cafe_article_destination") or {}
        source = detail.get("naver_cafe_article_source") or {}
        options = (
            destination.get("write_options")
            or destination.get("cafe_write_options")
            or source.get("write_options")
            or source.get("cafe_write_options")
            or {}
        )
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except json.JSONDecodeError:
                return None
        if not isinstance(options, dict):
            return None
        value = options.get("enableComment")
        if value is None:
            value = options.get("enable_comment")
        return value if isinstance(value, bool) else None

    def _verify_comment_permission(
        self,
        source_id: str,
        expected: bool,
    ) -> None:
        detail = self._request(
            "GET",
            "/naver_cafe_articles/article",
            query={"source_id": source_id},
        )
        actual = self._comment_permission(detail)
        if actual is not expected:
            raise AffiliateApiError(
                "등록 후 댓글 허용 설정 검증에 실패했습니다: "
                f"기대 {expected} / 실제 {actual}"
            )

    def _verify_destination_settings(
        self,
        source_id: str,
        *,
        expected_menu_id: int,
        expected_head_id: int | None,
        expected_comment: bool,
    ) -> None:
        detail = self._request(
            "GET",
            "/naver_cafe_articles/article",
            query={"source_id": source_id},
        )
        destination = detail.get("naver_cafe_article_destination") or {}
        actual_menu_id = destination.get("menu_id")
        actual_head_id = destination.get("head_id")
        if int(actual_menu_id or 0) != expected_menu_id:
            raise AffiliateApiError(
                "등록 후 게시판 검증에 실패했습니다: "
                f"기대 {expected_menu_id} / 실제 {actual_menu_id}"
            )
        if (
            int(actual_head_id or 0)
            != int(expected_head_id or 0)
        ):
            raise AffiliateApiError(
                "등록 후 말머리 검증에 실패했습니다: "
                f"기대 {expected_head_id} / 실제 {actual_head_id}"
            )
        actual_comment = self._comment_permission(detail)
        if actual_comment is not expected_comment:
            raise AffiliateApiError(
                "등록 후 댓글 허용 설정 검증에 실패했습니다: "
                f"기대 {expected_comment} / 실제 {actual_comment}"
            )

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
                raise AffiliateApiError(
                    "사진 표시가 있지만 이미지 브랜드 또는 Drive 설정을 "
                    "확인하지 못해 발행을 중단했습니다"
                )
            return _content_json(clean_body)

        if getattr(job, "prepared_images", None):
            resolved = list(getattr(job, "prepared_images", []))
        else:
            try:
                resolved = self.image_resolver.resolve(job)
            except Exception as exc:
                raise AffiliateApiError(
                    "Google Drive 이미지 확인 실패로 사진 없는 글 등록을 "
                    f"중단했습니다: {exc}"
                ) from exc
            job.prepared_images = list(resolved)
        if not resolved:
            if PLACEHOLDER_PATTERN.search(job.body):
                raise AffiliateApiError(
                    "본문에 사진 표시가 있지만 사용할 이미지를 찾지 못해 "
                    "발행을 중단했습니다"
                )
            return _content_json(clean_body)
        try:
            uploaded = self.browser.upload_affiliate_images(
                job,
                destination,
                [item.local_path for item in resolved],
            )
        except Exception as exc:
            raise AffiliateApiError(
                f"사진 첨부에 실패하여 사진 없는 글 등록을 중단했습니다: {exc}"
            ) from exc

        failed = [
            item.file_name
            for item, component in zip(resolved, uploaded)
            if not component
        ]
        if len(uploaded) < len(resolved):
            failed.extend(item.file_name for item in resolved[len(uploaded) :])
        if failed:
            raise AffiliateApiError(
                "사진 첨부에 실패하여 사진 없는 글 등록을 중단했습니다: "
                + ", ".join(failed)
            )

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

    def _verify(
        self,
        source_id: str,
        job: AffiliateJob,
        start_at: datetime,
        *,
        expected_menu_id: int,
        expected_head_id: int | None,
    ) -> None:
        detail = self._request(
            "GET", "/naver_cafe_articles/article", query={"source_id": source_id}
        )
        source = detail["naver_cafe_article_source"]
        destination = detail["naver_cafe_article_destination"]
        if self._comment_permission(detail) is not True:
            raise AffiliateApiError(
                "등록 후 수정 글 댓글 허용 설정 검증에 실패했습니다"
            )
        if int(destination.get("menu_id") or 0) != expected_menu_id:
            raise AffiliateApiError("등록 후 수정 글 게시판 검증에 실패했습니다")
        if int(destination.get("head_id") or 0) != int(expected_head_id or 0):
            raise AffiliateApiError("등록 후 수정 글 말머리 검증에 실패했습니다")
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
        media_components = [
            component
            for component in document["document"]["components"]
            if component.get("@ctype") in {"image", "imageGroup", "imageStrip"}
        ]
        media_count = len(media_components)
        if job.prepared_image_count and media_count < job.prepared_image_count:
            raise AffiliateApiError("등록 후 본문 이미지 개수 검증에 실패했습니다")
        if job.prepared_image_count and not all(
            _image_resource_ready(component)
            for component in media_components
        ):
            raise AffiliateApiError(
                "등록 후 본문 사진 주소 또는 파일 정보 검증에 실패했습니다"
            )
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
        daily_only: bool = False,
        wait_control=None,
    ) -> str:
        if job.daily_post is None:
            raise AffiliateApiError("배정된 일상 글이 없습니다")
        self._capture_authorization()
        destination = self._resolve_destination(job)
        daily_destination_template = dict(destination)
        revision_destination_template = dict(destination)
        if job.cafe == "씨씨앙":
            daily_destination_template = self._retarget_destination(
                destination,
                menu_id=int(CCCANG_CURRENT_BOARD["menu_id"]),
                menu_name=str(CCCANG_CURRENT_BOARD["menu_name"]),
                head_name=str(CCCANG_DAILY_HEAD["head_name"]),
            )
            revision_destination_template = self._retarget_destination(
                destination,
                menu_id=int(CCCANG_CURRENT_BOARD["menu_id"]),
                menu_name=str(CCCANG_CURRENT_BOARD["menu_name"]),
                head_name=None,
            )
        if dry_run:
            self.logger.info(
                "행 %s API 검증 완료: %s / 일상 %s (%s) / 수정 %s (말머리 없음)",
                job.row_number,
                destination["cafe_name"],
                daily_destination_template["menu_name"],
                daily_destination_template["head_name"] or "말머리 없음",
                revision_destination_template["menu_name"],
            )
            return ""

        resume = resume or {}
        if job.daily_scheduled_at is None and resume.get("daily_scheduled_at"):
            job.daily_scheduled_at = datetime.fromisoformat(
                str(resume["daily_scheduled_at"]).replace("Z", "+00:00")
            )
        if job.daily_scheduled_at is None:
            job.daily_scheduled_at = datetime.now(timezone.utc) + timedelta(
                minutes=random.SystemRandom().randint(5, 15)
            )
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
            self._verify(
                revision_source_id,
                job,
                revision_at,
                expected_menu_id=int(revision_destination_template["menu_id"]),
                expected_head_id=(
                    int(revision_destination_template["head_id"])
                    if revision_destination_template.get("head_id") is not None
                    else None
                ),
            )
            if checkpoint:
                checkpoint("VERIFIED", revision_source_id=revision_source_id)
            return f"https://v2r.daboja.im/nc/articleDetail/{revision_source_id}"

        revision_at: datetime | None = None
        revision_destination: dict[str, Any] | None = None
        revision_content_json: str | None = None
        if not daily_only:
            if wait_control:
                wait_control()
            revision_at = job.daily_scheduled_at + timedelta(
                hours=CAFE_DELAYS[job.cafe]
            )
            revision_destination = dict(revision_destination_template)
            revision_destination["start_at"] = revision_at.isoformat().replace(
                "+00:00", "Z"
            )
            revision_destination["target_view_count"] = (
                random.SystemRandom().randint(80, 100)
            )
            # Images must be completely prepared before creating either V2R
            # reservation. A browser-side failure must not consume or orphan a
            # daily reservation.
            revision_content_json = self._prepare_revision_content(
                job,
                revision_destination,
            )

        daily_source_id = str(resume.get("daily_source_id") or "")
        if not daily_source_id:
            daily_destination = dict(daily_destination_template)
            daily_destination["start_at"] = (
                job.daily_scheduled_at.isoformat().replace("+00:00", "Z")
            )
            daily_source_id = self._create_source(
                job.daily_post.title,
                job.daily_post.body,
                [],
                daily_destination,
                [],
                recovery_statuses=("RESERVED", "DONE"),
                enable_comment=True,
            )
            if checkpoint:
                checkpoint(
                    "DAILY_CREATED",
                    daily_source_id=daily_source_id,
                    daily_scheduled_at=(
                        job.daily_scheduled_at.isoformat()
                        .replace("+00:00", "Z")
                    ),
                )
            self.logger.info(
                "행 %s 제휴 일상 글 예약: %s / 댓글 허용 %s",
                job.row_number,
                job.daily_scheduled_at.astimezone().strftime(
                    "%Y-%m-%d %H:%M"
                ),
                True,
            )
        job.daily_post_url = (
            f"https://v2r.daboja.im/nc/articleDetail/{daily_source_id}"
        )
        try:
            self._verify_destination_settings(
                daily_source_id,
                expected_menu_id=int(daily_destination_template["menu_id"]),
                expected_head_id=(
                    int(daily_destination_template["head_id"])
                    if daily_destination_template.get("head_id") is not None
                    else None
                ),
                expected_comment=True,
            )
            if daily_only:
                return (
                    "https://v2r.daboja.im/nc/articleDetail/"
                    f"{daily_source_id}"
                )
            assert revision_at is not None
            assert revision_destination is not None
            assert revision_content_json is not None
            comments = self._comments(job, revision_at, destination["cafe_id"])
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
            self._verify(
                revision_source_id,
                job,
                revision_at,
                expected_menu_id=int(revision_destination["menu_id"]),
                expected_head_id=(
                    int(revision_destination["head_id"])
                    if revision_destination.get("head_id") is not None
                    else None
                ),
            )
            if checkpoint:
                checkpoint(
                    "VERIFIED",
                    daily_source_id=daily_source_id,
                    revision_source_id=revision_source_id,
                )
        except (AffiliateDailyPending, AffiliateRunStopped):
            raise
        except Exception:
            if revision_source_id:
                self._delete_source(revision_source_id)
            self._delete_source(daily_source_id)
            if checkpoint:
                checkpoint(
                    "ACCOUNT_ASSIGNED",
                    daily_source_id="",
                    daily_scheduled_at="",
                    revision_source_id="",
                )
            job.daily_scheduled_at = None
            job.daily_written_at = None
            raise
        self.logger.info(
            "행 %s 수정 예약 API 검증 완료: 댓글 12개 / 조회수 %s",
            job.row_number,
            revision_destination["target_view_count"],
        )
        return f"https://v2r.daboja.im/nc/articleDetail/{revision_source_id}"
