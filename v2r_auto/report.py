from __future__ import annotations

from pathlib import Path

from .models import RunResult


def write_report(result: RunResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.finished_at.strftime("%Y%m%d_%H%M%S_%f")
    path = output_dir / f"실행결과_{stamp}.txt"
    mode = "검증 모드(실제 발행 없음)" if result.dry_run else "실제 발행 모드"
    lines = [
        "V2R 자동 발행 실행 결과",
        "=" * 50,
        f"실행 모드: {mode}",
        f"시작: {result.started_at:%Y-%m-%d %H:%M:%S}",
        f"종료: {result.finished_at:%Y-%m-%d %H:%M:%S}",
        f"전체: {len(result.jobs)}",
        f"완료: {result.succeeded}",
        f"실패: {result.failed}",
        f"건너뜀: {result.skipped}",
        "",
        "상세 결과",
        "-" * 50,
    ]
    for job in result.jobs:
        lines.extend(
            [
                f"[행 {job.row_number}] {job.status.value}",
                f"키워드: {job.keyword or '-'}",
                f"제목: {job.title}",
                f"카페/게시판: {job.cafe} / {job.board}",
                f"결과: {job.message or '-'}",
                f"URL: {job.post_url or '-'}",
                f"댓글: {len(job.comments)}개 (계정 매핑 제공 후 별도 처리)",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    return path
