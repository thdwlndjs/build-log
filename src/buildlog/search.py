"""Search, neighbor lookup, join path lookup, and the agent-facing query Markdown."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import BuildlogError, NotFoundError, RELATION_TYPES, ValidationError, normalize_text
from .read import (
    CONFIRMATION_LABELS, RULE_LABELS, _evidence_lines, _page_record, _render_join_paths, _render_page, _status,
)

DEFAULT_CANDIDATES = 20
DEFAULT_LIMIT = 5
DEFAULT_MAX_RELATED = 10
DEFAULT_MAX_BYTES = 24_000
MIN_RELATIVE_SCORE = 0.2  # candidates scoring below this share of the best one are not selected
MAX_PATH_DEPTH = 4
MAX_PATH_CANDIDATES = 5

# Korean particles stripped from the end of question words ("파티션을" -> "파티션").
_PARTICLES = sorted(["에서는", "으로는", "에서", "으로", "에게", "한테", "까지", "부터", "처럼", "보다", "이랑",
                     "을", "를", "이", "가", "은", "는", "의", "에", "로", "와", "과", "도", "만", "랑"], key=len, reverse=True)
_WORD_RE = re.compile(r"[\w][\w\-.:/]*[\w]|[\w]", re.UNICODE)
_HANGUL_RE = re.compile(r"[가-힣]")
_STATUS_PENALTY = {"deprecated": 0.6, "superseded": 0.6, "draft": 0.9}
_FIELD_WEIGHTS = {"title": 6.0, "aliases": 4.0, "summary": 3.0, "body": 1.5, "extra": 1.0}
_CONFLICT_TYPES = ("CONTRADICTS", "SUPERSEDES")


class BudgetTooSmall(BuildlogError):
    def __init__(self, needed: int, max_bytes: int, record_ids: list[str]) -> None:
        super().__init__(
            f"BUDGET_TOO_SMALL: the top record needs {needed} bytes with its rules, but max_bytes is {max_bytes}; "
            f"raise max_bytes or read the records one by one: {', '.join(record_ids)}"
        )
        self.data = {"needed_bytes": needed, "max_bytes": max_bytes, "record_ids": record_ids}


# ---------------------------------------------------------------- query terms


def _strip_particle(word: str) -> str:
    if not _HANGUL_RE.search(word[-1:]):
        return word
    for particle in _PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def query_terms(text: str, extra_terms: list[str] | None = None) -> list[str]:
    """Words of the question (particles stripped) plus host-supplied terms, deduplicated."""
    # One-character words of the question match almost everything; host terms are kept as given.
    words = [w for w in (_strip_particle(w) for w in _WORD_RE.findall(unicodedata.normalize("NFKC", text or ""))) if len(w) > 1]
    terms: list[str] = []
    seen: set[str] = set()
    for term in words + [unicodedata.normalize("NFKC", t).strip() for t in extra_terms or []]:
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def _is_identifier(term: str) -> bool:
    """Error codes, commands, and code names: matched exactly and ranked first."""
    return bool(re.search(r"\d", term) and re.search(r"[A-Za-z]", term)) or "_" in term or bool(re.fullmatch(r"[A-Z]{2,}[A-Z0-9\-]*", term))


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- candidates and scoring


def _candidates(c: sqlite3.Connection, terms: list[str]) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    long_terms = [t for t in terms if len(t) >= 3]
    if long_terms:
        expr = " OR ".join(_fts_phrase(t) for t in long_terms)
        for row in c.execute("SELECT * FROM search_index WHERE search_index MATCH ? ORDER BY bm25(search_index) LIMIT 500", (expr,)):
            rows[row["page_id"]] = row
    for term in (t for t in terms if len(t) < 3):
        pattern = _like(term)
        for row in c.execute("SELECT * FROM search_index WHERE title LIKE ? ESCAPE '\\' OR aliases LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' LIMIT 500",
                             (pattern, pattern, pattern)):
            rows[row["page_id"]] = row
    normalized = {normalize_text(t) for t in terms}
    marks = ",".join("?" * len(normalized))
    for row in c.execute(f"""SELECT DISTINCT si.* FROM aliases a JOIN term_senses t ON t.id = a.term_sense_id
                             JOIN search_index si ON si.page_id = t.page_id WHERE a.normalized_expression IN ({marks})""",
                         tuple(normalized)):
        rows[row["page_id"]] = row
    return rows


def _score(row: sqlite3.Row, terms: list[str]) -> tuple[float, list[str]]:
    total = 0.0
    matched: list[str] = []
    alias_set = {normalize_text(a) for a in re.split(r"\s+", row["aliases"] or "") if a}
    for term in terms:
        folded = term.casefold()
        term_score = 0.0
        for field, weight in _FIELD_WEIGHTS.items():
            value = (row[field] or "").casefold()
            if folded in value:
                term_score += weight
        if normalize_text(term) in alias_set or normalize_text(term) in normalize_text(row["aliases"] or "").split(" "):
            term_score += 4.0
        if _is_identifier(term):
            pattern = re.compile(r"(?<![\w\-])" + re.escape(term) + r"(?![\w\-])", re.IGNORECASE)
            if any(pattern.search(row[f] or "") for f in _FIELD_WEIGHTS):
                term_score = term_score * 2 + 10
        if term_score:
            matched.append(term)
            total += term_score
    coverage = len(matched) / len(terms)
    return total * (0.5 + coverage), matched


# ---------------------------------------------------------------- scope


def _systems(c: sqlite3.Connection) -> set[str]:
    return {r[0].casefold() for r in c.execute("SELECT DISTINCT system_key FROM schema_objects")}


def _page_system(c: sqlite3.Connection, page_id: str) -> str | None:
    row = c.execute("SELECT system_key FROM schema_objects WHERE page_id = ?", (page_id,)).fetchone()
    return row["system_key"] if row else None


def _neighbor_systems(c: sqlite3.Connection, page_id: str) -> set[str]:
    rows = c.execute("""SELECT so.system_key FROM relations r JOIN schema_objects so
                        ON so.page_id = CASE WHEN r.source_id = ? THEN r.target_id ELSE r.source_id END
                        WHERE (r.source_id = ? OR r.target_id = ?) AND r.status != 'rejected' AND r.removed_at IS NULL""", (page_id, page_id, page_id))
    return {r[0].casefold() for r in rows}


def _scope_label(c: sqlite3.Connection, page_id: str) -> str:
    system = _page_system(c, page_id)
    if system:
        return f"시스템 {system}"
    systems = sorted(_neighbor_systems(c, page_id))
    return f"관련 시스템 {', '.join(systems)}" if systems else "시스템 없음"


def _environment_text(row: sqlite3.Row) -> str:
    """Where a page states its environment: title, summary, and the body's 환경/Environment section.

    Other sections may mention the scope word only to exclude it ("프록시가 없는 네트워크에서만").
    """
    body = row["body"] or ""
    match = re.search(r"^#+\s*(?:환경|Environment)[^\n]*\n(.*?)(?=^#+\s|\Z)", body, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    section = match.group(1) if match else body
    return " ".join([row["title"] or "", row["summary"] or "", section, row["extra"] or ""]).casefold()


def _in_scope(c: sqlite3.Connection, row: sqlite3.Row, scope: str | None, systems: set[str]) -> bool:
    if not scope:
        return True
    folded = scope.casefold()
    if folded in systems:
        system = _page_system(c, row["page_id"])
        if system is not None:
            return system.casefold() == folded
        return folded in _neighbor_systems(c, row["page_id"]) or folded in _environment_text(row)
    return folded in _environment_text(row)


# ---------------------------------------------------------------- query


def _shift_headings(lines: list[str]) -> list[str]:
    flat = [line for chunk in lines for line in chunk.split("\n")]
    return ["#" + line if line.startswith("#") else line for line in flat]


def _rule_lines(rule: dict[str, Any]) -> list[str]:
    label = RULE_LABELS[rule["kind"]]
    if rule["kind"] == "unsupported_operation":
        label += f" ({CONFIRMATION_LABELS[rule['confirmation']]})"
    via = " (함께 전달한 조인 경로가 지나는 테이블)" if rule.get("via_join_path") else ""
    lines = [f"- **{label}** — {rule['instruction_md']} · 대상: {rule['scope_title']}{via} [{_status(rule['status'])}] · `{rule['id']}` v{rule['version']}"]
    for field, name in (("condition_md", "적용 조건"), ("reason_md", "이유"), ("alternative_md", "대안")):
        if rule[field]:
            lines.append(f"  - {name}: {rule[field]}")
    lines += _evidence_lines(rule["evidence"], "  ")
    return lines


def _conflicts(c: sqlite3.Connection, page_ids: list[str]) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(page_ids))
    types = ",".join("?" * len(_CONFLICT_TYPES))
    rows = c.execute(f"""SELECT r.*, s.title AS source_title, s.status AS source_status, t.title AS target_title,
                                t.status AS target_status
                         FROM relations r JOIN pages s ON s.id = r.source_id JOIN pages t ON t.id = r.target_id
                         WHERE r.type IN ({types}) AND r.status != 'rejected' AND r.removed_at IS NULL
                           AND (r.source_id IN ({marks}) OR r.target_id IN ({marks}))
                         ORDER BY r.type, s.title""", (*_CONFLICT_TYPES, *page_ids, *page_ids)).fetchall()
    return [dict(r) for r in rows]


def query(store: Path, text: str, terms: list[str] | None = None, scope: str | None = None, limit: int = DEFAULT_LIMIT,
          candidates: int = DEFAULT_CANDIDATES, max_related: int = DEFAULT_MAX_RELATED, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    all_terms = query_terms(text, terms)
    if not all_terms:
        raise ValidationError("query needs at least one search word")
    for name, value in (("limit", limit), ("candidates", candidates), ("max_bytes", max_bytes)):
        if not isinstance(value, int) or value < 1:
            raise ValidationError(f"{name} must be a positive integer")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = _candidates(c, all_terms)
        scored = []
        for page_id, row in rows.items():
            if c.execute("SELECT 1 FROM page_redirects WHERE from_page_id = ?", (page_id,)).fetchone():
                continue  # merged pages are found through their representative
            score, matched = _score(row, all_terms)
            if not matched:
                continue
            page = c.execute("SELECT id, title, type, status, version, summary_md FROM pages WHERE id = ?", (page_id,)).fetchone()
            score *= _STATUS_PENALTY.get(page["status"], 1.0) * (0.9 if page["type"] == "schema_column" else 1.0)
            scored.append({"row": row, "page": dict(page), "score": round(score, 3), "matched": matched})
        scored.sort(key=lambda item: (-item["score"], item["page"]["title"]))
        scored = scored[:candidates]
        best = scored[0]["score"] if scored else 0.0
        systems = _systems(c)
        in_scope, out_of_scope = [], []
        for item in scored:
            if item["score"] < best * MIN_RELATIVE_SCORE:
                continue
            (in_scope if _in_scope(c, item["row"], scope, systems) else out_of_scope).append(item)
        selected = in_scope[:limit]
        selected_ids = [item["page"]["id"] for item in selected]
        records = [_page_record(c, dict(c.execute("SELECT * FROM pages WHERE id = ?", (pid,)).fetchone())) for pid in selected_ids]

        rules: list[dict[str, Any]] = []
        for record in records:
            for rule in record["rules"]:
                if rule["id"] not in {r["id"] for r in rules}:
                    rules.append(rule)
        # Tables that a delivered join path passes through bring their rules too:
        # an agent following the path will query those tables.
        path_tables = {page for record in records for path in record["join_paths"]
                       for step in path["steps"] for page in (step["from_table_id"], step["to_table_id"])}
        path_tables -= set(selected_ids)
        for table_id in sorted(path_tables):
            table_record = _page_record(c, dict(c.execute("SELECT * FROM pages WHERE id = ?", (table_id,)).fetchone()))
            for rule in table_record["rules"]:
                if rule["id"] not in {r["id"] for r in rules}:
                    rules.append(rule | {"via_join_path": True})
        rules.sort(key=lambda r: {"requirement": 0, "prohibition": 1}.get(r["kind"], 2))

        normalized = {normalize_text(t) for t in all_terms}
        marks = ",".join("?" * len(normalized))
        term_rows = [dict(r) for r in c.execute(
            f"""SELECT DISTINCT t.*, p.title AS page_title FROM term_senses t JOIN aliases a ON a.term_sense_id = t.id
                LEFT JOIN pages p ON p.id = t.page_id WHERE a.normalized_expression IN ({marks}) AND t.status != 'rejected'
                ORDER BY t.normalized_expression, t.scope""", tuple(normalized))]
        for term in term_rows:
            term["in_scope"] = not scope or scope.casefold() in term["scope"].casefold()

        conflicts = _conflicts(c, selected_ids) if selected_ids else []

        related: list[dict[str, Any]] = []
        if selected_ids:
            marks_sel = ",".join("?" * len(selected_ids))
            for r in c.execute(f"""SELECT r.type, r.status, r.explanation_md, r.source_id, r.target_id, p.id AS other_id, p.title, p.summary_md, p.type AS page_type
                                   FROM relations r JOIN pages p ON p.id = CASE WHEN r.source_id IN ({marks_sel}) THEN r.target_id ELSE r.source_id END
                                   WHERE (r.source_id IN ({marks_sel}) OR r.target_id IN ({marks_sel})) AND r.type != 'PART_OF' AND r.status != 'rejected' AND r.removed_at IS NULL
                                   ORDER BY CASE r.type WHEN 'CONTRADICTS' THEN 0 WHEN 'SUPERSEDES' THEN 1 ELSE 2 END, p.title""",
                                (*selected_ids, *selected_ids, *selected_ids)):
                if r["other_id"] in selected_ids or any(x["id"] == r["other_id"] for x in related):
                    continue
                related.append({"id": r["other_id"], "title": r["title"], "summary_md": r["summary_md"], "relation": r["type"],
                                "status": r["status"], "via": r["source_id"] if r["source_id"] in selected_ids else r["target_id"]})
                if len(related) >= max_related:
                    break

    result = {
        "query": text, "terms": all_terms, "scope": scope,
        "results": [{"id": i["page"]["id"], "title": i["page"]["title"], "type": i["page"]["type"], "version": i["page"]["version"],
                     "status": i["page"]["status"], "score": i["score"], "matched_terms": i["matched"]} for i in selected],
        "rules": [{"id": r["id"], "kind": r["kind"], "scope": r["scope_title"], "instruction_md": r["instruction_md"],
                   "confirmation": r["confirmation"], "status": r["status"]} for r in rules],
        "terms_found": [{"id": t["id"], "expression": t["expression"], "scope": t["scope"], "in_scope": t["in_scope"]} for t in term_rows],
        "conflicts": [{"id": x["id"], "type": x["type"], "source": x["source_title"], "target": x["target_title"]} for x in conflicts],
        "related": [{k: v for k, v in r.items() if k != "summary_md"} for r in related],
        "out_of_scope": [{"id": i["page"]["id"], "title": i["page"]["title"], "score": i["score"]} for i in out_of_scope],
    }
    with closing(db.connect(store)) as c:
        scope_labels = {i["page"]["id"]: _scope_label(c, i["page"]["id"]) for i in out_of_scope}
    markdown, omitted = _render_query(result, records, rules, term_rows, conflicts, related, out_of_scope, scope_labels, max_bytes)
    result["omitted"] = omitted
    result["markdown"] = markdown
    result["bytes"] = len(markdown.encode("utf-8"))
    return result


def _render_query(result: dict[str, Any], records: list[dict[str, Any]], rules: list[dict[str, Any]], terms: list[dict[str, Any]],
                  conflicts: list[dict[str, Any]], related: list[dict[str, Any]], out_of_scope: list[dict[str, Any]],
                  scope_labels: dict[str, str], max_bytes: int) -> tuple[str, list[dict[str, Any]]]:
    header = ["# Buildlog 조회 결과", "", f"- 질문: {result['query']}", f"- 검색어: {', '.join(f'`{t}`' for t in result['terms'])}",
              f"- 범위: {result['scope'] or '지정 없음'}"]
    if not records:
        header += ["", "관련 기록을 찾지 못했다. 이 문제에 대한 저장된 기록이 없거나 다른 검색어가 필요하다. 없는 기록을 추측해 채우지 않는다."]
        if out_of_scope:
            header += ["", "## 다른 범위의 후보", "", "지정한 범위 밖에서만 맞는 기록이다. 범위가 맞는지 확인한 뒤 `buildlog read <id>`로 읽는다.", ""]
            header += [f"- {i['page']['title']} (`{i['page']['id']}`) · {scope_labels[i['page']['id']]}" for i in out_of_scope]
        return "\n".join(header) + "\n", []
    header.append("- 선택한 기록: " + ", ".join(f"{r['page']['title']} (`{r['page']['id']}` v{r['page']['version']})" for r in records))

    fixed: list[str] = []
    if rules:
        fixed += ["", "## 반드시 지킬 규칙", "", "선택한 기록에 연결된 필수 조건·금칙·미지원 연산이다. 작업에 그대로 적용하고, 미지원 연산은 있다고 가정하지 않는다.", ""]
        for rule in rules:
            fixed += _rule_lines(rule)
    if terms:
        fixed += ["", "## 질문에 나온 용어", "", "같은 표현이라도 맥락마다 뜻이 다르다. 작업 대상의 맥락에 맞는 정의만 쓴다.", ""]
        for t in terms:
            mark = "" if not result["scope"] else (" · **이 범위**" if t["in_scope"] else " · 다른 범위")
            target = f" · 대상: {t['page_title']}" if t.get("page_title") else ""
            fixed.append(f"- **{t['expression']}** (맥락: {t['scope']}){mark}{target} [{_status(t['status'])}] · `{t['id']}`: {t['definition_md']}")
    if conflicts:
        fixed += ["", "## 상충·정정 기록", "", "아래 기록끼리 결론이 다르거나 한쪽이 다른 쪽을 대체한다. 적용 조건을 비교한 뒤 쓴다.", ""]
        for x in conflicts:
            note = ""
            for side in ("source", "target"):
                if x[f"{side}_status"] in {"superseded", "deprecated"}:
                    note += f" · {x[f'{side}_title']} 상태: {x[f'{side}_status']}"
            fixed.append(f"- {x['source_title']} —{x['type']}→ {x['target_title']} [{_status(x['status'])}] · `{x['id']}`: {x['explanation_md']}{note}")

    rendered = [_shift_headings(_render_page(record, include_rules=False)) for record in records]
    for i, block in enumerate(rendered):
        block[0] = f"## 기록 {i + 1}. " + block[0].lstrip("#").strip()

    def tail(omitted_records: list[dict[str, Any]], with_related: bool, with_scope: bool) -> list[str]:
        lines: list[str] = []
        if with_related and related:
            lines += ["", "## 관련 기록", "", "선택한 기록과 한 단계로 연결된 기록이다. 필요하면 `buildlog read <id>`로 읽는다.", ""]
            lines += [f"- {r['title']} (`{r['id']}`) · {r['relation']} [{_status(r['status'])}] · {r['summary_md']}" for r in related]
        if with_scope and out_of_scope:
            lines += ["", "## 다른 범위의 후보", "", "검색어와 맞지만 지정한 범위 밖의 기록이다.", ""]
            lines += [f"- {i['page']['title']} (`{i['page']['id']}`) · {scope_labels[i['page']['id']]}" for i in out_of_scope]
        if omitted_records:
            lines += ["", "## 생략한 기록", "", "출력 예산 때문에 본문을 넣지 않았다. 규칙은 위 '반드시 지킬 규칙'에 포함돼 있다. `buildlog read <id>`로 읽는다.", ""]
            lines += [f"- {r['page']['title']} (`{r['page']['id']}` v{r['page']['version']})" for r in omitted_records]
        return lines

    def size(lines: list[str]) -> int:
        return len(("\n".join(lines) + "\n").encode("utf-8"))

    base = header + fixed
    first = base + [""] + rendered[0]
    if size(first + tail(records[1:], False, False)) > max_bytes:
        raise BudgetTooSmall(size(first + tail(records[1:], False, False)), max_bytes, [r["page"]["id"] for r in records])
    body = list(first)
    included = [records[0]]
    omitted_records: list[dict[str, Any]] = []
    for record, block in zip(records[1:], rendered[1:]):
        remaining = [r for r in records if r not in included and r is not record and r not in omitted_records]
        if size(body + [""] + block + tail(omitted_records + remaining, False, False)) <= max_bytes:
            body += [""] + block
            included.append(record)
        else:
            omitted_records.append(record)
    for with_related, with_scope in ((True, True), (False, True), (False, False)):
        candidate = body + tail(omitted_records, with_related, with_scope)
        if size(candidate) <= max_bytes:
            body = candidate
            break
    else:
        body = body + tail(omitted_records, False, False)
    omitted = [{"id": r["page"]["id"], "title": r["page"]["title"]} for r in omitted_records]
    return "\n".join(body) + "\n", omitted


# ---------------------------------------------------------------- neighbors


def neighbors(store: Path, page_id: str, rel_type: str | None = None, direction: str = "both") -> dict[str, Any]:
    if rel_type is not None and rel_type not in RELATION_TYPES:
        raise ValidationError(f"type must be one of {sorted(RELATION_TYPES)}")
    if direction not in {"out", "in", "both"}:
        raise ValidationError("direction must be out, in, or both")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        page = c.execute("SELECT id, title FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            raise NotFoundError(f"page not found: {page_id}")
        items = []
        for side, other, label in (("source_id", "target_id", "out"), ("target_id", "source_id", "in")):
            if direction not in {label, "both"}:
                continue
            sql = f"""SELECT r.*, p.title AS other_title, p.type AS other_type FROM relations r JOIN pages p ON p.id = r.{other}
                      WHERE r.{side} = ? AND r.status != 'rejected' AND r.removed_at IS NULL""" + (" AND r.type = ?" if rel_type else "") + " ORDER BY r.type, p.title"
            for r in c.execute(sql, (page_id, rel_type) if rel_type else (page_id,)):
                items.append({"direction": label, "relation_id": r["id"], "type": r["type"], "status": r["status"],
                              "other_id": r[other], "other_title": r["other_title"], "other_type": r["other_type"],
                              "explanation_md": r["explanation_md"], "version": r["version"]})
    return {"page_id": page_id, "title": page["title"], "neighbors": items}


def render_neighbors(result: dict[str, Any]) -> str:
    lines = [f"# {result['title']}의 관계", "", f"- 페이지: `{result['page_id']}`", ""]
    if not result["neighbors"]:
        lines.append("연결된 관계가 없다.")
    for n in result["neighbors"]:
        arrow = f"이 페이지 —{n['type']}→ {n['other_title']}" if n["direction"] == "out" else f"{n['other_title']} —{n['type']}→ 이 페이지"
        lines.append(f"- {arrow} (`{n['other_id']}`) [{_status(n['status'])}] · 관계 `{n['relation_id']}` v{n['version']}: {n['explanation_md']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- join paths


def _table_page(c: sqlite3.Connection, system_key: str, name: str) -> sqlite3.Row:
    parts = name.split(".")
    rows = [r for r in c.execute("SELECT * FROM schema_objects WHERE object_type = 'table' AND lower(system_key) = lower(?)", (system_key,))
            if json.loads(r["metadata_json"])["name"].casefold() == parts[-1].casefold()
            and (len(parts) == 1 or (r["schema_name"] or "").casefold() == parts[0].casefold())]
    if not rows:
        raise NotFoundError(f"table {name!r} is not known in system {system_key!r}")
    if len(rows) > 1:
        raise ValidationError(f"table {name!r} exists in several schemas of {system_key!r}; qualify it as schema.table")
    return rows[0]


def paths(store: Path, system_key: str, from_table: str, to_table: str, purpose: str | None = None,
          max_depth: int = MAX_PATH_DEPTH, max_candidates: int = MAX_PATH_CANDIDATES) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        start = _table_page(c, system_key, from_table)
        end = _table_page(c, system_key, to_table)
        if start["page_id"] == end["page_id"]:
            raise ValidationError("from and to are the same table")
        stored = [dict(r) for r in c.execute(
            """SELECT j.* FROM join_paths j WHERE j.status != 'rejected'
                 AND EXISTS (SELECT 1 FROM join_path_tables a WHERE a.join_path_id = j.id AND a.page_id = ?)
                 AND EXISTS (SELECT 1 FROM join_path_tables b WHERE b.join_path_id = j.id AND b.page_id = ?)
               ORDER BY j.title""", (start["page_id"], end["page_id"]))]
        for path in stored:
            path["steps"] = json.loads(path.pop("steps_json"))
            path["evidence"] = []
            from .read import _object_evidence
            path["evidence"] = _object_evidence(c, "join_path", path["id"])
        if purpose:
            words = {w.casefold() for w in query_terms(purpose)}
            stored.sort(key=lambda p: -sum(w in (p["purpose"] + " " + p["title"]).casefold() for w in words))

        candidates: list[list[dict[str, Any]]] = []
        if not stored:
            names = {r["page_id"]: json.loads(r["metadata_json"])["name"]
                     for r in c.execute("SELECT * FROM schema_objects WHERE object_type = 'table' AND lower(system_key) = lower(?)", (system_key,))}
            edges: dict[str, list[dict[str, Any]]] = {}
            for r in c.execute("SELECT * FROM relations WHERE type = 'JOINS_TO' AND status != 'rejected' AND removed_at IS NULL"):
                if r["source_id"] in names and r["target_id"] in names:
                    edge = {"relation_id": r["id"], "status": r["status"], "explanation_md": r["explanation_md"]}
                    edges.setdefault(r["source_id"], []).append(edge | {"to": r["target_id"], "direction": "forward"})
                    edges.setdefault(r["target_id"], []).append(edge | {"to": r["source_id"], "direction": "backward"})
            found: list[list[dict[str, Any]]] = []

            def walk(node: str, visited: list[str], trail: list[dict[str, Any]]) -> None:
                if node == end["page_id"]:
                    found.append(list(trail))
                    return
                if len(trail) >= max_depth:
                    return
                for edge in edges.get(node, []):
                    if edge["to"] not in visited:
                        walk(edge["to"], visited + [edge["to"]], trail + [edge | {"from_name": names[node], "to_name": names[edge["to"]]}])

            walk(start["page_id"], [start["page_id"]], [])
            found.sort(key=lambda t: (len(t), sum(e["status"] == "inferred" for e in t)))
            candidates = found[:max_candidates]
    return {"system_key": system_key, "from": from_table, "to": to_table, "purpose": purpose,
            "stored": stored, "candidates": candidates}


def render_paths(result: dict[str, Any]) -> str:
    lines = [f"# 조인 경로: {result['from']} → {result['to']} ({result['system_key']})", ""]
    if result["purpose"]:
        lines += [f"- 목적: {result['purpose']}", ""]
    if result["stored"]:
        lines += ["등록된 조인 경로다. 목적·조건·주의점을 확인하고 쓴다.", ""]
        lines += _render_join_paths(result["stored"])[2:]
        return "\n".join(lines) + "\n"
    if not result["candidates"]:
        lines.append("등록된 경로도, 관계로 이어지는 후보도 없다. 연결 방법을 지어내지 않는다.")
        return "\n".join(lines) + "\n"
    lines += ["등록된 조인 경로가 없어 테이블 관계로 찾은 **추정 후보**다. 업무 목적·필수 필터·관계 수를 검토하지 않았으므로 정답 경로로 쓰지 않는다. "
              "검토한 뒤에는 join_paths로 저장한다.", ""]
    for i, trail in enumerate(result["candidates"], 1):
        hops = " → ".join([trail[0]["from_name"]] + [e["to_name"] for e in trail])
        lines += [f"### 후보 {i}: {hops}", ""]
        for e in trail:
            lines.append(f"- `{e['from_name']}` — `{e['to_name']}` [{_status(e['status'])}] · 관계 `{e['relation_id']}`: {e['explanation_md']}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
