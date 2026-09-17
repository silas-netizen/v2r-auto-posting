from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .control_server import CONTROL_CSS, CONTROL_HTML, CONTROL_JS, ControlBackend


LOGIN_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>V2R 관리자 로그인</title>
<link rel="stylesheet" href="/style.css"></head><body><main class="auth-main">
<section class="panel auth-card"><p class="eyebrow">SECURE REMOTE CONTROL</p>
<h1>V2R 관리자 로그인</h1><p class="description">메인 PC의 게시 작업을 제어합니다.</p>
<form method="post" action="/auth/login"><label>관리자 비밀번호
<input name="password" type="password" minlength="10" required autofocus>
</label><button class="primary auth-button" type="submit">로그인</button></form>
<p class="message">{message}</p></section></main></body></html>"""

SETUP_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>V2R 관리자 설정</title>
<link rel="stylesheet" href="/style.css"></head><body><main class="auth-main">
<section class="panel auth-card"><p class="eyebrow">FIRST RUN SETUP</p>
<h1>관리자 비밀번호 설정</h1>
<p class="description">이 화면은 메인 PC에서만 사용할 수 있습니다.</p>
<form method="post" action="/auth/setup"><label>새 비밀번호
<input name="password" type="password" minlength="10" required autofocus>
</label><label>비밀번호 확인<input name="confirmation" type="password"
minlength="10" required></label><button class="primary auth-button"
type="submit">보안 설정 완료</button></form><p class="message">{message}</p>
</section></main></body></html>"""

REMOTE_CSS = CONTROL_CSS + """
.auth-main{max-width:480px;padding-top:10vh}.auth-card form{display:flex;
flex-direction:column;gap:14px;margin-top:22px}.auth-button{margin-top:4px}
"""


def _password_record(password: str, salt: bytes | None = None) -> dict[str, str]:
    if len(password) < 10:
        raise ValueError("관리자 비밀번호는 10자 이상이어야 합니다")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return {"salt": salt.hex(), "digest": digest.hex()}


class AdminCredentialStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    def set_password(self, password: str) -> None:
        record = _password_record(password)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            temporary.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def verify(self, password: str) -> bool:
        with self._lock:
            try:
                record = json.loads(self.path.read_text(encoding="utf-8"))
                expected = bytes.fromhex(record["digest"])
                actual = bytes.fromhex(
                    _password_record(
                        password,
                        bytes.fromhex(record["salt"]),
                    )["digest"]
                )
            except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
                return False
        return hmac.compare_digest(expected, actual)


@dataclass(slots=True)
class Session:
    csrf_token: str
    expires_at: float


