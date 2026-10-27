# 작업 공간과 기존 기능의 연결 현황 (초안)

상태: 초안. 사용자가 QA하면서 각 기능을 작업 공간 어디에 붙일지 정한 뒤 spec §10과 plan.md에 병합한다.

- 작업 공간은 기준 화면 [reperence/buildlog-workbench.html](../reperence/buildlog-workbench.html) 그대로 만든다(spec §10.2). 기준 화면에 위치가 없는 기능은 Phase 5에서 작업 공간에 넣지 않는다.
- 연결하지 않은 기능도 지우지 않는다. API·서버 함수·CLI는 그대로 두고, 그 테스트(`test_review` 등)도 계속 실행한다. 기존 검토 화면을 별도 주소로 운영하지는 않는다.
- QA는 `buildlog serve --detach`로 로컬에서 띄운 작업 공간에서 한다.
- Phase 3 검토 화면의 코드(폼·대화 상자)는 Phase 5-3에서 작업 공간으로 교체됐다. 연결할 때 재사용하려면 교체 직전 커밋에서 꺼낸다: `git show 3972750:src/buildlog/web/app.js` (`index.html`, `app.css`도 같은 방식).

## 1. Phase 5에서 작업 공간에 연결하는 기능

| 기능 | 현재 동작 | 작업 공간 위치 | API |
| --- | --- | --- | --- |
| 검색 | 상단 검색어 → 결과를 그래프와 노드 목록으로 표시 | 제목줄 [검색] → 검색 대화상자 → 문서 탭 | `GET /api/search` |
| 문서 읽기 | 노드 선택 → 오른쪽 패널 '본문' 탭에 Markdown | 문서 탭 가운데 | `GET /api/objects/{id}` |
| 관련 문서 확인 | '관계' 탭 목록, 그래프 연결선 | 문서 탭 "관련 문서"(눌러서 이동), 주변 연결 그래프 | `GET /api/objects/{id}`, `GET /api/graph?focus=` |
| 주변 그래프 펼치기 | '주변 펼치기' 버튼 → 큰 그래프를 그 노드 중심으로 | 문서 탭 오른쪽 "주변 연결" | `GET /api/graph?focus=` |
| 본문 수정 | '편집' 탭에서 제목·요약·본문·상태를 함께 수정 | 도구줄 [본문 수정] → 본문(`body_md`)만 수정·저장·취소 | `PATCH /api/pages/{id}` |
| 이력 | '이력' 탭(버전·시각·이유) | 오른쪽 [이력] | `GET /api/objects/{id}` |
| 근거 | 본문 Markdown 안의 출처 절 | 오른쪽 [근거] → 소스 탭(원문 또는 저장된 발췌·인용, 연결 문서) | `GET /api/objects/{id}`, `GET /api/sources/{id}` |
| 전체 그래프 | 처음 화면(최근 저장 실행 주변) | 탭 줄 그래프 버튼, 주변 연결의 [전체 그래프] → 전체 그래프 탭(선택한 노드 → 문서 열기) | `GET /api/graph?mode=all` |
| 설정 확인 | 없음(CLI `config show`) | 제목줄 [설정] → 읽기 전용 설정 탭 | `GET /api/settings` |
| 탐색·목록·검색 | 노드 목록과 검색 결과 그래프 | 탐색기 세션·소스, [전체 문서], [검색] | `GET /api/sessions`, `/api/sources`, `/api/pages`, `/api/search` |
| 저장 실행 링크 | `?run=<id>`로 그 실행의 그래프 | `?run=<id>` → 전체 그래프 탭의 저장 실행 보기 | `GET /api/graph?run=` |

## 2. 작업 공간에 연결하지 않는 기존 기능

