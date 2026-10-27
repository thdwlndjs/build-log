# build-log

학습한 내용과 해결한 오류, 데이터 시스템 지식(테이블·용어·조인 경로·규칙)을 기록하고 재사용하는 로컬 지식 저장소입니다. 외부 DB나 API 키는 사용하지 않습니다.

기본 저장소는 `%LOCALAPPDATA%\Buildlog\buildlog.sqlite3`입니다. `--store <경로>`는 `-m buildlog` 바로 뒤에 둡니다.

## 공통 설정

저장소와 agy 경로는 CLI, Claude Code, Codex가 함께 쓰는 설정입니다. 우선순위는 명령 옵션(`--store`, `--agy`) > 환경 변수(`BUILDLOG_STORE`, `BUILDLOG_AGY`) > 설정 파일 > 기본값입니다.

```powershell
.\.venv\Scripts\python.exe -m buildlog config show                 # 현재 값과 출처
.\.venv\Scripts\python.exe -m buildlog config set store "D:\kb\buildlog.sqlite3"
.\.venv\Scripts\python.exe -m buildlog config unset store
```

설정 파일은 `%APPDATA%\Buildlog\config.toml`이고, `BUILDLOG_CONFIG`로 위치를 바꿀 수 있습니다. 키는 `store`와 `agy` 두 개입니다. 값에 `%LOCALAPPDATA%` 같은 환경 변수를 쓸 수 있고, 상대 경로는 설정 파일 위치를 기준으로 합니다. 알 수 없는 키나 잘못된 TOML은 입력 오류(종료 코드 2)로 처리합니다.

## 명령

```powershell
# 프로젝트 환경에 패키지 설치
.\.venv\Scripts\python.exe -m pip install -e .

# 저장소 생성 또는 최신 구조로 갱신 (기존 데이터는 지우지 않음)
.\.venv\Scripts\python.exe -m buildlog init

# UTF-8 JSON 저장 계획으로 저장
.\.venv\Scripts\python.exe -m buildlog ingest --input .\plan.json

# 페이지·용어·관계·조인 경로·규칙을 ID로 읽기 (Markdown, 또는 --format json)
.\.venv\Scripts\python.exe -m buildlog read <id>

# 버전을 확인하고 수정
.\.venv\Scripts\python.exe -m buildlog update <id> --expected-version 1 --patch .\patch.json --reason "정정 이유"

# 질문으로 관련 기록 찾기 (작업 에이전트용 Markdown, 또는 --format json)
.\.venv\Scripts\python.exe -m buildlog query "ORA-12541 오류" --term listener --scope demo_mes
.\.venv\Scripts\python.exe -m buildlog query --input .\query.json

# 관계, 조인 경로
.\.venv\Scripts\python.exe -m buildlog neighbors <page_id> --type JOINS_TO --direction out
.\.venv\Scripts\python.exe -m buildlog paths --system demo_mes --from site --to sensor_reading

# 검색 색인 재구축 (색인은 언제든 다시 만들 수 있는 파생 데이터)
.\.venv\Scripts\python.exe -m buildlog reindex

# Antigravity 외부 조사: 저장 계획의 research 요청 실행, 또는 기존 페이지에 질문 추가
.\.venv\Scripts\python.exe -m buildlog research --run <run_id>
.\.venv\Scripts\python.exe -m buildlog research --page <page_id> --question "질문" --context "맥락"

# 저장 실행 기록: 단계·상태·시도 횟수·보존된 실패
.\.venv\Scripts\python.exe -m buildlog runs [--status failed]

# Claude Code·Codex 스킬 설치/갱신 (skills/buildlog/ 원본에서)
.\.venv\Scripts\python.exe -m buildlog install-skill [--host claude-code|codex|all] [--dry-run]

# 백업·복원, Markdown 내보내기·가져오기, 웹 근거 원문 대조 (아래 "운영" 참고)
.\.venv\Scripts\python.exe -m buildlog backup --out D:\kb\buildlog-20261027.sqlite3
.\.venv\Scripts\python.exe -m buildlog restore --from D:\kb\buildlog-20261027.sqlite3
.\.venv\Scripts\python.exe -m buildlog export --out D:\kb\export [--page <page_id>] [--force]
.\.venv\Scripts\python.exe -m buildlog import --in D:\kb\export [--dry-run]
.\.venv\Scripts\python.exe -m buildlog verify-sources --page <page_id> | --all
```

