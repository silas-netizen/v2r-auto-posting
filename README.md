# V2R internal command runtime

The execution PC now runs V2R work as local Python commands. Clear Korean
instructions become a job JSON without spending model tokens. Telegram or Slack
can send the same commands from another machine. Only daily-post collection and
daily-post planning use Claude, and only when `ANTHROPIC_API_KEY` is set.

Korean guide: [README.ko.md](README.ko.md)

```text
한국어 명령  →  규칙 파서(토큰 0)  →  SQLite 대기열  →  실행 PC 워커
                     ↘ 모호한 명령만 Claude
일상 글 수집/발행 계획  →  Claude
V2R 등록/예약/계정/중복  →  Python
```

## What stays token-free

- command parsing for explicit Korean
- Google Sheet / Excel source sync
- account filtering and assignment
- publish-window and interval math
- duplicate checks
- V2R browser steps, revision links, comment trees
- SQLite history, backup, restore

## What Claude does

- collect public daily-post material
- rewrite that material into manuscripts
- turn collected manuscripts into a `publish_daily` plan

The worker still registers the plan on V2R. Claude does not receive API keys
from chat, and it does not run arbitrary shell commands.

## Command examples

```powershell
.\start.cmd run "내일 오전 9시부터 오후 6시까지만 일상 글 20개 올려줘. 아이디 5개 자동으로 쓰고 10분 간격으로 해줘."
.\start.cmd run "일상 글 수집해줘"
.\start.cmd run "최근 실패 글 점검해줘"
.\start.cmd status
.\start.cmd stop
.\start.cmd listen --once
```

Default publish mode is dry-run. Add `실제 발행` only after login and a successful
verification run.

## External messages

Set these on the execution PC, never in the repository:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_CHAT_IDS
SLACK_WEBHOOK_URL
SLACK_ALLOWED_CHANNEL_IDS
ANTHROPIC_API_KEY
```

Unknown chats and unknown tasks are rejected. Remote messages cannot open a
shell.

## Public collection boundaries

The bundled reader is the README-equivalent collector for public pages:

- public HTML, RSS/Atom, Open Graph, JSON-LD
- public Google Visualization CSV
- stop at login or paywall
- no TLS impersonation, CAPTCHA solving, or WAF bypass

## Install on a new PC

```powershell
.\install.ps1
.\backup.cmd
.\restore.cmd data\backup_YYYYMMDD_HHMMSS.sqlite
```

Move code, `config\`, `data\v2r.sqlite`, and logs together. Re-enter API keys
and V2R/Google logins on the new PC. Only one executor lease can run a job.

## Safety

- dry-run is the default
- exact duplicates are blocked
- high similarity requires review
- grey-shaded and manager accounts are excluded
- excluded document `1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT` is never synced
- V2R results must be re-read before a job is marked complete
