<!-- installed by `buildlog install-skill` for {{HOST_NAME}}; edit skills/buildlog/ in the Buildlog repository, not this copy -->

# Buildlog save plan (format v2)

One JSON object. Every list is optional, but the plan must save something.

```json
{
  "idempotency_key": "{{KEY_PREFIX}}:<YYYY-MM-DD>:<short-kebab-slug>",
  "sources": [],
  "evidence": [],
  "pages": [],
  "index_members": [],
  "terms": [],
  "relations": [],
  "join_paths": [],
  "rules": [],
  "research": []
}
```

Keep the same `idempotency_key` when retrying the same save. A new save of new material gets a new key.

## References

Objects point at pages with a reference string:

- a `ref` defined in this plan's `pages`,
- an existing page ID (UUID),
- a stored or just-ingested schema object: `system:table`, `system:schema.table`, `system:table.column`, `system:schema.table.column`. Qualify with the schema when one system has the same table name in several schemas.

Evidence is referenced by the `ref` of an item in this plan's `evidence`.

## sources

| kind | Required | Notes |
| --- | --- | --- |
| `conversation` | `ref`, `locator`, `accessible_excerpt` | No content. `locator`: "Claude Code conversation, <date>, <project>, <which part>" |
| `markdown`, `text` | `ref`, `path` or `content` | Text is stored; excerpts are checked against it |
| `sql_ddl` | `ref`, `path`/`content`, `dialect` (`postgresql` or `oracle`), `system_key` | Creates table and column pages and FK relations. Unread statements become diagnostics and `parse_status: partial` |
| `schema_json` | `ref`, `path`/`content` | `{system_key, tables: [{schema?, name, description?, columns: [{name, type?, nullable?, description?}], primary_key?, unique?, foreign_keys?: [{columns, target_table, target_schema?, target_columns}]}]}`. Omitted `nullable` stays unknown |
| `sample_csv`, `sample_json` | `ref`, `path`/`content`, `system_key`, `table` | First 50 rows kept; credential-like columns redacted; columns checked against the table |
| `web`, `document` | `ref`, `locator`, `accessible_excerpt` | Reference only, no content |

Optional on any source: `locator` (defaults to the file path), `accessible_excerpt` (defaults to the start of the content), `captured_at`. On `sql_ddl` / `schema_json`: `"replace_existing_schema": true` only when the user wants a changed schema to replace the stored one (otherwise a changed table is a `SCHEMA_CONFLICT`).

`system_key`: a short stable name for the system the schema belongs to, e.g. `mes_prod`, `crm`. Use the same key every time for the same system.

## evidence

```json
{"ref": "e1", "source": "guide", "evidence_kind": "observed", "excerpt": "exact text from the source", "section_locator": "optional", "span": {"start_line": 8, "end_line": 9}}
```

- `evidence_kind`: `observed` (seen in the source or output), `reported` (someone said it), `inferred` (your reasoning; the excerpt is not checked).
- For sources with content, observed / reported excerpts must appear in the source. Line numbers are recorded automatically; `span` is optional and narrows the search.

## pages

New page:

```json
{"ref": "p1", "type": "experience", "title": "...", "summary_md": "...", "body_md": "...", "status": "active", "evidence": ["e1"]}
```

