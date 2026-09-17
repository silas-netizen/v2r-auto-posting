from __future__ import annotations

import threading

import pytest

from v2r_auto.automation_service import ControlConfig, UnifiedAutomationBackend


def test_control_config_accepts_parallel_affiliate_settings() -> None:
    config = ControlConfig.from_values(
        {
            "program": "affiliate",
            "worker_count": 3,
            "sheet_url": "https://docs.google.com/spreadsheets/d/example",
            "dry_run": False,
        }
    )

    assert config.program == "affiliate"
    assert config.worker_count == 3
    assert config.dry_run is False


def test_control_config_accepts_immediate_daily_settings() -> None:
    config = ControlConfig.from_values(
        {
            "program": "immediate",
            "worker_count": 5,
            "input_mode": "daily",
            "excel_path": "C:/work/daily.xlsx",
            "publish_mode": "immediate",
            "auto_account_limit": 8,
            "immediate_interval_minutes": 4,
            "browser_recycle_jobs": 30,
        }
    )

    assert config.input_mode == "daily"
    assert config.publish_mode == "immediate"
    assert config.auto_account_limit == 8
    assert config.immediate_interval_minutes == 4
    assert config.browser_recycle_jobs == 30


def test_account_test_login_opens_only_one_worker(monkeypatch) -> None:
    class Pool:
        def __init__(self):
            self.calls = []

        def open_login_windows(self, sheet_url, *, worker_limit=None):
            self.calls.append((sheet_url, worker_limit))

    pool = Pool()
    backend = UnifiedAutomationBackend.__new__(UnifiedAutomationBackend)
    backend.config = ControlConfig(
        program="immediate",
        worker_count=3,
        input_mode="account_test",
    )
    backend._lock = threading.RLock()
    monkeypatch.setattr(backend, "_ensure_pool", lambda: pool)

    backend._open_login()

    assert pool.calls == [(backend._sheet_url_for_login(), 1)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("program", "other"),
        ("worker_count", 6),
        ("input_mode", "other"),
        ("publish_mode", "other"),
        ("auto_account_limit", 1),
        ("immediate_interval_minutes", 16),
        ("browser_recycle_jobs", 15),
    ],
)
def test_control_config_rejects_unsafe_values(field: str, value) -> None:
    values = {
        "program": "affiliate",
        "worker_count": 3,
        "input_mode": "brand",
        "publish_mode": "reserved",
        "auto_account_limit": 10,
        "immediate_interval_minutes": 1,
        "browser_recycle_jobs": 20,
    }
    values[field] = value
    with pytest.raises(ValueError):
        ControlConfig.from_values(values)