## Claude Code·Codex에서 사용

두 호스트 모두 같은 프로그램과 같은 저장소를 씁니다. 스킬 원본은 저장소의 [skills/buildlog/](../skills/buildlog/)에 있고, `install-skill`이 호스트별 표기(대화 이름, `idempotency_key` 접두어, 파일 쓰는 방법, Python 절대 경로)만 바꿔 설치합니다.

| 호스트 | 설치 위치 |
| --- | --- |
| Claude Code | `%USERPROFILE%\.claude\skills\buildlog\` |
| Codex | `%USERPROFILE%\.codex\skills\buildlog\` (`CODEX_HOME`이 있으면 그 아래) |

설치기가 쓰지 않은 기존 파일은 덮어쓰기 전에 `*.bak-<시각>`으로 백업합니다. 스킬을 고칠 때는 설치된 사본이 아니라 원본을 고치고 다시 설치합니다.

어느 프로젝트에서든 요청하면 됩니다. 파일 읽기, JSON 작성, 명령 실행은 호스트 에이전트가 합니다.

- "이 문제 해결한 거 buildlog에 저장해줘" → 대화의 경험을 정리해 저장
- "이 DDL이랑 운영 문서 buildlog에 인제스트해줘" → 테이블·컬럼·용어·조인 경로·규칙으로 정리해 저장
- "예전에 이 오류 본 적 있어? buildlog에서 찾아줘" → 검색어를 보강해 조회하고 관련 기록과 규칙을 작업에 적용
- "이 기록 공식 문서 근거 조사해줘" → Antigravity로 조사해 출처를 연결
- "buildlog에서 `<id>` 읽어줘" → Markdown으로 보여줌

## 작업 공간 화면

```powershell
.\.venv\Scripts\python.exe -m buildlog serve --open          # 이 창에서 실행, Ctrl+C로 종료
.\.venv\Scripts\python.exe -m buildlog serve --detach        # 백그라운드 실행 후 주소 반환
.\.venv\Scripts\python.exe -m buildlog serve --status | --stop
```

- 주소는 `http://127.0.0.1:8765/`(`--port`로 변경)이고, loopback 주소에만 바인딩합니다. 외부 CDN 없이 저장소에 포함된 Cytoscape.js·marked·DOMPurify·lucide 아이콘을 씁니다.
- 화면 배치는 기준 화면 `reperence/buildlog-workbench.html`을 따릅니다: 제목줄, 왼쪽 탐색기, 가운데 탭 작업 영역(문서), 오른쪽 속성(주변 연결 그래프와 정보·이력·근거), 아래 상태줄.
- 문서 탭에서는 문서와 그 주변 그래프를 함께 봅니다. 주변 그래프의 노드나 "관련 문서"를 누르면 그 문서가 새 탭으로 열리고 그래프도 그 문서를 따라갑니다.
- 주소: `?tab=doc:<page_id>`가 문서 탭입니다. 탭을 옮길 때마다 기록이 쌓여 뒤로·앞으로 가기와 직접 링크가 됩니다. 예전 주소 `?open=<page_id>`, `?focus=<page_id>`도 그 문서 탭으로 열립니다. 열린 탭 목록은 브라우저에 기억됩니다.
- 본문 수정: 도구줄 [본문 수정] → 입력 → [저장]. 새 버전으로 기록되고 이력 탭에 남습니다. 본문만 바뀌므로 요약에 "갱신 필요" 표시가 붙고, 검색에는 새 본문이 바로 반영됩니다.
- 모든 수정은 `expected_version`을 확인합니다. 그사이 다른 곳(CLI, 다른 탭)에서 바뀌었으면 덮어쓰지 않고 `VERSION_CONFLICT`를 상태줄에 보여줍니다. 입력한 내용은 그대로 남습니다.
- 수정 요청은 서버가 페이지에 넣어 준 세션 토큰과 같은 Origin일 때만 받습니다. 저장된 Markdown 속 HTML은 글자로 보여 주고, 렌더링 결과도 DOMPurify로 정화합니다.
- 연결 진행 상황(plan.md Phase 5):
  - 5-3: 문서·관련 문서·주변 그래프·정보·이력·근거·본문 수정
  - 5-4: 탐색기 세션(목차 묶음)·소스, 전체 문서(유형 필터), 검색 대화상자, 세션·소스 탭, 근거에서 소스로 이동. 목차에 없는 문서가 있으면 상태줄과 탐색기 아래에 건수를 보여줍니다
  - 5-5 예정: 전체 그래프와 저장 결과 주소 `?run=<run_id>`(`ingest` 결과의 `graph.url`), 설정
