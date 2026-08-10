import logging

import pytest

from v2r_auto.cafe_catalog import (
    CafeCatalogEntry,
    CafeCatalogService,
    CafeMenu,
    CatalogMatchError,
    match_catalog_name,
    normalized_name,
)


def test_normalized_name_ignores_spacing_and_emoji() -> None:
    assert normalized_name("이모저모 이야기💕") == normalized_name("이모저모이야기")
    assert normalized_name("마이 웨딩 드림") == normalized_name("마이웨딩드림")


def test_match_uses_unique_korean_fallback() -> None:
    menus = [CafeMenu(1, "후기 게시판✨"), CafeMenu(2, "질문 게시판")]

    assert match_catalog_name(
        "후기게시판",
        menus,
        label="게시판",
    ).menu_id == 1


def test_match_never_guesses_ambiguous_korean_name() -> None:
    cafes = [
        CafeCatalogEntry(1, "러브 인썸 (Love in Some)", "자사 카페"),
        CafeCatalogEntry(2, "러브 인썸 (Love in Other)", "기타 카페"),
    ]

    with pytest.raises(CatalogMatchError, match="후보가 여러 개"):
        match_catalog_name("러브 인썸", cafes, label="카페")


def test_catalog_loads_all_account_menu_permissions() -> None:
    def request(method, path, payload=None, query=None):
        assert method == "GET"
        if path == "/naver_cafes/naver_join_cafes":
            return {
                "naver_join_cafes": [
                    {"cafe_id": 101, "pc_cafe_name": "고요한 아침"},
                    {"cafe_id": 102, "pc_cafe_name": "태국마케팅센터"},
                    {
                        "cafe_id": 25016228,
                        "pc_cafe_name": "국내1위 다이어트 커뮤니티 씨씨앙",
                    },
                ]
            }
        if path == "/naver_cafes/naver_join_cafe":
            return {
                "naver_accounts": [
                    {"login_id": "writer-a"},
                    {"login_id": "writer-b", "stop_cafe_member": True},
                ]
            }
        if path == "/naver_cafes/menus":
            assert query["naver_login_id"] == "writer-a"
            return {
                "cafe_menus": [
                    {"menuId": 1, "menuName": "가입인사🌱", "writable": True},
                    {"menuId": 2, "menuName": "공지사항", "writable": False},
                ]
            }
        raise AssertionError(path)

    catalog = CafeCatalogService(request, logging.getLogger("test")).load()

    assert [item.category for item in catalog] == [
        "노출 테스트 카페",
        "자사 카페",
        "제휴 카페",
    ]
    self_owned = next(item for item in catalog if item.cafe_id == 101)
    assert self_owned.accounts == ["writer-a"]
    greeting = next(menu for menu in self_owned.menus if menu.menu_id == 1)
    notice = next(menu for menu in self_owned.menus if menu.menu_id == 2)
    assert greeting.name == "가입인사🌱"
    assert greeting.writable_accounts == ["writer-a"]
    assert notice.writable_accounts == []
