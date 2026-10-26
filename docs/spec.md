# Buildlog Technical Specification

버전: 1.0 · 작성일: 2026-10-26

기준: `mvp.md`, `prd.md`, 사용자 제공 Slipbox 화면 및 보완 요구. 제품 요구 ID는 PRD의 R01–R13을 참조한다. 구체적인 인터페이스·필드·기본값은 Buildlog 구현 결정이며 Slipbox 내부 구현에 대한 주장이 아니다.

## 1. 실행 구조

Python 3.12, SQLite, 로컬 CLI와 그래프 검토 UI를 사용한다. Windows PowerShell을 첫 검증 환경으로 삼는다. 사용자 공통 경로의 단일 저장소를 Codex·Claude Code가 함께 사용한다. 프로젝트별 DB로 분할하지 않는다. 경로는 설정 파일과 `--store`로 변경 가능하게 한다.

| 구성 | 책임 |
| --- | --- |
| 호스트 스킬 | 접근 가능한 대화 수집, 경험 선별·해설·용어 정의·관계 제안 |
| 인제스트 모듈 | 입력 정규화, 출처·원본 구간 추적, 구조 검증, 저장 계획 검증 |
| 저장 서비스 | 트랜잭션, 버전·멱등성·병합·피드백 |
| 조사 어댑터 | Antigravity 호출과 결과·실패 상태 관리 |
| 검색·관계 서비스 | FTS5 검색, 별칭·조건 필터, 관계·경로 조회 |
| 렌더러 | 페이지·용어·관계·규칙을 Markdown으로 출력 |
| 로컬 UI | 그래프·본문·경로 검토와 수정 |

LLM 역할은 호스트가 수행한다. 저장·검색·관계 조회를 위해 별도의 상시 LLM 서버를 두지 않는다. 그래프 데이터는 SQLite 관계에서 생성한다. UI용 데이터 파일은 원본 저장소가 아니다.

## 2. 저장 계약

SQLite가 기준 원본이다. Markdown 본문은 DB의 TEXT로 보관하고 `.md` 내보내기와 명시적 가져오기를 지원한다. 연결 참조는 제목이나 파일명 대신 UUID를 사용한다. 페이지 이름 변경은 연결을 바꾸지 않는다.

모든 DB 연결에 외래키를 활성화하고 쓰기 작업은 트랜잭션으로 처리한다. 버전은 정수로 증가시키며 수정 요청에 `expected_version`을 요구한다. 충돌은 `VERSION_CONFLICT`로 반환한다. 고유 제약은 구조를 보호하며, 의미적 중복 여부는 호스트가 후보를 비교해 판단한다.

## 3. 논리 데이터 모델

| 레코드 | 핵심 필드·제약 |
| --- | --- |
| pages | id, type, title, summary_md, body_md, status, version, created_at, updated_at |
| term_senses | id, expression, normalized_expression, scope, definition_md, page_id, evidence_ref, status |
| aliases | expression, term_sense_id. 같은 표현의 여러 의미 허용 |
| relations | id, source_id, target_id, type, explanation_md, evidence_ref, status, version |
| index_members | index_page_id, child_page_id, position. 중복 연결·목차 순환 금지 |
| sources | id, kind, locator, captured_at, content_hash, accessible_excerpt |
| evidence | id, source_id, section_locator, excerpt, evidence_kind |
| page_sources | page_id, evidence_id |
| revisions | object_type, object_id, version, snapshot_json, reason, actor, changed_at |
| feedback | id, target_type, target_id, target_version, feedback_text, resulting_revision |
| runs | id, idempotency_key UNIQUE, input_hash, stage, status, research_status, error |
| schema_objects | page_id, system_key, schema_name, object_name, object_type, metadata_json |
| join_paths | id, title, purpose, steps_json, conditions_md, caveats_md, evidence_ref, status, version |
| rules | id, scope_page_id, kind, condition_md, instruction_md, reason_md, alternative_md, evidence_ref, status, version |
| search_index | 페이지 단위의 제목·별칭·요약·본문을 FTS5로 색인 |

