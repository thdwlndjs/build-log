"""Validate a save plan and turn it into one normalized structure (no database access).

Two input shapes are accepted:
- v1 (Phase 1): {idempotency_key, page, source, evidence}
- v2: {idempotency_key, sources, evidence, pages, terms, relations, join_paths, rules, index_members}
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db import (
    HOST_PAGE_TYPES, KNOWLEDGE_STATUSES, PAGE_STATUSES, PAGE_TYPES, RELATION_TYPES, RULE_KINDS,
    ValidationError, sha256,
)
from .ddl import SUPPORTED_DIALECTS, parse_ddl
from .schema import check_system_key, normalize_schema_json, parse_sample

SOURCE_KINDS = {"conversation", "markdown", "text", "sql_ddl", "schema_json", "sample_json", "sample_csv", "web", "document"}
CONTENT_KINDS = {"markdown", "text", "sql_ddl", "schema_json", "sample_json", "sample_csv"}
EVIDENCE_KINDS = {"observed", "reported", "inferred"}
JOIN_KINDS = {"inner", "left", "right", "full", "unknown"}
CARDINALITIES = {"1:1", "1:N", "N:1", "N:M", "unknown"}
MAX_CONTENT_BYTES = 5 * 1024 * 1024
V2_KEYS = {"idempotency_key", "sources", "evidence", "pages", "terms", "relations", "join_paths", "rules", "index_members"}

_TOKEN_VALUE_RE = re.compile(r"\b(?:ghp|gho|github_pat|sk|xox[bp])[-_][A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b")
_ASSIGNED_SECRET_RE = re.compile(r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*)(['\"]?)[^\s'\";,]+\2")


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    return value


def reject_secrets(value: Any, path: str = "input", skip: frozenset[str] = frozenset()) -> None:
    if path in skip:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if re.search(r"(?:password|secret|token|api[_-]?key|credential)", str(key), re.I):
                raise ValidationError(f"{path}.{key} may not contain credentials")
            reject_secrets(nested, f"{path}.{key}", skip)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            reject_secrets(nested, f"{path}[{index}]", skip)
    elif isinstance(value, str) and _TOKEN_VALUE_RE.search(value):
        raise ValidationError(f"{path} appears to contain a credential")


def redact_content(text: str) -> tuple[str, bool]:
    redacted = _TOKEN_VALUE_RE.sub("[REDACTED]", text)
    redacted = _ASSIGNED_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", redacted)
    return redacted, redacted != text


def _refs(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValidationError(f"{field} must be a list of reference strings")
    return list(value)


def _choice(value: Any, allowed: set[str], field: str, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if value not in allowed:
        raise ValidationError(f"{field} must be one of {sorted(allowed)}")
    return value


# ---------------------------------------------------------------- v1


def _legacy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Phase-1 validation, kept byte-for-byte so stored input hashes stay comparable."""
    idempotency_key = require_text(plan.get("idempotency_key"), "idempotency_key")
    page = plan.get("page")
    source = plan.get("source")
    if not isinstance(page, dict) or not isinstance(source, dict):
        raise ValidationError("page and source must be objects")
    page_type = page.get("type", "experience")
    if page_type not in {"experience", "concept", "method", "source", "index"}:
        raise ValidationError("page.type is not supported for a single-page plan")
    status = page.get("status", "active")
    if status not in PAGE_STATUSES:
        raise ValidationError("page.status is invalid")
    evidence = plan.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValidationError("evidence must be a list")
    normalized_evidence: list[dict[str, str]] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValidationError(f"evidence[{index}] must be an object")
        normalized_evidence.append({
            "section_locator": require_text(item.get("section_locator", "unspecified"), f"evidence[{index}].section_locator"),
            "excerpt": require_text(item.get("excerpt"), f"evidence[{index}].excerpt"),
            "evidence_kind": require_text(item.get("evidence_kind", "observed"), f"evidence[{index}].evidence_kind"),
        })
    captured_at = source.get("captured_at")
    if captured_at is not None:
        captured_at = require_text(captured_at, "source.captured_at")
    return {
        "idempotency_key": idempotency_key,
        "page": {
            "type": page_type,
            "title": require_text(page.get("title"), "page.title"),
            "summary_md": require_text(page.get("summary_md"), "page.summary_md"),
            "body_md": require_text(page.get("body_md"), "page.body_md"),
            "status": status,
        },
        "source": {
            "kind": require_text(source.get("kind"), "source.kind"),
            "locator": require_text(source.get("locator"), "source.locator"),
            "captured_at": captured_at,
            "accessible_excerpt": require_text(source.get("accessible_excerpt"), "source.accessible_excerpt"),
        },
        "evidence": normalized_evidence,
    }


