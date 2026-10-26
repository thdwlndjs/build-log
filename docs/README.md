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
```

## Claude Code에서 사용

`C:\Users\aicam\.claude\skills\buildlog\`에 스킬이 설치돼 있습니다. 어느 프로젝트에서든 Claude Code에 요청하면 됩니다. 파일 읽기, JSON 작성, 명령 실행은 Claude가 합니다.

- "이 문제 해결한 거 buildlog에 저장해줘" → 대화의 경험을 정리해 저장
- "이 DDL이랑 운영 문서 buildlog에 인제스트해줘" → 테이블·컬럼·용어·조인 경로·규칙으로 정리해 저장
- "buildlog에서 `<id>` 읽어줘" → Markdown으로 보여줌

유사 기록 검색은 Phase 2.2에서 추가됩니다.

## 저장 계획

형식은 [plan-format.md](C:/Users/aicam/.claude/skills/buildlog/plan-format.md)에 있습니다. Phase 1 형식(`page` 하나)도 계속 받습니다.

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
| 5 | 외부 조사 실패 (Phase 2.3) |
| 6 | 저장소 오류 |

같은 `idempotency_key`로 같은 입력을 다시 보내면 기존 결과를 돌려주고(`reused: true`), 같은 키에 다른 입력을 보내면 충돌로 종료합니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
