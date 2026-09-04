from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .affiliate_api import (
    COMMENT_ACCOUNTS,
    AffiliateApiError,
    AffiliateApiPublisher,
    _content_json,
    _image_resource_ready,
    _walk_dicts,
)
from .cafe_catalog import (
    CafeCatalogEntry,
    CafeMenu,
    CatalogMatchError,
    SELF_OWNED_CAFE_IDS,
    TEST_CAFE_IDS,
    match_catalog_name,
    normalized_name,
)
from .images import PLACEHOLDER_PATTERN, strip_placeholders
from .models import ImmediateJob, JobStatus
from .restrictions import AccountRestrictionStore


SELF_COMMENT_ACCOUNTS = (
    "repalbass",
    "jeehashed",
    "maritane",
    "skabakc",
    "uatrayb",
    "rowpalse",
)
ALL_COMMENT_ACCOUNTS = set(COMMENT_ACCOUNTS) | set(SELF_COMMENT_ACCOUNTS)
MANAGER_ACCOUNTS = {"redsagua01", "clktrade"}
BOARD_ALIASES = {
    (10174516, normalized_name("가족업체 자유게시판")): "ㄴ가족업체 자유게시판",
    (26680163, "웨딩홀탐방기"): "웨딩홀탑방기",
}
KNOWN_CAFE_IDS = {
    normalized_name("쌍둥이맘 모여라"): 10174516,
    normalized_name("고요한 아침"): 14567700,
    normalized_name("글로시 마이"): 15175096,
    normalized_name("웨딩노트"): 15441090,
    normalized_name("웨딩 노트"): 15441090,
    normalized_name("송도포털"): 16149995,
    normalized_name("헬씨트리"): 23708088,
    normalized_name("헬씨 트리"): 23708088,
    normalized_name("러브인썸"): 26616683,
    normalized_name("러브 인썸 (Love in Some)"): 26616683,
    normalized_name("마이웨딩드림"): 26680163,
    normalized_name("마이 웨딩 드림"): 26680163,
}


