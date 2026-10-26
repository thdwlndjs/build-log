---
name: buildlog
description: The user's local Buildlog knowledge store. Search it for past experiences and data-system knowledge relevant to the current task (similar errors, fixes, table structures, join paths, required query conditions, prohibitions, unsupported operations), save new knowledge into it, research supporting sources with Antigravity, and read or correct stored records. Saves (1) an experience from the current conversation — problem, environment, evidence, mitigation, RCA — and (2) material the user points to — Markdown/text documents, SQL DDL, schema JSON, CSV/JSON samples — as tables, columns, terms, relations, join paths and rules. Use when the user asks to search, look up, save, ingest, or research with Buildlog (빌드로그에서 찾아줘 / 저장 / 인제스트 / 근거 조사), asks whether a problem was seen before, or asks to read a Buildlog record by ID. Not for graph editing (not available yet).
---
<!-- installed by `buildlog install-skill` for {{HOST_NAME}}; edit skills/buildlog/ in the Buildlog repository, not this copy -->

# Buildlog

Buildlog is a local Python program with one SQLite store shared by every project and host (Claude Code and Codex use the same store).

- Python (always this absolute path; never `python` from PATH; works from any directory):
  `{{PYTHON}}`
- Default store: `%LOCALAPPDATA%\Buildlog\buildlog.sqlite3`. Use another store only when the user names one: add `--store "<path>"` right after `-m buildlog`.
- Plan format reference: [plan-format.md](plan-format.md) — read it before writing any plan that has more than one page.
- Writing JSON input files: {{WRITE_FILE}}

You do all the work. Never ask the user to write JSON, copy anything, or run a command.

## Save

1. Decide what to save.
   - Conversation experience: the one problem the user means. Do not invent facts that are not in the conversation. Source `locator`: "{{HOST_NAME}} conversation, <date>, <project folder>, <which part>".
   - Material: read every file the user points to yourself. Decide which tables, terms, relations, join paths and rules the material actually supports.
   - A general setup or tooling problem gets an `experience` page only. Do not add terms, join paths or rules just because the format allows them.
2. Write the save plan as a UTF-8 JSON file. Follow [plan-format.md](plan-format.md). `idempotency_key`: `{{KEY_PREFIX}}:<YYYY-MM-DD>:<short-kebab-slug>`.
   - Pass files as `path` sources; Buildlog reads them, keeps their text, and parses DDL / schema JSON / samples itself.
   - Every non-inferred evidence `excerpt` must be copied exactly from its source text (whitespace may differ). Buildlog rejects excerpts it cannot find. Your own reasoning is evidence with `evidence_kind: "inferred"`.
3. Run:

```
"{{PYTHON}}" -m buildlog ingest --input "<plan.json path>"
```

4. If it fails with exit 2, read the message, fix the plan, and run again with the same `idempotency_key`. Do not drop content to make it pass; if a claim cannot be supported, lower its status (e.g. `inferred`, cardinality `unknown`, `unconfirmed`) instead.
5. Report to the user from the JSON result: what was created / updated / unchanged (counts and the main IDs), each source's `parse_status` and any diagnostics, every warning, whether the run was `reused`, `research_status`, and the store path. Then delete the plan file.

## Search

Use this when the user asks whether a problem was seen before, asks you to look something up in Buildlog, or asks you to work with a system whose knowledge is in Buildlog.

1. Write a query file (UTF-8 JSON) with the question and extra search terms you choose:

```json
{"query": "the user's question in their words", "terms": ["ORA-12541", "listener", "리스너", "sensor_reading"], "scope": "demo_mes"}
```

   - `terms`: what the question does not literally say but a stored record would — exact error codes and messages, command and tool names, English and Korean forms of the key words, synonyms, table and column names. 3–8 terms. Similar problems described in different words are found only through these.
   - `scope`: optional. A system key (e.g. `demo_mes`) or one environment word (e.g. `프록시`, `WSL`). Leave it out when unsure; records outside the scope are listed separately, not hidden.
   - Optional: `limit` (records with full text, default 5), `max_bytes` (default 24000).
2. Run:

```
"{{PYTHON}}" -m buildlog query --input "<query.json path>"
```

3. Read the returned Markdown as working context. Then:
   - Apply everything under "반드시 지킬 규칙" to the task. Never assume an unsupported operation exists.
   - Under "질문에 나온 용어", use only the definition whose context matches the task.
   - Under "상충·정정 기록", compare the records' conditions before reusing a fix; prefer the one that supersedes.
   - If it says no record was found ("관련 기록을 찾지 못했다"), tell the user plainly. Do not present unrelated records as matches.
   - Tell the user briefly which records you used (title and ID).
4. If it fails with `BUDGET_TOO_SMALL`, run again with a larger `max_bytes` or read the listed IDs one by one.

