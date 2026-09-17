from __future__ import annotations

import inspect
import logging
from pathlib import Path
from unittest.mock import MagicMock

from v2r_auto.content import parse_article
from v2r_auto.models import ImmediateJob
from v2r_auto.playwright_edition import PlaywrightWebBrowser


def make_browser(tmp_path: Path) -> PlaywrightWebBrowser:
    config = MagicMock()
    config.profile_dir = tmp_path / "profile"
    config.download_dir = tmp_path / "downloads"
    config.timeout_seconds = 1
    return PlaywrightWebBrowser(
        config,
        logging.getLogger("playwright-edition-test"),
    )


def test_edition_adapter_has_no_direct_v2r_api_client() -> None:
    source = inspect.getsource(inspect.getmodule(PlaywrightWebBrowser)).casefold()

    assert "api-v2r" not in source
    assert "from .affiliate_api" not in source
    assert "from .immediate_api" not in source
    assert "urlopen" not in source


def test_immediate_preparation_uses_ui_account_and_names(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    browser.first_available_account = MagicMock(return_value="ui-account")
    job = ImmediateJob(
        row_number=2,
        article=parse_article("태그", "제목 : 제목\n본문 : 본문"),
        cafe="헬씨 트리",
        board="자유게시판",
        account="",
        account_type="실명",
    )

    browser.prepare_immediate_jobs([job], auto_account_limit=7)

    assert job.account == "ui-account"
    assert job.cafe_id == 1
    assert job.menu_id == 1
    assert job.canonical_cafe_name == "헬씨 트리"
    assert job.canonical_board_name == "자유게시판"


def test_runner_compatibility_methods_never_probe_api(tmp_path: Path) -> None:
    browser = make_browser(tmp_path)
    urls = {"https://v2r.daboja.im/nc/articleDetail/existing"}

    assert browser.probe_v2r_source_urls(urls) == {
        next(iter(urls)): None,
    }
    assert browser.inspect_immediate_source_urls(urls) == {
        next(iter(urls)): {
            "state": "unknown",
            "reason": "웹 전용판은 기존 링크를 보존합니다",
        }
    }
    assert browser.consume_failed_immediate_urls() == set()
