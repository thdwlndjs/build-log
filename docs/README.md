# build-log

학습한 내용과 해결한 오류, 데이터 시스템 지식(테이블·용어·조인 경로·규칙)을 기록하고 재사용하는 로컬 지식 저장소입니다. 외부 DB나 API 키는 사용하지 않습니다.

기본 저장소는 `%LOCALAPPDATA%\Buildlog\buildlog.sqlite3`이며 `--store <경로>`로 바꿀 수 있습니다. `--store`는 `-m buildlog` 바로 뒤에 둡니다.

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

# Claude Code·Codex 스킬 설치/갱신 (skills/buildlog/ 원본에서)
.\.venv\Scripts\python.exe -m buildlog install-skill [--host claude-code|codex|all] [--dry-run]
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

## 외부 조사 (Antigravity)

`research`는 Antigravity CLI(`agy`)로 공식 문서 등의 근거를 찾아 기록에 출처로 연결합니다. 조사는 `ingest` 트랜잭션 밖에서 실행하므로, 조사가 실패해도 저장한 경험은 사라지지 않습니다.

- 준비: `agy.exe`를 한 번 실행해 로그인합니다. 경로는 기본 `%LOCALAPPDATA%\agy\bin\agy.exe`이며 `BUILDLOG_AGY` 또는 `--agy`로 바꿀 수 있습니다.
- 실행 방식: `agy --print … --output-format json --json-schema <파일> --mode plan --print-timeout <초>`를 인자 배열로, 빈 임시 폴더에서 실행합니다. 권한 생략 옵션(`--dangerously-skip-permissions`)은 쓰지 않고, 프롬프트로 웹 검색·읽기 외 도구를 금지합니다.
- 성공 판정: agy는 권한 거부나 시간 초과 때도 종료 코드 0과 `status: SUCCESS`를 냅니다. 그래서 스키마에 맞는 구조화 결과와 유효한 근거가 1건 이상 있을 때만 성공으로 봅니다.
- 실패 분류: `not_installed`, `auth`, `permission`, `timeout`, `output`, `no_findings`, `process`. 실패하면 종료 코드 5를 내고, 같은 명령으로 끝나지 않은 요청만 재개합니다. 이미 성공한 요청을 `--retry-succeeded`로 다시 돌리다 실패해도 기존 결과와 상태는 유지됩니다.
- 저장: 근거마다 `web` 출처와 근거를 만들고 확보 수준을 남깁니다. 원문 확인(fetched)은 `observed`, 검색 결과 요약(search_snippet)과 모델 기억(from_memory)은 `reported`입니다. 검색 리디렉션 링크는 "원문 주소 아님"으로 표시합니다. 결론 수정은 하지 않습니다. 근거가 결론을 바꾸면 호스트가 `update`로 반영합니다.
- 페이지 Markdown의 "외부 조사" 섹션에서 요청별 상태와 시도 횟수, 실패 원인을 볼 수 있습니다.

## 검색

`query`는 질문 단어와 호스트가 보강한 검색어(`terms`)로 후보를 찾고, 작업 에이전트가 그대로 읽을 Markdown 묶음을 돌려줍니다.

- 색인: 페이지 제목·별칭·요약·본문, 그리고 페이지에 연결된 용어 정의·규칙·조인 경로·컬럼 이름을 SQLite FTS5 trigram으로 색인합니다. 한국어 조사("파티션을")는 떼고 찾습니다. 3자 미만 검색어는 제목·별칭·요약에서만 찾습니다. 질문 속 FTS 문법은 일반 글자로 취급합니다.
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
- 계획 중 하나라도 실패하면 아무것도 저장하지 않습니다.

예제 자료는 [examples/demo_mes/](../examples/demo_mes/)에 있습니다. 실제 시스템이 아닌 데모 자료입니다.

## 응답과 오류

stdout에는 `{"schema_version", "request_id", "status", "data", "warnings", "error"}` 형태의 결과가 나갑니다. 오류는 같은 형태로 stderr에 나갑니다. `read`의 Markdown 출력만 예외로 그대로 나갑니다.

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
