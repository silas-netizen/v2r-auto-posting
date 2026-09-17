from __future__ import annotations

import json
import re
import ssl
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
    urlopen,
)

import pytest
from cryptography import x509

from v2r_auto.remote_control import (
    AdminCredentialStore,
    LoginRateLimiter,
    RemoteHttpsControlServer,
    ensure_remote_certificate,
)


class FakeBackend:
    def snapshot(self):
        return {"state": "대기", "message": "", "workers": []}

    def configure(self, values):
        return {**self.snapshot(), "configured": values}

    def action(self, name):
        return {**self.snapshot(), "action": name}


def insecure_context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def post_form(url: str, values: dict[str, str], **headers) -> Request:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return Request(
        url,
        method="POST",
        data=urlencode(values).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            **headers,
        },
    )


def test_admin_password_is_salted_and_verified(tmp_path: Path) -> None:
    store = AdminCredentialStore(tmp_path / "credential.json")
    store.set_password("correct horse battery")

    saved = json.loads(store.path.read_text())
    assert saved["digest"] != "correct horse battery"
    assert store.verify("correct horse battery")
    assert not store.verify("wrong password")


def test_login_rate_limiter_blocks_repeated_failures(monkeypatch) -> None:
    current = [1000.0]
    monkeypatch.setattr("v2r_auto.remote_control.time.time", lambda: current[0])
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)

    for _ in range(3):
        assert limiter.allowed("203.0.113.4")
        limiter.fail("203.0.113.4")
    assert not limiter.allowed("203.0.113.4")
    current[0] += 61
    assert limiter.allowed("203.0.113.4")


def test_remote_certificate_contains_configured_public_ip(tmp_path: Path) -> None:
    cert_path, _key_path = ensure_remote_certificate(
        tmp_path,
        ["203.0.113.10"],
    )
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    addresses = {
        str(item) for item in names.get_values_for_type(x509.IPAddress)
    }
    assert {"127.0.0.1", "203.0.113.10"} <= addresses


def test_null_origin_requires_same_origin_fetch_metadata(tmp_path: Path) -> None:
    server = RemoteHttpsControlServer(
        FakeBackend(),
        data_dir=tmp_path,
        public_host="127.0.0.1",
        bind_host="127.0.0.1",
        port=0,
    )
    server.credentials.set_password("correct horse battery")
    server.start()
    base = server.local_url
    context = insecure_context()
    opener = build_opener(
        HTTPSHandler(context=context),
        HTTPCookieProcessor(CookieJar()),
    )
    try:
        cross_site = post_form(
            base + "auth/login",
            {"password": "correct horse battery"},
            Origin="null",
            **{"Sec-Fetch-Site": "cross-site"},
        )
        with pytest.raises(HTTPError) as rejected:
            opener.open(cross_site)
        assert rejected.value.code == 403

        same_origin = post_form(
            base + "auth/login",
            {"password": "correct horse battery"},
            Origin="null",
            **{"Sec-Fetch-Site": "same-origin"},
        )
        response = opener.open(same_origin)
        assert response.url == base
        assert "V2R Playwright 웹 제어" in response.read().decode()
    finally:
        server.close()


def test_remote_server_requires_setup_login_and_csrf(tmp_path: Path) -> None:
    server = RemoteHttpsControlServer(
        FakeBackend(),
        data_dir=tmp_path,
        public_host="127.0.0.1",
        bind_host="127.0.0.1",
        port=0,
    )
    server.start()
    base = server.local_url
    context = insecure_context()
    try:
        with urlopen(base, context=context) as response:
            assert "관리자 비밀번호 설정" in response.read().decode()

        setup = post_form(
            base + "auth/setup",
            {
                "password": "correct horse battery",
                "confirmation": "correct horse battery",
            },
        )
        try:
            urlopen(setup, context=context)
        except HTTPError as exc:
            assert exc.code == 401
            assert "관리자 로그인" in exc.read().decode()

        login = post_form(
            base + "auth/login",
            {"password": "correct horse battery"},
        )
        jar = CookieJar()
        opener = build_opener(
            HTTPSHandler(context=context),
            HTTPCookieProcessor(jar),
        )
        response = opener.open(login)
        body = response.read().decode()
        cookie = "; ".join(f"{item.name}={item.value}" for item in jar)
        assert "__Host-v2r_session=" in cookie
        assert "V2R Playwright 웹 제어" in body
        csrf = re.search(
            r'<meta name="csrf-token" content="([^"]+)"',
            body,
        ).group(1)

        missing_csrf = Request(
            base + "api/action/pause",
            method="POST",
            data=b"{}",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "Origin": base.rstrip("/"),
            },
        )
        try:
            urlopen(missing_csrf, context=context)
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("missing CSRF token must be rejected")

        missing_origin = Request(
            base + "api/action/pause",
            method="POST",
            data=b"{}",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
            },
        )
        try:
            urlopen(missing_origin, context=context)
        except HTTPError as exc:
            assert exc.code == 403
            assert "요청 출처" in exc.read().decode()
        else:
            raise AssertionError("missing Origin must be rejected")

        valid = Request(
            base + "api/action/pause",
            method="POST",
            data=b"{}",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Origin": base.rstrip("/"),
            },
        )
        with urlopen(valid, context=context) as response:
            assert json.load(response)["action"] == "pause"
    finally:
        server.close()


def test_remote_server_refuses_public_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="직접 노출"):
        RemoteHttpsControlServer(
            FakeBackend(),
            data_dir=tmp_path,
            public_host="203.0.113.10",
            bind_host="0.0.0.0",
            port=0,
        )