- 이전 검토 화면의 기능 중 작업 공간에 아직 연결하지 않은 것(제목·요약·상태 수정, 관계·용어·규칙·조인 경로 수정, 연결 해제, 병합, 피드백, 조인 경로 강조)은 아래 CLI로 합니다. 연결 여부는 [workbench-menu-draft.md](workbench-menu-draft.md)에 정리했습니다.

CLI로 하려면:

```powershell
.\.venv\Scripts\python.exe -m buildlog unlink <relation_id> --expected-version 1 --reason "이유"
.\.venv\Scripts\python.exe -m buildlog feedback add <id> --expected-version 1 --text "틀린 점"
.\.venv\Scripts\python.exe -m buildlog feedback list --status open
.\.venv\Scripts\python.exe -m buildlog merge <대표_id> <중복_id> --expected-version <대표_id>=1 --expected-version <중복_id>=1 --reason "이유"
.\.venv\Scripts\python.exe -m buildlog update <id> --expected-version 2 --patch .\patch.json --reason "이유" --feedback <feedback_id>
```

- 해제한 관계는 조회·검색·그래프에서 빠지지만 행, 근거, 이력은 남고, 이후 같은 내용을 저장해도 되살아나지 않습니다(경고만 냄).
- 병합하면 중복 페이지는 `superseded`가 되고 대표 페이지로 redirect됩니다. 옛 ID로 `read`하거나 저장 계획에서 참조해도 대표로 연결됩니다. 관계는 대표로 옮겨지고, 같은 의미의 관계는 근거를 합쳐 하나로 남기며, 자기 자신을 가리키게 되는 관계는 해제됩니다. 근거, 용어, 규칙, 목차, 조사 요청도 옮겨집니다. 대표 페이지의 본문은 자동으로 다시 쓰지 않습니다.

브라우저 E2E 테스트에는 Playwright가 필요합니다: `pip install -e .[dev]` 후 `python -m playwright install chromium`. 설치되지 않았으면 해당 테스트는 건너뜁니다.

## 외부 조사 (Antigravity)

`research`는 Antigravity CLI(`agy`)로 공식 문서 등의 근거를 찾아 기록에 출처로 연결합니다. 조사는 `ingest` 트랜잭션 밖에서 실행하므로, 조사가 실패해도 저장한 경험은 사라지지 않습니다.

