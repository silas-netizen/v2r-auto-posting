from __future__ import annotations

import json
import ssl
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography import x509

from v2r_auto.control_server import (
    LocalHttpsControlServer,
    ensure_localhost_certificate,
)


class FakeBackend:
    def __init__(self):
        self.values = {}
        self.actions: list[str] = []

    def snapshot(self):
        return {"state": "대기", "workers": [], "completed": 0, "total": 0}

    def configure(self, values):
        self.values = values
        return {**self.snapshot(), "message": "설정 저장"}

    def action(self, name):
        self.actions.append(name)
        return {**self.snapshot(), "message": name}


def context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def test_local_certificate_covers_localhost_and_loopback(tmp_path: Path) -> None:
    cert_path, key_path = ensure_localhost_certificate(tmp_path)
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value

    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert str(names.get_values_for_type(x509.IPAddress)[0]) == "127.0.0.1"
    assert key_path.exists()


def test_https_control_server_serves_token_scoped_actions(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    server = LocalHttpsControlServer(backend, data_dir=tmp_path)
    server.start(open_browser=False)
    try:
        with urlopen(server.url, context=context()) as response:
            assert response.status == 200
            assert "V2R Playwright 웹 제어" in response.read().decode("utf-8")

        configure = Request(
            server.url + "api/configure",
            method="POST",
            data=json.dumps(
                {
                    "program": "affiliate",
                    "worker_count": 3,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": f"https://localhost:{server.port}",
            },
        )
        with urlopen(configure, context=context()) as response:
            payload = json.load(response)
        assert payload["message"] == "설정 저장"
        assert backend.values["worker_count"] == 3

        action = Request(
            server.url + "api/action/open_login",
            method="POST",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"https://localhost:{server.port}",
            },
        )
        with urlopen(action, context=context()) as response:
            assert json.load(response)["message"] == "open_login"
        assert backend.actions == ["open_login"]
    finally:
        server.close()


def test_control_server_rejects_missing_session_token(tmp_path: Path) -> None:
    server = LocalHttpsControlServer(FakeBackend(), data_dir=tmp_path)
    server.start(open_browser=False)
    try:
        try:
            urlopen(
                f"https://localhost:{server.port}/api/status",
                context=context(),
            )
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("missing token must be rejected")
    finally:
        server.close()