def _from_legacy(legacy: dict[str, Any]) -> dict[str, Any]:
    source = {**legacy["source"], "ref": "source", "content": None, "content_hash": None, "metadata": {},
              "parse_status": None, "diagnostics": [], "schema": None, "rows": None, "replace_existing_schema": False}
    evidence = [
        {"ref": f"e{i}", "source": "source", **item, "start_line": None, "end_line": None}
        for i, item in enumerate(legacy["evidence"])
    ]
    page = {"ref": "page", "id": None, "expected_version": None, **legacy["page"], "evidence": [e["ref"] for e in evidence]}
    return _empty_plan(legacy["idempotency_key"]) | {"sources": [source], "evidence": evidence, "pages": [page]}


def _empty_plan(key: str) -> dict[str, Any]:
    return {"idempotency_key": key, "sources": [], "evidence": [], "pages": [], "terms": [], "relations": [],
            "join_paths": [], "rules": [], "index_members": [], "warnings": []}


# ---------------------------------------------------------------- v2 sources and evidence


def _read_content(raw: dict[str, Any], where: str) -> tuple[str, str | None]:
    content, path = raw.get("content"), raw.get("path")
    if (content is None) == (path is None):
        raise ValidationError(f"{where} needs exactly one of content or path")
    if path is not None:
        file = Path(require_text(path, f"{where}.path")).expanduser()
        try:
            data = file.read_bytes()
        except OSError as error:
            raise ValidationError(f"{where}.path cannot be read: {error}") from error
        if len(data) > MAX_CONTENT_BYTES:
            raise ValidationError(f"{where}.path is larger than {MAX_CONTENT_BYTES} bytes")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValidationError(f"{where}.path is not UTF-8 text") from error
        path = str(file.resolve())
    content = require_text(content, f"{where}.content").replace("\r\n", "\n").replace("\r", "\n")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValidationError(f"{where}.content is larger than {MAX_CONTENT_BYTES} bytes")
    return content, path