- 준비: `agy.exe`를 한 번 실행해 로그인합니다. 경로는 기본 `%LOCALAPPDATA%\agy\bin\agy.exe`이며 `BUILDLOG_AGY` 또는 `--agy`로 바꿀 수 있습니다.
- 실행 방식: `agy --print … --output-format json --json-schema <파일> --mode plan --print-timeout <초>`를 인자 배열로, 빈 임시 폴더에서 실행합니다. 권한 생략 옵션(`--dangerously-skip-permissions`)은 쓰지 않고, 프롬프트로 웹 검색·읽기 외 도구를 금지합니다.
- 성공 판정: agy는 권한 거부나 시간 초과 때도 종료 코드 0과 `status: SUCCESS`를 냅니다. 그래서 스키마에 맞는 구조화 결과와 유효한 근거가 1건 이상 있을 때만 성공으로 봅니다.
- 실패 분류: `not_installed`, `auth`, `permission`, `timeout`, `output`, `no_findings`, `process`. 실패하면 종료 코드 5를 내고, 같은 명령으로 끝나지 않은 요청만 재개합니다. 이미 성공한 요청을 `--retry-succeeded`로 다시 돌리다 실패해도 기존 결과와 상태는 유지됩니다.
- 저장: 근거마다 `web` 출처와 근거를 만들고 조사자가 밝힌 확보 수준(원문 확인 fetched, 검색 결과 요약 search_snippet, 모델 기억 from_memory)을 남깁니다. 조사 직후 Buildlog가 원문 페이지를 직접 열어 리디렉션을 풀고 인용문을 대조합니다(아래 "웹 근거 원문 대조"). 근거는 인용이 원문에 그대로 있을 때만 `observed`이고, 나머지는 확보 수준과 관계없이 `reported`입니다. 결론 수정은 하지 않습니다. 근거가 결론을 바꾸면 호스트가 `update`로 반영합니다.
- 페이지 Markdown의 "외부 조사" 섹션에서 요청별 상태와 시도 횟수, 실패 원인을 볼 수 있습니다.

## 검색

`query`는 질문 단어와 호스트가 보강한 검색어(`terms`)로 후보를 찾고, 작업 에이전트가 그대로 읽을 Markdown 묶음을 돌려줍니다.

- 색인: 페이지 제목·별칭·요약·본문, 그리고 페이지에 연결된 용어 정의·규칙·조인 경로·컬럼 이름을 SQLite FTS5 trigram으로 색인합니다. 한국어 조사("파티션을")는 떼고 찾습니다. 3자 미만 검색어("설치", "권한")는 trigram으로 찾을 수 없어 제목·별칭·요약·본문을 부분 문자열로 찾습니다. 제목·별칭 일치가 더 높은 점수를 받습니다. 질문 속 FTS 문법은 일반 글자로 취급합니다.
- 순위: 제목·별칭 일치에 가중치를 두고, 오류 코드·명령 이름 같은 식별자가 정확히 일치하면 크게 올립니다. 대체(superseded)·폐기(deprecated)된 기록은 내립니다. 최고 점수의 20%에 못 미치는 후보는 고르지 않습니다.
- 범위(`scope`): 시스템 키를 주면 그 시스템의 테이블과 연결된 기록만 고릅니다. 환경 단어를 주면 제목·요약·`## 환경` 섹션에서만 판단합니다. 범위 밖 후보는 숨기지 않고 "다른 범위의 후보"로 따로 보여줍니다.
- 기본값: 후보 20, 본문을 넣는 기록 5, 관계 1단계 확장 최대 10, 출력 예산 24,000 bytes.

출력 순서:

1. 반드시 지킬 규칙: 선택한 기록의 필수 조건·금칙·미지원 연산, 그리고 함께 전달한 조인 경로가 지나는 테이블의 규칙. 예산 때문에 본문이 빠진 기록의 규칙도 남깁니다.
2. 질문에 나온 용어: 같은 표현의 모든 맥락별 정의와 범위 일치 여부
3. 상충·정정 기록: CONTRADICTS, SUPERSEDES 관계
4. 기록 본문: 기록 단위로만 넣고 중간에 자르지 않습니다.
5. 관련 기록, 다른 범위의 후보, 생략한 기록(ID와 읽는 방법)

