from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


AFFILIATE_CAFE_IDS = {25016228, 22788814}
SELF_OWNED_CAFE_IDS = {
    10174516,
    14567700,
    15175096,
    15441090,
    16149995,
    23708088,
    26616683,
    26680163,
}
TEST_CAFE_IDS = {31670254, 31670256}
SELF_OWNED_CAFE_NAMES = {
    "쌍둥이맘 모여라",
    "고요한 아침",
    "글로시 마이",
    "웨딩 노트",
    "송도포털",
    "헬씨 트리",
    "러브 인썸 (Love in Some)",
    "마이 웨딩 드림",
}
TEST_CAFE_NAMES = {"태극마케팅센터", "소나무마케팅센터"}


class CatalogMatchError(ValueError):
    pass


@dataclass(slots=True)
class CafeMenu:
    menu_id: int
    name: str
    writable_accounts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CafeCatalogEntry:
    cafe_id: int
    name: str
    category: str
    accounts: list[str] = field(default_factory=list)
    menus: list[CafeMenu] = field(default_factory=list)


def normalized_name(value: str) -> str:
    """Ignore spacing, emoji, and decoration while keeping meaningful text."""
    return re.sub(
        r"[^0-9a-z가-힣]+",
        "",
        html.unescape(value or ""),
        flags=re.IGNORECASE,
    ).casefold()


def korean_name(value: str) -> str:
    return "".join(re.findall(r"[가-힣]+", html.unescape(value or "")))


def readable_name(value: str) -> str:
    """Remove emoji from messages so Windows Tk does not render replacement boxes."""
    return re.sub(
        r"[^0-9a-z가-힣\s&·/()'`.,!?_+-]+",
        "",
        html.unescape(value or ""),
        flags=re.IGNORECASE,
    ).strip()


def match_catalog_name(
    wanted: str,
    candidates: Iterable[Any],
    *,
    name: Callable[[Any], str] = lambda item: item.name,
    label: str,
) -> Any:
    rows = list(candidates)
    exact_key = normalized_name(wanted)
    exact = [item for item in rows if normalized_name(name(item)) == exact_key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        options = ", ".join(readable_name(name(item)) for item in exact)
        raise CatalogMatchError(f"{label} 이름이 중복됩니다: {wanted} → {options}")

    korean_key = korean_name(wanted)
    korean = [
        item
        for item in rows
        if korean_key and korean_name(name(item)) == korean_key
    ]
    if len(korean) == 1:
        return korean[0]
    if len(korean) > 1:
        options = ", ".join(readable_name(name(item)) for item in korean)
        raise CatalogMatchError(
            f"{label} 한글 이름 후보가 여러 개라 자동 선택하지 않습니다: "
            f"{wanted} → {options}"
        )
    options = ", ".join(readable_name(name(item)) for item in rows[:20])
    raise CatalogMatchError(
        f"V2R에서 {label}을 찾지 못했습니다: {wanted}"
        + (f" / 사용 가능: {options}" if options else "")
    )


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    for item in _walk_dicts(payload):
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _field(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return default


def _category(cafe_id: int, name: str) -> str:
    if cafe_id in AFFILIATE_CAFE_IDS:
        return "제휴 카페"
    if cafe_id in SELF_OWNED_CAFE_IDS:
        return "자사 카페"
    if cafe_id in TEST_CAFE_IDS:
        return "노출 테스트 카페"
    normalized = normalized_name(name)
    if normalized in {normalized_name(item) for item in SELF_OWNED_CAFE_NAMES}:
        return "자사 카페"
    if normalized in {normalized_name(item) for item in TEST_CAFE_NAMES}:
        return "노출 테스트 카페"
    return "기타 카페"


class CafeCatalogService:
    """Read V2R's current cafes and per-account writable menu catalog."""

    def __init__(self, request: Callable[..., Any], logger):
        self.request = request
        self.logger = logger

    def load(self) -> list[CafeCatalogEntry]:
        response = self.request("GET", "/naver_cafes/naver_join_cafes")
        cafes = _first_list(response, "naver_join_cafes", "cafes")
        if not cafes:
            cafes = [
                item
                for item in _walk_dicts(response)
                if _field(item, "cafe_id", "cafeId")
                and _field(
                    item,
                    "pc_cafe_name",
                    "cafe_name",
                    "cafeName",
                    "name",
                )
            ]

        catalog: list[CafeCatalogEntry] = []
        seen_cafes: set[int] = set()
        for cafe in cafes:
            cafe_id = int(_field(cafe, "cafe_id", "cafeId", default=0) or 0)
            cafe_name = str(
                _field(
                    cafe,
                    "pc_cafe_name",
                    "cafe_name",
                    "cafeName",
                    "mobile_cafe_name",
                    "name",
                    default="",
                )
                or ""
            )
            if not cafe_id or not cafe_name or cafe_id in seen_cafes:
                continue
            seen_cafes.add(cafe_id)
            category = _category(cafe_id, cafe_name)
            if category not in {"자사 카페", "노출 테스트 카페"}:
                catalog.append(
                    CafeCatalogEntry(
                        cafe_id=cafe_id,
                        name=html.unescape(cafe_name),
                        category=category,
                    )
                )
                continue

            status = self.request(
                "GET",
                "/naver_cafes/naver_join_cafe",
                query={"cafe_id": cafe_id},
            )
            account_rows = _first_list(
                status,
                "naver_join_cafe",
                "naver_accounts",
                "accounts",
            )
            if not account_rows:
                account_rows = [
                    item
                    for item in _walk_dicts(status)
                    if _field(item, "login_id", "naver_login_id")
                ]
            accounts = sorted(
                {
                    str(_field(item, "login_id", "naver_login_id") or "")
                    for item in account_rows
                    if _field(item, "login_id", "naver_login_id")
                    and not item.get("force_drop")
                    and not item.get("stop_cafe_member")
                }
            )

            menu_map: dict[int, CafeMenu] = {}
            for account in accounts:
                try:
                    menu_response = self.request(
                        "GET",
                        "/naver_cafes/menus",
                        query={
                            "cafe_id": cafe_id,
                            "naver_login_id": account,
                        },
                    )
                except Exception as exc:
                    self.logger.warning(
                        "%s / %s 게시판 API 조회 실패: %s",
                        cafe_name,
                        account,
                        exc,
                    )
                    continue
                menu_rows = _first_list(menu_response, "cafe_menus", "menus")
                for menu in menu_rows:
                    menu_id = int(
                        _field(menu, "menuId", "menu_id", "id", default=0) or 0
                    )
                    menu_name = str(
                        _field(menu, "menuName", "menu_name", "name", default="") or ""
                    )
                    if not menu_id or not menu_name:
                        continue
                    item = menu_map.setdefault(
                        menu_id,
                        CafeMenu(menu_id=menu_id, name=html.unescape(menu_name)),
                    )
                    if menu.get("writable", True) and account not in item.writable_accounts:
                        item.writable_accounts.append(account)

            catalog.append(
                CafeCatalogEntry(
                    cafe_id=cafe_id,
                    name=html.unescape(cafe_name),
                    category=category,
                    accounts=accounts,
                    menus=sorted(menu_map.values(), key=lambda item: (item.name, item.menu_id)),
                )
            )
        return sorted(catalog, key=lambda item: (item.category, item.name))


def write_catalog_report(
    catalog: list[CafeCatalogEntry],
    report_dir: Path,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"V2R_카페_게시판_API_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "cafes": [asdict(item) for item in catalog],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    return path
