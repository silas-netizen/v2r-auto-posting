from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class ControlBackend(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def configure(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def action(self, name: str) -> dict[str, Any]: ...


CONTROL_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>V2R Playwright 웹 제어</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">PLAYWRIGHT WEB ONLY · API OFF</p>
        <h1>V2R Playwright 웹 제어</h1>
        <p class="description">V2R API 없이 자사·제휴 작업을 Chrome 웹 화면으로 처리합니다.</p>
      </div>
      <span id="server-state" class="pill">연결 중</span>
    </header>

    <nav class="major-nav" aria-label="큰 메뉴">
      <a href="#settings"><span>01</span><strong>작업 설정</strong><small>입력과 발행 조건</small></a>
      <a href="#controls"><span>02</span><strong>실행 제어</strong><small>로그인부터 시작</small></a>
      <a href="#browser-status"><span>03</span><strong>브라우저 상태</strong><small>작업 창 진행률</small></a>
    </nav>

    <section id="settings" class="panel major-menu settings">
      <div class="major-heading"><span class="major-index">01</span><div>
        <p class="menu-level">큰 메뉴</p><h2>작업 설정</h2>
        <p>작업 종류와 입력 자료, 발행 방식을 순서대로 설정합니다.</p>
      </div></div>
      <div class="sub-menu">
        <div class="sub-heading"><span>작은 메뉴 1</span><h3>기본 작업</h3></div>
        <div class="grid">
          <label>프로그램
            <select id="program">
              <option value="affiliate">제휴 수정 발행</option>
              <option value="immediate">자사 발행</option>
            </select>
          </label>
          <label>작업 창 수
            <select id="worker_count">
              <option>1</option><option>2</option><option selected>3</option>
              <option>4</option><option>5</option>
            </select>
          </label>
          <label>브라우저 메모리 회수
            <select id="browser_recycle_jobs">
              <option value="0">사용 안 함</option>
              <option value="10">10건마다</option>
              <option value="20" selected>20건마다</option>
              <option value="30">30건마다</option>
              <option value="50">50건마다</option>
            </select>
          </label>
          <label>자사 입력 모드
            <select id="input_mode">
              <option value="brand">브랜드 Google Sheet</option>
              <option value="daily">일상 글 Excel</option>
              <option value="account_test">전체 계정 한 줄 테스트</option>
            </select>
          </label>
          <label>자동 배정 ID 수
            <select id="auto_account_limit">
              <option>2</option><option>3</option><option>4</option>
              <option>5</option><option>6</option><option>7</option>
              <option>8</option><option>9</option><option selected>10</option>
            </select>
          </label>
        </div>
      </div>
      <div class="sub-menu">
        <div class="sub-heading"><span>작은 메뉴 2</span><h3>입력 데이터</h3></div>
        <div class="grid">
          <label class="wide">Google Sheet URL
            <input id="sheet_url" type="url" placeholder="https://docs.google.com/spreadsheets/...">
          </label>
          <label class="wide">일상 글 Excel 경로
            <input id="excel_path" type="text" placeholder="자사 일상 Excel 모드에서만 사용">
          </label>
          <label class="wide">포토워셔 main.exe 경로
            <input id="photo_washer_path" type="text" placeholder="비워두면 저장된 경로 자동 사용">
          </label>
        </div>
      </div>
      <div class="sub-menu">
        <div class="sub-heading"><span>작은 메뉴 3</span><h3>발행 옵션</h3></div>
        <div class="grid">
          <label>실행 모드
            <select id="dry_run">
              <option value="true">검증 모드</option>
              <option value="false">실제 발행</option>
            </select>
          </label>
          <label>자사 발행 방식
            <select id="publish_mode">
              <option value="reserved">예약 발행</option>
              <option value="immediate">즉시 발행</option>
            </select>
          </label>
          <label>즉시 발행 간격(분)
            <select id="immediate_interval_minutes">
              <option>1</option><option>2</option><option>3</option>
              <option>4</option><option>5</option><option>6</option>
              <option>7</option><option>8</option><option>9</option>
              <option>10</option><option>11</option><option>12</option>
              <option>13</option><option>14</option><option>15</option>
            </select>
          </label>
        </div>
      </div>
      <button id="save" class="secondary">설정 저장</button>
    </section>

    <section id="controls" class="panel major-menu">
      <div class="major-heading"><span class="major-index">02</span><div>
        <p class="menu-level">큰 메뉴</p><h2>실행 제어</h2>
        <p>번호 순서대로 준비한 뒤 작업을 시작합니다.</p>
      </div></div>
      <div class="sub-heading"><span>작은 메뉴</span><h3>작업 버튼</h3></div>
      <div class="actions">
        <button data-action="open_login">1. 로그인 창 열기</button>
        <button data-action="verify_login">2. 로그인 확인</button>
        <button data-action="check_data">3. 데이터 확인</button>
        <button data-action="start" class="primary">4. 작업 시작</button>
        <button data-action="pause" class="warning">일시정지</button>
        <button data-action="resume">다시 시작</button>
        <button data-action="stop" class="danger">중지</button>
        <button data-action="shutdown" class="danger">프로그램 종료</button>
      </div>
      <p id="message" class="message">설정을 저장한 뒤 로그인 창을 열어주세요.</p>
    </section>

    <section id="browser-status" class="panel major-menu">
      <div class="section-title major-heading">
        <span class="major-index">03</span><div>
          <p class="menu-level">큰 메뉴</p><h2>브라우저 창 상태</h2>
          <p>각 Playwright 작업 창의 현재 단계와 처리 행을 확인합니다.</p>
        </div>
        <span id="summary"></span>
      </div>
      <div id="workers" class="workers"></div>
    </section>
  </main>
  <script src="app.js"></script>
</body>
</html>
"""


CONTROL_CSS = """
:root{font-family:Inter,"Pretendard","Malgun Gothic",sans-serif;color:#172033;
background:#eef2f7}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;
margin:0 auto;padding:28px 20px 60px}header{display:flex;justify-content:space-between;
align-items:flex-start;margin-bottom:20px}h1{font-size:34px;margin:4px 0 8px}h2{font-size:18px;
margin:0 0 16px}.eyebrow{font-size:12px;letter-spacing:.16em;color:#3267d6;margin:0;
font-weight:800}.description{margin:0;color:#637086}.panel{background:#fff;border:1px solid #dce3ed;
border-radius:18px;padding:20px;margin:14px 0;box-shadow:0 10px 28px rgba(31,45,72,.05)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.wide{grid-column:1/-1}
label{display:flex;flex-direction:column;gap:7px;font-size:13px;font-weight:700;color:#49566c}
input,select{height:42px;border:1px solid #cdd6e3;border-radius:10px;padding:0 12px;background:#fff;
font:inherit;color:#172033}button{border:0;border-radius:10px;padding:11px 15px;background:#e9eef6;
color:#26354d;font-weight:800;cursor:pointer}button:hover{filter:brightness(.97)}
button:disabled{opacity:.45;cursor:not-allowed}.primary{background:#3267d6;color:#fff}
.secondary{margin-top:14px;background:#25324a;color:#fff}.danger{background:#ffe8e8;color:#ad2929}
.warning{background:#fff2cd;color:#785800}.actions{display:flex;flex-wrap:wrap;gap:9px}
.pill{padding:8px 12px;border-radius:999px;background:#dff7e8;color:#17633a;font-size:12px;
font-weight:800}.message{min-height:21px;margin:15px 0 0;color:#53627a}.section-title{display:flex;
justify-content:space-between;align-items:center}.workers{display:grid;
grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.worker{padding:14px;
border:1px solid #e0e6ef;border-radius:12px;background:#f8fafc}.worker strong{display:block;
margin-bottom:5px}.worker span{font-size:13px;color:#617086}.worker.error{border-color:#f1b3b3;
background:#fff7f7}.major-nav{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 18px}
.major-nav a{display:grid;grid-template-columns:auto 1fr;column-gap:10px;align-items:center;padding:14px 16px;
border:1px solid #d8e1ee;border-radius:14px;background:#fff;color:#172033;text-decoration:none}
.major-nav a:hover{border-color:#3267d6;box-shadow:0 7px 18px rgba(50,103,214,.10)}
.major-nav span{grid-row:1/3;font-size:12px;font-weight:900;color:#3267d6;background:#eaf1ff;
border-radius:8px;padding:8px}.major-nav strong{font-size:15px}.major-nav small{color:#7a879b;margin-top:2px}
.major-menu{padding:24px;border-top:5px solid #3267d6;scroll-margin-top:12px}.major-heading{display:flex;
align-items:flex-start;gap:14px;margin-bottom:18px}.major-heading>div{flex:1}.major-heading h2{font-size:24px;
margin:1px 0 5px}.major-heading p{margin:0;color:#6b778b;font-size:13px}.major-index{display:inline-flex;
align-items:center;justify-content:center;min-width:46px;height:46px;border-radius:13px;background:#3267d6;
color:#fff;font-size:15px;font-weight:900}.menu-level{font-size:10px!important;color:#3267d6!important;
font-weight:900;letter-spacing:.12em;text-transform:uppercase}.sub-menu{padding:18px;margin:12px 0;
border:1px solid #e1e7f0;border-radius:14px;background:#f8fafc}.sub-heading{display:flex;align-items:center;
gap:9px;margin-bottom:13px}.sub-heading span{padding:5px 8px;border-radius:7px;background:#e8eef8;color:#52627a;
font-size:10px;font-weight:900}.sub-heading h3{font-size:16px;margin:0;color:#283750}.section-title #summary{
align-self:center;padding:8px 12px;border-radius:9px;background:#edf3ff;color:#315da8;font-weight:800}
@media(max-width:680px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}
header{display:block}.pill{display:inline-block;margin-top:12px}.major-nav{grid-template-columns:1fr}
.major-menu{padding:18px}.major-heading h2{font-size:21px}.section-title{align-items:flex-start;flex-wrap:wrap}}
"""


CONTROL_JS = """
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
const api = (path, options={}) => fetch(`api/${path}`, {
  headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, ...options
}).then(async response => {
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
});
const fields = ['program','worker_count','input_mode','auto_account_limit',
  'sheet_url','excel_path','photo_washer_path','dry_run','publish_mode',
  'immediate_interval_minutes','browser_recycle_jobs'];
let configured = false;

function values() {
  const result = {};
  for (const name of fields) result[name] = document.getElementById(name).value;
  result.worker_count = Number(result.worker_count);
  result.auto_account_limit = Number(result.auto_account_limit);
  result.immediate_interval_minutes = Number(result.immediate_interval_minutes);
  result.browser_recycle_jobs = Number(result.browser_recycle_jobs);
  result.dry_run = result.dry_run === 'true';
  return result;
}
function render(data) {
  document.getElementById('server-state').textContent = data.state || '대기';
  document.getElementById('message').textContent = data.message || '';
  document.getElementById('summary').textContent =
    `완료 ${data.completed || 0} / 전체 ${data.total || 0}`;
  const root = document.getElementById('workers');
  root.replaceChildren(...(data.workers || []).map(worker => {
    const card = document.createElement('div');
    card.className = `worker ${worker.state === '오류' ? 'error' : ''}`;
    const title = document.createElement('strong');
    title.textContent = worker.worker_id < 0 ? '시트·준비 창' : `작업 창 ${worker.worker_id + 1}`;
    const status = document.createElement('span');
    status.textContent = `${worker.state}${worker.current_row ? ` · ${worker.current_row}행` : ''}`;
    const detail = document.createElement('span');
    detail.textContent = worker.message || '';
    card.append(title, status);
    if (worker.message) card.append(document.createElement('br'), detail);
    return card;
  }));
}
async function refresh() {
  try { render(await api('status')); }
  catch (error) { document.getElementById('server-state').textContent = '연결 오류'; }
}
document.getElementById('save').addEventListener('click', async () => {
  try {
    const result = await api('configure', {method:'POST', body:JSON.stringify(values())});
    configured = true; render(result);
  } catch (error) { document.getElementById('message').textContent = error.message; }
});
document.querySelectorAll('[data-action]').forEach(button => {
  button.addEventListener('click', async () => {
    if (!configured && button.dataset.action !== 'stop') {
      document.getElementById('message').textContent = '먼저 설정 저장을 눌러주세요.';
      return;
    }
    button.disabled = true;
    try { render(await api(`action/${button.dataset.action}`, {method:'POST', body:'{}'})); }
    catch (error) { document.getElementById('message').textContent = error.message; }
    finally { button.disabled = false; }
  });
});
refresh(); setInterval(refresh, 1000);
"""


def ensure_localhost_certificate(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "localhost-cert.pem"
    key_path = directory / "localhost-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "V2R Local Control")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
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


class LocalHttpsControlServer:
    def __init__(
        self,
        backend: ControlBackend,
        *,
        data_dir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.backend = backend
        self.host = host
        self.token = secrets.token_urlsafe(24)
        self._thread: threading.Thread | None = None
        cert_path, key_path = ensure_localhost_certificate(data_dir / "https")
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _authorized(self) -> bool:
                host = self.headers.get("Host", "").split(":", 1)[0]
                return (
                    host in {"localhost", "127.0.0.1"}
                    and self.path.startswith(f"/{server_ref.token}/")
                )

            def _send(
                self,
                status: int,
                body: bytes,
                content_type: str,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, value: dict[str, Any]) -> None:
                self._send(
                    status,
                    json.dumps(value, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:
                if not self._authorized():
                    self._json(404, {"error": "not found"})
                    return
                relative = self.path.removeprefix(f"/{server_ref.token}/")
                if relative in {"", "index.html"}:
                    self._send(
                        200,
                        CONTROL_HTML.encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                elif relative == "style.css":
                    self._send(
                        200,
                        CONTROL_CSS.encode("utf-8"),
                        "text/css; charset=utf-8",
                    )
                elif relative == "app.js":
                    self._send(
                        200,
                        CONTROL_JS.encode("utf-8"),
                        "text/javascript; charset=utf-8",
                    )
                elif relative == "api/status":
                    self._json(200, server_ref.backend.snapshot())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._json(404, {"error": "not found"})
                    return
                origin = self.headers.get("Origin")
                expected_origins = {
                    f"https://localhost:{server_ref.port}",
                    f"https://127.0.0.1:{server_ref.port}",
                }
                if origin and origin not in expected_origins:
                    self._json(403, {"error": "잘못된 요청 출처입니다"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 65536)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    relative = self.path.removeprefix(
                        f"/{server_ref.token}/"
                    )
                    if relative == "api/configure":
                        result = server_ref.backend.configure(payload)
                    elif relative.startswith("api/action/"):
                        result = server_ref.backend.action(
                            relative.rsplit("/", 1)[-1]
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

        self._server = ThreadingHTTPServer((host, port), Handler)
        self.port = int(self._server.server_address[1])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        self._server.socket = context.wrap_socket(
            self._server.socket,
            server_side=True,
        )

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/{self.token}/"

    def start(self, *, open_browser: bool = True) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="v2r-local-https-control",
            daemon=True,
        )
        self._thread.start()
        if open_browser:
            webbrowser.open(self.url)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def launch_control_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    """Open a dedicated app-style browser window for the localhost controller."""
    candidates: list[Path] = []
    for command in ("chrome", "google-chrome", "msedge", "chromium"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    if sys.platform == "win32":
        roots = [
            Path(os.environ.get("PROGRAMFILES", "")),
            Path(os.environ.get("PROGRAMFILES(X86)", "")),
            Path(os.environ.get("LOCALAPPDATA", "")),
        ]
        candidates.extend(
            root / relative
            for root in roots
            if str(root)
            for relative in (
                Path("Google/Chrome/Application/chrome.exe"),
                Path("Microsoft/Edge/Application/msedge.exe"),
            )
        )
    executable = next((item for item in candidates if item.is_file()), None)
    if executable is None:
        webbrowser.open(url)
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(executable),
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--ignore-certificate-errors",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1180,900",
        ]
    )