def _source(raw: Any, index: int, warnings: list[str]) -> dict[str, Any]:
    where = f"sources[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    kind = _choice(raw.get("kind"), SOURCE_KINDS, f"{where}.kind")
    source: dict[str, Any] = {
        "ref": require_text(raw.get("ref"), f"{where}.ref"),
        "kind": kind,
        "captured_at": raw.get("captured_at"),
        "content": None, "content_hash": None, "metadata": {}, "parse_status": None, "diagnostics": [],
        "schema": None, "rows": None,
        "replace_existing_schema": bool(raw.get("replace_existing_schema", False)),
    }
    if source["captured_at"] is not None:
        require_text(source["captured_at"], f"{where}.captured_at")
    if kind in CONTENT_KINDS:
        content, path = _read_content(raw, where)
        content, redacted = redact_content(content)
        if redacted:
            warnings.append(f"{where}: credential-like values in the content were replaced with [REDACTED]")
        if path:
            source["metadata"]["path"] = path
        source["locator"] = _optional_text(raw.get("locator"), f"{where}.locator") or path or f"inline {kind}"
    else:
        if raw.get("content") is not None or raw.get("path") is not None:
            raise ValidationError(f"{where}: kind {kind} does not take content or path")
        content = None
        source["locator"] = require_text(raw.get("locator"), f"{where}.locator")

    if kind == "sql_ddl":
        dialect = raw.get("dialect")
        if dialect not in SUPPORTED_DIALECTS:
            raise ValidationError(f"{where}.dialect must be one of {sorted(SUPPORTED_DIALECTS)} (stated explicitly)")
        system_key = check_system_key(raw.get("system_key"), f"{where}.system_key")
        parsed = parse_ddl(content, dialect)
        source["metadata"].update({"dialect": dialect, "system_key": system_key})
        source["parse_status"] = parsed["status"]
        source["diagnostics"] = parsed["diagnostics"]
        if parsed["status"] == "failed":
            raise ValidationError(f"{where}: no CREATE TABLE could be read: {json.dumps(parsed['diagnostics'], ensure_ascii=False)[:500]}")
        if parsed["status"] == "partial":
            warnings.append(f"{where}: DDL read partially; see diagnostics ({sum(d['severity'] != 'info' for d in parsed['diagnostics'])} not interpreted)")
        source["schema"] = {"system_key": system_key, "tables": parsed["tables"]}
    elif kind == "schema_json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{where}: invalid schema_json: {error}") from error
        source["schema"] = normalize_schema_json(data, where)
        source["metadata"]["system_key"] = source["schema"]["system_key"]
        source["parse_status"] = "complete"
    elif kind in {"sample_json", "sample_csv"}:
        system_key = check_system_key(raw.get("system_key"), f"{where}.system_key")
        table = require_text(raw.get("table"), f"{where}.table")
        rows, sample_warnings, content = parse_sample(kind, content, where)
        warnings.extend(sample_warnings)
        source["rows"] = rows
        source["metadata"].update({"system_key": system_key, "table": table, "row_count": len(rows)})
        source["parse_status"] = "complete"
    elif kind in {"markdown", "text"}:
        source["parse_status"] = "complete"

    if content is not None:
        source["content"] = content
        source["content_hash"] = sha256(content)
        default_excerpt = " ".join(content.split())[:200]
    else:
        default_excerpt = None
    excerpt = raw.get("accessible_excerpt", default_excerpt)
    source["accessible_excerpt"] = require_text(excerpt, f"{where}.accessible_excerpt")
    return source


def _locate(content: str, excerpt: str) -> list[tuple[int, int]]:
    """All (start_line, end_line) where the excerpt occurs, comparing with collapsed whitespace."""
    norm_chars: list[str] = []
    lines: list[int] = []
    line = 1
    previous_space = True
    for ch in content:
        if ch.isspace():
            if not previous_space:
                norm_chars.append(" ")
                lines.append(line)
            previous_space = True
        else:
            norm_chars.append(ch)
            lines.append(line)
            previous_space = False
        if ch == "\n":
            line += 1
    haystack = "".join(norm_chars)
    needle = " ".join(excerpt.split())
    spans: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start != -1 and needle:
        spans.append((lines[start], lines[start + len(needle) - 1]))
        start = haystack.find(needle, start + 1)
    return spans


