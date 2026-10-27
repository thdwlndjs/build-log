"""Versioned edits, unlinking, feedback and merges of stored knowledge.

Every change runs in one locked write transaction, checks `expected_version`, writes a revision,
and refreshes the search index of the pages it touches.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import db
from .db import FEEDBACK_STATUSES, KNOWLEDGE_STATUSES, PAGE_STATUSES, RELATION_TYPES, ConflictError, NotFoundError, ValidationError
from .index import reindex
from .plan import reject_secrets, require_text

EDITABLE: dict[str, tuple[str, set[str]]] = {
    "page": ("pages", {"title", "summary_md", "body_md", "status"}),
    "term": ("term_senses", {"definition_md", "scope", "status", "aliases"}),
    "relation": ("relations", {"explanation_md", "status", "type", "reverse"}),
    "join_path": ("join_paths", {"title", "purpose", "conditions_md", "caveats_md", "status"}),
    "rule": ("rules", {"condition_md", "instruction_md", "reason_md", "alternative_md", "status", "confirmation"}),
}
# Fields that may be set to an empty string.
OPTIONAL_TEXT = {"conditions_md", "caveats_md", "condition_md", "alternative_md", "reason_md"}
# Patch keys that are not columns of their table.
VIRTUAL = {"aliases", "reverse"}


def _new_id() -> str:
    return str(uuid.uuid4())


def _ready(store: Path, object_id: str) -> None:
    if not store.exists():
        raise NotFoundError(f"not found: {object_id} (store does not exist: {store})")
    db.initialize(store)


def _find(c: sqlite3.Connection, object_id: str) -> tuple[str, str, sqlite3.Row]:
    for object_type, (table, _) in EDITABLE.items():
        row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone()
        if row is not None:
            return object_type, table, row
    raise NotFoundError(f"not found: {object_id}")


def _affected_pages(c: sqlite3.Connection, object_type: str, values: dict[str, Any], object_id: str) -> list[str]:
    if object_type == "page":
        return [object_id]
    if object_type == "term":
        return [values["page_id"]]
    if object_type == "rule":
        return [values["scope_page_id"]]
    if object_type == "join_path":
        return [r["page_id"] for r in c.execute("SELECT page_id FROM join_path_tables WHERE join_path_id = ?", (object_id,))]
    return []


def _link_feedback(c: sqlite3.Connection, feedback_id: str | None, object_id: str, revision_id: str) -> None:
    if feedback_id is None:
        return
    row = c.execute("SELECT target_id, status FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"feedback not found: {feedback_id}")
    if row["target_id"] != object_id:
        raise ValidationError(f"feedback {feedback_id} is about {row['target_id']}, not {object_id}")
    c.execute("UPDATE feedback SET status = 'addressed', resulting_revision = ?, updated_at = ? WHERE id = ?",
              (revision_id, db.now(), feedback_id))


def update_object(store: Path, object_id: str, expected_version: int, patch: dict[str, Any], reason: str,
                  feedback_id: str | None = None) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValidationError("patch must be a non-empty JSON object")
    reject_secrets(patch, "patch")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise ValidationError("expected_version must be a positive integer")
    require_text(reason, "reason")
    _ready(store, object_id)
    # The version check runs inside the locked transaction so a concurrent
    # update cannot slip in between the check and the write.
    with db.write(store) as c:
        object_type, table, row = _find(c, object_id)
        allowed = EDITABLE[object_type][1]
        unknown = set(patch) - allowed
        if unknown:
            raise ValidationError(f"{object_type} patch may contain only {sorted(allowed)}; got {sorted(unknown)}")
        if row["version"] != expected_version:
            raise ConflictError(f"VERSION_CONFLICT: expected {expected_version}, current {row['version']}")
        values = dict(row)
        for key, value in patch.items():
            if key in VIRTUAL:
                continue
            if key in OPTIONAL_TEXT and value == "" and not (object_type == "rule" and key == "reason_md" and values["kind"] != "unsupported_operation"):
                values[key] = ""
            elif key == "confirmation":
                values[key] = value
            else:
                values[key] = require_text(value, f"patch.{key}")
        statuses = PAGE_STATUSES if object_type == "page" else KNOWLEDGE_STATUSES
        if values["status"] not in statuses:
            raise ValidationError(f"patch.status must be one of {sorted(statuses)}")
        extra_columns: set[str] = set()
        if object_type == "rule":
            _check_rule(c, values)
        if object_type == "page":
            if row["type"] in {"schema_table", "schema_column"} and "title" in patch:
                raise ValidationError("schema page titles come from the schema; re-ingest the schema to rename")
            if c.execute("SELECT 1 FROM page_redirects WHERE from_page_id = ?", (object_id,)).fetchone():
                raise ValidationError(f"page {object_id} was merged into another page; edit that page instead")
            # A new body without a new summary leaves the summary out of date until someone rewrites it.
            if "summary_md" in patch:
                values["summary_dirty"] = 0
                extra_columns.add("summary_dirty")
            elif "body_md" in patch and patch["body_md"] != row["body_md"]:
                values["summary_dirty"] = 1
                extra_columns.add("summary_dirty")
        if object_type == "term":
            if c.execute("SELECT 1 FROM term_senses WHERE normalized_expression = ? AND scope = ? AND id != ?",
                         (values["normalized_expression"], values["scope"], object_id)).fetchone():
                raise ConflictError("TERM_CONFLICT: another term already uses this expression in that scope")
            if "aliases" in patch:
                aliases = patch["aliases"]
                if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
                    raise ValidationError("patch.aliases must be a list of non-empty strings")
                c.execute("DELETE FROM aliases WHERE term_sense_id = ?", (object_id,))
                for alias in [values["expression"], *aliases]:
                    c.execute("INSERT OR IGNORE INTO aliases(expression, normalized_expression, term_sense_id) VALUES (?, ?, ?)",
                              (alias.strip(), db.normalize_text(alias), object_id))
                values["aliases"] = [a.strip() for a in aliases]
        if object_type == "relation":
            if row["removed_at"]:
                raise ValidationError(f"relation {object_id} was unlinked; it cannot be edited")
            if "type" in patch:
                if patch["type"] not in RELATION_TYPES:
                    raise ValidationError(f"patch.type must be one of {sorted(RELATION_TYPES)}")
                values["type"] = patch["type"]
            if patch.get("reverse") is True:
                values["source_id"], values["target_id"] = row["target_id"], row["source_id"]
                extra_columns |= {"source_id", "target_id"}
            elif "reverse" in patch and patch["reverse"] is not False:
                raise ValidationError("patch.reverse must be true or false")
            if c.execute("""SELECT 1 FROM relations WHERE source_id = ? AND target_id = ? AND type = ? AND id != ? AND removed_at IS NULL""",
                         (values["source_id"], values["target_id"], values["type"], object_id)).fetchone():
                raise ConflictError("RELATION_CONFLICT: the same relation already exists between these pages")
        if object_type == "join_path" and "title" in patch:
            values["normalized_title"] = db.normalize_text(values["title"])
            extra_columns.add("normalized_title")
            if c.execute("SELECT 1 FROM join_paths WHERE system_key = ? AND normalized_title = ? AND id != ?",
                         (values["system_key"], values["normalized_title"], object_id)).fetchone():
                raise ConflictError("JOIN_PATH_CONFLICT: another join path in this system already has that title")
        timestamp = db.now()
        version = expected_version + 1
        values["version"], values["updated_at"] = version, timestamp
        columns = sorted((set(patch) - VIRTUAL) | {"version", "updated_at"} | extra_columns)
        c.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in columns)} WHERE id = ?",
                  tuple(values[k] for k in columns) + (object_id,))
        snapshot = {k: v for k, v in values.items() if k not in {"created_at"}}
        if "steps_json" in snapshot:
            snapshot["steps"] = json.loads(snapshot.pop("steps_json"))
        revision_id = _new_id()
        db.add_revision(c, object_type, object_id, version, snapshot, reason, timestamp, revision_id)
        _link_feedback(c, feedback_id, object_id, revision_id)
        reindex(c, _affected_pages(c, object_type, values, object_id))
    result: dict[str, Any] = {"object_type": object_type, "id": object_id, "version": version, "store": str(store)}
    if object_type == "page":
        result["page_id"] = object_id
        result["summary_dirty"] = bool(values.get("summary_dirty"))
    return result


def unlink_relation(store: Path, relation_id: str, expected_version: int, reason: str, feedback_id: str | None = None) -> dict[str, Any]:
    """Hide a relation from reads and search. The row, its evidence and its history are kept."""
    require_text(reason, "reason")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise ValidationError("expected_version must be a positive integer")
    _ready(store, relation_id)
    with db.write(store) as c:
        row = c.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"relation not found: {relation_id}")
        if row["removed_at"]:
            raise ValidationError(f"relation {relation_id} is already unlinked")
        if row["version"] != expected_version:
            raise ConflictError(f"VERSION_CONFLICT: expected {expected_version}, current {row['version']}")
        timestamp, version = db.now(), expected_version + 1
        c.execute("UPDATE relations SET removed_at = ?, removed_reason = ?, version = ?, updated_at = ? WHERE id = ?",
                  (timestamp, reason, version, timestamp, relation_id))
        snapshot = dict(row) | {"removed_at": timestamp, "removed_reason": reason, "version": version, "updated_at": timestamp}
        revision_id = _new_id()
        db.add_revision(c, "relation", relation_id, version, snapshot, f"unlinked: {reason}", timestamp, revision_id)
        _link_feedback(c, feedback_id, relation_id, revision_id)
    return {"object_type": "relation", "id": relation_id, "version": version, "removed_at": timestamp, "store": str(store)}


def add_feedback(store: Path, target_id: str, target_version: int, text: str) -> dict[str, Any]:
    """Record what is wrong with a stored item, at the version the reviewer saw."""
    require_text(text, "feedback")
    reject_secrets({"feedback": text}, "feedback")
    if not isinstance(target_version, int) or target_version < 1:
        raise ValidationError("target_version must be a positive integer")
    _ready(store, target_id)
    with db.write(store) as c:
        object_type, _, row = _find(c, target_id)
        if row["version"] != target_version:
            raise ConflictError(f"VERSION_CONFLICT: feedback is about version {target_version}, current {row['version']}")
        feedback_id, timestamp = _new_id(), db.now()
        c.execute("""INSERT INTO feedback(id, target_type, target_id, target_version, feedback_text, status, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                  (feedback_id, object_type, target_id, target_version, text.strip(), timestamp, timestamp))
    return {"id": feedback_id, "target_type": object_type, "target_id": target_id, "target_version": target_version,
            "status": "open", "store": str(store)}


