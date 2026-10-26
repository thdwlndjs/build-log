# 커밋 규칙

[Conventional Commits 1.0.0](https://www.conventionalcommits.org/ko/v1.0.0/)을 따릅니다.

## 형식

```
<type>(<scope>): <subject>

<body>

<footer>
```

### type

| type | 쓰는 경우 |
| --- | --- |
| `feat` | 사용자가 쓸 수 있는 기능 추가 |
| `fix` | 버그 수정 |
| `refactor` | 동작 변화 없는 코드 구조 변경 |
| `perf` | 성능 개선 |
| `test` | 테스트 추가·수정 |
| `docs` | 문서만 변경 |
| `build` | 패키징·의존성(`pyproject.toml` 등) |
| `ci` | CI 설정 |
| `chore` | 그 밖의 잡무(`.gitignore` 등) |
| `revert` | 이전 커밋 되돌리기 |

### scope

바뀐 코드 영역을 씁니다. Phase 번호는 scope에 넣지 않습니다.

`store`, `cli`, `ingest`, `search`, `skill`, `agy`, `graph`, `export`, `plan` 등을 씁니다. 여러 영역에 걸치면 scope를 생략합니다.

### subject

- 50자 안팎으로 씁니다.
- 끝에 마침표를 찍지 않습니다.
- "추가", "수정", "제거"처럼 무엇을 했는지 명령형·명사형으로 끝냅니다.

### body

- 제목과 본문 사이는 한 줄 비웁니다.
- 무엇을 바꿨는지보다 **왜** 바꿨는지를 먼저 씁니다.
- 한 줄은 72자 안쪽으로 맞춥니다.

### footer

- 하위 호환이 깨지는 변경(예: DB 스키마, CLI 계약)은 `BREAKING CHANGE: <내용>`으로 적습니다.
- 관련 계획 단계는 `Refs: plan.md Phase 2.1`처럼 적습니다.
- 공동 작성자는 `Co-Authored-By: 이름 <이메일>`로 적습니다.

## 커밋 단위

- 한 커밋에는 한 가지 성격의 변경만 담습니다. 기능, 테스트, 문서가 서로 독립적이면 나눕니다.
- 기능과 그 기능을 검증하는 테스트는 한 커밋에 함께 넣어도 됩니다.
- 커밋하기 전에 `.\.venv\Scripts\python.exe -m unittest discover -s tests`가 통과해야 합니다.
- 빌드 산출물, 임시 파일, 자격증명은 커밋하지 않습니다.

## 브랜치와 push

- 기본 브랜치는 `main`이고 직접 커밋합니다.
- `plan.md`의 Phase를 하나 끝낼 때마다 커밋하고 `origin/main`에 push합니다.
- 이미 push한 커밋은 다시 쓰지 않습니다(force push 금지).

## 예시

```
feat(search): FTS5 기반 경험 검색 추가

Phase 1은 ID로만 읽을 수 있어 비슷한 문제를 다시 찾을 수 없었다.
오류 코드·명령어 일치와 한국어 부분 문자열 보완을 함께 적용한다.

Refs: plan.md Phase 2.2
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
