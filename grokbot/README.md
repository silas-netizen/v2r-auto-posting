# Grok Bot

이 폴더는 Grok Bot 전용입니다. 기존 카페 글 발행 프로그램과 섞지 마세요.

- `config/` — 시트 주소, 브랜드, 카페
- `jobs/search-volume/` — 검색량 채우기
- `jobs/exposure-check/` — 노출 조회
- `shared/` — 브라우저, 로그인, 시트 쓰기 공통
- `logs/` — 실행 로그

새 작업은 `jobs/` 아래에 폴더를 추가합니다.

실행 순서: 검색량 채우기(`search-volume`) 다음 노출 조회(`exposure-check`)
