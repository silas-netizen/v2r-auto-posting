from v2r_auto.browser import V2RBrowser, is_browser_session_dead, user_facing_browser_error
from v2r_auto.gatling_paste import DAILY_POST_SHEET_URL


def test_invalid_session_id_is_treated_as_closed_chrome() -> None:
    error = RuntimeError(
        "Message: invalid session id: session deleted as the browser "
        "has closed the connection\nfrom disconnected: not connected to DevTools"
    )

    assert is_browser_session_dead(error) is True
    assert "Chrome 창이 닫혀" in user_facing_browser_error(error)


def test_ordinary_errors_are_not_session_deaths() -> None:
    error = RuntimeError("시트를 내려받지 못했습니다")

    assert is_browser_session_dead(error) is False
    assert user_facing_browser_error(error) == "시트를 내려받지 못했습니다"


def test_daily_sheet_export_keeps_the_daily_tab() -> None:
    url = V2RBrowser._sheet_export_url(DAILY_POST_SHEET_URL)

    assert "1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw" in url
    assert "gid=1842684291" in url
