# PRD 요구 검증 (R01–R13)

기준일 2026-10-27, Phase 4 완료 시점. 이 문서는 요구마다 자동 테스트와 실제 실행 기록을 근거로 붙인다. 실제 실행은 아래 두 저장소에서 했다.

- 실사용 저장소 `%LOCALAPPDATA%\Buildlog\buildlog.sqlite3`: Claude Code와 Codex가 실제 대화에서 저장·조회한 기록. 페이지 7개, 실행 5건.
- 데모 저장소 `%LOCALAPPDATA%\Buildlog\demo.sqlite3`: [examples/demo_mes/](../examples/demo_mes/)의 DDL·스키마 JSON·운영 문서를 인제스트한 것. 테이블 6, 컬럼 21, 용어 4, 조인 경로 3, 규칙 5. 실제 시스템이 아닌 데모 자료다.

자동 테스트는 `.\.venv\Scripts\python.exe -m unittest discover -s tests`로 실행한다. 138건이 통과했다(Playwright가 없으면 브라우저 E2E 9건은 건너뜀). 표의 테스트 이름은 `파일::테스트` 형식이다.

| ID | 요구 | 자동 테스트 | 실제 실행 | 판정 |
| --- | --- | --- | --- | --- |
| R01 | 명시적 저장 호출 | `test_store::test_ingest_read_and_markdown`, `test_save_then_read_across_processes`, `test_runs_config::test_success_passes_every_stage` | Claude Code에서 "저장해줘" 한 번으로 경험 페이지·방법·목차를 저장(실행 `e13e2825`, committed). Codex도 실행 `439d3eb1`·`25114aa5`로 저장 | 충족 |
| R02 | 경험 선별과 중복 관리 | `test_store::test_same_input_is_idempotent`, `test_runs_config::test_same_key_from_parallel_processes_commits_once`, `test_saveflow` 4건(조회 누락 경고, 후속 연결, 결정 불일치, 중복 대신 갱신) | `e13e2825`가 저장 전 조회 후 `follow_up`으로 기존 `f62436c8`과 RELATED_TO 연결. 같은 경험을 Codex에서 다시 저장하게 하자 `already_recorded`로 판단해 새 페이지를 만들지 않음 | 충족 |
| R03 | Mitigation·RCA | `test_search::test_markdown_alone_explains_the_record`, `test_knowledge::test_config_experience_gets_no_database_sections` | 실사용 경험 페이지들이 문제·환경·Mitigation·RCA 상태를 나눠 저장되고, RCA는 관찰로 확인한 것과 확인하지 않은 것을 나눠 "부분 확인"으로 남음(`f62436c8`) | 충족 |
| R04 | 인제스트 기반 용어 정의 | `test_knowledge::test_same_term_has_context_specific_meanings`, `test_explicit_inferred_and_unknown_are_kept_apart`, `test_term_conflict_and_versioned_update` | 데모 저장소의 "사업자번호"가 `site.biz_no`(사업장), `equipment.vendor_biz_no`(공급업체), `crm.customer.biz_no`(고객사) 세 맥락의 정의로 따로 저장되고, 표본만으로 읽은 `operator_code`는 `inferred` | 충족 |
| R05 | 상위 프레이밍과 근거 | `test_saveflow::test_follow_up_with_concept_and_index_update`, `test_research::test_success_links_sources_with_access_level`, `test_sources` 5건 | 경험 `bd2ae586`이 방법 페이지 `c6f9706e`("웹 조사 근거의 원문 대조")와 USES로 연결. agy 조사 근거 22건을 원문과 대조(인용 그대로 1, 일부 일치 6, 찾지 못함 15). 그대로인 1건만 `observed` | 충족 |
| R06 | 구조와 관계 | `test_knowledge::test_tables_columns_and_foreign_keys`, `test_ambiguous_table_across_schemas`, `test_changed_schema_needs_explicit_replace` | 데모 DDL·스키마 JSON에서 테이블 6·컬럼 21과 FK 관계를 만들고 그래프 화면에 표시 | 충족 |
| R07 | 조인 경로 | `test_knowledge::test_three_table_join_path_with_fanout_caveat`, `test_join_without_foreign_key_shows_its_basis`, `test_cardinality_without_basis_is_rejected`, `test_search::test_stored_path_comes_first`, `test_candidates_are_marked_as_estimates` | `paths --system demo_mes --from site --to sensor_reading`이 등록 경로를 키·순서·1:N·NOT NULL(스키마 키 근거)과 capacity_kw 중복 합산 주의, 원문 줄 번호 근거와 함께 반환 | 충족 |
| R08 | 금칙·필수 조건 | `test_knowledge::test_rules_reach_the_table_markdown`, `test_search::test_rules_come_first_and_survive_omission`, `test_join_path_tables_bring_their_rules` | "센서 측정값 시계열 조회" 조회 결과 맨 앞에 reading_time 31일 필수 조건과 기간 없는 전체 조회 금칙, 조인 경로가 지나는 equipment의 capacity_kw 합계 금칙이 나옴 | 충족 |
| R09 | 미지원 연산 | `test_knowledge::test_confirmed_absence_of_change_history`, `test_confirmed_absence_needs_direct_evidence` | 데모 저장소에 "설비 마스터 변경 이력 조회 없음"은 `confirmed`, "실시간 스트리밍 API"는 `unconfirmed`로 구분 저장 | 충족 |
| R10 | 그래프 피드백 | `test_review` 13건, `test_ui_e2e` 9건(Chromium) | 실사용 저장소를 브라우저 검토 화면으로 열어 `bd2ae586`에 피드백을 남기고 본문·요약을 고쳐 v2 저장, 피드백 `e3500f3f`가 해결(addressed)로 바뀌고 수정 revision과 연결. 콘솔 오류 0 | 충족 |
| R11 | 조회와 Markdown 전달 | `test_search` 19건(회귀 질문 10종 포함), `test_query_markdown_and_budget_error` | 새 Claude Code 세션(`claude -p`)과 새 Codex 세션이 스킬 이름 없는 자연어 질문으로 스킬을 골라 조회하고, 검토 피드백이 반영된 v2 내용을 답에 사용. 성능은 [benchmark.md](benchmark.md) | 충족 |
| R12 | 버전·이동성 | `test_store::test_update_keeps_every_revision`, `test_portability` 8건 | 실사용 저장소 사본을 내보내 빈 저장소로 가져오자 18개 테이블 행이 모두 같음. 내보낸 Markdown에서 본문을 고친 페이지는 v2로 저장. 내보낸 뒤 저장소에서 바뀐 페이지는 `IMPORT_CONFLICT`(종료 코드 3)로 거부되고 저장소는 그대로 | 충족 |
| R13 | Antigravity 조사 | `test_research` 14건(실패 종류, 재개, 중복 방지, 리디렉션 표시) | 실제 agy로 질문 2건 조사(시도 4회·2회 후 성공). 실행 중 만난 시간 초과·권한 자동 거부를 실패로 분류하고 같은 명령으로 재개. 조사 결과의 리디렉션 링크와 의역 인용은 원문 대조로 확인(R05) | 충족 |

## 남은 제한

- 범위 충돌 시 항목별 차이 비교는 없다. 범위 밖 후보와 그 범위만 표시한다(spec §16).
- 의미가 같은 다른 표현의 중복 판단, 개념·방법 선정, 목차 갱신은 호스트 에이전트가 스킬 지침에 따라 한다. Buildlog는 조회 기록 누락과 결정·계획 불일치를 경고하고 참조·순환을 검사한다.
- agy 조사는 대부분 검색 결과 요약 수준이고, 인용은 원문과 그대로 일치하는 경우가 적다. 원문 대조 결과가 `observed`가 아니면 근거는 `reported`로 남는다.
- 성능 수치는 합성 데이터로 한 컴퓨터에서 잰 값이다.
