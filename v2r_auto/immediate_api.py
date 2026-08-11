from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .affiliate_api import (
    COMMENT_ACCOUNTS,
    AffiliateApiError,
    AffiliateApiPublisher,
    _content_json,
    _walk_dicts,
)
from .cafe_catalog import (
    CafeCatalogEntry,
    CafeMenu,
    SELF_OWNED_CAFE_IDS,
    TEST_CAFE_IDS,
    match_catalog_name,
)
from .images import PLACEHOLDER_PATTERN, strip_placeholders
from .models import ImmediateJob, JobStatus


SELF_COMMENT_ACCOUNTS = (
    "repalbass",
    "jeehashed",
    "maritane",
    "skabakc",
    "uatrayb",
    "rowpalse",
)
ALL_COMMENT_ACCOUNTS = set(COMMENT_ACCOUNTS) | set(SELF_COMMENT_ACCOUNTS)


class ImmediateApiPublisher(AffiliateApiPublisher):
    """Publish self-owned-cafe articles immediately through V2R's API."""

    def __init__(self, browser, logger):
        super().__init__(browser, logger)
        self.menu_pools: dict[tuple[int, int], list[str]] = {}
        self.pool_indexes: dict[tuple[int, int, str], int] = {}
        self.global_accounts: dict[str, dict[str, Any]] = {}

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
        eligible = [
            account
            for account in joined
            if account not in ALL_COMMENT_ACCOUNTS
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

    def prepare_jobs(self, jobs: list[ImmediateJob]) -> None:
        self._capture_authorization()
        cafe_payload = self._request("GET", "/naver_cafes/naver_join_cafes")
        cafes = self._cafe_rows(cafe_payload)
        global_payload = self._request("GET", "/navers/accounts")
        self.global_accounts = {
            str(item["naver_login_id"]): item
            for item in _walk_dicts(global_payload)
            if item.get("naver_login_id")
        }

        by_wanted: dict[str, list[ImmediateJob]] = {}
        for job in jobs:
            if job.status == JobStatus.PENDING:
                by_wanted.setdefault(job.cafe, []).append(job)

        for wanted, cafe_jobs in by_wanted.items():
            cafe = match_catalog_name(wanted, cafes, label="카페")
            if cafe.cafe_id not in SELF_OWNED_CAFE_IDS | TEST_CAFE_IDS:
                for job in cafe_jobs:
                    job.status = JobStatus.SKIPPED
                    job.message = "즉시 발행 허용 카페가 아닙니다"
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
            for menu in menus:
                self.menu_pools[(cafe.cafe_id, menu.menu_id)] = list(
                    menu.writable_accounts
                )

            for job in cafe_jobs:
                if job.status != JobStatus.PENDING:
                    continue
                menu = match_catalog_name(job.board, menus, label="게시판")
                job.cafe_id = cafe.cafe_id
                job.menu_id = menu.menu_id
                job.canonical_cafe_name = cafe.name
                job.canonical_board_name = menu.name
                menu_pool = self.menu_pools.get((job.cafe_id, job.menu_id), [])
                if job.account:
                    if job.account not in menu_pool:
                        job.status = JobStatus.FAILED
                        job.message = (
                            f"지정 작성계정으로 해당 게시판을 사용할 수 없습니다: "
                            f"{job.account}"
                        )
                        continue
                else:
                    job.account = self.pick_account(job)
                    if not job.account:
                        job.status = JobStatus.FAILED
                        job.message = "조건에 맞는 작성계정이 없습니다"
                        continue
                self._resolve_head(job)
                self.logger.info(
                    "행 %s API 목적지: %s(%s) / %s(%s) / %s",
                    job.row_number,
                    job.canonical_cafe_name,
                    job.cafe_id,
                    job.canonical_board_name,
                    job.menu_id,
                    job.account,
                )

    def _pool_for(
        self,
        job: ImmediateJob,
    ) -> tuple[tuple[int, int, str], list[str]]:
        pool = self.menu_pools.get((job.cafe_id, job.menu_id), [])
        if job.account_type in {"실명", "비실명"}:
            wanted = job.account_type == "실명"
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
            key = (job.cafe_id, job.menu_id, job.account_type)
            return key, pool
        key = (job.cafe_id, job.menu_id, "전체")
        return key, pool

    def pick_account(self, job: ImmediateJob) -> str:
        key, pool = self._pool_for(job)
        if not pool:
            return ""
        start = self.pool_indexes.get(key, 0)
        for offset in range(len(pool)):
            account = pool[(start + offset) % len(pool)]
            if account not in self.blocked_accounts:
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
        is_test_cafe = job.cafe_id in TEST_CAFE_IDS
        if job.scheduled_at is None and not is_test_cafe:
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
                if is_test_cafe
                else job.scheduled_at.isoformat().replace("+00:00", "Z")
            ),
            "target_view_count": 0,
            "use_comment_ai": True,
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
        if job.cafe_id in TEST_CAFE_IDS:
            if destination.get("start_at") is not None:
                raise AffiliateApiError("한 줄 테스트가 즉시 발행으로 등록되지 않았습니다")
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
        if job.scheduled_at is None and not is_test_cafe:
            raise AffiliateApiError("예약 발행 시간이 준비되지 않았습니다")
        destination = self._destination(job)
        if dry_run:
            if is_test_cafe:
                self.logger.info("행 %s 한 줄 즉시 발행 검증", job.row_number)
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
                job.scheduled_at,
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
                    ("DONE",) if is_test_cafe else ("RESERVED", "DONE")
                ),
            )
            if is_test_cafe:
                self._wait_for_written_at(source_id, job.cafe_id)
            self._verify_immediate(source_id, job)
        except Exception:
            if source_id:
                self._delete_source(source_id)
            raise
        if is_test_cafe:
            self.logger.info("행 %s 한 줄 즉시 발행 완료", job.row_number)
        else:
            self.logger.info(
                "행 %s 예약 발행 등록 완료: %s",
                job.row_number,
                job.scheduled_at.isoformat(),
            )
        return f"https://v2r.daboja.im/nc/articleDetail/{source_id}"