class SessionStore:
    def __init__(self, lifetime_seconds: int = 8 * 60 * 60):
        self.lifetime_seconds = lifetime_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, Session]:
        session_id = secrets.token_urlsafe(32)
        session = Session(
            csrf_token=secrets.token_urlsafe(24),
            expires_at=time.time() + self.lifetime_seconds,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= time.time():
                self._sessions.pop(session_id, None)
                return None
            return session

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 5 * 60,
        lockout_seconds: int = 15 * 60,
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        now = time.time()
        with self._lock:
            return self._blocked_until.get(address, 0) <= now

    def fail(self, address: str) -> None:
        now = time.time()
        with self._lock:
            attempts = [
                item
                for item in self._attempts.get(address, [])
                if item >= now - self.window_seconds
            ]
            attempts.append(now)
            self._attempts[address] = attempts
            if len(attempts) >= self.max_attempts:
                self._blocked_until[address] = now + self.lockout_seconds
                self._attempts[address] = []

    def succeed(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)
            self._blocked_until.pop(address, None)


def _san_values(hosts: Iterable[str]) -> list[x509.GeneralName]:
    values: list[x509.GeneralName] = []
    for host in dict.fromkeys(hosts):
        try:
            values.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if host:
                values.append(x509.DNSName(host))
    return values


def ensure_remote_certificate(
    directory: Path,
    hosts: Iterable[str],
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "remote-control-cert.pem"
    key_path = directory / "remote-control-key.pem"
    required_hosts = tuple(dict.fromkeys(("localhost", "127.0.0.1", *hosts)))
    if cert_path.exists() and key_path.exists():
        try:
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
            names = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            existing = {
                *names.get_values_for_type(x509.DNSName),
                *(str(item) for item in names.get_values_for_type(x509.IPAddress)),
            }
            if set(required_hosts).issubset(existing):
                return cert_path, key_path
        except (ValueError, x509.ExtensionNotFound):
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "V2R Remote Control")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(_san_values(required_hosts)),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, key_path


class RemoteHttpsControlServer:
    def __init__(
        self,
        backend: ControlBackend,
        *,
        data_dir: Path,
        public_host: str = "",
        bind_host: str = "0.0.0.0",
        port: int = 8765,
    ):
        self.backend = backend
        self.public_host = public_host.strip()
        self.credentials = AdminCredentialStore(data_dir / "admin-credential.json")
        self.sessions = SessionStore()
        self.rate_limiter = LoginRateLimiter()
        self._thread: threading.Thread | None = None
        allowed_hosts = {"localhost", "127.0.0.1", self.public_host} - {""}
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "V2RControl"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _is_loopback(self) -> bool:
                try:
                    return ipaddress.ip_address(self.client_address[0]).is_loopback
                except ValueError:
                    return False

            def _host_allowed(self) -> bool:
                host = self.headers.get("Host", "")
                if host.startswith("["):
                    host = host.split("]", 1)[0].lstrip("[")
                else:
                    host = host.rsplit(":", 1)[0]
                return host in allowed_hosts

            def _session_id(self) -> str:
                jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                morsel = jar.get("v2r_session")
                return morsel.value if morsel else ""

            def _session(self) -> Session | None:
                return server_ref.sessions.get(self._session_id())

            def _send(
                self,
                status: int,
                body: bytes,
                content_type: str,
                *,
                cookie: str = "",
                location: str = "",
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Strict-Transport-Security",
                    "max-age=31536000",
                )
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
                )
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, value: dict[str, Any]) -> None:
                self._send(
                    status,
                    json.dumps(value, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def _redirect(self, location: str, cookie: str = "") -> None:
                self._send(303, b"", "text/plain", cookie=cookie, location=location)

            def _form(self) -> dict[str, str]:
                from urllib.parse import parse_qs

                length = min(int(self.headers.get("Content-Length", "0")), 65536)
                parsed = parse_qs(
                    self.rfile.read(length).decode("utf-8", errors="replace")
                )
                return {key: values[0] for key, values in parsed.items()}

            def do_GET(self) -> None:
                if not self._host_allowed():
                    self._json(421, {"error": "허용되지 않은 접속 주소입니다"})
                    return
                if self.path == "/style.css":
                    self._send(
                        200,
                        REMOTE_CSS.encode("utf-8"),
                        "text/css; charset=utf-8",
                    )
                    return
                if not server_ref.credentials.configured:
                    if not self._is_loopback():
                        self._send(
                            403,
                            "메인 PC에서 관리자 비밀번호를 먼저 설정하세요.".encode(),
                            "text/plain; charset=utf-8",
                        )
                        return
                    body = SETUP_HTML.format(message="")
                    self._send(200, body.encode(), "text/html; charset=utf-8")
                    return
                session = self._session()
                if session is None:
                    body = LOGIN_HTML.format(message="")
                    self._send(401, body.encode(), "text/html; charset=utf-8")
                    return
                if self.path in {"/", "/index.html"}:
                    body = CONTROL_HTML.replace(
                        "<head>",
                        f'<head><meta name="csrf-token" '
                        f'content="{html.escape(session.csrf_token)}">',
                    )
                    self._send(200, body.encode(), "text/html; charset=utf-8")
                elif self.path == "/app.js":
                    self._send(
                        200,
                        CONTROL_JS.encode(),
                        "text/javascript; charset=utf-8",
                    )
                elif self.path == "/api/status":
                    self._json(200, server_ref.backend.snapshot())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._host_allowed():
                    self._json(421, {"error": "허용되지 않은 접속 주소입니다"})
                    return
                address = self.client_address[0]
                if self.path == "/auth/setup":
                    if server_ref.credentials.configured or not self._is_loopback():
                        self._json(403, {"error": "설정할 수 없습니다"})
                        return
                    form = self._form()
                    if form.get("password") != form.get("confirmation"):
                        body = SETUP_HTML.format(
                            message="비밀번호 확인이 일치하지 않습니다."
                        )
                        self._send(400, body.encode(), "text/html; charset=utf-8")
                        return
                    try:
                        server_ref.credentials.set_password(
                            form.get("password", "")
                        )
                    except ValueError as exc:
                        body = SETUP_HTML.format(message=html.escape(str(exc)))
                        self._send(400, body.encode(), "text/html; charset=utf-8")
                        return
                    self._redirect("/")
                    return
                if self.path == "/auth/login":
                    if not server_ref.rate_limiter.allowed(address):
                        self._json(429, {"error": "로그인 시도가 잠시 제한됐습니다"})
                        return
                    if not server_ref.credentials.verify(
                        self._form().get("password", "")
                    ):
                        server_ref.rate_limiter.fail(address)
                        body = LOGIN_HTML.format(
                            message="비밀번호가 올바르지 않습니다."
                        )
                        self._send(401, body.encode(), "text/html; charset=utf-8")
                        return
                    server_ref.rate_limiter.succeed(address)
                    session_id, _session = server_ref.sessions.create()
                    self._redirect(
                        "/",
                        "v2r_session="
                        + session_id
                        + "; Path=/; Secure; HttpOnly; SameSite=Strict",
                    )
                    return

                session = self._session()
                if session is None:
                    self._json(401, {"error": "로그인이 필요합니다"})
                    return
                if not hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", ""),
                    session.csrf_token,
                ):
                    self._json(403, {"error": "보안 토큰이 올바르지 않습니다"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 65536)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if self.path == "/api/configure":
                        result = server_ref.backend.configure(payload)
                    elif self.path.startswith("/api/action/"):
                        result = server_ref.backend.action(
                            self.path.rsplit("/", 1)[-1]
                        )
                    else:
                        self._json(404, {"error": "not found"})
                        return
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._json(409, {"error": str(exc)})
                    return
                self._json(200, result)

        self._server = ThreadingHTTPServer((bind_host, port), Handler)
        self.port = int(self._server.server_address[1])
        cert_path, key_path = ensure_remote_certificate(
            data_dir / "https",
            [self.public_host] if self.public_host else [],
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        self._server.socket = context.wrap_socket(
            self._server.socket,
            server_side=True,
        )

    @property
    def local_url(self) -> str:
        return f"https://localhost:{self.port}/"

    @property
    def remote_url(self) -> str:
        host = self.public_host or "<공인-IP>"
        return f"https://{host}:{self.port}/"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="v2r-remote-https-control",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
