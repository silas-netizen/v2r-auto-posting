from v2r_auto.browser import is_browser_session_dead, user_facing_browser_error


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
