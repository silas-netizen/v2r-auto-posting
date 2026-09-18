# V2R 내부 명령 시스템

실행 PC가 V2R 반복 작업을 토큰 없이 처리합니다. 사용자는 한국어로 명령하고,
다른 PC에서는 Telegram 또는 Slack으로 같은 명령을 보냅니다.

일상 글 수집과 일상 글 발행 계획만 Claude가 담당합니다. 계정 배정, 시간 계산,
중복 검사, V2R 등록, 수정 연결, 댓글 예약은 Python이 실행합니다.

```text
한국어 자연어 명령
        ↓
명령 해석기
  ├─ 명확한 표현: 규칙 파서, AI 토큰 0
  └─ 자유로운/모호한 표현: Claude
        ↓
표준 작업 JSON
        ↓
SQLite 작업 대기열
        ↓
Python 단일 실행기
        ↓
전용 Chrome으로 V2R UI 조작
        ↓
결과 검증·기록·알림
```

## 실행 PC에서 쓰는 법

```powershell
.\install.ps1
set V2R_BROWSER=playwright
.\start.cmd run "로그인 창 열어줘"
.\start.cmd run "내일 오전 9시부터 오후 6시까지만 일상 글 20개 올려줘. 아이디 5개 자동으로 쓰고 10분 간격으로 해줘."
.\start.cmd run "일상 글 수집해줘"
.\start.cmd run "전체 원본 지금 동기화"
.\start.cmd run "최근 7일 실패 글 점검해줘"
.\start.cmd status
.\start.cmd stop
```

기본값은 검증 모드입니다. 로그인과 1건 검증이 끝난 뒤에만 `실제 발행`을 붙입니다.

## 자사·제휴 내부 발행

- 자사: 카페·계정·게시판 선택 → 원고 입력 → 예약 → 등록 → 상세 URL 재확인
- 제휴: 랜덤 일상 등록 → 원본 URL 확인 → 카페별 수정 예약 → 브랜드 원고 등록 → 댓글 예약
- 실행 PC의 `browser-profile`에 로그인 세션만 유지하며 비밀번호는 저장하지 않음
- V2R API를 직접 호출하지 않고 보이는 UI만 사용
- V2R이 비정상 접근 경고를 표시하면 우회하지 않고 즉시 중단

실제 Playwright 브라우저를 쓰려면 `V2R_BROWSER=playwright`를 설정합니다.
설정하지 않으면 모든 단계가 기록만 되는 검증 브라우저로 실행됩니다.

## 외부에서 메시지로 명령

실행 PC 환경변수만 사용합니다. 저장소나 채팅에 토큰을 넣지 않습니다.

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
SLACK_WEBHOOK_URL=...
SLACK_ALLOWED_CHANNEL_IDS=C0123456789
ANTHROPIC_API_KEY=...
```

```powershell
.\start.cmd listen --once
```

허용된 채팅과 허용된 작업만 실행합니다. 원격 메시지로 셸 명령을 열지 않습니다.

## Claude가 하는 일

- 공개 페이지·피드·공개 시트에서 일상 글 후보 수집
- 수집한 글을 한국어 일상 원고로 정리
- `publish_daily` 작업 JSON 작성

Claude 키가 없으면 공개 수집 결과 또는 시트 캐시를 그대로 사용하고, 발행 계획은
규칙 엔진이 만듭니다.

## 공개 수집 범위

이 저장소의 수집기는 공개 페이지 리더입니다.

- 공개 HTML, RSS/Atom, Open Graph, JSON-LD
- 공개 Google Visualization CSV
- 로그인·페이월이면 `authentication required`로 정지
- TLS 위장, CAPTCHA 해제, WAF 우회 없음

## 카페와 원본

제휴 게시판:

- 씨씨앙 → 자유 수다방
- 양평맘 → 이모저모 이야기
- 쌍둥이맘모여라 → 가족업체 자유게시판

자사 카페 게시판은 V2R 화면의 현재 권한을 다시 확인합니다.

제외 원본 문서 ID `1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT` 는 동기화하지 않습니다.

## 계정 규칙

- 작업 구분과 `V2R` 연동이 맞는 계정만 사용
- 회색 음영, 미확인, 매니저 등급 제외
- 부족한 계정을 임의로 채우지 않음
- 영문만 된 닉네임은 한글+숫자 변경 대상으로 표시

## 발행 시간

- 시작 시각 포함, 종료 시각 제외
- 종료를 넘기면 다음 허용 시간대 시작으로 이월
- 자정 넘김 지원, 예: 23:00~02:00
- 이미 V2R에 넣은 예약은 설정 변경으로 고치지 않음

## PC 이전

함께 옮기는 것: 코드, `config`, `data/v2r.sqlite`, 로그, 사진 조합 기록.
새 PC에서 다시 넣는 것: API 키, V2R 로그인, Google 로그인, 로컬 사진 경로.

```powershell
.\backup.cmd
.\restore.cmd data\backup_YYYYMMDD_HHMMSS.sqlite
```

실행기는 임대 잠금을 쓰므로 두 PC가 같은 작업을 동시에 실행하지 않습니다.