def _evidence(raw: Any, index: int, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    where = f"evidence[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    ref = require_text(raw.get("ref"), f"{where}.ref")
    source_ref = require_text(raw.get("source"), f"{where}.source")
    if source_ref not in sources:
        raise ValidationError(f"{where}.source refers to unknown source ref {source_ref!r}")
    kind = _choice(raw.get("evidence_kind"), EVIDENCE_KINDS, f"{where}.evidence_kind")
    excerpt = require_text(raw.get("excerpt"), f"{where}.excerpt")
    item = {"ref": ref, "source": source_ref, "excerpt": excerpt, "evidence_kind": kind,
            "section_locator": raw.get("section_locator"), "start_line": None, "end_line": None}
    content = sources[source_ref]["content"]
    span = raw.get("span")
    if content is not None and kind != "inferred":
        found = _locate(content, excerpt)
        if span is not None:
            if not isinstance(span, dict) or not all(isinstance(span.get(k), int) for k in ("start_line", "end_line")):
                raise ValidationError(f"{where}.span needs integer start_line and end_line")
            found = [s for s in found if s[0] >= span["start_line"] and s[1] <= span["end_line"]]
        if not found:
            raise ValidationError(
                f"{where}: excerpt was not found in source {source_ref!r}"
                + (" within the given span" if span is not None else "")
                + "; quote the source text exactly or mark the evidence as inferred"
            )
        item["start_line"], item["end_line"] = found[0]
        if item["section_locator"] is None:
            item["section_locator"] = f"lines {found[0][0]}-{found[0][1]}"
    if item["section_locator"] is None:
        item["section_locator"] = "unspecified"
    require_text(item["section_locator"], f"{where}.section_locator")
    return item


# ---------------------------------------------------------------- v2 knowledge objects


def _page(raw: Any, index: int) -> dict[str, Any]:
    where = f"pages[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    page_id = raw.get("id")
    page = {"ref": raw.get("ref"), "id": page_id, "expected_version": raw.get("expected_version"),
            "evidence": _refs(raw.get("evidence"), f"{where}.evidence")}
    if page_id is not None:
        require_text(page_id, f"{where}.id")
        if not isinstance(page["expected_version"], int) or page["expected_version"] < 1:
            raise ValidationError(f"{where}: updating page {page_id} needs expected_version")
        for field in ("title", "summary_md", "body_md"):
            if field in raw:
                page[field] = require_text(raw[field], f"{where}.{field}")
        if "status" in raw:
            page["status"] = _choice(raw["status"], PAGE_STATUSES, f"{where}.status")
        if "type" in raw:
            raise ValidationError(f"{where}: page type cannot be changed")
        if not any(f in page for f in ("title", "summary_md", "body_md", "status")) and not page["evidence"]:
            raise ValidationError(f"{where}: update has nothing to change")
        page["reason"] = require_text(raw.get("reason", "updated by ingest"), f"{where}.reason")
    else:
        page["ref"] = require_text(raw.get("ref"), f"{where}.ref")
        page_type = raw.get("type", "experience")
        if page_type not in HOST_PAGE_TYPES:
            hint = " (schema pages come from sql_ddl or schema_json sources)" if page_type in PAGE_TYPES else ""
            raise ValidationError(f"{where}.type must be one of {sorted(HOST_PAGE_TYPES)}{hint}")
        page.update({
            "type": page_type,
            "title": require_text(raw.get("title"), f"{where}.title"),
            "summary_md": require_text(raw.get("summary_md"), f"{where}.summary_md"),
            "body_md": require_text(raw.get("body_md"), f"{where}.body_md"),
            "status": _choice(raw.get("status"), PAGE_STATUSES, f"{where}.status", "active"),
        })
    return page


def _require_evidence(refs: list[str], where: str, minimum: int = 1) -> None:
    if len(refs) < minimum:
        raise ValidationError(f"{where} needs at least {minimum} evidence reference(s)")


def _term(raw: Any, index: int) -> dict[str, Any]:
    where = f"terms[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    aliases = raw.get("aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
        raise ValidationError(f"{where}.aliases must be a list of strings")
    term = {
        "ref": raw.get("ref"),
        "id": raw.get("id"),
        "expected_version": raw.get("expected_version"),
        "expression": require_text(raw.get("expression"), f"{where}.expression"),
        "scope": require_text(raw.get("scope"), f"{where}.scope"),
        "definition_md": require_text(raw.get("definition_md"), f"{where}.definition_md"),
        "page": raw.get("page"),
        "aliases": aliases,
        "evidence": _refs(raw.get("evidence"), f"{where}.evidence"),
        "status": _choice(raw.get("status"), KNOWLEDGE_STATUSES, f"{where}.status"),
    }
    _require_evidence(term["evidence"], where)
    return term


def _relation(raw: Any, index: int) -> dict[str, Any]:
    where = f"relations[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    relation = {
        "source": require_text(raw.get("source"), f"{where}.source"),
        "target": require_text(raw.get("target"), f"{where}.target"),
        "type": _choice(raw.get("type"), RELATION_TYPES, f"{where}.type"),
        "explanation_md": require_text(raw.get("explanation_md"), f"{where}.explanation_md"),
        "evidence": _refs(raw.get("evidence"), f"{where}.evidence"),
        "status": _choice(raw.get("status"), KNOWLEDGE_STATUSES, f"{where}.status"),
    }
    _require_evidence(relation["evidence"], where)
    return relation