class ImmediateApiPublisher(AffiliateApiPublisher):
    """Publish self-owned-cafe articles immediately through V2R's API."""

    def __init__(self, browser, logger):
        super().__init__(browser, logger)
        self.menu_pools: dict[tuple[int, int], list[str]] = {}
        self.cafe_pools: dict[int, list[str]] = {}
        self.auto_selected_pools: dict[tuple[int, str], list[str]] = {}
        self.pool_indexes: dict[tuple[int, str], int] = {}
        self.global_accounts: dict[str, dict[str, Any]] = {}
        data_dir = (
            self.browser.config.download_dir.parent
            if self.browser is not None and getattr(self.browser, "config", None)
            else Path("/tmp") / f"v2r-immediate-{id(self)}"
        )
        self.restrictions = AccountRestrictionStore(
            data_dir / "restricted-accounts.json",
            logger,
        )
        self.last_restricted_account = ""
        self.auto_account_limit = 10

    def _restriction_result(self, account: str) -> str:
        record = self.restrictions.account_record(account)
        if not record:
            return "코드 27000 활동 제한"
        detected = datetime.fromisoformat(record["detected_at"])
        blocked_until = datetime.fromisoformat(record["blocked_until"])
        return (
            "코드 27000 활동 제한 "
            f"(발견 {detected.astimezone().strftime('%Y-%m-%d')} / "
            f"제외 종료 {blocked_until.astimezone().strftime('%Y-%m-%d')})"
        )

    @staticmethod
    def _cafe_rows(payload: Any) -> list[CafeCatalogEntry]:
        rows: list[CafeCatalogEntry] = []
        seen: set[int] = set()
        for item in _walk_dicts(payload):
            cafe_id = item.get("cafe_id") or item.get("cafeId")
            name = (
                item.get("pc_cafe_name")
                or item.get("cafe_name")
                or item.get("cafeName")
                or item.get("mobile_cafe_name")
            )
            if not cafe_id or not name or int(cafe_id) in seen:
                continue
            seen.add(int(cafe_id))
            rows.append(
                CafeCatalogEntry(
                    cafe_id=int(cafe_id),
                    name=str(name),
                    category="",
                )
            )
        return rows

    @staticmethod
    def _joined_accounts(payload: Any) -> dict[str, dict[str, Any]]:
        joined: dict[str, dict[str, Any]] = {}
        for item in _walk_dicts(payload):
            account = str(item.get("login_id") or item.get("naver_login_id") or "")
            if (
                account
                and not item.get("force_drop")
                and not item.get("stop_cafe_member")
            ):
                joined[account] = item
        return joined

    @staticmethod
    def _menu_rows(payload: Any) -> list[CafeMenu]:
        menus: list[CafeMenu] = []
        seen: set[int] = set()
        for item in _walk_dicts(payload):
            menu_id = item.get("menuId") or item.get("menu_id")
            name = item.get("menuName") or item.get("menu_name")
            if (
                not menu_id
                or not name
                or item.get("writable") is False
                or int(menu_id) in seen
            ):
                continue
            seen.add(int(menu_id))
            menus.append(
                CafeMenu(
                    menu_id=int(menu_id),
                    name=str(name),
                )
            )
        return menus

    def _eligible_accounts(
        self,
        cafe_id: int,
    ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        status = self._request(
            "GET",
            "/naver_cafes/naver_join_cafe",
            query={"cafe_id": cafe_id},
        )
        joined = self._joined_accounts(status)
        restricted = self.restrictions.blocked_accounts()
        eligible = [
            account
            for account in joined
            if (
                joined[account].get("member_key")
                or joined[account].get("memberKey")
            )
            if account not in ALL_COMMENT_ACCOUNTS
            and account not in MANAGER_ACCOUNTS
            and account not in restricted
            and account in self.global_accounts
            and not self.global_accounts[account].get("is_block")
            and not self.global_accounts[account].get("is_login_fail")
        ]
        last_used = self._last_used(cafe_id)
        eligible.sort(
            key=lambda account: (
                account in last_used,
                last_used.get(account, ""),
                account,
            )
        )
        return eligible, joined

    def _load_menus(
        self,
        cafe_id: int,
        accounts: list[str],
    ) -> tuple[list[CafeMenu], list[str]]:
        healthy: list[str] = []
        menu_map: dict[int, CafeMenu] = {}
        for account in accounts:
            try:
                response = self._request(
                    "GET",
                    "/naver_cafes/menus",
                    query={
                        "cafe_id": cafe_id,
                        "naver_login_id": account,
                    },
                )
            except Exception as exc:
                reason = str(exc)
                if (
                    "NOT_LOGIN" in reason
                    or "session not found" in reason
                    or "SAFETY_RELEASE" in reason
                ):
                    self.logger.warning(
                        "%s 계정은 로그인/보호조치 문제로 작성목록에서 제외: %s",
                        account,
                        reason[:120],
                    )
                    continue
                raise
            healthy.append(account)
            for menu in self._menu_rows(response):
                item = menu_map.setdefault(
                    menu.menu_id,
                    CafeMenu(menu_id=menu.menu_id, name=menu.name),
                )
                if account not in item.writable_accounts:
                    item.writable_accounts.append(account)
        return list(menu_map.values()), healthy

    def _prepare_account_tests(
        self,
        jobs: list[ImmediateJob],
        cafes: list[CafeCatalogEntry],
    ) -> None:
        test_cafes = [
            cafe for cafe in cafes if cafe.cafe_id in TEST_CAFE_IDS
        ]
        active_members: dict[int, set[str]] = {}
        unavailable_members: set[str] = set()
        for cafe in test_cafes:
            response = self._request(
                "GET",
                "/naver_cafes/naver_join_cafe",
                query={"cafe_id": cafe.cafe_id},
            )
            active_members[cafe.cafe_id] = set(
                self._joined_accounts(response)
            )
            for item in _walk_dicts(response):
                account = str(
                    item.get("login_id") or item.get("naver_login_id") or ""
                )
                if account and (
                    item.get("force_drop") or item.get("stop_cafe_member")
                ):
                    unavailable_members.add(account)

        menu_cache: dict[tuple[int, str], list[CafeMenu]] = {}
        both_index = 0
        for job in jobs:
            if (
                job.status != JobStatus.PENDING
                or job.source_kind != "account_test"
            ):
                continue
            if self.restrictions.account_record(job.account):
                job.status = JobStatus.FAILED
                job.message = self._restriction_result(job.account)
                continue
            global_account = self.global_accounts.get(job.account)
            if not global_account:
                job.status = JobStatus.FAILED
                job.message = "V2R 미등록 계정"
                continue
            if global_account.get("is_login_fail"):
                job.status = JobStatus.FAILED
                job.message = "네이버 로그인 실패"
                continue
            if global_account.get("is_block"):
                job.status = JobStatus.FAILED
                job.message = "V2R 계정 차단"
                continue
            joined = [
                cafe
                for cafe in test_cafes
                if job.account in active_members.get(cafe.cafe_id, set())
            ]
            if not joined:
                job.status = JobStatus.FAILED
                job.message = (
                    "활동정지·탈퇴"
                    if job.account in unavailable_members
                    else "테스트 카페 미가입"
                )
                continue
            if len(joined) > 1:
                cafe = joined[both_index % len(joined)]
                both_index += 1
            else:
                cafe = joined[0]
            cache_key = (cafe.cafe_id, job.account)
            try:
                menus = menu_cache.get(cache_key)
                if menus is None:
                    response = self._request(
                        "GET",
                        "/naver_cafes/menus",
                        query={
                            "cafe_id": cafe.cafe_id,
                            "naver_login_id": job.account,
                        },
                    )
                    menus = self._menu_rows(response)
                    menu_cache[cache_key] = menus
                menu = match_catalog_name(
                    "자유게시판",
                    menus,
                    label="게시판",
                )
            except Exception as exc:
                reason = str(exc)
                job.status = JobStatus.FAILED
                job.message = (
                    "네이버 로그인 실패"
                    if "NOT_LOGIN" in reason or "NAVER_LOGIN_FAIL" in reason
                    else f"테스트 준비 실패: {reason[:120]}"
                )
                continue
            job.cafe = cafe.name
            job.cafe_id = cafe.cafe_id
            job.menu_id = menu.menu_id
            job.canonical_cafe_name = cafe.name
            job.canonical_board_name = menu.name
            self.logger.debug(
                "행 %s 한줄테스트 준비: %s / %s / %s",
                job.row_number,
                cafe.name,
                menu.name,
                job.account,
            )

    def prepare_jobs(
        self,
        jobs: list[ImmediateJob],
        *,
        auto_account_limit: int = 10,
    ) -> None:
        if not 2 <= auto_account_limit <= 10:
            raise ValueError("자동 배정 ID 수는 2개부터 10개까지 선택하세요")
        self.auto_account_limit = auto_account_limit
        self._capture_authorization()
        cafe_payload = self._request("GET", "/naver_cafes/naver_join_cafes")
        cafes = self._cafe_rows(cafe_payload)
        global_payload = self._request("GET", "/navers/accounts")
        self.global_accounts = {
            str(item["naver_login_id"]): item
            for item in _walk_dicts(global_payload)
            if item.get("naver_login_id")
        }
        self._prepare_account_tests(jobs, cafes)

        by_wanted: dict[str, list[ImmediateJob]] = {}
        for job in jobs:
            if (
                job.status == JobStatus.PENDING
                and job.source_kind != "account_test"
            ):
                by_wanted.setdefault(job.cafe, []).append(job)

        for wanted, cafe_jobs in by_wanted.items():
            known_cafe_id = KNOWN_CAFE_IDS.get(normalized_name(wanted))
            cafe = next(
                (
                    candidate
                    for candidate in cafes
                    if known_cafe_id is not None
                    and candidate.cafe_id == known_cafe_id
                ),
                None,
            )
            if cafe is None:
                cafe = match_catalog_name(wanted, cafes, label="카페")
            if cafe.cafe_id not in SELF_OWNED_CAFE_IDS | TEST_CAFE_IDS:
                for job in cafe_jobs:
                    job.status = JobStatus.SKIPPED
                    job.message = "자사 카페 허용 목록에 등록되지 않은 카페입니다"
                continue
            if any(job.source_kind == "brand" for job in cafe_jobs) and (
                cafe.cafe_id not in SELF_OWNED_CAFE_IDS
            ):
                for job in cafe_jobs:
                    if job.source_kind == "brand":
                        job.status = JobStatus.SKIPPED
                        job.message = "브랜드 원고는 자사 카페에만 발행할 수 있습니다"

            eligible, _joined = self._eligible_accounts(cafe.cafe_id)
            menus, healthy = self._load_menus(cafe.cafe_id, eligible)
            if not healthy:
                raise AffiliateApiError(
                    f"{cafe.name}에 로그인 가능한 작성계정이 없습니다"
                )
            if not menus:
                raise AffiliateApiError(f"{cafe.name} 게시판 목록이 비어 있습니다")
            self.cafe_pools[cafe.cafe_id] = list(healthy)
            for menu in menus:
                self.menu_pools[(cafe.cafe_id, menu.menu_id)] = list(
                    menu.writable_accounts
                )

            for job in cafe_jobs:
                if job.status != JobStatus.PENDING:
                    continue
                input_cafe_id = job.cafe_id
                input_menu_id = job.menu_id
                if input_cafe_id and input_cafe_id != cafe.cafe_id:
                    job.status = JobStatus.FAILED
                    job.message = (
                        "게시판링크의 카페 ID가 카페명과 다릅니다: "
                        f"{input_cafe_id} != {cafe.cafe_id}"
                    )
                    continue
                job.cafe_id = cafe.cafe_id
                wanted_board = BOARD_ALIASES.get(
                    (cafe.cafe_id, normalized_name(job.board)),
                    job.board,
                )
                try:
                    menu = (
                        next(
                            (
                                candidate
                                for candidate in menus
                                if candidate.menu_id == input_menu_id
                            ),
                            None,
                        )
                        if input_menu_id
                        else match_catalog_name(
                            wanted_board,
                            menus,
                            label="게시판",
                        )
                    )
                    if menu is None:
                        raise CatalogMatchError(
                            "게시판링크의 게시판 ID를 사용할 수 있는 작성계정이 "
                            f"없습니다: {input_menu_id} "
                            "(카페 등급·게시판 작성 권한 확인 필요)"
                        )
                except CatalogMatchError as exc:
                    job.status = JobStatus.FAILED
                    job.message = str(exc)
                    self.logger.error(
                        "행 %s 게시판 매칭 실패: %s",
                        job.row_number,
                        exc,
                    )
                    continue
                job.menu_id = menu.menu_id
                job.canonical_cafe_name = cafe.name
                job.canonical_board_name = menu.name

            self._prepare_auto_account_pools(cafe_jobs, healthy)
            for job in cafe_jobs:
                if job.status != JobStatus.PENDING:
                    continue
                menu_pool = self.menu_pools.get((job.cafe_id, job.menu_id), [])
                if job.account:
                    account_info = self.global_accounts.get(job.account) or {}
                    actual_real_name = (
                        account_info.get("my_info_v2") or {}
                    ).get("is_real_name")
                    if actual_real_name is True:
                        job.account_type = "실명"
                    elif actual_real_name is False:
                        job.account_type = "비실명"
                    if job.account not in menu_pool:
                        previous = job.account
                        self.blocked_accounts.add(previous)
                        job.account = ""
                        replacement = self.pick_account(job)
                        if not replacement:
                            job.status = JobStatus.FAILED
                            job.message = (
                                "가입 연결정보와 게시판 권한이 있는 "
                                f"대체 작성계정이 없습니다: {previous}"
                            )
                            continue
                        job.account = replacement
                        self.logger.warning(
                            "행 %s 가입 또는 게시판 권한이 없는 지정계정 "
                            "자동 교체: %s → %s",
                            job.row_number,
                            previous,
                            replacement,
                        )
                else:
                    job.account = self.pick_account(job)
                    if not job.account:
                        job.status = JobStatus.FAILED
                        job.message = "조건에 맞는 작성계정이 없습니다"
                        continue
                self._resolve_head(job)
                self.logger.debug(
                    "행 %s API 목적지: %s(%s) / %s(%s) / %s",
                    job.row_number,
                    job.canonical_cafe_name,
                    job.cafe_id,
                    job.canonical_board_name,
                    job.menu_id,
                    job.account,
                )
            prepared = [
                job
                for job in cafe_jobs
                if job.status == JobStatus.PENDING and job.cafe_id == cafe.cafe_id
            ]
            self.logger.info(
                "%s 목적지 준비 완료: 원고 %s건 / 게시판 %s개 / 작성계정 %s개",
                cafe.name,
                len(prepared),
                len({job.menu_id for job in prepared}),
                len({job.account for job in prepared}),
            )

    def _inspect_saved_source_url(
        self,
        url: str,
        request_timeout: float,
    ) -> dict[str, str]:
        source_id = self._source_id_from_url(url)
        if not source_id:
            return {"state": "unknown", "reason": "올바른 V2R 링크가 아님"}
        try:
            detail = self._request(
                "GET",
                "/naver_cafe_articles/article",
                query={"source_id": source_id},
                retry_auth=False,
                max_attempts=1,
                request_timeout=request_timeout,
            )
        except AffiliateApiError as exc:
            if "DELETED_NAVER_CAFE_ARTICLE_SOURCE" in str(exc):
                return {"state": "deleted", "reason": str(exc)}
            return {"state": "unknown", "reason": str(exc)}
        except Exception as exc:
            return {"state": "unknown", "reason": str(exc)}

        history = detail.get("naver_cafe_article_history") or {}
        destination = detail.get("naver_cafe_article_destination") or {}
        state = str(history.get("status") or destination.get("status") or "")
        reason = str(
            history.get("fail_reason")
            or history.get("reason")
            or destination.get("fail_reason")
            or ""
        )
        account = str(
            history.get("naver_account_login_id")
            or destination.get("naver_login_id")
            or ""
        )
        return {
            "state": "failed" if state == "FAIL" else "present",
            "reason": reason,
            "account": account,
            "source_id": source_id,
        }

    def inspect_saved_source_urls(
        self,
        urls: set[str],
    ) -> dict[str, dict[str, str]]:
        """Inspect only locally saved sources, never a cafe's global history."""
        unique_urls = {url for url in urls if self._source_id_from_url(url)}
        if not unique_urls:
            return {}
        self._capture_authorization()
        worker_count = min(6, len(unique_urls))
        pending_urls = list(unique_urls)
        results: dict[str, dict[str, str]] = {}
        deadline = time.monotonic() + 8
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for offset in range(0, len(pending_urls), worker_count):
                batch = pending_urls[offset : offset + worker_count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    results.update(
                        {url: {"state": "unknown"} for url in pending_urls[offset:]}
                    )
                    break
                timeout = min(4.0, max(0.1, remaining))
                states = list(
                    executor.map(
                        lambda url: self._inspect_saved_source_url(url, timeout),
                        batch,
                    )
                )
                results.update(zip(batch, states))
                if all(item["state"] == "unknown" for item in states):
                    results.update(
                        {
                            url: {"state": "unknown"}
                            for url in pending_urls[offset + worker_count :]
                        }
                    )
                    break

        for result in results.values():
            if result.get("state") != "failed":
                continue
            reason = result.get("reason", "")
            if "27000" not in reason and "게시글 작성 및 카페" not in reason:
                continue
            self.restrictions.observe_code_27000(
                source_id=result.get("source_id", ""),
                account=result.get("account", ""),
                reason=reason[:500],
            )
        return results

    def is_deleted_source_url(self, url: str) -> bool:
        status = self.probe_source_urls({url}).get(url)
        if status is None:
            raise AffiliateApiError("저장된 V2R 링크 상태를 확인하지 못했습니다")
        return status

    def classify_failure(self, error: Exception) -> tuple[str, bool]:
        text = str(error)
        if "27000" in text or "게시글 작성 및 카페" in text:
            account_match = re.search(
                r"(?:naver_login_id[=:'\" ]+|)([0-9a-zA-Z_-]+) 회원님",
                text,
            )
            account = account_match.group(1) if account_match else ""
            account = account or self.last_restricted_account
            return self._restriction_result(account), False
        if "20004" in text or "연속으로 등록" in text:
            return "연속 글 등록 제한", False
        return AffiliateApiPublisher.classify_failure(error)

    def _prepare_auto_account_pools(
        self,
        jobs: list[ImmediateJob],
        healthy_accounts: list[str],
    ) -> None:
        grouped_menu_ids: dict[tuple[int, str], set[int]] = {}
        for job in jobs:
            if job.status != JobStatus.PENDING or job.account:
                continue
            required_type = (
                "실명" if job.source_kind == "daily" else job.account_type or "전체"
            )
            grouped_menu_ids.setdefault(
                (job.cafe_id, required_type),
                set(),
            ).add(job.menu_id)

        original_order = {
            account: index for index, account in enumerate(healthy_accounts)
        }
        for key, menu_ids in grouped_menu_ids.items():
            cafe_id, required_type = key
            candidates = list(healthy_accounts)
            if required_type in {"실명", "비실명"}:
                wanted_real_name = required_type == "실명"
                candidates = [
                    account
                    for account in candidates
                    if (
                        (self.global_accounts[account].get("my_info_v2") or {}).get(
                            "is_real_name"
                        )
                        is wanted_real_name
                    )
                ]
            candidates.sort(
                key=lambda account: (
                    -sum(
                        account
                        in self.menu_pools.get((cafe_id, menu_id), [])
                        for menu_id in menu_ids
                    ),
                    original_order[account],
                )
            )
            selected = [
                account
                for account in candidates
                if any(
                    account in self.menu_pools.get((cafe_id, menu_id), [])
                    for menu_id in menu_ids
                )
            ][: self.auto_account_limit]
            self.auto_selected_pools[key] = selected
            self.logger.info(
                "%s 자동 배정 계정 선택: %s개 / 요청 게시판 %s개",
                jobs[0].canonical_cafe_name if jobs else cafe_id,
                len(selected),
                len(menu_ids),
            )

    def _pool_for(
        self,
        job: ImmediateJob,
    ) -> tuple[tuple[int, str], list[str], set[str]]:
        pool = self.cafe_pools.get(job.cafe_id, [])
        allowed = set(self.menu_pools.get((job.cafe_id, job.menu_id), []))
        required_type = "실명" if job.source_kind == "daily" else job.account_type
        selected_key = (job.cafe_id, required_type or "전체")
        has_selected_pool = selected_key in self.auto_selected_pools
        if has_selected_pool:
            pool = self.auto_selected_pools[selected_key]
        if required_type in {"실명", "비실명"}:
            wanted = required_type == "실명"
            pool = [
                account
                for account in pool
                if (
                    (self.global_accounts[account].get("my_info_v2") or {}).get(
                        "is_real_name"
                    )
                    is wanted
                )
            ]
            key = (job.cafe_id, required_type)
            return key, (
                pool if has_selected_pool else pool[: self.auto_account_limit]
            ), allowed
        key = (job.cafe_id, "전체")
        return key, (
            pool if has_selected_pool else pool[: self.auto_account_limit]
        ), allowed

    def pick_account(self, job: ImmediateJob) -> str:
        key, pool, allowed = self._pool_for(job)
        if not pool:
            return ""
        start = self.pool_indexes.get(key, 0)
        for offset in range(len(pool)):
            account = pool[(start + offset) % len(pool)]
            if account in allowed and account not in self.blocked_accounts:
                self.pool_indexes[key] = (start + offset + 1) % len(pool)
                return account
        return ""

    def replace_failed_account(self, job: ImmediateJob) -> str:
        if job.account:
            self.blocked_accounts.add(job.account)
        job.account = self.pick_account(job)
        if job.account:
            self._resolve_head(job)
        return job.account

    def _resolve_head(self, job: ImmediateJob) -> None:
        job.head_id = None
        job.canonical_head_name = None
        if not job.prefix:
            return
        response = self._request(
            "GET",
            "/naver_cafes/heads",
            query={
                "cafe_id": job.cafe_id,
                "naver_login_id": job.account,
                "menu_id": job.menu_id,
            },
        )
        heads = [
            item
            for item in _walk_dicts(response)
            if item.get("head_id") or item.get("headId")
        ]
        head = match_catalog_name(
            job.prefix,
            heads,
            name=lambda item: str(item.get("head_name") or item.get("headName") or ""),
            label="말머리",
        )
        job.head_id = int(head.get("head_id") or head.get("headId"))
        job.canonical_head_name = str(
            head.get("head_name") or head.get("headName") or ""
        )

    def _destination(
        self,
        job: ImmediateJob,
    ) -> dict[str, Any]:
        is_immediate = (
            job.cafe_id in TEST_CAFE_IDS or job.publish_immediately
        )
        if job.scheduled_at is None and not is_immediate:
            raise AffiliateApiError("예약 발행 시간이 준비되지 않았습니다")
        return {
            "cafe_id": job.cafe_id,
            "cafe_name": job.canonical_cafe_name,
            "head_id": job.head_id,
            "head_name": job.canonical_head_name,
            "menu_id": job.menu_id,
            "menu_name": job.canonical_board_name,
            "naver_login_id": job.account,
            "start_at": (
                None
                if is_immediate
                else job.scheduled_at.isoformat().replace("+00:00", "Z")
            ),
            "target_view_count": 0,
            "use_comment_ai": job.use_comment_ai,
            "parent_id": None,
        }

    def _verify_immediate(
        self,
        source_id: str,
        job: ImmediateJob,
    ) -> None:
        detail = self._request(
            "GET",
            "/naver_cafe_articles/article",
            query={"source_id": source_id},
        )
        source = detail["naver_cafe_article_source"]
        destination = detail["naver_cafe_article_destination"]
        comments = detail.get("naver_cafe_article_source_comments") or []
        document = json.loads(detail["naver_cafe_article_source_detail"]["body"])
        lines = [
            "".join(str(node.get("value") or "") for node in paragraph.get("nodes", []))
            for component in document["document"]["components"]
            if component.get("@ctype") == "text"
            for paragraph in component.get("value", [])
        ]
        if source["title"] != job.title or source.get("tag_list", []) != job.tags:
            raise AffiliateApiError("등록 후 제목 또는 태그 검증에 실패했습니다")
        if job.cafe_id in TEST_CAFE_IDS or job.publish_immediately:
            if destination.get("start_at") is not None:
                raise AffiliateApiError("글이 즉시 발행으로 등록되지 않았습니다")
        else:
            actual_at = datetime.fromisoformat(
                str(destination["start_at"]).replace("Z", "+00:00")
            )
            if job.scheduled_at is None or actual_at != job.scheduled_at:
                raise AffiliateApiError("등록 후 예약 발행 시간 검증에 실패했습니다")
        if destination.get("status") not in {None, "RESERVED", "DONE", "SUCCESS"}:
            raise AffiliateApiError(
                f"등록 후 예약 상태가 올바르지 않습니다: {destination.get('status')}"
            )
        if lines != strip_placeholders(job.body).splitlines():
            raise AffiliateApiError("등록 후 본문 문단 검증에 실패했습니다")
        if any(PLACEHOLDER_PATTERN.search(line) for line in lines):
            raise AffiliateApiError("등록 후 본문에 중괄호 표시가 남아 있습니다")
        expected_comments = 12 if job.comments else 0
        if len(comments) != expected_comments:
            raise AffiliateApiError("등록 후 댓글 개수 검증에 실패했습니다")
        media_components = [
            component
            for component in document["document"]["components"]
            if component.get("@ctype") in {"image", "imageGroup", "imageStrip"}
        ]
        if job.prepared_image_count and len(media_components) < job.prepared_image_count:
            raise AffiliateApiError("등록 후 본문 이미지 개수 검증에 실패했습니다")
        if job.prepared_image_count and not all(
            _image_resource_ready(component)
            for component in media_components
        ):
            raise AffiliateApiError(
                "등록 후 본문 사진 주소 또는 파일 정보 검증에 실패했습니다"
            )
        if job.scheduled_at and comments and any(
            datetime.fromisoformat(str(comment["start_at"]).replace("Z", "+00:00"))
            < job.scheduled_at
            for comment in comments
        ):
            raise AffiliateApiError("댓글 예약 시간이 글 발행 시간보다 빠릅니다")

    def publish(self, job: ImmediateJob, dry_run: bool) -> str:
        if not job.cafe_id or not job.menu_id or not job.account:
            raise AffiliateApiError("API 목적지 또는 작성계정이 준비되지 않았습니다")
        is_test_cafe = job.cafe_id in TEST_CAFE_IDS
        is_immediate = is_test_cafe or job.publish_immediately
        if job.scheduled_at is None and not is_immediate:
            raise AffiliateApiError("예약 발행 시간이 준비되지 않았습니다")
        destination = self._destination(job)
        if dry_run:
            if is_immediate:
                self.logger.info("행 %s 즉시 발행 검증", job.row_number)
            else:
                self.logger.info(
                    "행 %s 예약 발행 검증: %s",
                    job.row_number,
                    job.scheduled_at.isoformat(),
                )
            return ""

        content_json = (
            self._prepare_revision_content(job, destination)
            if job.source_kind == "brand"
            else _content_json(strip_placeholders(job.body))
        )
        if is_test_cafe and job.comments:
            raise AffiliateApiError("한 줄 테스트 카페에는 댓글을 등록하지 않습니다")
        comments = (
            self._comments(
                job,
                job.scheduled_at or datetime.now(timezone.utc),
                job.cafe_id,
                comment_accounts=SELF_COMMENT_ACCOUNTS,
            )
            if job.comments
            else []
        )
        source_id = ""
        try:
            source_id = self._create_source(
                job.title,
                strip_placeholders(job.body),
                job.tags,
                destination,
                comments,
                content_json=content_json,
                recovery_statuses=(
                    ("DONE",) if is_immediate else ("RESERVED", "DONE")
                ),
            )
            if is_immediate:
                self._wait_for_written_at(source_id, job.cafe_id)
            self._verify_immediate(source_id, job)
        except Exception as exc:
            error_text = str(exc)
            if "27000" in error_text or "게시글 작성 및 카페" in error_text:
                self.last_restricted_account = job.account
                self.restrictions.observe_code_27000(
                    source_id=source_id
                    or f"account-test-{job.account}-{datetime.now().date()}",
                    account=job.account,
                    reason=error_text[:500],
                )
            if source_id:
                self._delete_source(source_id)
            raise
        if is_immediate:
            self.logger.info("행 %s 즉시 발행 완료", job.row_number)
        else:
            self.logger.info(
                "행 %s 예약 발행 등록 완료: %s",
                job.row_number,
                job.scheduled_at.isoformat(),
            )
        return f"https://v2r.daboja.im/nc/articleDetail/{source_id}"
