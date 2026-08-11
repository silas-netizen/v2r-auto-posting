import logging
from datetime import datetime, timedelta, timezone

from v2r_auto.restrictions import AccountRestrictionStore


def test_code_27000_blocks_for_thirty_days_without_same_source_extension(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    store = AccountRestrictionStore(
        tmp_path / "restricted.json",
        logging.getLogger("test"),
    )

    assert store.observe_code_27000(
        source_id="source-1",
        account="imgdrang",
        reason="error_code 27000",
        now=now,
    )
    assert not store.observe_code_27000(
        source_id="source-1",
        account="imgdrang",
        reason="error_code 27000",
        now=now + timedelta(days=10),
    )

    assert store.blocked_accounts(now + timedelta(days=29)) == {"imgdrang"}
    assert store.blocked_accounts(now + timedelta(days=30)) == set()


def test_restriction_history_survives_reload(tmp_path) -> None:
    path = tmp_path / "restricted.json"
    now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    first = AccountRestrictionStore(path, logging.getLogger("test"))
    first.observe_code_27000(
        source_id="source-1",
        account="imgdrang",
        reason="error_code 27000",
        now=now,
    )

    loaded = AccountRestrictionStore(path, logging.getLogger("test"))

    assert loaded.blocked_accounts(now + timedelta(days=1)) == {"imgdrang"}