첫 기록조차 예산에 들어가지 않으면 잘라내지 않고 `BUDGET_TOO_SMALL`과 필요한 크기를 돌려줍니다. 결과가 없으면 "관련 기록을 찾지 못했다"고 반환합니다.

`paths`는 등록된 조인 경로를 먼저 돌려줍니다. 없으면 JOINS_TO 관계로 깊이 4 이내의 후보를 최대 5개 찾아 **추정 후보**로 표시합니다.

## 저장 계획

형식은 [plan-format.md](../skills/buildlog/plan-format.md)에 있습니다. Phase 1 형식(`page` 하나)도 계속 받습니다.

| 입력 | 처리 |
| --- | --- |
| 대화 (`conversation`) | 호스트가 정리한 경험 페이지 |
| Markdown·텍스트 | 원문을 보관하고, 근거로 인용한 문장이 원문에 실제로 있는지 확인해 줄 번호를 기록 |
| SQL DDL (`postgresql`, `oracle`) | `CREATE TABLE`, `COMMENT ON`, `ALTER TABLE ... ADD 제약`, `CREATE [UNIQUE] INDEX`를 해석. 그 밖의 구문은 줄 번호와 함께 진단으로 남기고 `parse_status: partial`로 보고 |
| schema_json | 같은 테이블 구조로 변환. `nullable`이 없으면 "미확인"으로 둠 |
| CSV·JSON 샘플 | 앞 50행만 보관하고 자격증명처럼 보이는 컬럼은 `[REDACTED]`로 바꿈. 테이블에 없는 컬럼은 경고 |

검증 규칙:

- FK는 `extracted` 관계로 자동 저장됩니다. 그 밖의 연결은 근거를 붙여 `inferred`로 저장합니다.
- 조인 단계의 관계 수(1:N 등)와 NULL 여부는 PK·FK로 증명될 때만 자동으로 채웁니다. 근거 없이 단정하거나 스키마와 모순되는 값은 거부합니다.
- 미지원 연산의 "부재 확인됨"은 직접 근거(observed·reported)가 있어야 합니다. 언급이 없을 뿐이면 "미확인"입니다.
- 같은 표현이라도 시스템·테이블·컬럼 맥락이 다르면 별도의 용어로 저장합니다.
- 바뀐 스키마를 다시 넣으면 `SCHEMA_CONFLICT`가 납니다. `replace_existing_schema: true`를 지정해야 버전이 올라가며 갱신되고, 사라진 컬럼은 `deprecated`로 표시됩니다.
- 계획 중 하나라도 실패하면 데이터는 아무것도 저장하지 않습니다. 대신 실행 기록에 도달한 단계, 오류 종류, 메시지를 남깁니다(`runs`).
- schema_json 안의 `samples`(`{"table", "rows"}` 또는 `{"table", "format", "content"}`)는 별도 샘플 출처로 처리합니다. 행은 스키마 출처 원문에 다시 보관하지 않습니다.
- 저장은 됐지만 입력 일부를 읽지 못한 경우가 있습니다. 샘플 행을 잘라냈거나, 테이블·컬럼을 찾지 못했거나, 읽지 않은 필드나 DDL 구문이 있는 경우입니다. 이때 결과 `status`와 응답 envelope의 `status`가 `partial`이 되고, `partial_sources`에 해당 출처가 나옵니다.

### 저장 단계와 재시도

`received → normalized → planned → committed` 순서로 진행합니다.

| 단계 | 내용 |
| --- | --- |
| received | 실행 기록 생성(또는 실패한 같은 키의 재시도 시작) |
| normalized | 입력 검증 완료 |
| planned | 현재 저장소에 시험 적용해 참조·충돌을 확인하고 되돌림 |
| committed | 데이터와 완료 표시를 한 트랜잭션으로 저장 |