- `type`: `experience`, `concept`, `method`, `source` (a document's own page), `index` (table of contents). Table and column pages come only from schema sources.
- experience `body_md` sections: `## 문제`, `## 환경`, `## 증거와 시도`, `## Mitigation`, `## RCA 상태`, `## 재사용 조건`.

Update an existing page (follow-up result of the same experience):

```json
{"id": "<page id>", "expected_version": 2, "body_md": "...", "summary_md": "...", "reason": "follow-up: root cause found", "evidence": ["e3"]}
```

Update only the fields that changed, including the summary when the body changes its conclusion.

## index_members

```json
{"index": "toc", "child": "p1", "position": 1}
```

`index` must be an `index` page (a ref or an existing page ID). Cycles are rejected.

Required: every page the plan creates must end up in an index — `experience`, `concept`, `method`, `source` pages and the tables a schema source creates (refer to them as `system:table`). Otherwise the save is rejected with `BUNDLE_REQUIRED` and nothing is stored. Exempt: columns (they belong to their table) and an `index` page with no parent, which is a top-level bundle. Updating an existing page that is in no index gives a `BUNDLE_MISSING` warning; add it to an index in a later plan.

## terms

```json
{"expression": "사업자번호", "scope": "mes_prod:site.biz_no", "page": "mes_prod:site.biz_no", "definition_md": "...", "aliases": ["biz_no"], "evidence": ["e2"], "status": "extracted"}
```

- `scope`: where this meaning holds — usually `system:table.column`, or a document / domain name for non-DB terms. One expression + scope has one definition.
- `page`: optional page the term describes.
- To change a stored term's definition, use `update`, or put `"id"` and `"expected_version"` on the term.

## relations

```json
{"source": "p1", "target": "mes_prod:sensor_reading", "type": "USES", "explanation_md": "...", "evidence": ["e1"], "status": "extracted"}
```

`type`: `USES`, `DEPENDS_ON`, `SUPPORTS`, `TESTED_BY`, `DERIVED_FROM`, `SUPERSEDES`, `CONTRADICTS`, `RELATED_TO`, `PART_OF`, `JOINS_TO`. Use `JOINS_TO` between tables only for joins that are not foreign keys (those are added automatically), with `status: inferred` unless the source states the join.

## join_paths

```json
{
  "system_key": "mes_prod", "title": "사업장별 센서 측정값", "purpose": "what question this path answers",
  "steps": [
    {"from_table": "site", "from_columns": ["id"], "to_table": "equipment", "to_columns": ["site_id"], "join_kind": "inner"},
    {"from_table": "equipment", "from_columns": ["id"], "to_table": "sensor_reading", "to_columns": ["equipment_id"], "join_kind": "inner"}
  ],
  "conditions_md": "filters that must be applied", "caveats_md": "fan-out, NULL, duplicate-count risks",
  "evidence": ["e3"], "status": "extracted"
}
```

- Each step's `from_table` must already be in the path (the first step starts it). Composite keys: column lists pair up by position.
- `join_kind`: `inner`, `left`, `right`, `full`, `unknown`. `cardinality`: `1:1`, `1:N`, `N:1`, `N:M`, `unknown`. `nullable`: `true`, `false`, `"unknown"`.
- Leave `cardinality` / `nullable` out (unknown) unless the source says so; Buildlog derives them from keys. A value that contradicts the keys, or one with no key support and no step `evidence`, is rejected.
- A step with no foreign key behind it needs step `evidence`, or the whole path `status: inferred`.

## rules

```json
{"scope": "mes_prod:sensor_reading", "kind": "requirement", "condition_md": "when it applies", "instruction_md": "what to do", "reason_md": "why", "alternative_md": "what to do instead", "evidence": ["e1"], "status": "extracted"}
```

- `kind`: `requirement` (must do), `prohibition` (must not do), `unsupported_operation` (the system does not offer this).
- requirement / prohibition need `reason_md` and evidence.
- unsupported_operation needs `confirmation`: `confirmed` (observed / reported evidence of absence) or `unconfirmed` (not known either way; evidence optional).
- `scope` is the page the rule applies to — usually a table or column. Rules on a table also show on its column pages.

## research

```json
{"question": "What does ORA-12541 mean and how is the listener started on Oracle Database 21c XE for Windows?", "page": "p1", "context": "Local XE on Windows 11; connection fails with ORA-12541"}
```

- Records a question for Antigravity research on the page it should support (`page`: a ref from this plan or a page ID). Nothing is researched during `ingest`; run `buildlog research --run <run_id>` afterwards.
- One narrow, documentation-answerable question per item (up to 1000 characters). `context` is optional orientation, not evidence. No secrets or private data.
- The run's `research_status` stays `pending` until the research runs, then becomes `succeeded` or `failed` (resumable).

## Samples inside schema_json

`schema_json` may carry `samples` next to `tables` (optional):

```json
{"system_key": "inv", "tables": [...], "samples": [
  {"table": "item", "rows": [{"id": 1, "name": "bolt"}]},
  {"table": "item", "format": "csv", "content": "id,name\n2,nut\n"}
]}
```

Each sample becomes its own sample source (`<ref>#samples[i]`) and follows the same rules as `sample_csv` / `sample_json`: first 50 rows kept, credential-like columns redacted, checked against the table. The rows are not kept again inside the schema source. A truncated sample, an unknown table or column, or an unread field makes that source `partial`, and the save result `status` becomes `partial`.

## prior_check

```json
{"query": "agy --print SUCCESS 결과 비어 있음", "terms": ["agy", "print timeout", "denied_actions"],
 "candidates": ["<page id read before saving>"], "decision": "follow_up", "reason": "same tool, new cause: redirect links and paraphrased quotes"}
```

- What was looked up before saving and what was decided. `decision`: `new`, `update` (a candidate page is updated in this plan), `follow_up` (a new page is related to a candidate), `already_recorded` (nothing new to save).
- `candidates`: the page IDs actually read. They must exist; merged IDs lead to their representative. The ingest result repeats the check under `prior_check`.
- Without it, saving a new experience page gives a warning. A decision the plan does not carry out (e.g. `update` with no page update) also gives a warning.