Related lookups:

```
"{{PYTHON}}" -m buildlog neighbors <page-id> [--type JOINS_TO] [--direction out|in|both]
"{{PYTHON}}" -m buildlog paths --system <system_key> --from <table> --to <table> [--purpose "<what for>"]
```

`paths` returns registered join paths first. Without one it returns **estimated candidates** from table relations; do not treat a candidate as the correct business path until its purpose, filters and cardinality are reviewed.

## Research (Antigravity)

Use this when the user asks to research or back up a record with sources (근거 조사, 공식 문서 확인), or when saving an experience whose cause or method rests on outside facts the conversation did not verify (what an error code means, documented behavior of a version) and the user asked for research. Each question takes about 1–4 minutes, so do not research on every save unasked.

1. Ask narrow questions that official documentation can answer, one fact each, with no secrets or private data in the question or context.
   - While saving: add `research` items to the plan (see [plan-format.md](plan-format.md)). The ingest result has the `run_id`.
   - For a stored page: pass `--page <page-id> --question "<question>" --context "<short context>"`.
2. Run with a command timeout of at least 10 minutes (e.g. 600000 ms):

```
"{{PYTHON}}" -m buildlog research --run <run_id>
"{{PYTHON}}" -m buildlog research --page <page-id> --question "<question>" --context "<context>"
```

3. Exit 0: each finding is stored as a web source and evidence on the page. Report title, URL, and access level to the user.
   - `원문 확인` (fetched): the researcher opened the page.
   - `검색 결과 요약만 확인` (search_snippet) and `모델 기억(원문 미확인)` (from_memory): second-hand. Do not call them verified.
   - If a finding changes the record's conclusion or RCA, update the page with `update` and say so; Buildlog does not rewrite conclusions by itself.
4. Exit 5 (`RESEARCH_FAILED`): the experience is already saved. Tell the user the failure kind and hint from the output. Run the same command again to resume only the unfinished questions. For `auth`, ask the user to start `agy.exe` and sign in — that is the only step the user has to do.

## Read

```
"{{PYTHON}}" -m buildlog read <id>
```

The ID may be a page (experience, table, column, document, index), term, relation, join path, or rule. Show the returned Markdown to the user as-is. When the user asks to apply a stored table's knowledge, treat its requirements, prohibitions and unsupported operations as binding.

## Correct a stored item

```
"{{PYTHON}}" -m buildlog update <id> --expected-version <n> --patch "<patch.json>" --reason "<why>"
```

Read the item first to get its current version. Allowed patch fields: page `title, summary_md, body_md, status`; term `definition_md, scope, status`; relation `explanation_md, status`; join path `title, purpose, conditions_md, caveats_md, status`; rule `condition_md, instruction_md, reason_md, alternative_md, status, confirmation`.

## Judgment rules

- Mitigation is not RCA. A workaround that worked does not prove the root cause; write "RCA 미확정" and what is still unknown.
- Status of terms, relations, join paths and rules: `extracted` (stated in the source), `inferred` (your reading of samples or context), `verified` (the user or a test confirmed it), `rejected`. Foreign keys from DDL / schema JSON are stored as `extracted` automatically; do not restate them as relations.
- The same expression in a different system, table or column is a different term: give each its own `scope`.
- Do not state cardinality or nullability the source does not support. Leave `unknown`; Buildlog fills them in from primary and foreign keys when the schema proves them.
- A join step without a foreign key must cite evidence; mark such paths `inferred` unless the source states the join.
- Unsupported operation: `confirmed` only with observed or reported evidence that the feature is absent. Not finding a mention is `unconfirmed`.
- Research output is a lead, not a verdict, until a fetched primary source or a test confirms it.
- Never include passwords, tokens, API keys or other credentials in structured fields. Buildlog redacts credential-like values inside file content and samples and reports a warning.

## Errors

The CLI prints a JSON envelope `{"status": "error", "error": "..."}` on stderr.

| Exit | Meaning | What to do |
| --- | --- | --- |
| 2 | Invalid plan (missing field, excerpt not found, unsupported claim, unknown reference, credential), or `BUDGET_TOO_SMALL` | Fix and retry (Save step 4 / Search step 4) |
| 3 | Conflict: `IDEMPOTENCY_CONFLICT`, `VERSION_CONFLICT`, `SCHEMA_CONFLICT`, `TERM_CONFLICT`, `JOIN_PATH_CONFLICT`, `RULE_CONFLICT` | Do not overwrite. Tell the user what already exists (the message names it) and ask how to proceed |
| 4 | Not found (ID, table, run, or store) | Tell the user; for a lost ID, search instead |
| 5 | `RESEARCH_FAILED` | Research step 4 |
| 6 | Store failure | Report the message as-is |

If search results look stale after editing the store by other means, run `buildlog reindex`.
