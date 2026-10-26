"""schema_json and sample inputs: validation into the same table shape the DDL reader returns."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from .db import ValidationError

MAX_SAMPLE_ROWS = 50
_SYSTEM_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_CREDENTIAL_NAME_RE = re.compile(r"(?:password|passwd|pwd|secret|token|api[_-]?key|credential)", re.I)
REDACTED = "[REDACTED]"


def check_system_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SYSTEM_KEY_RE.match(value):
        raise ValidationError(f"{field} must match [A-Za-z0-9_.-]{{1,100}}")
    return value


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _names(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValidationError(f"{field} must be a list of column names")
    return [v.strip() for v in value]


def normalize_schema_json(data: Any, where: str) -> dict[str, Any]:
    """Validate the schema_json contract (spec §4). Missing nullable stays unknown (None)."""
    if not isinstance(data, dict):
        raise ValidationError(f"{where} must be a JSON object")
    system_key = check_system_key(data.get("system_key"), f"{where}.system_key")
    tables_in = data.get("tables")
    if not isinstance(tables_in, list) or not tables_in:
        raise ValidationError(f"{where}.tables must be a non-empty list")
    tables: list[dict[str, Any]] = []
    for t_index, raw in enumerate(tables_in):
        at = f"{where}.tables[{t_index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{at} must be an object")
        columns: list[dict[str, Any]] = []
        for c_index, col in enumerate(raw.get("columns") or []):
            cat = f"{at}.columns[{c_index}]"
            if not isinstance(col, dict):
                raise ValidationError(f"{cat} must be an object")
            nullable = col.get("nullable")
            if nullable is not None and not isinstance(nullable, bool):
                raise ValidationError(f"{cat}.nullable must be true, false, or omitted (unknown)")
            columns.append({
                "name": _name(col.get("name"), f"{cat}.name"),
                "type": col.get("type") if isinstance(col.get("type"), str) else None,
                "nullable": nullable,
                "description": col.get("description") if isinstance(col.get("description"), str) else None,
            })
        if not columns:
            raise ValidationError(f"{at}.columns must be a non-empty list")
        fks = []
        for f_index, fk in enumerate(raw.get("foreign_keys") or []):
            fat = f"{at}.foreign_keys[{f_index}]"
            if not isinstance(fk, dict):
                raise ValidationError(f"{fat} must be an object")
            fks.append({
                "columns": _names(fk.get("columns"), f"{fat}.columns"),
                "target_schema": fk.get("target_schema") if isinstance(fk.get("target_schema"), str) else None,
                "target_table": _name(fk.get("target_table"), f"{fat}.target_table"),
                "target_columns": _names(fk.get("target_columns"), f"{fat}.target_columns"),
            })
        tables.append({
            "schema": raw.get("schema") if isinstance(raw.get("schema"), str) and raw.get("schema") else None,
            "name": _name(raw.get("name"), f"{at}.name"),
            "description": raw.get("description") if isinstance(raw.get("description"), str) else None,
            "columns": columns,
            "primary_key": _names(raw.get("primary_key") or [], f"{at}.primary_key"),
            "unique": [_names(u, f"{at}.unique") for u in raw.get("unique") or []],
            "foreign_keys": fks,
            "checks": [],
            "indexes": [],
            "span": None,
            "pointer": f"/tables/{t_index}",
        })
    return {"system_key": system_key, "tables": tables}


def _redact_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    redacted = False
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if _CREDENTIAL_NAME_RE.search(str(key)) and value not in (None, ""):
            clean[key] = REDACTED
            redacted = True
        else:
            clean[key] = value
    return clean, redacted


def parse_sample(kind: str, content: str, where: str) -> tuple[list[dict[str, Any]], list[str], str]:
    """Return (rows, warnings, normalized_content). Credential-named columns are redacted."""
    warnings: list[str] = []
    if kind == "sample_json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{where}: invalid JSON sample: {error}") from error
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            data = data["rows"]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValidationError(f"{where}: JSON sample must be a list of row objects")
        rows = data
    else:
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise ValidationError(f"{where}: CSV sample needs a header row")
        rows = [dict(r) for r in reader]
    if len(rows) > MAX_SAMPLE_ROWS:
        warnings.append(f"{where}: kept the first {MAX_SAMPLE_ROWS} of {len(rows)} sample rows")
        rows = rows[:MAX_SAMPLE_ROWS]
    clean_rows = []
    any_redacted = False
    for row in rows:
        clean, redacted = _redact_row(row)
        clean_rows.append(clean)
        any_redacted = any_redacted or redacted
    if any_redacted:
        warnings.append(f"{where}: values in credential-like columns were replaced with {REDACTED}")
    if kind == "sample_json":
        normalized = json.dumps(clean_rows, ensure_ascii=False, indent=1)
    else:
        out = io.StringIO()
        fieldnames = list(clean_rows[0].keys()) if clean_rows else list(reader.fieldnames or [])
        writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(clean_rows)
        normalized = out.getvalue()
    return clean_rows, warnings, normalized