def set_feedback_status(store: Path, feedback_id: str, status: str) -> dict[str, Any]:
    if status not in FEEDBACK_STATUSES:
        raise ValidationError(f"status must be one of {sorted(FEEDBACK_STATUSES)}")
    _ready(store, feedback_id)
    with db.write(store) as c:
        if c.execute("SELECT 1 FROM feedback WHERE id = ?", (feedback_id,)).fetchone() is None:
            raise NotFoundError(f"feedback not found: {feedback_id}")
        c.execute("UPDATE feedback SET status = ?, updated_at = ? WHERE id = ?", (status, db.now(), feedback_id))
    return {"id": feedback_id, "status": status}


def list_feedback(store: Path, target_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    _ready(store, target_id or "feedback")
    from contextlib import closing

    with closing(db.connect(store)) as c:
        sql, args = "SELECT * FROM feedback", []
        clauses = []
        if target_id:
            clauses.append("target_id = ?")
            args.append(target_id)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = [dict(r) for r in c.execute(sql + " ORDER BY created_at DESC, rowid DESC", args)]
    return {"feedback": rows}


# ---------------------------------------------------------------- merge


def _bump(c: sqlite3.Connection, table: str, object_type: str, object_id: str, changes: dict[str, Any], reason: str, ts: str) -> None:
    row = dict(c.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone())
    version = row["version"] + 1
    sets = {**changes, "version": version, "updated_at": ts}
    c.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), object_id))
    snapshot = {k: v for k, v in (row | sets).items() if k != "created_at"}
    if "steps_json" in snapshot:
        snapshot["steps"] = json.loads(snapshot.pop("steps_json"))
    db.add_revision(c, object_type, object_id, version, snapshot, reason, ts, _new_id())