- 저장 재시도: 실패한 저장은 같은 `idempotency_key`로 다시 `ingest`하면 같은 실행 기록에서 다시 진행합니다. 입력을 고쳐 다시 보내도 됩니다.
- 조사 재개: `research`는 이미 committed된 실행의 조사 요청만 이어서 실행합니다.
- 동시에 같은 키로 저장해도 한 번만 저장되고 나머지는 `reused`로 돌아옵니다.

예제 자료는 [examples/demo_mes/](../examples/demo_mes/)에 있습니다. 실제 시스템이 아닌 데모 자료입니다.

## 운영

### 설치와 갱신

```powershell
git clone https://github.com/thdwlndjs/build-log; cd build-log
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .            # 브라우저 E2E 테스트까지: -e .[dev]
.\.venv\Scripts\python.exe -m buildlog init               # 저장소 생성
.\.venv\Scripts\python.exe -m buildlog install-skill      # Claude Code·Codex 스킬 설치
```

- 갱신: `git pull` 후 `pip install -e .`와 `install-skill`을 다시 실행합니다. 저장소 구조는 다음 명령 실행 때 자동으로 최신 버전으로 올라가고(기존 데이터 유지), 필요한 경우 검색 색인도 한 번 다시 만듭니다. 큰 갱신 전에는 `backup`을 먼저 받아 둡니다.
- 처음 사용: Claude Code나 Codex에서 "이 문제 해결한 거 buildlog에 저장해줘"라고 요청하고, 다른 세션에서 "예전에 이 오류 본 적 있어?"로 찾습니다. 외부 조사를 쓰려면 `agy.exe`에 한 번 로그인합니다.

### 저장 전 기존 기록 확인 (prior_check)

호스트는 새 경험을 저장하기 전에 `query`로 기존 기록을 찾고, 결정을 저장 계획의 `prior_check`에 남깁니다.

| 결정 | 의미 |
| --- | --- |
| `new` | 같은 문제의 기록이 없어 새로 만듦 |
| `update` | 기존 기록을 고침(계획에 그 페이지 갱신이 있어야 함) |
| `follow_up` | 새 기록을 만들고 기존 기록과 관계로 연결 |
| `already_recorded` | 이미 기록돼 있어 새 경험 페이지를 만들지 않음 |

`prior_check` 없이 새 경험 페이지를 만들거나, 결정과 계획 내용이 맞지 않으면 저장은 되지만 경고가 나옵니다. 경험에 쓰인 개념·방법은 `concept`·`method` 페이지로 연결하고, 주제별 목차(`index`) 페이지에 등록합니다.

### 백업과 복원

- `backup --out <파일>`: SQLite 백업 API로 실행 중에도 일관된 사본을 만들고 무결성 검사를 통과해야 성공합니다. 같은 파일이 있으면 `--force`가 필요합니다.
- `restore --from <파일>`: 백업의 무결성과 스키마 버전을 확인한 뒤, 현재 저장소를 `<저장소>.pre-restore-<시각>`으로 남기고 바꿉니다. 검토 서버가 이 저장소를 쓰고 있으면 거부합니다(`serve --stop` 먼저).

### 내보내기와 가져오기

- `export --out <폴더>`: 한 시점의 스냅숏으로 페이지마다 `pages/<id>.md`를, 전체 행은 `manifest.json`에 씁니다. `--page`로 특정 페이지와 거기 딸린 용어·규칙·근거만 내보낼 수 있습니다.
- Markdown 파일에서 고칠 수 있는 것은 제목·상태·요약·본문입니다(`buildlog:summary`/`body`/`end` 표시 사이). 앞부분 ID·버전과 `manifest.json`의 행은 고치지 않습니다. 행을 고치면 해시가 맞지 않아 가져오기를 거부합니다.
- `import --in <폴더>`: 행을 UUID로만 맞춥니다. 없는 행은 추가하고, 버전이 더 높은 행은 갱신하고, Markdown에서 고친 페이지는 새 버전으로 저장합니다. 내보낸 뒤 저장소에서 바뀐 항목이 있으면 덮어쓰지 않고 `IMPORT_CONFLICT`(종료 코드 3)로 전체를 취소합니다. `--dry-run`으로 결과만 먼저 볼 수 있습니다.