검색 인덱스는 파생 데이터로 재구축 가능하게 한다. 페이지 본문과 구조화된 용어·관계·규칙은 함께 렌더링한다. 동일 내용을 두 곳에 독립적으로 작성하지 않고, 한 수정 요청에서 관련 필드와 표시를 함께 갱신한다.

페이지 type은 experience, concept, method, source, index, schema_table, schema_column을 사용한다. 문서 상태는 draft, active, deprecated, superseded다. 근거 상태는 extracted, inferred, verified, rejected로 구분하며 문서 상태와 별도로 관리한다.

관계는 USES, DEPENDS_ON, SUPPORTS, TESTED_BY, DERIVED_FROM, SUPERSEDES, CONTRADICTS, RELATED_TO, PART_OF, JOINS_TO를 사용한다. JOINS_TO는 연결 가능성이며 인과관계가 아니다. 일반 그래프의 순환은 허용하되 목차 계층의 순환은 거부한다.

## 4. 인제스트 입력

입력은 current_conversation, markdown, text, sql_ddl, schema_json, sample_json/csv다. 대화·문서 의미 해석은 호스트가 수행하고 백엔드에는 구조화된 저장 계획을 전달한다. DDL은 방언을 명시하며 해석할 수 없는 구문은 원문과 진단을 남긴다. 일부만 해석하고 전체 분석에 성공한 것으로 처리하지 않는다.

초기 schema_json 계약:

```json
{
  "system_key": "demo_mes",
  "tables": [{
    "schema": "public", "name": "sensor_reading", "description": "설비 센서 측정",
    "columns": [{"name": "equipment_id", "type": "integer", "nullable": false}],
    "primary_key": [],
    "foreign_keys": [{"columns": ["equipment_id"], "target_table": "equipment", "target_columns": ["id"]}]
  }],
  "samples": [], "source_ref": "source-uuid"
}
```

위 값은 인터페이스 예시이며 실제 사용자 DB의 사실이 아니다. 업무 데이터를 상시 복제하지 않고 제공된 스키마·필요한 샘플·맥락을 지식으로 정리한다.

## 5. 용어·관계·조인·규칙 추출

1. 원문 위치와 시스템 식별자를 확정한다.
2. 테이블·컬럼 이름, 주석, 명시 키, 샘플의 순서로 증거를 수집한다.
3. 업무 용어와 약칭을 정의한다. 같은 이름이라도 system/table/column 맥락이 다르면 다른 term_sense를 만든다.
4. FK 등 명시 관계를 extracted로 저장하고, 샘플·업무 설명으로 추정한 관계는 inferred로 분리한다.
5. 관계에서 조인 후보를 만들고 업무 목적·필수 필터·집계 주의점을 붙인다. 증거 없는 유일성·카디널리티를 확정하지 않는다.
6. 금칙·필수 조건·미지원 연산을 원문과 실험 결과에서 추출한다. 언급이 없다는 이유만으로 부재를 단정하지 않는다.
7. 페이지·요약·관계·규칙을 연결해 저장하고 그래프 검토 대상으로 반환한다.

조인 경로 step 필드는 from_table, from_columns[], to_table, to_columns[], join_kind, cardinality, nullable, evidence_ref를 갖는다. unknown을 허용한다. 복합 키는 컬럼 배열 쌍으로 보존한다. graph 최단 경로를 업무상 올바른 경로로 자동 확정하지 않는다. 조인 경로는 실행 가능한 SQL 자체가 아닌 조회 방법의 지식이다.

rule kind는 prohibition, requirement, unsupported_operation이다. 각각 적용 대상·조건·지시·이유·근거를 갖는다. 예: “sensor_reading은 reading_time 기간 조건 없이 전체 조회하지 않는다.” 미지원 기능은 확인된 부재와 아직 확인하지 못한 상태를 구분한다.

## 6. 저장 실행·갱신

`received → normalized → planned → committed`로 진행하고 조사 상태를 별도로 관리한다. 인제스트 계획은 pages, term_senses, relations, join_paths, rules, evidence와 예상 버전을 포함한다. idempotency_key와 input_hash가 같으면 기존 실행 결과를 반환하고, 같은 key에 다른 입력이면 `IDEMPOTENCY_CONFLICT`를 반환한다.

