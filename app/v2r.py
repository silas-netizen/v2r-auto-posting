from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .paths import project_root
from .store import JobStore
from .worker import CommandRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실행 PC에서 V2R 작업을 토큰 없이 명령으로 처리합니다.",
    )
    parser.add_argument("--root", default="", help="프로그램 루트. 없으면 V2R_HOME 또는 저장소 루트")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="한국어 명령 한 건을 해석하고 실행합니다")
    run.add_argument("text", nargs="+", help="한국어 명령")
    sub.add_parser("worker", help="대기열에서 작업 한 건을 실행합니다")
    sub.add_parser("status", help="최근 작업 상태를 보여줍니다")
    sub.add_parser("stop", help="대기·실행 중 작업을 중지합니다")
    listen = sub.add_parser("listen", help="Telegram/Slack 명령을 한 번 읽습니다")
    listen.add_argument("--once", action="store_true")
    return parser


def runtime_from_args(args: argparse.Namespace) -> CommandRuntime:
    root = Path(args.root).expanduser() if args.root else project_root()
    return CommandRuntime(root, store=JobStore(root / "data" / "v2r.sqlite"))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = runtime_from_args(args)
    if args.command == "run":
        result = runtime.handle_text(" ".join(args.text))
    elif args.command == "worker":
        result = runtime.run_once() or {"ok": True, "processed": None}
    elif args.command == "status":
        result = runtime.status()
    elif args.command == "stop":
        result = runtime.handle_text("작업 중지")
    elif args.command == "listen":
        result = _listen_once(runtime)
    else:
        parser.error("알 수 없는 명령입니다")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


def _listen_once(runtime: CommandRuntime) -> dict:
    accepted: list[dict] = []
    if runtime.telegram.enabled():
        for update in runtime.telegram.poll():
            try:
                spec = runtime.telegram.parse_update(update)
            except (PermissionError, ValueError) as exc:
                accepted.append({"source": "telegram", "error": str(exc)})
                continue
            if spec is None:
                continue
            accepted.append(runtime.handle_text(spec.notes or spec.task))
    return {"ok": True, "accepted": accepted}


if __name__ == "__main__":
    raise SystemExit(main())