def _join_path(raw: Any, index: int) -> dict[str, Any]:
    where = f"join_paths[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    steps_in = raw.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        raise ValidationError(f"{where}.steps must be a non-empty list")
    steps = []
    for s_index, step in enumerate(steps_in):
        sat = f"{where}.steps[{s_index}]"
        if not isinstance(step, dict):
            raise ValidationError(f"{sat} must be an object")
        from_columns = step.get("from_columns")
        to_columns = step.get("to_columns")
        for name, cols in (("from_columns", from_columns), ("to_columns", to_columns)):
            if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c.strip() for c in cols):
                raise ValidationError(f"{sat}.{name} must be a non-empty list of column names")
        if len(from_columns) != len(to_columns):
            raise ValidationError(f"{sat}: from_columns and to_columns must pair up (composite keys keep their order)")
        nullable = step.get("nullable", "unknown")
        if nullable not in (True, False, "unknown"):
            raise ValidationError(f"{sat}.nullable must be true, false, or \"unknown\"")
        steps.append({
            "from_table": require_text(step.get("from_table"), f"{sat}.from_table"),
            "from_columns": from_columns,
            "to_table": require_text(step.get("to_table"), f"{sat}.to_table"),
            "to_columns": to_columns,
            "join_kind": _choice(step.get("join_kind"), JOIN_KINDS, f"{sat}.join_kind", "unknown"),
            "cardinality": _choice(step.get("cardinality"), CARDINALITIES, f"{sat}.cardinality", "unknown"),
            "nullable": nullable,
            "evidence": _refs(step.get("evidence"), f"{sat}.evidence"),
        })
    path = {
        "ref": raw.get("ref"),
        "system_key": check_system_key(raw.get("system_key"), f"{where}.system_key"),
        "title": require_text(raw.get("title"), f"{where}.title"),
        "purpose": require_text(raw.get("purpose"), f"{where}.purpose"),
        "steps": steps,
        "conditions_md": _optional_text(raw.get("conditions_md"), f"{where}.conditions_md"),
        "caveats_md": _optional_text(raw.get("caveats_md"), f"{where}.caveats_md"),
        "evidence": _refs(raw.get("evidence"), f"{where}.evidence"),
        "status": _choice(raw.get("status"), KNOWLEDGE_STATUSES, f"{where}.status"),
    }
    _require_evidence(path["evidence"], where)
    return path


def _rule(raw: Any, index: int, evidence_kinds: dict[str, str]) -> dict[str, Any]:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    kind = _choice(raw.get("kind"), RULE_KINDS, f"{where}.kind")
    rule = {
        "ref": raw.get("ref"),
        "scope": require_text(raw.get("scope"), f"{where}.scope"),
        "kind": kind,
        "condition_md": _optional_text(raw.get("condition_md"), f"{where}.condition_md"),
        "instruction_md": require_text(raw.get("instruction_md"), f"{where}.instruction_md"),
        "reason_md": _optional_text(raw.get("reason_md"), f"{where}.reason_md"),
        "alternative_md": _optional_text(raw.get("alternative_md"), f"{where}.alternative_md"),
        "confirmation": None,
        "evidence": _refs(raw.get("evidence"), f"{where}.evidence"),
        "status": _choice(raw.get("status"), KNOWLEDGE_STATUSES, f"{where}.status"),
    }
    if kind == "unsupported_operation":
        rule["confirmation"] = _choice(raw.get("confirmation"), {"confirmed", "unconfirmed"}, f"{where}.confirmation")
        if rule["confirmation"] == "confirmed":
            direct = [r for r in rule["evidence"] if evidence_kinds.get(r) in {"observed", "reported"}]
            if not direct:
                raise ValidationError(
                    f"{where}: a confirmed absence needs observed or reported evidence; "
                    "not finding a mention is not proof — use confirmation \"unconfirmed\""
                )
    else:
        if raw.get("confirmation") is not None:
            raise ValidationError(f"{where}.confirmation applies only to unsupported_operation")
        rule["reason_md"] = require_text(raw.get("reason_md"), f"{where}.reason_md")
        _require_evidence(rule["evidence"], where)
    return rule


