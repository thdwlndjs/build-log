"""Versioned edits of stored pages, terms, relations, join paths and rules."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from . import db
from .db import KNOWLEDGE_STATUSES, PAGE_STATUSES, ConflictError, NotFoundError, ValidationError
from .index import reindex
from .plan import reject_secrets, require_text

EDITABLE: dict[str, tuple[str, set[str]]] = {
    "page": ("pages", {"title", "summary_md", "body_md", "status"}),
    "term": ("term_senses", {"definition_md", "scope", "status"}),
    "relation": ("relations", {"explanation_md", "status"}),
    "join_path": ("join_paths", {"title", "purpose", "conditions_md", "caveats_md", "status"}),
    "rule": ("rules", {"condition_md", "instruction_md", "reason_md", "alternative_md", "status", "confirmation"}),
}
# Fields that may be set to an empty string.
OPTIONAL_TEXT = {"conditions_md", "caveats_md", "condition_md", "alternative_md", "reason_md"}


def update_object(store: Path, object_id: str, expected_version: int, patch: dict[str, Any], reason: str) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValidationError("patch must be a non-empty JSON object")
    reject_secrets(patch, "patch")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise ValidationError("expected_version must be a positive integer")
    require_text(reason, "reason")
    if not store.exists():
        raise NotFoundError(f"not found: {object_id} (store does not exist: {store})")
    db.initialize(store)
    # The version check runs inside the locked transaction so a concurrent
    # update cannot slip in between the check and the write.
    with db.write(store) as c:
        for object_type, (table, allowed) in EDITABLE.items():
            row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone()
            if row is not None:
                break
        else:
            raise NotFoundError(f"not found: {object_id}")
        unknown = set(patch) - allowed
        if unknown:
            raise ValidationError(f"{object_type} patch may contain only {sorted(allowed)}; got {sorted(unknown)}")
        if row["version"] != expected_version:
            raise ConflictError(f"VERSION_CONFLICT: expected {expected_version}, current {row['version']}")
        values = dict(row)
        for key, value in patch.items():
            if key in OPTIONAL_TEXT and value == "" and not (object_type == "rule" and key == "reason_md" and values["kind"] != "unsupported_operation"):
                values[key] = ""
            elif key == "confirmation":
                values[key] = value
            else:
                values[key] = require_text(value, f"patch.{key}")
        statuses = PAGE_STATUSES if object_type == "page" else KNOWLEDGE_STATUSES
        if values["status"] not in statuses:
            raise ValidationError(f"patch.status must be one of {sorted(statuses)}")
        if object_type == "rule":
            _check_rule(c, values)
        if object_type == "page" and row["type"] in {"schema_table", "schema_column"} and "title" in patch:
            raise ValidationError("schema page titles come from the schema; re-ingest the schema to rename")
        if object_type == "term":
            if c.execute("SELECT 1 FROM term_senses WHERE normalized_expression = ? AND scope = ? AND id != ?",
                         (values["normalized_expression"], values["scope"], object_id)).fetchone():
                raise ConflictError("TERM_CONFLICT: another term already uses this expression in that scope")
        if object_type == "join_path" and "title" in patch:
            values["normalized_title"] = db.normalize_text(values["title"])
            if c.execute("SELECT 1 FROM join_paths WHERE system_key = ? AND normalized_title = ? AND id != ?",
                         (values["system_key"], values["normalized_title"], object_id)).fetchone():
                raise ConflictError("JOIN_PATH_CONFLICT: another join path in this system already has that title")
        timestamp = db.now()
        version = expected_version + 1
        values["version"], values["updated_at"] = version, timestamp
        columns = sorted(set(patch) | {"version", "updated_at"} | ({"normalized_title"} if "normalized_title" in values and object_type == "join_path" else set()))
        c.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in columns)} WHERE id = ?",
                  tuple(values[k] for k in columns) + (object_id,))
        snapshot = {k: v for k, v in values.items() if k not in {"created_at"}}
        if "steps_json" in snapshot:
            snapshot["steps"] = json.loads(snapshot.pop("steps_json"))
        db.add_revision(c, object_type, object_id, version, snapshot, reason, timestamp, str(uuid.uuid4()))
        if object_type == "page":
            affected = [object_id]
        elif object_type == "term":
            affected = [values["page_id"]]
        elif object_type == "rule":
            affected = [values["scope_page_id"]]
        elif object_type == "join_path":
            affected = [r["page_id"] for r in c.execute("SELECT page_id FROM join_path_tables WHERE join_path_id = ?", (object_id,))]
        else:
            affected = []
        reindex(c, affected)
    return {"object_type": object_type, "id": object_id, "version": version, "store": str(store),
            **({"page_id": object_id} if object_type == "page" else {})}


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