기존 의미·환경과 다르면 무조건 덮어쓰지 않고 새 기록과 관계를 만든다. 기존 경험의 후속 결과라면 해당 버전을 갱신한다. 호스트가 본문과 직접 영향을 받은 요약을 준비하고, DB 서비스가 전체 계획을 검증한 후 원자적으로 반영한다. 실패 시 부분 저장을 노출하지 않는다.

조사 실패는 경험 자체를 소실시키지 않는다. 사용 가능한 증거로 저장하고 research_status를 failed/pending으로 남기며 재개 시 해당 실행에 결과를 추가한다. 원문·샘플의 자격증명은 저장 계획에서 제거한다.

## 7. 조회·경로 탐색

FTS5로 제목·별칭·요약·본문 후보를 찾고 도구명·오류 코드의 정확 일치를 우선한다. 한국어는 부분 문자열 보조 색인을 사용하고 3자 미만 검색어는 정규화된 별칭·제목 일치 경로를 사용한다. raw FTS 쿼리를 그대로 실행하지 않고 리터럴을 인코딩한다. SQL은 파라미터 바인딩한다.

기본값: 후보 20개, 반환 페이지 5개, 관계 확장 1 hop, 추가 관계 페이지 최대 10개. 사용자가 조정 가능하다. 누락된 관련 용어·규칙은 별도 조회할 수 있다. 적용 페이지에 연결된 필수 규칙은 랭킹이 낮다는 이유로 제외하지 않는다.

환경·시스템 범위가 충돌하면 후보와 차이를 제공한다. 관련성이 없는 후보를 강제로 채우지 않는다. 광범위한 설명형 질문에서는 호스트가 검색어·용어·목차 요약을 이용해 보완한다. 전체 본문을 다시 주입하는 대체 동작은 두지 않는다.

경로 조회는 등록된 join_paths를 우선한다. 없으면 JOINS_TO 관계의 단순 경로 후보를 깊이 최대 4, 후보 최대 5로 제한해 제시하고 추정 상태를 붙인다. 목적·필터·카디널리티를 검토하지 않은 후보를 verified로 승격하지 않는다.

## 8. 에이전트 전달 Markdown

렌더러는 다음 항목을 출력한다: 질문/적용 범위, 선택한 지식 ID·버전, 문제·환경, Mitigation·RCA, 용어 정의, 관계·조인 경로, 필수 조건·금칙·미지원 연산, 출처, 상충·정정 기록.

출력 예산은 기본 24,000 UTF-8 bytes이며 설정 가능하다. 규칙과 적용 조건을 포함해 전체 기록 단위로 선택한다. 핵심 기록 하나가 예산을 넘으면 조용히 잘라내지 않고 `BUDGET_TOO_SMALL`과 필요한 크기·추가 읽기 ID를 반환한다. 생략한 보조 기록은 ID·제목·추가 조회 방법을 반환한다. 이 예산은 전송량 상한이며 토큰 수를 보장하지 않는다.

## 9. CLI 계약

표기의 `buildlog`는 구현할 명령 이름이다. 구조화 입력은 UTF-8 JSON 파일 또는 stdin으로 받고, 셸 인자로 대화 본문을 직접 전달하지 않는다.

| 명령 | 입력 | 출력 |
| --- | --- | --- |
| init | store 경로 | 생성된 저장소·schema_version |
| ingest | 저장 계획, idempotency_key | run_id, 생성/갱신 ID, 상태 |
| query | query, scope, limit, max_bytes | Markdown 또는 JSON envelope |
| read | page_id | 본문·메타데이터·규칙 |
| neighbors | page_id, type, direction | 연결과 설명 |
| paths | 시작/도착 테이블, purpose | 저장된 경로와 후보 |
| update | object_id, expected_version, patch, reason | 새 버전 |
| feedback | target, expected_version, message | 피드백과 후속 변경 |
| merge | source_ids, target_id, expected_versions | 리다이렉트·변경 이력 |
| export/import | 경로, 범위, manifest | Markdown 묶음 / 검증 결과 |
| research | run_id, 조사 질문 | 구조화된 근거 또는 실패 상태 |
| serve | host, port | 그래프 UI 주소 |

