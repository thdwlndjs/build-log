from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


class BuildlogError(Exception):
    """Base error for an expected Buildlog failure."""


class ValidationError(BuildlogError):
    pass


class ConflictError(BuildlogError):
    pass


class NotFoundError(BuildlogError):
    pass


PAGE_TYPES = {"experience", "concept", "method", "source", "index"}
PAGE_STATUSES = {"draft", "active", "deprecated", "superseded"}

# Each entry upgrades the store by one schema version: version N is MIGRATIONS[N - 1].
# Append new entries; never edit one that has already been applied to a real store.
MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE IF NOT EXISTS pages (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary_md TEXT NOT NULL,
            body_md TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            locator TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            accessible_excerpt TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id),
            section_locator TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            evidence_kind TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS page_sources (
            page_id TEXT NOT NULL REFERENCES pages(id),
            evidence_id TEXT NOT NULL REFERENCES evidence(id),
            PRIMARY KEY (page_id, evidence_id)
        )""",
        """CREATE TABLE IF NOT EXISTS revisions (
            id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            UNIQUE(object_type, object_id, version)
        )""",
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            input_hash TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            research_status TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def default_store_path() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "Buildlog" / "buildlog.sqlite3"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _connect(store: Path) -> sqlite3.Connection:
    store.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def _write(store: Path) -> Iterator[sqlite3.Connection]:
    """One locked write transaction: commits on success, rolls back everything on any error."""
    with closing(_connect(store)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def initialize(store: Path) -> dict[str, Any]:
    """Create the store or apply pending migrations. Never deletes existing data."""
    with _write(store) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row is not None else 0
        if current > SCHEMA_VERSION:
            raise ValidationError(
                f"store schema version {current} is newer than this Buildlog version ({SCHEMA_VERSION})"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version - 1]:
                connection.execute(statement)
        if current != SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
    return {"store": str(store), "schema_version": SCHEMA_VERSION}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _reject_secrets(value: Any, path: str = "input") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if re.search(r"(?:password|secret|token|api[_-]?key|credential)", str(key), re.I):
                raise ValidationError(f"{path}.{key} may not contain credentials")
            _reject_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secrets(nested, f"{path}[{index}]")
    elif isinstance(value, str) and re.search(r"\b(?:ghp|github_pat|sk)_[A-Za-z0-9_-]{16,}\b", value):
        raise ValidationError(f"{path} appears to contain a credential")


def _validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValidationError("input must be a JSON object")
    _reject_secrets(plan)
    idempotency_key = _require_text(plan.get("idempotency_key"), "idempotency_key")
    page = plan.get("page")
    source = plan.get("source")
    if not isinstance(page, dict) or not isinstance(source, dict):
        raise ValidationError("page and source must be objects")
    page_type = page.get("type", "experience")
    if page_type not in PAGE_TYPES:
        raise ValidationError("page.type is not supported in Phase 1")
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
        normalized_evidence.append(
            {
                "section_locator": _require_text(item.get("section_locator", "unspecified"), f"evidence[{index}].section_locator"),
                "excerpt": _require_text(item.get("excerpt"), f"evidence[{index}].excerpt"),
                "evidence_kind": _require_text(item.get("evidence_kind", "observed"), f"evidence[{index}].evidence_kind"),
            }
        )
    captured_at = source.get("captured_at")
    if captured_at is not None:
        captured_at = _require_text(captured_at, "source.captured_at")
    return {
        "idempotency_key": idempotency_key,
        "page": {
            "type": page_type,
            "title": _require_text(page.get("title"), "page.title"),
            "summary_md": _require_text(page.get("summary_md"), "page.summary_md"),
            "body_md": _require_text(page.get("body_md"), "page.body_md"),
            "status": status,
        },
        "source": {
            "kind": _require_text(source.get("kind"), "source.kind"),
            "locator": _require_text(source.get("locator"), "source.locator"),
            "captured_at": captured_at,
            "accessible_excerpt": _require_text(source.get("accessible_excerpt"), "source.accessible_excerpt"),
        },
        "evidence": normalized_evidence,
    }


def _matches_legacy_hash(connection: sqlite3.Connection, stored_hash: str, result: dict[str, Any], plan: dict[str, Any]) -> bool:
    # Early Phase-1 development stores hashed the plan after filling in captured_at.
    # Accept only that exact legacy shape so those records stay idempotent.
    source = connection.execute(
        """SELECT s.captured_at FROM sources s JOIN evidence e ON e.source_id = s.id
           JOIN page_sources ps ON ps.evidence_id = e.id WHERE ps.page_id = ? LIMIT 1""",
        (result["page_id"],),
    ).fetchone()
    legacy_plan = json.loads(_json(plan))
    if source is not None:
        legacy_plan["source"]["captured_at"] = source["captured_at"]
    return stored_hash == _hash(legacy_plan)


def ingest(store: Path, raw_plan: Any) -> dict[str, Any]:
    plan = _validate_plan(raw_plan)
    input_hash = _hash(plan)
    initialize(store)
    # The idempotency check runs inside the locked transaction so two concurrent
    # saves of the same key cannot both create a page.
    with _write(store) as connection:
        existing = connection.execute(
            "SELECT input_hash, result_json FROM runs WHERE idempotency_key = ?", (plan["idempotency_key"],)
        ).fetchone()
        if existing is not None:
            result = json.loads(existing["result_json"])
            if existing["input_hash"] != input_hash and not _matches_legacy_hash(
                connection, existing["input_hash"], result, plan
            ):
                raise ConflictError(
                    f"IDEMPOTENCY_CONFLICT: key was used with different input (existing page {result['page_id']})"
                )
            result["reused"] = True
            return result

        timestamp = _now()
        run_id, page_id, source_id = (str(uuid.uuid4()) for _ in range(3))
        connection.execute(
            """INSERT INTO runs(id, idempotency_key, input_hash, stage, status, research_status, created_at, updated_at)
               VALUES (?, ?, ?, 'received', 'running', 'not_requested', ?, ?)""",
            (run_id, plan["idempotency_key"], input_hash, timestamp, timestamp),
        )
        page = plan["page"]
        connection.execute(
            """INSERT INTO pages(id, type, title, summary_md, body_md, status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (page_id, page["type"], page["title"], page["summary_md"], page["body_md"], page["status"], timestamp, timestamp),
        )
        source = plan["source"]
        connection.execute(
            "INSERT INTO sources(id, kind, locator, captured_at, accessible_excerpt) VALUES (?, ?, ?, ?, ?)",
            (source_id, source["kind"], source["locator"], source["captured_at"] or timestamp, source["accessible_excerpt"]),
        )
        for item in plan["evidence"]:
            evidence_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind)
                   VALUES (?, ?, ?, ?, ?)""",
                (evidence_id, source_id, item["section_locator"], item["excerpt"], item["evidence_kind"]),
            )
            connection.execute("INSERT INTO page_sources(page_id, evidence_id) VALUES (?, ?)", (page_id, evidence_id))
        snapshot = {"id": page_id, **page, "version": 1, "updated_at": timestamp}
        connection.execute(
            """INSERT INTO revisions(id, object_type, object_id, version, snapshot_json, reason, actor, changed_at)
               VALUES (?, 'page', ?, 1, ?, 'created', 'buildlog', ?)""",
            (str(uuid.uuid4()), page_id, _json(snapshot), timestamp),
        )
        result = {"run_id": run_id, "page_id": page_id, "version": 1, "store": str(store), "reused": False}
        connection.execute(
            """UPDATE runs SET stage = 'committed', status = 'succeeded', result_json = ?, updated_at = ?
               WHERE id = ?""",
            (_json(result), timestamp, run_id),
        )
        return result


def read_page(store: Path, page_id: str) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"page not found: {page_id} (store does not exist: {store})")
    initialize(store)
    with closing(_connect(store)) as connection:
        page = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            raise NotFoundError(f"page not found: {page_id}")
        sources = connection.execute(
            """SELECT s.*, e.section_locator, e.excerpt, e.evidence_kind
               FROM sources s JOIN evidence e ON e.source_id = s.id
               JOIN page_sources ps ON ps.evidence_id = e.id WHERE ps.page_id = ?
               ORDER BY e.rowid""",
            (page_id,),
        ).fetchall()
    return {"page": dict(page), "evidence": [dict(row) for row in sources]}


def render_markdown(record: dict[str, Any]) -> str:
    page = record["page"]
    lines = [
        f"# {page['title']}",
        "",
        f"- ID: `{page['id']}`",
        f"- Type: `{page['type']}`",
        f"- Status: `{page['status']}`",
        f"- Version: {page['version']}",
        f"- Updated: {page['updated_at']}",
        "",
        "## Summary",
        "",
        page["summary_md"],
        "",
        "## Record",
        "",
        page["body_md"],
    ]
    if record["evidence"]:
        lines.extend(["", "## Evidence"])
        for evidence in record["evidence"]:
            lines.extend(
                [
                    "",
                    f"- Source: {evidence['kind']} — {evidence['locator']}",
                    f"  - Location: {evidence['section_locator']}",
                    f"  - Kind: {evidence['evidence_kind']}",
                    f"  - Excerpt: {evidence['excerpt']}",
                ]
            )
    return "\n".join(lines) + "\n"


def update_page(store: Path, page_id: str, expected_version: int, patch: dict[str, Any], reason: str) -> dict[str, Any]:
    allowed = {"title", "summary_md", "body_md", "status"}
    if not patch or any(key not in allowed for key in patch):
        raise ValidationError("patch must contain only title, summary_md, body_md, or status")
    _reject_secrets(patch, "patch")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise ValidationError("expected_version must be a positive integer")
    _require_text(reason, "reason")
    if not store.exists():
        raise NotFoundError(f"page not found: {page_id} (store does not exist: {store})")
    initialize(store)
    # The version check runs inside the locked transaction so a concurrent
    # update cannot slip in between the check and the write.
    with _write(store) as connection:
        page = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            raise NotFoundError(f"page not found: {page_id}")
        if page["version"] != expected_version:
            raise ConflictError(f"VERSION_CONFLICT: expected {expected_version}, current {page['version']}")
        values = dict(page)
        for key, value in patch.items():
            values[key] = _require_text(value, f"patch.{key}")
        if values["status"] not in PAGE_STATUSES:
            raise ValidationError("patch.status is invalid")
        timestamp = _now()
        new_version = expected_version + 1
        connection.execute(
            """UPDATE pages SET title = ?, summary_md = ?, body_md = ?, status = ?, version = ?, updated_at = ?
               WHERE id = ?""",
            (values["title"], values["summary_md"], values["body_md"], values["status"], new_version, timestamp, page_id),
        )
        snapshot = {key: values[key] for key in ("id", "type", "title", "summary_md", "body_md", "status")}
        snapshot.update({"version": new_version, "updated_at": timestamp})
        connection.execute(
            """INSERT INTO revisions(id, object_type, object_id, version, snapshot_json, reason, actor, changed_at)
               VALUES (?, 'page', ?, ?, ?, ?, 'buildlog', ?)""",
            (str(uuid.uuid4()), page_id, new_version, _json(snapshot), reason, timestamp),
        )
    return {"page_id": page_id, "version": new_version, "store": str(store)}
