from v2r_auto.browser import AutomationError
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