JSON envelope는 schema_version, request_id, status, data, warnings, error를 갖는다. stdout은 결과 전용, 진단은 stderr. 종료 코드는 0 성공, 2 입력 오류, 3 충돌, 4 미발견, 5 외부 조사 실패, 6 저장 실패로 구분한다. 성공적으로 경험을 저장한 조사 실패는 run 결과에 복합 상태로 표현한다.

## 10. 그래프 피드백 UI

로컬 웹 화면은 127.0.0.1에 바인딩한다. 그래프 렌더러와 Markdown 렌더러를 자산으로 번들링해 외부 CDN이 없어도 열리게 한다. 생성 화면에 독자적인 데이터 복사본을 저장하지 않는다.

| API | 기능 |
| --- | --- |
| GET /api/graph | 최근 실행 또는 검색 결과 주변 그래프. cursor와 cap 제공 |
| GET /api/pages/{id} | 본문·용어·출처·규칙·버전 |
| GET /api/relations/{id} | 방향·의미·근거 |
| GET /api/paths | 조인 경로·조건·주의점 |
| PATCH /api/pages/{id} | expected_version 기반 수정 |
| PATCH /api/relations/{id} | 관계·설명 수정 |
| PATCH /api/terms/{id} | 정의·맥락·별칭 수정 |
| PATCH /api/rules/{id} | 금칙·조건·미지원 연산 수정 |
| PATCH /api/paths/{id} | 조인 경로·조건·주의점 수정 |
| POST /api/feedback | 수정 지적 기록 |
| POST /api/merge | 버전 확인 후 중복 병합 |

페이지/관계/규칙/경로의 수정은 동일한 버전 정책을 따른다. 연결 해제는 soft-delete와 이력 보존으로 처리한다. 초기 graph 응답 cap은 150 nodes/300 edges, 상한 500/1,000으로 두고 초과분은 펼치기로 접근한다. 노드 클릭으로 본문, 연결선 클릭으로 설명을 읽고 수정한다. 조인 경로 선택 시 해당 테이블·연결을 강조한다.

UI 수정 시 요약 갱신은 호스트 수정 작업으로 이어지게 한다. 호스트 없이 본문을 직접 편집하면 summary_dirty로 표시하고 검색은 최신 본문을 즉시 반영한다. 요약을 새 본문과 동기화할 때까지 오래된 요약을 최신으로 표현하지 않는다. 실제 문자열 편집은 저장 서비스를 통해 즉시 버전화한다.

Markdown의 raw HTML은 비활성화하거나 정화한다. 변경 API는 로컬 세션 토큰과 origin 확인을 적용한다. 운영용 공개 배포·외부 DB 실행 API는 제공하지 않는다.

## 11. Antigravity와 호스트 어댑터

현재 확인된 계약은 사용자가 제공한 `agy --help`다. `--print`, `--output-format json`, `--json-schema`, `--print-timeout`을 사용한다. subprocess 인자 배열로 실행하고 인증은 사용자의 기존 agy 실행 환경을 사용한다. 프로세스 타임아웃·출력 크기 제한·JSON 스키마 검증을 둔다.

조사 입력은 필요한 오류·도구·개념의 맥락만 전달하고 출력은 질문, 원문 URL, 제목, 핵심 근거, 경험과의 관련성, 확인 상태로 정규화한다. 수신했다고 실제 원문을 읽은 것으로 처리하지 않고 확보 수준을 기록한다. 권한 생략 옵션은 사용하지 않는다. 실패는 원인을 반환하고 run에서 재개한다.

Codex와 Claude Code 스킬은 호스트별 호출 표기만 다르게 하며 CLI·저장소 계약을 공유한다. 접근 가능한 현재 대화에서 저장 계획을 구성한다. ChatGPT 직접 연결은 후속 MCP가 소유하며 초기에는 Markdown 이동을 지원한다. 구현 시 실제 agy 인증·검색 성공까지 확인한 후 연동 완료로 보고한다.

