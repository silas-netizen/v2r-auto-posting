# Grok Bot / V2R desktop program learnings

Read-only research snapshot for the next agent. **Do not treat this file as license to change program behavior.** Actual code and EXEs live on feature branches; `main` still has only the repo README.

Sources: git history and PR bodies on `silas-netizen/v2r-auto-posting` (PRs #2–#31, 2026-08-07 through 2026-08-28). GitHub Issues were not readable (API 403). PR review/issue comments were empty. `grokbot/HANDOFF.md` exists only on `cursor/sheet-search-volume-a2fe`.

---

## 1. What each program does

Grok Bot jobs (`grokbot/HANDOFF.md`) are **검색량 채우기** and **노출 조회** only. Do not mix them with cafe posting. Do not read column E (작성자 비밀번호). Do not type passwords. Login stays with the operator. Daily window mentioned in HANDOFF: 08:15 Seoul, including weekends — do not auto-run until the operator says the sheet is ready.

Configured sheet tabs (`grokbot/config/sheets.json` on `cursor/sheet-search-volume-a2fe`):

| Brand | Tab | Notes |
|---|---|---|
| `newdermiss` | gid `1782605844` | 뉴더미스 노출 현황 |
| `patsoon` | gid `1325327696` | 팥순이 노출 현황 (PR #28: 1177 keywords in CSV) |

`grokbot/jobs/search-volume/` and `grokbot/jobs/exposure-check/` are placeholders only. The runnable Windows programs are the `v2r_auto/gui_*.py` apps below.

### 검색량 채우기 / 노출 확인 (Grok Bot)

| Operator name | EXE | GUI / entry | Latest source / EXE branch | What it does |
|---|---|---|---|---|
| 검색량 채우기 | `V2R-Search-Volume.exe` | `v2r_auto/gui_search_volume.py`, `main_search_volume.py`, spec `v2r-search-volume.spec` | Source: `cursor/sheet-search-volume-a2fe` (PR #29). EXE: `cursor/exe-sheet-search-volume-a2fe` (PR #31, tip `c52dbac`) | Fills **empty** 키워드 검색량 cells and the 통합검색 URL. Does **not** change 노출상태. `0` is already filled. Spacing: first autocomplete item only if it is the same letters with different spaces; otherwise first visible 통합검색 cafe-title match (any cafe). Writes 최종 편집 일시 as now. Sheet-only in the latest EXE (Notion-only filler is the older PRs #26–#27). |
| 노출 확인 | `V2R-Exposure-Checker.exe` | `v2r_auto/gui_exposure.py`, `main_exposure.py`, spec `v2r-exposure-checker.spec` | Source with Notion+Sheet: `cursor/sheet-exposure-a2fe` (PR #28). EXE: `cursor/exe-sheet-exposure-a2fe` (PR #30, tip `9a398bc`). Older Notion-only GUI is still what `cursor/sheet-search-volume-a2fe` carries. | Checks each keyword on Naver 통합검색. Visible our-cafe post + brand marker in title/body/comments → `노출완`; else `밀려남`. Writes status, cafe name (name-only, and only when 노출완 and cafe changed), 키워드 검색량 (Ads Center PC+mobile), 노출된 검색량 (same as volume if 노출완, else blank), 최종 편집 일시. Supports 전체 조회 and 선택 조회. |

**노출 현황 sheet columns the programs bind by header name** (팥순이 sample in `tests/test_exposure_sheet.py`):

| Col | Header | Search-volume writes? | Exposure writes? |
|---|---|---|---|
| A | 카페명 | no | yes, name only, only if 노출완 and cafe changed |
| B | url | no | no |
| C | 발행시간 | no | no |
| D | 작성자 아이디 | no | no |
| E | 작성자 비밀번호 | **never read / never type** | **never read / never type** |
| F | 발행 URL | no | no |
| G | 노출 상태 | no (required to find the tab) | `노출완` / `밀려남` |
| H | 키워드 | yes, if spacing changes | read only |
| I | 통합검색 | yes, Naver 통합검색 URL | no (search-volume job) |
| J | 최종 편집 일시 | yes, now (`YYYY-MM-DD HH:MM:SS`) | yes, now |
| K | 키워드 검색량 | yes, if Ads tool returned a number | yes, if Ads tool returned a number |
| L | 노출된 검색량 | no | 노출완: same as K; 밀려남: clear |

HANDOFF “used columns”: A, G, H, I, J, K, L.

Header aliases in `v2r_auto/exposure.py`: 키워드/검색어; 노출상태/노출 상태; 통합검색/네이버 통합검색; 카페명/카페/ID; 키워드 검색량/검색량; 노출된 검색량; 최종 편집 일시.

Default our cafes: 씨씨앙, 양평맘, 러브인썸, 마이웨딩드림, 우아한갱년기.  
Default brand markers: 팥순추출물, 자연방패 항문세정제, 장으뜸 장어즙, 코숨핏, 그린커피아하바하.

### Other desktop / EXE jobs in this repo

These are **not** Grok Bot jobs. Keep them separate.

| Operator name | EXE | GUI | Latest branch / PR | What it does |
|---|---|---|---|---|
| 제휴 카페 수정 발행 | `V2R-Affiliate-Revision.exe` | `gui_affiliate.py` | `cursor/immediate-cafe-api-f548`, EXE refresh PRs #7/#10/#12 | Posts a daily cafe article via V2R API, then reserves a brand revision (씨씨앙 +4h, 양평맘 +10h). Separate data folder `V2RAffiliatePosting`. |
| 자사 카페 예약 발행 | `V2R-Immediate-Posting.exe` | `gui_immediate.py` | same posting family | Direct/reserved posts to owned cafes (고요한 아침, 러브 인썸, 마이 웨딩 드림). Data folder separate from affiliate. |
| 일상 글 댓글 확인 | `V2R-Comment-Watch.exe` | `gui_comment_watch.py` | `cursor/comment-watch-a2fe` (PR #17), EXE PR #18 | Watches 완료 링크 daily posts on 씨씨앙/양평맘. If another member commented, writes that cafe URL to column K. Skips deleted V2R sources. Clears leftover K when there is no comment. |
| 카페 가입 표시 | `V2R-Join-Marker.exe` | `gui_join_marker.py` | `cursor/join-marker-a2fe` (PR #15), EXE PR #16 | Marks 씨씨앙/양평맘 joined IDs as `가입` on the sheet. Prefers Sheets API via Chrome Google login; paste is fallback. |
| 기관총 붙여넣기 | `V2R-Gatling-Paste.exe` (+ `V2R-Gatling-Master.xlsm`) | `gui_gatling_paste.py` | `cursor/gatling-paste-a2fe` (PR #21), EXE PR #22 | Pastes sheet manuscripts into 기관총. Recognizes `제목`/`본문`/`댓글` even with `#`/`"`/`*` wrappers. Skips duplicate title+body. Fills empty types and proxy chrome/id/password from Excel. |
| 카페 제외 닉네임 | `V2R-Nickname-Exclude.exe` | `gui_nickname_exclude.py` | `cursor/nickname-exclude-a2fe` (PR #24), EXE PR #25 | Operator enters cafe URL, logs into Naver first, walks every cafe article-search page, opens nicknames in Notepad for 신고기 exclude paste. |
| PhotoWasher helper | path remembered by posting EXEs (not a grokbot job) | integrated in affiliate/immediate | PRs around #7 family | Manual image-wash workflow; posting EXEs remember the PhotoWasher path and open batches in Explorer. |

Posting programs also have `사용설명서.md` (`cursor/user-guide-a2fe`, PR #8) and `PROGRAM_HANDOFF.md` (`cursor/immediate-cafe-api-f548`, PR #6). Collection/adaptation generators are **not** in this repo.

Older Notion-only EXEs still exist on `cursor/exe-search-volume-fill-a2fe` (PR #27) and `cursor/exe-exposure-checker-a2fe` / `cursor/exe-visible-tonggeom-a2fe` (PRs #13–#14, #20). Prefer the sheet EXEs above for current operator work.

---

## 2. Recent error history

Chronological, newest first. All of these shipped as commits/PR bodies; there are no GitHub issue threads.

### 검색량 채우기 (2026-08-27 → 08-28)

| When | Commit / PR | Failure | What changed |
|---|---|---|---|
| 08-28 | `1aac697` / EXE `c52dbac` (PR #31) | Clipboard paste in Sheets could **clear the cell** and leave it empty on the first try. Verify then saw blank and retried/aborted. | Write via CDP `Input.insertText` in one shot. Stay on the sheet tab between cells. |
| 08-28 | `4ee7213` / EXE `a17b86f` | Autocomplete first item added **different words** (`비만` → `비만 계산기`). Enter would search the wrong query. | Use first suggestion only when compact letters match. Otherwise ignore and use 통합검색 cafe title. |
| 08-28 | `edee71b` | When paste was unavailable, Sheets column autocomplete replaced a short typed URL with a longer neighbor (`비만` URL overwritten by `비만 계산기`). | Type a throwaway character and delete it so Enter keeps the intended value. |
| 08-28 | `a2288b1` / EXE `7c35e86` | One cell write/verify failure **stopped the whole run**. Typical log: `시트 I13 저장에 3회 실패했습니다` (통합검색 URL stolen by autocomplete). | Per-cell try/except in `update_volume_and_keyword`; filler continues to the next keyword. Treat URL encoding / `HYPERLINK()` as the same value. |
| 08-28 | `1e58306` / EXE `bdfdc89` | **Sheet J write verify abort.** J is 최종 편집 일시. Writer stored `2026-08-28 02:22:14`; CSV export showed `2026-08-28 2:22:14` (unpadded hour). `_verify_sheet_cell` compared strings, retried 3×, then `AutomationError`: `시트 J{row} 저장값을 다시 확인하지 못했습니다`. | `sheet_values_match` compares datetimes by value (`02:22:14` == `2:22:14`, also `2026-8-28` / `2026/8/28`). |
| 08-28 | `d475eb0` / EXE `6ab5dc4` | Same verify abort on **K (키워드 검색량)**. Writer stored `5680`; Sheets displayed `5,680`. Volume write looked failed, so **통합검색 (I) never ran**. | Treat comma-formatted numbers as the same. Also process rows that already have volume but still miss the 통합검색 URL. |
| 08-27 | `260ecbd` (PR #29) | Operator moved off Notion. Notion filler could not write the 노출 현황 sheet. | Search-volume app is Google Sheet only. Notion filler left on PRs #26–#27. |
| 08-27 | `ad6356b` (PR #26) | Empty Notion 검색량 cells; keywords without spaces. | First Notion-only filler + spacing from autocomplete or first cafe title. |

Live spacing check on 팥순이 empty-volume keywords (PR #29 body): `허벅지안쪽살빼기` → skip `허벅지 안쪽 살 빼기 운동`, take `허벅지 안쪽살 빼기`; `복부비만` → skip `여자 복부비만`; `지방추출주사` / `부종원인` follow autocomplete; `내과 식욕억제제 처방` has no autocomplete, keep from cafe title.

### 노출 확인 (2026-08-13 → 08-27)

| When | Commit / PR | Failure | What changed |
|---|---|---|---|
| 08-27 | `fa3c411` (PR #28) | Same checker needed the 노출 현황 **sheet**, not only Notion. | Source radio: 노션 / 구글 시트. Sheet writes status, cafe name, K, L, J. Dry-run writes neither. |
| 08-16 | `ae470e6` (PR #19), EXE PR #20 | Keyword `치질`: 양평맘 `yangmom/727928` was `visibility: hidden` in 통검 HTML. Checker opened hidden cards, found `자연방패 항문세정제`, kept **노출완** while a human saw only other cafes. | Count only cafe posts **visible** on 통합검색. Hidden cards → 밀려남. |
| 08-13–14 | `d0ed1ba`, cafe-first EXE PR #14 | Clustered search results: sub-posts under a main card were treated as their own hits. RE previews were mistaken for other posts. | Only the top representative cafe post. RE preview = comment on that main post. Sub cards → 밀려남. |
| 08-13–14 | `bb1e15f`, `5129d3c` | **Keyword tool missing / wrong box.** Volume was typed into the Ads Center **header search**, not 연관키워드 조회 기준. Log: `키워드 도구 입력칸을 찾지 못했습니다`. | Fill the keyword-tool textarea (placeholder “한줄에 하나씩 입력하세요”), click 조회하기, sum PC+mobile. |
| 08-13–14 | `a998b6a`, `9a301d2` | Ads Center dashboard moved. Old keyword-planner URL did not open the tool. | Open from new Ads Center; use real keyword-planner URL. |
| 08-13–14 | `ec8bd17` | Cafe/ID write picked `씨씨앙/dtsx` (id-suffixed select). | Name-only option (`씨씨앙`). Keep cafe/ID on 밀려남. |
| 08-13–14 | `7ca74b1`, `f13de0e`, `415a45d` | 뉴더미스: tag-named 키워드 column + split linked tables + view id. Checker saw 21 or 23 rows instead of ~334–335. | Prefer the text 키워드 column; read every linked table; honor `?v=` view. `app.notion.com/p/` URLs work. |
| 08-13 | `4aae967`, `b3a6cbf` | Product-review cafe cards and selected-keyword runs were missing. | Detect our cafes in 상품리뷰; add 선택 조회 + volume update. |
| 08-13 | `b8fd3d2` | Naver login dropped between keywords. | Keep login until the EXE is closed. Do not close Chrome. |
| 08-13 | `d496e5c` (PR #13) | First Notion exposure checker. | Baseline behavior in `노출관리설명서.md`. |

### Shared Chrome / driver / packaging

| When | Commit / PR | Failure | What changed |
|---|---|---|---|
| 08-07 | `ae89b83` (PR #2) | Windows EXE: `No module named` Selenium / Chrome bits missing. | `collect_all("selenium")` in PyInstaller specs. |
| ongoing | `v2r_auto/browser.py` `V2RBrowser.start` | `webdriver.Chrome(options=...)` with **no explicit chromedriver path**. Relies on Selenium Manager + a real Chrome install on the operator PC. If Chrome is missing or the driver cannot start: `브라우저가 시작되지 않았습니다` / `Chrome이 시작되지 않았습니다`. | Not a code bug by itself. Operator must have Chrome. Do not close the automation Chrome window. |
| ongoing | `SeleniumNaverSearch._ensure_browser` | Operator closed Chrome mid-run. | Reopens Chrome and warns that login may have dropped. |
| 08-27 | `f3f1743` (nickname EXE) | Startup crash: `No module named v2r_auto.nickname_browser` (broken indent omitted the file from the bundle). | Spec/hiddenimport fix. Old nickname EXE must not be reused (PR #25). |

### Other EXE jobs (short)

- **Comment watch (PR #17):** deleted V2R daily source aborted the whole watch. Now skip and continue. Leftover K links were not cleared when no comment remained.
- **Join marker:** clipboard UTF-16 BOM landed in the first cell; paste hit the cell editor instead of the grid; chunk paste missed cells. Later: one paste + leftover cells, then Sheets API.
- **Gatling paste:** read the brand sheet as the 일상 글 sheet; filled row 31 instead of the first empty title/body; Drive folder UI only listed ~50 files so late `{키워드}` images were missing; label marks `#제목` were dropped.
- **Posting (PRs #9–#12):** Chrome treated the previous brand CSV download as the new 일상 글 CSV. Headers `내용`/`카페` are now required; retry up to 3 times.
- **Nickname exclude:** missing cafe URL field; login after cafe open failed; only first search page collected.

---

## 3. Operator caveats

### dry_run / 검증 default

- Both Grok Bot GUIs default **`dry_run = True`** (`검증 모드`).
- Search-volume label: `검증 모드(시트에 쓰지 않음)`. Confirm dialog: Naver only, no sheet writes.
- Exposure label follows source: Notion → `검증 모드(노션에 쓰지 않음)`; Sheet → `검증 모드(시트에 쓰지 않음)`.
- Start always asks Yes/No. Unchecking 검증 is a second confirm that real writes will happen (search-volume: empty volume + 통합검색 + spacing + J timestamp; exposure: status + volume + cafe-if-changed + J).
- **Always run 검증 once, then uncheck and run again.** HANDOFF/PR #29 say the same.
- Dry-run does not prove sheet write/verify. J/K display mismatches only appear on a real write.

### Skip rules — 검색량 채우기

Skip (do not overwrite):

- 키워드 검색량 already has any non-blank value, including **`0`**.
- Duplicate `page_id` (sheet row number).
- Keyword that is empty after stripping `(메모)`.
- Keyword-column type not in `{title, rich_text, text}` → spacing is not written (volume/URL still can be).

Still process:

- Volume present but 통합검색 empty → write I (+ J) only.
- Autocomplete first item has extra words → ignore it; try first visible cafe title.
- No spacing source → leave H as-is, still try volume + URL.
- Volume lookup failed → still write 통합검색 + J; leave K.

After `a2288b1`: one cell/row failure increments `failed_rows` and **continues**. End dialog: `N건 중 M건은 시트에 다 못 넣었습니다`.

### Skip rules — 노출 확인

- Empty keyword after stripping parentheses → skip that row.
- 선택 조회: keyword not in Notion/sheet → log and skip.
- Empty sheet keyword rows are skipped at load (`키워드가 비어 건너뛴 행`).
- Hidden 통검 cafe cards → not 노출완.
- Clustered sub-results / non-representative cards → 밀려남.
- Not our cafe, or our cafe post without a brand marker → 밀려남.
- 밀려남: **do not clear** cafe/ID (Notion) / 카페명 (sheet).
- 노출완 and same cafe → keep current cafe/ID (including `/id` suffix).
- 노출완 and different cafe → write **name only**.
- Keyword-tool volume missing: still write status/cafe; leave K/L alone.
- `<10` / `미만` volume → store **10**.

**Exposure sheet writes are stricter than search-volume.** `update_check_result` has no per-cell continue. `ExposureChecker.run` has no per-row try/except. A J/K verify abort can still **stop the whole exposure run**. Search-volume already continues; exposure sheet mode does not.

### Keyword tool missing

Volume comes from Naver **Ads Center → 도구 → 키워드 도구**, not from Naver search login.

- Chrome 준비 opens Naver **and** an Ads tab. Operator must log into **both**.
- Type into 연관키워드 조회 기준 (placeholder 한줄에 하나씩 입력하세요). Never the header search box.
- Spaces are stripped for the tool query (`뱃살 다이어트 보조제` → `뱃살다이어트보조제`).
- If `_focus_keyword_tool` fails once, `SeleniumNaverSearch._volume_unavailable = True` for the **rest of that process**. Every later keyword skips volume. Restart the EXE (and log into Ads) if you see: `광고주센터 키워드 도구로 들어가면 검색량을 채울 수 있습니다`.
- Box-not-found log: `키워드 도구 입력칸을 찾지 못했습니다`.
- Search-volume without `lookup_search_volume`: `검색량 조회 기능이 없습니다`.

### Chrome / driver

- Latest code starts Chrome with Selenium Manager (`webdriver.Chrome(options=...)`). There is **no bundled chromedriver path**. Chrome must be installed on the operator PC and must stay open.
- Profile lives under the app data folder (`V2RSearchVolume` / `V2RExposureChecker`). Second instance is blocked (`AnotherInstanceRunningError`).
- Closing Chrome mid-run: `크롬 창이 닫혀 다시 엽니다. 로그인이 풀렸으면 한 번만 다시 로그인하세요`.
- Historical EXE gap: Selenium modules omitted from the bundle (PR #2). Current specs `collect_all("selenium")`.
- Attach-to-existing-Chrome (`debugger_address`) exists for posting; Grok Bot apps start their own profile Chrome.
- Sheet read: public CSV export first; if HTML/login wall, download via that Chrome session. Need `gid=` of the **노출 현황** tab. Anyone-with-link-edit can read/write without Google login; otherwise Google-login in the automation Chrome.

### Notion vs sheet

| | 검색량 채우기 (latest) | 노출 확인 (latest EXE) |
|---|---|---|
| Source | **Google Sheet only** | Operator picks **노션** or **구글 시트** (default saved source: `notion`) |
| Token | none | Notion: integration secret, invited to the DB. Never commit it. |
| URL | 노출 현황 tab URL with `gid=` | Notion DB/view URL (`?v=` honored) **or** sheet tab URL with `gid=` |
| What it writes | H (spacing), I, J, K | G, A (conditional), J, K, L |
| What it must not write | G 노출상태, E password | E password; cafe on 밀려남 |
| Older EXE | PRs #26–#27 still Notion-only | PRs #13–#14, #20 Notion-only |

`cursor/sheet-search-volume-a2fe` has `exposure_sheet.py` but its `gui_exposure.py` is **still Notion-only**. Do not assume that branch’s exposure GUI can write sheets. Use `cursor/sheet-exposure-a2fe` / PR #30 EXE for sheet-mode 노출 확인.

Notion leftover bugs (fixed on the Notion path, still relevant if someone uses the old EXE): split linked tables (21 of 334), tag-vs-text 키워드 column, `노출 완` vs `노출완`, name-only cafe option.

### Sheet J write verify (operator meaning)

J = 최종 편집 일시. After each write, `update_sheet_cell` exports the sheet CSV up to 10 times and compares with `sheet_values_match`. Mismatch → retry up to 3 times → `시트 J{row} 저장값을 다시 확인하지 못했습니다 (기대 … / 실제 …)`.

Already treated as success: unpadded times, comma thousands, URL encoding, `HYPERLINK()`.

If J verify still fails on a **real** (non-검증) run:

- Search-volume: that keyword is skipped; the rest continue.
- Exposure: the run can abort. Re-run from the next keyword via 선택 조회, or fix the cell format and retry.

Do not “fix” J by rewriting the programs in a learning-only task.

### Grok Bot operating rules (from HANDOFF + PRs)

1. Process **all matching rows**. Do not cherry-pick keywords unless the operator asked for 선택 조회.
2. Do not auto-start the daily 08:15 job until the operator says the sheet is ready.
3. Use the tab URL as opened (`gid=` required).
4. Never read or type column E.
5. Chrome 준비 → 빈 검색량/키워드 확인 → 검증 모드 once → uncheck 검증 → real run.
6. Take EXEs from GitHub branch blobs / Releases on this repo. Do not copy binaries off the operator PC.
7. Cafe posting EXEs, 기관총, join marker, comment watch, and nickname exclude are out of Grok Bot scope unless the operator says otherwise.
