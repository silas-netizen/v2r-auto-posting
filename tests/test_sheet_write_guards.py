from v2r_auto.browser import AutomationError, V2RBrowser
from v2r_auto.exposure_sheet import GoogleSheetExposureStore
from v2r_auto.sheet_values import pick_nearby_keyword_row


def test_pick_nearby_skips_unread_cells() -> None:
    assert (
        pick_nearby_keyword_row(
            {1386: None, 1385: "원포 얼리", 1387: None},
            "원포 얼리",
            1386,
        )
        == 1385
    )


def test_keyword_mismatch_message_is_retryable() -> None:
    assert GoogleSheetExposureStore._looks_like_row_mismatch(
        AutomationError("시트에서 키워드를 화면으로 확인하지 못했습니다: 원포 얼리 (기억한 행 1386)")
    )
    assert GoogleSheetExposureStore._looks_like_row_mismatch(
        AutomationError("시트 H1386 키워드가 연세사랑모아여성병원입니다. 원포 얼리에 쓰지 않습니다")
    )
    assert not GoogleSheetExposureStore._looks_like_row_mismatch(
        AutomationError("시트 J1386 저장값을 다시 확인하지 못했습니다")
    )


def _typing_browser(order: list[str], *, bar_ok: bool = False) -> V2RBrowser:
    browser = V2RBrowser.__new__(V2RBrowser)

    def formula_bar(value: str) -> bool:
        order.append(f"bar:{value}")
        return bar_ok

    def cell_editor(value: str, editor=None) -> None:
        order.append(f"cell:{value}")

    browser._type_into_formula_bar = formula_bar
    browser._type_into_cell_editor = cell_editor
    return browser


def test_status_text_types_in_the_cell_first() -> None:
    order: list[str] = []
    _typing_browser(order)._type_sheet_value("밀려남")
    assert order == ["cell:밀려남"]


def test_number_falls_back_to_cell_when_formula_bar_blocked() -> None:
    order: list[str] = []
    _typing_browser(order)._type_sheet_value("6700")
    assert order == ["bar:6700", "cell:6700"]


def test_status_text_falls_back_to_formula_bar_when_cell_blocked() -> None:
    browser = V2RBrowser.__new__(V2RBrowser)

    def cell_blocked(value: str, editor=None) -> None:
        raise RuntimeError("element not interactable")

    browser._type_into_cell_editor = cell_blocked
    browser._type_into_formula_bar = lambda value: True
    browser._type_sheet_value("밀려남")
