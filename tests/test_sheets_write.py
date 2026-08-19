import hashlib
import json

from v2r_auto.join_marker import build_plan
from v2r_auto.sheets_write import (
    batch_update_url,
    build_join_batch_update,
    cookie_header_and_sapisid,
    extract_bearer_tokens,
    post_sheets_batch_update,
    sapisid_authorization,
    sheet_gid_from_url,
    spreadsheet_id_from_url,
)


def test_spreadsheet_id_and_gid_from_account_sheet_url() -> None:
    url = (
        "https://docs.google.com/spreadsheets/d/"
        "1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs/"
        "edit?gid=218285244#gid=218285244"
    )
    assert spreadsheet_id_from_url(url) == "1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs"
    assert sheet_gid_from_url(url) == 218285244


def test_sapisid_authorization_matches_google_hash() -> None:
    digest = hashlib.sha1(b"1000 cookie https://docs.google.com").hexdigest()
    assert sapisid_authorization("cookie", timestamp=1000) == f"SAPISIDHASH 1000_{digest}"


def test_cookie_header_prefers_secure_sapisid() -> None:
    header, sapisid = cookie_header_and_sapisid(
        [
            {"name": "SID", "value": "sid"},
            {"name": "SAPISID", "value": "old"},
            {"name": "__Secure-1PAPISID", "value": "secure"},
        ]
    )
    assert "SID=sid" in header
    assert sapisid == "secure"


def test_extract_bearer_tokens_from_chrome_performance_log() -> None:
    entries = [
        {
            "message": json.dumps(
                {
                    "message": {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "request": {
                                "url": "https://sheets.googleapis.com/v4/spreadsheets/abc",
                                "headers": {"Authorization": "Bearer ya29.token"},
                            }
                        },
                    }
                }
            )
        }
    ]
    assert extract_bearer_tokens(entries) == ["ya29.token"]


def test_join_batch_update_writes_only_cafe_columns() -> None:
    plan = build_plan(
        ["번호", "ID", "김천kb보험", "씨씨앙", "양평맘"],
        [
            {
                "__row": "2",
                "번호": "1",
                "ID": "quilliant",
                "김천kb보험": "노출",
                "씨씨앙": "",
                "양평맘": "",
            },
            {
                "__row": "3",
                "번호": "2",
                "ID": "",
                "김천kb보험": "유지",
                "씨씨앙": "가입",
                "양평맘": "가입",
            },
        ],
        {"씨씨앙": {"quilliant"}, "양평맘": set()},
    )

    payload = build_join_batch_update(plan, 218285244)
    request = payload["requests"][0]["updateCells"]
    assert request["range"] == {
        "sheetId": 218285244,
        "startRowIndex": 1,
        "endRowIndex": 3,
        "startColumnIndex": 3,
        "endColumnIndex": 5,
    }
    assert request["fields"] == "userEnteredValue"
    assert request["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] == "가입"
    assert request["rows"][0]["values"][1]["userEnteredValue"]["stringValue"] == ""
    assert request["rows"][1]["values"][0]["userEnteredValue"]["stringValue"] == ""
    assert request["rows"][1]["values"][1]["userEnteredValue"]["stringValue"] == ""


def test_post_sheets_batch_update_sends_bearer() -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"replies":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    def opener(request, timeout=45):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = request.data
        return FakeResponse()

    result = post_sheets_batch_update(
        "abc123",
        {"requests": []},
        bearer="ya29.token",
        opener=opener,
    )
    assert result == {"replies": []}
    assert captured["url"] == batch_update_url("abc123")
    assert captured["auth"] == "Bearer ya29.token"
    assert captured["body"] == b'{"requests": []}'
