# build-log

학습한 내용과 해결한 오류를 기록하고 재사용하는 로컬 지식 저장소입니다.

## Phase 1 실행

Phase 1은 경험 한 건을 SQLite에 저장하고, 기록 ID로 Markdown을 다시 읽는 기능을 제공합니다. 외부 DB나 API 키는 사용하지 않습니다. 기본 저장소는 `%LOCALAPPDATA%\\Buildlog\\buildlog.sqlite3`이며 `--store`로 바꿀 수 있습니다.

```powershell
# 프로젝트 환경에 패키지 설치
.\.venv\Scripts\python.exe -m pip install -e .

# 기본 저장소 초기화
.\.venv\Scripts\python.exe -m buildlog init

# UTF-8 JSON 저장 계획을 사용해 한 건 저장
.\.venv\Scripts\python.exe -m buildlog ingest --input .\example-plan.json

# 반환된 page_id로 Markdown 읽기
.\.venv\Scripts\python.exe -m buildlog read <page_id>
```

## Claude Code에서 사용

`C:\Users\aicam\.claude\skills\buildlog\SKILL.md`에 스킬이 설치돼 있습니다. 어느 프로젝트에서든 Claude Code에 다음처럼 요청하면 됩니다. JSON 작성과 명령 실행은 Claude가 합니다.

- "이 문제 해결한 거 buildlog에 저장해줘" → 대화를 정리해 저장하고 page ID를 알려줌
- "buildlog에서 `<page_id>` 읽어줘" 또는 `/buildlog <page_id> 읽어줘` → Markdown 기록을 보여줌

유사 기록 검색은 Phase 2에서 추가됩니다.

## 오류 종료 코드

| 코드 | 의미 |
| --- | --- |
| 0 | 성공 |
| 2 | 입력 오류(필수 필드 누락, 자격증명 감지, 잘못된 JSON, 파일 없음) |
| 3 | `IDEMPOTENCY_CONFLICT` 또는 `VERSION_CONFLICT` |
| 4 | 기록 없음 |
| 5 | 저장소 오류 |

오류는 stderr에 `{"schema_version": 1, "status": "error", "error": "..."}` 형태로 출력됩니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

`ingest`는 같은 `idempotency_key`와 같은 입력을 재실행하면 기존 기록을 반환합니다. 같은 키에 다른 입력을 주면 충돌로 종료합니다. 자격증명으로 보이는 필드와 토큰 형태의 값은 저장 전에 거부합니다.

성공한 저장 명령의 출력 예시는 다음과 같습니다.

```json
{"schema_version": 1, "status": "ok", "data": {"run_id": "5ad13701-…", "page_id": "aae4c7c0-fe5e-45bc-a2c4-675adca81e07", "version": 1, "store": "C:\\Users\\aicam\\AppData\\Local\\Buildlog\\buildlog.sqlite3", "reused": false}}
```