## 12. 일관성·내보내기·장애 복구

내보내기는 일관된 DB 스냅샷에서 페이지 Markdown과 manifest.json을 생성한다. manifest에는 schema_version, UUID, version, 관계·규칙·경로와 내용 해시를 보존한다. 가져오기는 참조 검증과 버전 충돌 검사를 거쳐 단일 계획으로 반영한다. 파일 순서나 제목으로 대상을 추정하지 않는다.

병합은 대표 UUID와 redirect를 남기고 들어오는 참조를 재연결한다. 중복된 관계는 근거를 합치되 의미가 다른 관계는 보존한다. FTS5 인덱스 재구축과 DB 백업을 CLI로 제공한다. 중단한 작업은 committed가 아닌 run에서 재개한다.

## 13. 검증과 요구 추적

| 검증 | 확인 내용 | PRD |
| --- | --- | --- |
| 저장·멱등성 | 재호출 중복 방지, 잘못된 계획 rollback | R01–R03 |
| 용어 구분 | 같은 사업자번호 표현이 테이블별 다른 의미로 전달 | R04 |
| 근거·RCA | 조사 설명을 실행된 사실이나 확정 원인으로 바꾸지 않음 | R03·R05 |
| 스키마 | FK·복합 키·NULL·동명 테이블 범위 보존 | R06 |
| 경로 | 다중 경로, 1:N fan-out 집계, NULL 허용 연결의 주의점 | R07 |
| 규칙 | 조회량 제한·기간 필터 금칙이 Markdown에서 빠지지 않음 | R08 |
| 없는 기능 | 확인된 미지원·미확인 기능의 구분 | R09 |
| UI 수정 | 본문·정의·관계 수정/해제/병합 및 버전 충돌 | R10·R12 |
| 새 세션 조회 | 관련 경험과 실제 적용 조건 전달 | R11 |
| 실제 조사 | 인증·권한·timeout·JSON 오류 및 재개 | R13 |
| 성능 | 100/1,000/10,000페이지에서 검색·렌더링·관계 응답 측정 | R11 |

실제 경험 기반 질문에는 정답 기록 ID·필수 용어·필수 규칙을 사람이 판독 가능한 fixture로 정의한다. 검색 recall과 금칙 전달 누락을 기록한다. 로컬 검색·관계·렌더링과 원격 조사 시간을 분리한다. 초기 성능 목표는 1,000페이지 데이터에서 로컬 query p95 1초 이내, 기본 graph 응답 p95 500ms 이내이며 구현 기준 PC와 30회 이상 측정 조건을 기록한다. 목표값은 달성한 실측값과 구분한다.

## 14. 구현 순서

1. 저장·버전·출처·인제스트 계획·Markdown 렌더러 구현 및 최소 Codex 스킬 연결. 실제 대화에서 저장 → ID로 읽기 → Markdown 반환까지 확인.
2. 용어·스키마·관계·조인 경로·규칙 및 검색.
3. 호스트 스킬·Antigravity 프로세스 어댑터.
4. 그래프 검토·수정·병합·피드백 및 내보내기/가져오기.
5. 실제 새 세션 재사용·회귀 테스트·규모 측정.

각 단계는 위 계약을 검증하며 최종 통합을 끝내야 MVP 완료다. 이 문서는 구현 명세이며 코드나 연동이 이미 동작한다는 의미가 아니다.

## 15. 기술 근거

- [SQLite FTS5](https://www.sqlite.org/fts5.html): 전문 검색·부분 문자열 tokenizer·랭킹.
- [SQLite 외래키](https://www.sqlite.org/foreignkeys.html): 연결별 활성화와 참조 정합성.
- [Slipbox 가이드](https://agentria.ai/docs/slipbox): 페이지·요약·목차·인제스트.
- [Slipbox 관계](https://agentria.ai/docs/slip-link-node), [조회](https://agentria.ai/docs/slip-follow-links-node): 방향과 유형이 있는 연결.
- `mvp.md`와 사용자 첨부 화면: 제품 범위 및 테이블·조인·금칙·미지원 연산 요구.