def _index_member(raw: Any, index: int) -> dict[str, Any]:
    where = f"index_members[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    position = raw.get("position")
    if not isinstance(position, int):
        raise ValidationError(f"{where}.position must be an integer")
    return {"index": require_text(raw.get("index"), f"{where}.index"),
            "child": require_text(raw.get("child"), f"{where}.child"), "position": position}


def _list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValidationError(f"{key} must be a list")
    return value


def _unique_refs(items: list[dict[str, Any]], kind: str) -> None:
    seen: set[str] = set()
    for item in items:
        ref = item.get("ref")
        if ref is None:
            continue
        if ref in seen:
            raise ValidationError(f"duplicate {kind} ref {ref!r}")
        seen.add(ref)


def normalize_plan(raw: Any) -> tuple[dict[str, Any], str]:
    """Return (normalized plan, input hash)."""
    if not isinstance(raw, dict):
        raise ValidationError("input must be a JSON object")
    if "page" in raw and "pages" not in raw:
        reject_secrets(raw)
        legacy = _legacy_plan(raw)
        return _from_legacy(legacy), sha256(legacy)

    unknown = set(raw) - V2_KEYS
    if unknown:
        raise ValidationError(f"unknown plan fields: {sorted(unknown)}")
    content_paths = frozenset(f"input.sources[{i}].content" for i in range(len(_list(raw, "sources"))))
    reject_secrets(raw, skip=content_paths)
    plan = _empty_plan(require_text(raw.get("idempotency_key"), "idempotency_key"))
    warnings: list[str] = plan["warnings"]

    plan["sources"] = [_source(item, i, warnings) for i, item in enumerate(_list(raw, "sources"))]
    _unique_refs(plan["sources"], "source")
    sources = {s["ref"]: s for s in plan["sources"]}
    plan["evidence"] = [_evidence(item, i, sources) for i, item in enumerate(_list(raw, "evidence"))]
    _unique_refs(plan["evidence"], "evidence")
    evidence_kinds = {e["ref"]: e["evidence_kind"] for e in plan["evidence"]}

    plan["pages"] = [_page(item, i) for i, item in enumerate(_list(raw, "pages"))]
    plan["terms"] = [_term(item, i) for i, item in enumerate(_list(raw, "terms"))]
    plan["relations"] = [_relation(item, i) for i, item in enumerate(_list(raw, "relations"))]
    plan["join_paths"] = [_join_path(item, i) for i, item in enumerate(_list(raw, "join_paths"))]
    plan["rules"] = [_rule(item, i, evidence_kinds) for i, item in enumerate(_list(raw, "rules"))]
    plan["index_members"] = [_index_member(item, i) for i, item in enumerate(_list(raw, "index_members"))]
    for kind in ("pages", "terms", "join_paths", "rules"):
        _unique_refs(plan[kind], kind)

    for kind in ("pages", "terms", "relations", "join_paths", "rules"):
        for i, item in enumerate(plan[kind]):
            refs = item["evidence"] + [r for step in item.get("steps", []) for r in step["evidence"]]
            for ref in refs:
                if ref not in evidence_kinds:
                    raise ValidationError(f"{kind}[{i}] refers to unknown evidence ref {ref!r}")
    if not (plan["pages"] or plan["terms"] or plan["relations"] or plan["join_paths"] or plan["rules"]
            or any(s["schema"] for s in plan["sources"]) or any(s["rows"] is not None for s in plan["sources"])):
        raise ValidationError("plan saves nothing: add pages, terms, relations, join_paths, rules, or a schema source")

    hash_basis = {k: v for k, v in plan.items() if k != "warnings"}
    return plan, sha256(hash_basis)