### 웹 근거 원문 대조

`verify-sources --page <id>` 또는 `--all`은 외부 조사로 저장한 웹 근거마다 원문 페이지를 직접 열어 인용문을 대조합니다.

- 검색 리디렉션 링크는 실제 원문 주소로 바꿉니다.
- 결과는 `exact`(인용 그대로 있음), `partial`(단어 80% 이상 일치), `absent`(찾지 못함), `unreadable`(원문을 읽지 못함) 중 하나입니다. `exact`일 때만 근거를 `observed`로 올립니다.
- 같은 원문·같은 인용의 중복 연결은 정리합니다(행은 남김).
- 네트워크가 막힌 환경(샌드박스, 오프라인)에서 원문에 닿지 못하면 이전 결과와 주소를 그대로 두고, 실패만 `last_failed_check`에 남깁니다. `BUILDLOG_OFFLINE=1`이면 `research` 직후의 자동 대조를 건너뜁니다.

### 장애 대응

| 증상 | 확인 | 조치 |
| --- | --- | --- |
| 저장 실패 | `runs --status failed`로 단계·오류 종류·메시지 확인 | 계획을 고쳐 같은 `idempotency_key`로 `ingest` 재실행. 실패한 저장은 데이터를 남기지 않음 |
| 조사 실패(종료 코드 5) | 출력의 실패 종류(`auth`, `timeout` 등) | `auth`면 `agy.exe` 로그인 후, 같은 `research` 명령으로 남은 요청만 재개 |
| 검색 결과가 이상함 | 다른 도구로 DB를 직접 고쳤는지 | `reindex` |
| 검토 화면이 안 열림 | `serve --status` | `serve --stop` 후 `serve --detach`. 서버 정보는 저장소 옆 `serve.json` |
| 저장소 손상·잘못된 대량 수정 | `backup` 파일의 존재 | `serve --stop` 후 `restore --from <백업>` |
| 다른 저장소를 쓰는 것 같음 | `config show`로 값과 출처 확인 | `config set store "<경로>"` 또는 `BUILDLOG_STORE` |

### 규모

측정 결과는 [benchmark.json](benchmark.json)과 [benchmark.md](benchmark.md)에 있습니다. 다시 측정하려면 `.\.venv\Scripts\python.exe tools\benchmark.py --sizes 100 1000 10000 --runs 30 --out docs\benchmark.json`을 실행합니다.

## 응답과 오류

stdout에는 `{"schema_version", "request_id", "status", "data", "warnings", "error"}` 형태의 결과가 나갑니다. `status`는 `ok`, `partial`(저장됐지만 입력 일부를 읽지 못함), `error` 중 하나입니다. 오류는 같은 형태로 stderr에 나갑니다. Markdown 출력(`read`, `query` 등)은 envelope 없이 그대로 나갑니다.

| 코드 | 의미 |
| --- | --- |
| 0 | 성공 |
| 2 | 입력 오류(필수 필드 누락, 원문에 없는 인용, 근거 없는 주장, 알 수 없는 참조, 자격증명, 파일 없음) |
| 3 | 충돌: `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT`, `SCHEMA_CONFLICT`, `TERM_CONFLICT`, `JOIN_PATH_CONFLICT`, `RULE_CONFLICT` |
| 4 | 기록 없음 |
| 5 | 외부 조사 실패 (`RESEARCH_FAILED`, 저장한 경험은 유지) |
| 6 | 저장소 오류 |

같은 `idempotency_key`로 같은 입력을 다시 보내면 기존 결과를 돌려주고(`reused: true`), 같은 키에 다른 입력을 보내면 충돌로 종료합니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