def merge_pages(store: Path, source_ids: list[str], target_id: str, expected_versions: dict[str, int], reason: str) -> dict[str, Any]:
    """Merge duplicate pages into one representative page.

    The sources stay as `superseded` pages with a redirect to the target. Everything that pointed at a
    source now points at the target. Duplicate relations are folded into one with combined evidence;
    relations with a different meaning (type or direction) are kept. The target's text is not
    rewritten: whoever merges edits it afterwards.
    """
    require_text(reason, "reason")
    if not isinstance(source_ids, list) or not source_ids or not all(isinstance(s, str) for s in source_ids):
        raise ValidationError("source_ids must be a non-empty list of page ids")
    if target_id in source_ids or len(set(source_ids)) != len(source_ids):
        raise ValidationError("source_ids must be distinct and must not include the target")
    _ready(store, target_id)
    report: dict[str, Any] = {"target_id": target_id, "merged": [], "relations_moved": 0, "relations_folded": 0,
                              "relations_dropped_as_self_loop": 0, "references_moved": {}}
    with db.write(store) as c:
        pages = {}
        for page_id in [target_id, *source_ids]:
            row = c.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"page not found: {page_id}")
            if c.execute("SELECT 1 FROM page_redirects WHERE from_page_id = ?", (page_id,)).fetchone():
                raise ValidationError(f"page {page_id} was already merged into another page")
            if row["type"] in {"schema_table", "schema_column"}:
                raise ValidationError("schema pages follow their schema; re-ingest the schema instead of merging")
            expected = expected_versions.get(page_id)
            if expected != row["version"]:
                raise ConflictError(f"VERSION_CONFLICT: page {page_id} expected {expected}, current {row['version']}")
            pages[page_id] = row
        ts = db.now()
        moved: dict[str, int] = {}
        for source_id in source_ids:
            note = f"merged {source_id} into {target_id}: {reason}"
            for rel in c.execute("SELECT * FROM relations WHERE (source_id = ? OR target_id = ?) AND removed_at IS NULL",
                                 (source_id, source_id)).fetchall():
                new_source = target_id if rel["source_id"] == source_id else rel["source_id"]
                new_target = target_id if rel["target_id"] == source_id else rel["target_id"]
                if new_source == new_target:
                    _bump(c, "relations", "relation", rel["id"], {"removed_at": ts, "removed_reason": f"self-loop after merge into {target_id}"}, note, ts)
                    report["relations_dropped_as_self_loop"] += 1
                    continue
                twin = c.execute("""SELECT id FROM relations WHERE source_id = ? AND target_id = ? AND type = ? AND id != ?
                                    AND removed_at IS NULL""", (new_source, new_target, rel["type"], rel["id"])).fetchone()
                if twin is not None:
                    # Same meaning after the merge: keep one relation with the evidence of both.
                    c.execute("""INSERT OR IGNORE INTO object_evidence(object_type, object_id, evidence_id)
                                 SELECT 'relation', ?, evidence_id FROM object_evidence WHERE object_type = 'relation' AND object_id = ?""",
                              (twin["id"], rel["id"]))
                    _bump(c, "relations", "relation", rel["id"], {"removed_at": ts, "removed_reason": f"duplicate of {twin['id']} after merge"}, note, ts)
                    report["relations_folded"] += 1
                    continue
                _bump(c, "relations", "relation", rel["id"], {"source_id": new_source, "target_id": new_target}, note, ts)
                report["relations_moved"] += 1
            # Evidence: the target gains the source's evidence; the source keeps its own as history.
            n = c.execute("""INSERT OR IGNORE INTO page_sources(page_id, evidence_id)
                             SELECT ?, evidence_id FROM page_sources WHERE page_id = ?""", (target_id, source_id)).rowcount
            moved["evidence"] = moved.get("evidence", 0) + max(n, 0)
            for term in c.execute("SELECT id FROM term_senses WHERE page_id = ?", (source_id,)).fetchall():
                _bump(c, "term_senses", "term", term["id"], {"page_id": target_id}, note, ts)
                moved["terms"] = moved.get("terms", 0) + 1
            for rule in c.execute("SELECT id FROM rules WHERE scope_page_id = ?", (source_id,)).fetchall():
                _bump(c, "rules", "rule", rule["id"], {"scope_page_id": target_id}, note, ts)
                moved["rules"] = moved.get("rules", 0) + 1
            for member in c.execute("SELECT * FROM index_members WHERE child_page_id = ?", (source_id,)).fetchall():
                if c.execute("SELECT 1 FROM index_members WHERE index_page_id = ? AND child_page_id = ?",
                             (member["index_page_id"], target_id)).fetchone() or member["index_page_id"] == target_id:
                    c.execute("DELETE FROM index_members WHERE index_page_id = ? AND child_page_id = ?", (member["index_page_id"], source_id))
                else:
                    c.execute("UPDATE index_members SET child_page_id = ? WHERE index_page_id = ? AND child_page_id = ?",
                              (target_id, member["index_page_id"], source_id))
                moved["index_entries"] = moved.get("index_entries", 0) + 1
            for member in c.execute("SELECT * FROM index_members WHERE index_page_id = ?", (source_id,)).fetchall():
                if member["child_page_id"] == target_id or c.execute(
                        "SELECT 1 FROM index_members WHERE index_page_id = ? AND child_page_id = ?", (target_id, member["child_page_id"])).fetchone():
                    c.execute("DELETE FROM index_members WHERE index_page_id = ? AND child_page_id = ?", (source_id, member["child_page_id"]))
                else:
                    c.execute("UPDATE index_members SET index_page_id = ? WHERE index_page_id = ? AND child_page_id = ?",
                              (target_id, source_id, member["child_page_id"]))
                moved["index_entries"] = moved.get("index_entries", 0) + 1
            n = c.execute("UPDATE research_requests SET page_id = ?, updated_at = ? WHERE page_id = ?", (target_id, ts, source_id)).rowcount
            moved["research_requests"] = moved.get("research_requests", 0) + max(n, 0)
            # Earlier redirects to this source now lead to the target.
            c.execute("UPDATE page_redirects SET to_page_id = ? WHERE to_page_id = ?", (target_id, source_id))
            c.execute("INSERT INTO page_redirects(from_page_id, to_page_id, reason, merged_at) VALUES (?, ?, ?, ?)",
                      (source_id, target_id, reason, ts))
            _bump(c, "pages", "page", source_id, {"status": "superseded"}, note, ts)
            report["merged"].append({"id": source_id, "title": pages[source_id]["title"]})
        _bump(c, "pages", "page", target_id, {}, f"merged {', '.join(source_ids)}: {reason}", ts)
        report["references_moved"] = moved
        reindex(c, [target_id, *source_ids])
        report["target_version"] = c.execute("SELECT version FROM pages WHERE id = ?", (target_id,)).fetchone()["version"]
    report["store"] = str(store)
    return report


def _check_rule(c: Any, values: dict[str, Any]) -> None:
    if values["kind"] != "unsupported_operation":
        if values.get("confirmation") is not None:
            raise ValidationError("confirmation applies only to unsupported_operation rules")
        require_text(values["reason_md"], "patch.reason_md")
        return
    if values["confirmation"] not in {"confirmed", "unconfirmed"}:
        raise ValidationError("confirmation must be confirmed or unconfirmed")
    if values["confirmation"] == "confirmed":
        direct = c.execute(
            """SELECT 1 FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
               WHERE oe.object_type = 'rule' AND oe.object_id = ? AND e.evidence_kind IN ('observed', 'reported')""",
            (values["id"],),
        ).fetchone()
        if direct is None:
            raise ValidationError("a confirmed absence needs observed or reported evidence on the rule")