| 기능 | 현재 동작 (Phase 3 검토 화면 기준) | 작업 공간 연결 | API · 서버 함수 · CLI | 이 기능을 확인하던 기존 화면 E2E |
| --- | --- | --- | --- | --- |
| 제목·요약·상태 수정 | '편집' 탭에서 본문과 함께 수정. 본문만 바꾸면 요약에 '갱신 필요' 표시. 수정 이유와 해결한 피드백을 함께 기록 | 미연결 | `PATCH /api/pages/{id}` · `update.update_object` · `update` | `test_body_edit_reaches_cli_read_and_query`, `test_concurrent_change_is_not_overwritten` |
| 관계 수정 | '관계' 탭 또는 연결선 클릭 → 종류·설명·상태 수정, 방향 뒤집기 | 미연결(관계는 "관련 문서"와 주변 그래프로 보기만 함) | `PATCH /api/relations/{id}` · `update.update_object` · `update` | `test_relation_and_definition_edits_reach_cli` |
| 관계 연결 해제 | 이유를 적고 해제. 기록·근거·이력은 남고 조회·그래프에서 빠짐 | 미연결 | `POST /api/relations/{id}/unlink` · `update.unlink_relation` · `unlink` | `test_unlink_keeps_history_and_evidence` |
| 용어 수정 | '용어·규칙·경로' 탭 → 정의·맥락·별칭·상태 | 미연결 | `PATCH /api/terms/{id}` · `update.update_object` · `update` | `test_relation_and_definition_edits_reach_cli` |
| 규칙 수정 | 같은 탭 → 조건·지시·이유·대안·상태·확인 여부 | 미연결 | `PATCH /api/rules/{id}` · `update.update_object` · `update` | - |
| 조인 경로 수정 | 같은 탭 → 제목·목적·조건·주의점·상태 | 미연결 | `PATCH /api/paths/{id}` · `update.update_object` · `update` | - |
| 조인 경로 강조 | 같은 탭 → 그래프에서 경로의 테이블·연결 강조 | 미연결 | 화면 기능 · `paths` | `test_join_path_is_highlighted` |
| 피드백 | '피드백' 탭에서 지적 남기기, 상태 변경. 수정할 때 해결한 피드백을 고르면 수정 revision과 연결 | 미연결 | `POST /api/feedback`, `POST /api/feedback/{id}/status` · `update.add_feedback`, `set_feedback_status` · `feedback add/list` | `test_feedback_then_edit_marks_it_addressed` |
| 병합 | '병합' 탭에서 검색으로 중복 후보를 골라 대표 페이지로 합침. 중복은 대체 상태와 redirect, 관계·근거·용어·규칙·목차 소속은 대표로 이동 | 미연결 | `POST /api/merge` · `update.merge_pages` · `merge` | `test_merge_keeps_references_and_sources` |
| 저장 실행 선택 | 상단 '저장 실행' 목록에서 실행을 골라 그 그래프 | 미연결(링크 `?run=`만 연결) | `GET /api/runs` · `runs.list_runs` · `runs` | `test_graph_shows_latest_save_and_page_markdown` |
| 컬럼 표시 | 상단 체크박스로 그래프에 컬럼 노드 포함 | 미연결 | `GET /api/graph?columns=1` | - |
| 노드 목록 | 그래프 아래 현재 그래프의 노드 목록, 요약 갱신 필요 표시 | 미연결(탐색기·전체 문서가 대신함) | `GET /api/graph` | `test_graph_shows_latest_save_and_page_markdown` |
| 범례 | 새로 저장·갱신·요약 갱신 필요·추정 관계 표시 | 미연결 | - | - |

## 3. 기존 화면 E2E의 확인 내용

Phase 5-3에서 기존 화면을 작업 공간으로 바꾸면 아래 E2E는 조작할 화면이 없어진다. 작업 공간에 연결하는 부분은 작업 공간 E2E로 옮기고, 미연결 기능의 확인 내용은 여기에 남겨 연결할 때 다시 E2E로 만든다. 서버·CLI 쪽 확인은 `test_review`가 계속 한다.

| 기존 E2E (`tests/test_ui_e2e.py`) | 화면에서 확인하던 것 | Phase 5 처리 |
| --- | --- | --- |
| `test_graph_shows_latest_save_and_page_markdown` | 최근 저장 실행의 그래프와 노드 목록, 노드 선택 시 본문 Markdown, 연결선 선택 시 관계 | 그래프·문서 표시는 작업 공간 E2E로. 저장 실행 선택·노드 목록은 미연결 |
| `test_body_edit_reaches_cli_read_and_query` | 본문 수정 저장 → 요약 갱신 필요 표시 → CLI `read`·`query` 반영 | 본문 수정은 작업 공간 E2E로. 요약 갱신 필요 표시 위치는 미정 |
| `test_relation_and_definition_edits_reach_cli` | 관계 설명 수정, 용어 정의 수정 → CLI `read` 반영 | 미연결 |
| `test_unlink_keeps_history_and_evidence` | 관계 해제 → 그래프에서 사라지고 근거·이력은 남음 | 미연결 |
| `test_merge_keeps_references_and_sources` | 병합 후보 검색·선택 → 병합 → 참조·출처 보존, 목록에서 중복 제거 | 미연결 |
| `test_feedback_then_edit_marks_it_addressed` | 피드백 남기기 → 그 피드백을 고른 수정 → 해결 표시 | 미연결 |
| `test_concurrent_change_is_not_overwritten` | 다른 곳에서 먼저 바뀐 문서를 저장하면 거부·안내 | 본문 수정 충돌로 작업 공간 E2E에 옮김 |
| `test_join_path_is_highlighted` | 조인 경로 강조 시 테이블 3개·연결 2개 강조 | 미연결 |
| `test_markdown_is_sanitized` | 본문의 HTML이 실행되지 않고 글자로 보임 | 작업 공간 E2E로 |

## 4. QA 때 정할 것

- 2절 기능 각각을 작업 공간에 붙일지, 붙인다면 위치(도구줄·제목줄 메뉴, 대화상자, 문서 영역 등)
- 붙인 기능의 E2E를 3절 내용으로 다시 만들 시점
