"""Read stored knowledge by id and render it as Markdown for a working agent."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import NotFoundError

STATUS_LABELS = {"extracted": "원문 추출", "inferred": "추정", "verified": "검증됨", "rejected": "기각"}
RULE_LABELS = {"requirement": "필수 조건", "prohibition": "금칙", "unsupported_operation": "미지원 연산"}
CONFIRMATION_LABELS = {"confirmed": "부재 확인됨", "unconfirmed": "미확인 — 있는지 없는지 확인되지 않음"}
NULLABLE_LABELS = {True: "NULL 가능", False: "NOT NULL", None: "미확인", "unknown": "미확인"}

OBJECT_TABLES = {"term": "term_senses", "relation": "relations", "join_path": "join_paths", "rule": "rules"}


def _rows(c: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(r) for r in c.execute(sql, args)]


def _page_evidence(c: sqlite3.Connection, page_id: str) -> list[dict[str, Any]]:
    return _rows(c, """SELECT s.kind, s.locator, e.id AS evidence_id, e.section_locator, e.excerpt, e.evidence_kind,
                              e.start_line, e.end_line, s.metadata_json AS source_meta
                       FROM page_sources ps JOIN evidence e ON e.id = ps.evidence_id JOIN sources s ON s.id = e.source_id
                       WHERE ps.page_id = ? ORDER BY e.rowid""", (page_id,))


def _object_evidence(c: sqlite3.Connection, object_type: str, object_id: str) -> list[dict[str, Any]]:
    return _rows(c, """SELECT s.kind, s.locator, e.id AS evidence_id, e.section_locator, e.excerpt, e.evidence_kind,
                              e.start_line, e.end_line, s.metadata_json AS source_meta
                       FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id JOIN sources s ON s.id = e.source_id
                       WHERE oe.object_type = ? AND oe.object_id = ? ORDER BY e.rowid""", (object_type, object_id))


def _with_evidence(c: sqlite3.Connection, object_type: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["evidence"] = _object_evidence(c, object_type, row["id"])
    return rows


def _page_record(c: sqlite3.Connection, page: dict[str, Any]) -> dict[str, Any]:
    page_id = page["id"]
    schema = c.execute("SELECT * FROM schema_objects WHERE page_id = ?", (page_id,)).fetchone()
    schema_object = None
    related_pages = [page_id]  # pages whose terms, rules and join paths apply here
    if schema is not None:
        schema_object = dict(schema) | {"metadata": json.loads(schema["metadata_json"])}
        if schema["object_type"] == "table":
            related_pages += [r["page_id"] for r in c.execute(
                "SELECT page_id FROM schema_objects WHERE object_type = 'column' AND substr(norm_key, 1, ?) = ?",
                (len(schema["norm_key"]) + 1, schema["norm_key"] + "."))]
        else:
            table_key = schema["norm_key"].rsplit(".", 1)[0]
            table = c.execute("SELECT page_id FROM schema_objects WHERE object_type = 'table' AND norm_key = ?", (table_key,)).fetchone()
            if table is not None:
                related_pages.append(table["page_id"])
    marks = ",".join("?" * len(related_pages))
    terms = _rows(c, f"""SELECT t.*, p.title AS page_title FROM term_senses t LEFT JOIN pages p ON p.id = t.page_id
                         WHERE t.page_id IN ({marks}) ORDER BY t.normalized_expression, t.scope""", tuple(related_pages))
    for term in terms:
        term["aliases"] = [r["expression"] for r in c.execute(
            "SELECT expression FROM aliases WHERE term_sense_id = ? AND normalized_expression != ? ORDER BY expression",
            (term["id"], term["normalized_expression"]))]
    rules = _rows(c, f"""SELECT r.*, p.title AS scope_title FROM rules r JOIN pages p ON p.id = r.scope_page_id
                         WHERE r.scope_page_id IN ({marks}) AND r.status != 'rejected'
                         ORDER BY CASE r.kind WHEN 'requirement' THEN 0 WHEN 'prohibition' THEN 1 ELSE 2 END, r.created_at""",
                  tuple(related_pages))
    join_paths = _rows(c, f"""SELECT DISTINCT j.* FROM join_paths j JOIN join_path_tables jt ON jt.join_path_id = j.id
                              WHERE jt.page_id IN ({marks}) ORDER BY j.title""", tuple(related_pages))
    for path in join_paths:
        path["steps"] = json.loads(path.pop("steps_json"))
    outgoing = _rows(c, """SELECT r.*, p.title AS other_title, p.type AS other_type FROM relations r JOIN pages p ON p.id = r.target_id
                           WHERE r.source_id = ? AND r.type != 'PART_OF' AND r.removed_at IS NULL ORDER BY r.type, p.title""", (page_id,))
    incoming = _rows(c, """SELECT r.*, p.title AS other_title, p.type AS other_type FROM relations r JOIN pages p ON p.id = r.source_id
                           WHERE r.target_id = ? AND r.type != 'PART_OF' AND r.removed_at IS NULL ORDER BY r.type, p.title""", (page_id,))
    children = _rows(c, """SELECT p.id, p.title, p.type, m.position FROM index_members m JOIN pages p ON p.id = m.child_page_id
                           WHERE m.index_page_id = ? ORDER BY m.position""", (page_id,))
    parents = _rows(c, """SELECT p.id, p.title FROM index_members m JOIN pages p ON p.id = m.index_page_id
                          WHERE m.child_page_id = ? ORDER BY p.title""", (page_id,))
    return {
        "object_type": "page",
        "page": page,
        "schema_object": schema_object,
        "terms": _with_evidence(c, "term", terms),
        "rules": _with_evidence(c, "rule", rules),
        "join_paths": _with_evidence(c, "join_path", join_paths),
        "relations": {"outgoing": _with_evidence(c, "relation", outgoing), "incoming": _with_evidence(c, "relation", incoming)},
        "index": {"children": children, "parents": parents},
        "evidence": _page_evidence(c, page_id),
        "research": _rows(c, """SELECT id, question, status, attempts, error_kind, last_error, updated_at FROM research_requests
                                WHERE page_id = ? ORDER BY created_at, rowid""", (page_id,)),
    }


def read_object(store: Path, object_id: str) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"not found: {object_id} (store does not exist: {store})")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        page = c.execute("SELECT * FROM pages WHERE id = ?", (object_id,)).fetchone()
        if page is not None:
            target = db.resolve_redirect(c, object_id)
            if target != object_id:  # merged: show the representative page
                record = _page_record(c, dict(c.execute("SELECT * FROM pages WHERE id = ?", (target,)).fetchone()))
                record["redirected_from"] = object_id
                return record
            return _page_record(c, dict(page))
        for object_type, table in OBJECT_TABLES.items():
            row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone()
            if row is None:
                continue
            item = dict(row)
            item["evidence"] = _object_evidence(c, object_type, object_id)
            if object_type == "join_path":
                item["steps"] = json.loads(item.pop("steps_json"))
            if object_type == "term":
                item["aliases"] = [r["expression"] for r in c.execute(
                    "SELECT expression FROM aliases WHERE term_sense_id = ? AND normalized_expression != ?",
                    (object_id, item["normalized_expression"]))]
            for key in ("page_id", "scope_page_id", "source_id", "target_id"):
                if item.get(key):
                    linked = c.execute("SELECT title FROM pages WHERE id = ?", (item[key],)).fetchone()
                    item[key.replace("_id", "_title")] = linked["title"] if linked else None
            return {"object_type": object_type, object_type: item}
    raise NotFoundError(f"not found: {object_id}")


def read_page(store: Path, page_id: str) -> dict[str, Any]:
    record = read_object(store, page_id)
    if record["object_type"] != "page":
        raise NotFoundError(f"page not found: {page_id} (it is a {record['object_type']})")
    return record


# ---------------------------------------------------------------- rendering


def _excerpt(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + " …"


def _verification_lines(e: dict[str, Any]) -> list[str]:
    """For web evidence: what Buildlog found when it compared the quote with the page itself."""
    meta = json.loads(e.get("source_meta") or "{}")
    check = meta.get("verification")
    if not check:
        return []
    from .sources import MATCH_LABELS

    text = f"  - 원문 대조: {MATCH_LABELS.get(check['match'], check['match'])}"
    if check["match"] in ("partial", "absent"):
        text += f" (단어 일치 {int(check.get('coverage', 0) * 100)}%)"
    if check.get("error"):
        text += f" · {check['error']}"
    return [text, f"  - 확인 시각: {check.get('checked_at', '')}"]


def _evidence_lines(evidence: list[dict[str, Any]], indent: str = "  ") -> list[str]:
    lines = []
    for e in evidence:
        lines.append(f"{indent}- 근거({e['evidence_kind']}): {e['kind']} — {e['locator']} · {e['section_locator']} · \"{_excerpt(e['excerpt'])}\"")
    return lines


def _status(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def _render_columns(meta: dict[str, Any]) -> list[str]:
    pk = {c.casefold() for c in meta.get("primary_key") or []}
    fk_cols = {c.casefold(): fk for fk in meta.get("foreign_keys") or [] for c in fk["columns"]}
    unique_cols = {c.casefold() for u in meta.get("unique") or [] if len(u) == 1 for c in u}
    lines = ["| 컬럼 | 타입 | NULL | 키 | 설명 |", "| --- | --- | --- | --- | --- |"]
    for col in meta["columns"]:
        keys = []
        if col["name"].casefold() in pk:
            keys.append("PK")
        if col["name"].casefold() in unique_cols:
            keys.append("UNIQUE")
        fk = fk_cols.get(col["name"].casefold())
        if fk:
            keys.append(f"FK→{fk['target_table']}")
        description = (col.get("description") or "").replace("|", "\\|")
        lines.append(f"| `{col['name']}` | {col.get('type') or '미상'} | {NULLABLE_LABELS[col.get('nullable')]} | {', '.join(keys)} | {description} |")
    return lines


def _render_schema(schema: dict[str, Any]) -> list[str]:
    meta = schema["metadata"]
    if schema["object_type"] == "column":
        return [
            "## 컬럼 정보", "",
            f"- 시스템: `{schema['system_key']}` · 테이블: `{meta['table']}`",
            f"- 타입: {meta.get('type') or '미상'} · NULL: {NULLABLE_LABELS[meta.get('nullable')]}",
            *([f"- 기본값: `{meta['default']}`"] if meta.get("default") else []),
        ]
    lines = ["## 테이블 구조", "", f"- 시스템: `{schema['system_key']}`" + (f" · 스키마: `{meta['schema']}`" if meta.get("schema") else ""), ""]
    lines += _render_columns(meta)
    lines.append("")
    pk = meta.get("primary_key") or []
    lines.append(f"- 기본키: {', '.join(f'`{c}`' for c in pk) if pk else '원문에 없음 (유일성 미확인)'}")
    for unique in meta.get("unique") or []:
        lines.append(f"- 유일 제약: {', '.join(f'`{c}`' for c in unique)}")
    for fk in meta.get("foreign_keys") or []:
        target = f"{fk['target_schema']}.{fk['target_table']}" if fk.get("target_schema") else fk["target_table"]
        lines.append(f"- 외래키: ({', '.join(fk['columns'])}) → {target}({', '.join(fk['target_columns'])})")
    for check in meta.get("checks") or []:
        lines.append(f"- CHECK: `{check}`")
    if meta.get("options_raw"):
        lines.append(f"- 해석하지 않은 테이블 옵션: `{meta['options_raw']}`")
    return lines


def _render_terms(terms: list[dict[str, Any]]) -> list[str]:
    lines = ["## 용어 정의", ""]
    for t in terms:
        alias = f" · 별칭: {', '.join(t['aliases'])}" if t.get("aliases") else ""
        target = f" · 대상: {t['page_title']}" if t.get("page_title") else ""
        lines.append(f"- **{t['expression']}** (맥락: {t['scope']}) [{_status(t['status'])}]{target}{alias} · `{t['id']}` v{t['version']}")
        lines.append(f"  - {t['definition_md']}")
        lines += _evidence_lines(t["evidence"], "  ")
    return lines


def _render_rules(rules: list[dict[str, Any]]) -> list[str]:
    lines = ["## 필수 조건·금칙·미지원 연산", ""]
    for r in rules:
        label = RULE_LABELS[r["kind"]]
        if r["kind"] == "unsupported_operation":
            label += f" ({CONFIRMATION_LABELS[r['confirmation']]})"
        lines.append(f"- **{label}** — {r['instruction_md']} [{_status(r['status'])}] · 대상: {r['scope_title']} · `{r['id']}` v{r['version']}")
        if r["condition_md"]:
            lines.append(f"  - 적용 조건: {r['condition_md']}")
        if r["reason_md"]:
            lines.append(f"  - 이유: {r['reason_md']}")
        if r["alternative_md"]:
            lines.append(f"  - 대안: {r['alternative_md']}")
        lines += _evidence_lines(r["evidence"], "  ")
    return lines


def _basis(step: dict[str, Any], field: str) -> str:
    basis = step.get(f"{field}_basis")
    return {"schema": "스키마 키 근거", "evidence": "제시된 근거"}.get(basis or "", "근거 없음")


def _render_join_paths(paths: list[dict[str, Any]]) -> list[str]:
    lines = ["## 조인 경로", ""]
    for p in paths:
        lines.append(f"### {p['title']}")
        lines.append("")
        lines.append(f"- ID: `{p['id']}` v{p['version']} · 시스템: `{p['system_key']}` · 상태: {_status(p['status'])}")
        lines.append(f"- 목적: {p['purpose']}")
        for i, s in enumerate(p["steps"], 1):
            on = " AND ".join(f"{s['from_table']}.{a} = {s['to_table']}.{b}" for a, b in zip(s["from_columns"], s["to_columns"]))
            fk = {"forward": "FK 있음", "backward": "FK 있음(역방향)"}.get(s.get("foreign_key") or "", "FK 없음 — 업무상 연결")
            card = "미확인" if s["cardinality"] == "unknown" else f"{s['cardinality']} ({_basis(s, 'cardinality')})"
            null = NULLABLE_LABELS.get(s["nullable"], "미확인")
            null_text = null if s["nullable"] == "unknown" else f"{null} ({_basis(s, 'nullable')})"
            kind = "미지정" if s["join_kind"] == "unknown" else s["join_kind"].upper()
            lines.append(f"{i}. `{s['from_table']}` → `{s['to_table']}` ON `{on}` · {kind} JOIN · 관계 수: {card} · 연결 컬럼 NULL: {null_text} · {fk}")
        if p["conditions_md"]:
            lines.append(f"- 조건: {p['conditions_md']}")
        if p["caveats_md"]:
            lines.append(f"- 주의: {p['caveats_md']}")
        lines += _evidence_lines(p["evidence"], "")
        lines.append("")
    return lines[:-1]


def _render_relations(relations: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = ["## 관계", ""]
    for r in relations["outgoing"]:
        lines.append(f"- 이 페이지 —{r['type']}→ {r['other_title']} (`{r['target_id']}`) [{_status(r['status'])}] · `{r['id']}`: {r['explanation_md']}")
        lines += _evidence_lines(r["evidence"], "  ")
    for r in relations["incoming"]:
        lines.append(f"- {r['other_title']} (`{r['source_id']}`) —{r['type']}→ 이 페이지 [{_status(r['status'])}] · `{r['id']}`: {r['explanation_md']}")
        lines += _evidence_lines(r["evidence"], "  ")
    return lines


def _render_page(record: dict[str, Any], include_rules: bool = True) -> list[str]:
    page = record["page"]
    lines = [
        f"# {page['title']}", "",
        f"- ID: `{page['id']}`", f"- Type: `{page['type']}`", f"- Status: `{page['status']}`",
        f"- Version: {page['version']}", f"- Updated: {page['updated_at']}",
    ]
    if record.get("redirected_from"):
        lines.append(f"- 병합됨: 요청한 `{record['redirected_from']}`은 이 페이지로 병합됐다")
    lines += ["", "## 요약", ""]
    if page.get("summary_dirty"):
        lines += ["> 요약이 최근 본문 수정 전 내용이다. 본문을 기준으로 읽고, 요약은 갱신이 필요하다.", ""]
    lines.append(page["summary_md"])
    # Schema pages often have only the source description; do not print it twice.
    if page["body_md"].strip() != page["summary_md"].strip():
        lines += ["", "## 기록", "", page["body_md"]]
    sections: list[list[str]] = []
    if record.get("schema_object"):
        sections.append(_render_schema(record["schema_object"]))
    if include_rules and record.get("rules"):
        sections.append(_render_rules(record["rules"]))
    if record.get("terms"):
        sections.append(_render_terms(record["terms"]))
    if record.get("join_paths"):
        sections.append(_render_join_paths(record["join_paths"]))
    relations = record.get("relations") or {"outgoing": [], "incoming": []}
    if relations["outgoing"] or relations["incoming"]:
        sections.append(_render_relations(relations))
    index = record.get("index") or {"children": [], "parents": []}
    if index["children"]:
        sections.append(["## 목차", ""] + [f"{c['position']}. {c['title']} (`{c['id']}`)" for c in index["children"]])
    research = record.get("research") or []
    if research:
        labels = {"pending": "대기", "running": "진행 중 또는 중단됨", "succeeded": "완료", "failed": "실패"}
        block = ["## 외부 조사", ""]
        for r in research:
            line = f"- {r['question']} · {labels.get(r['status'], r['status'])} · 시도 {r['attempts']}회 · `{r['id']}`"
            if r["status"] == "failed":
                line += f" · 원인({r['error_kind']}): {_excerpt(r['last_error'] or '', 200)}"
            block.append(line)
        if any(r["status"] != "succeeded" for r in research):
            block += ["", "끝나지 않은 조사가 있다. 이 기록의 근거는 아직 보강 전이며 `buildlog research --page <id>`로 재개한다."]
        sections.append(block)
    if index["parents"]:
        sections.append(["## 상위 목차", ""] + [f"- {p['title']} (`{p['id']}`)" for p in index["parents"]])
    if record["evidence"]:
        sections.append(["## 근거"] + [
            line for e in record["evidence"] for line in (
                "", f"- Source: {e['kind']} — {e['locator']}", f"  - Location: {e['section_locator']}",
                f"  - Kind: {e['evidence_kind']}", f"  - Excerpt: {_excerpt(e['excerpt'], 600)}",
                *_verification_lines(e),
            )
        ])
    for section in sections:
        lines += [""] + section
    return lines


def render_markdown(record: dict[str, Any]) -> str:
    kind = record["object_type"]
    if kind == "page":
        lines = _render_page(record)
    elif kind == "term":
        t = record["term"]
        lines = [f"# 용어: {t['expression']}", ""] + _render_terms([t | {"page_title": t.get("page_title")}])[2:]
    elif kind == "rule":
        r = record["rule"]
        lines = [f"# {RULE_LABELS[r['kind']]}", ""] + _render_rules([r | {"scope_title": r.get("scope_page_title")}])[2:]
    elif kind == "join_path":
        lines = ["# 조인 경로", ""] + _render_join_paths([record["join_path"]])[2:]
    else:
        r = record["relation"]
        lines = [f"# 관계: {r.get('source_title')} —{r['type']}→ {r.get('target_title')}", "",
                 f"- ID: `{r['id']}` v{r['version']} · 상태: {_status(r['status'])}", f"- 설명: {r['explanation_md']}"]
        if r.get("removed_at"):
            lines.append(f"- 연결 해제됨: {r['removed_at']} · 이유: {r.get('removed_reason') or ''}")
        lines += _evidence_lines(r["evidence"], "")
    return "\n".join(lines) + "\n"
