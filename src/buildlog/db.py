"""SQLite connection, schema migrations, and shared helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
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


PAGE_TYPES = {"experience", "concept", "method", "source", "index", "schema_table", "schema_column"}
HOST_PAGE_TYPES = PAGE_TYPES - {"schema_table", "schema_column"}  # schema pages come from DDL / schema_json only
PAGE_STATUSES = {"draft", "active", "deprecated", "superseded"}
# Evidence status of terms, relations, join paths and rules. Kept separate from page status.
KNOWLEDGE_STATUSES = {"extracted", "inferred", "verified", "rejected"}
RELATION_TYPES = {
    "USES", "DEPENDS_ON", "SUPPORTS", "TESTED_BY", "DERIVED_FROM",
    "SUPERSEDES", "CONTRADICTS", "RELATED_TO", "PART_OF", "JOINS_TO",
}
RULE_KINDS = {"prohibition", "requirement", "unsupported_operation"}

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
    # 2: terms, relations, schema objects, join paths, rules, source content and spans.
    [
        "ALTER TABLE sources ADD COLUMN content TEXT",
        "ALTER TABLE sources ADD COLUMN content_hash TEXT",
        "ALTER TABLE sources ADD COLUMN metadata_json TEXT",
        "ALTER TABLE sources ADD COLUMN parse_status TEXT",
        "ALTER TABLE sources ADD COLUMN diagnostics_json TEXT",
        "ALTER TABLE evidence ADD COLUMN start_line INTEGER",
        "ALTER TABLE evidence ADD COLUMN end_line INTEGER",
        """CREATE TABLE term_senses (
            id TEXT PRIMARY KEY,
            expression TEXT NOT NULL,
            normalized_expression TEXT NOT NULL,
            scope TEXT NOT NULL,
            definition_md TEXT NOT NULL,
            page_id TEXT REFERENCES pages(id),
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(normalized_expression, scope)
        )""",
        """CREATE TABLE aliases (
            expression TEXT NOT NULL,
            normalized_expression TEXT NOT NULL,
            term_sense_id TEXT NOT NULL REFERENCES term_senses(id),
            PRIMARY KEY (normalized_expression, term_sense_id)
        )""",
        """CREATE TABLE relations (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES pages(id),
            target_id TEXT NOT NULL REFERENCES pages(id),
            type TEXT NOT NULL,
            explanation_md TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_id, target_id, type)
        )""",
        """CREATE TABLE index_members (
            index_page_id TEXT NOT NULL REFERENCES pages(id),
            child_page_id TEXT NOT NULL REFERENCES pages(id),
            position INTEGER NOT NULL,
            PRIMARY KEY (index_page_id, child_page_id)
        )""",
        """CREATE TABLE schema_objects (
            page_id TEXT PRIMARY KEY REFERENCES pages(id),
            system_key TEXT NOT NULL,
            schema_name TEXT NOT NULL,
            object_name TEXT NOT NULL,
            object_type TEXT NOT NULL,
            norm_key TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            UNIQUE(norm_key, object_type)
        )""",
        """CREATE TABLE join_paths (
            id TEXT PRIMARY KEY,
            system_key TEXT NOT NULL,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            purpose TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            conditions_md TEXT NOT NULL,
            caveats_md TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(system_key, normalized_title)
        )""",
        """CREATE TABLE join_path_tables (
            join_path_id TEXT NOT NULL REFERENCES join_paths(id),
            page_id TEXT NOT NULL REFERENCES pages(id),
            PRIMARY KEY (join_path_id, page_id)
        )""",
        """CREATE TABLE rules (
            id TEXT PRIMARY KEY,
            scope_page_id TEXT NOT NULL REFERENCES pages(id),
            kind TEXT NOT NULL,
            condition_md TEXT NOT NULL,
            instruction_md TEXT NOT NULL,
            reason_md TEXT NOT NULL,
            alternative_md TEXT NOT NULL,
            confirmation TEXT,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE object_evidence (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL REFERENCES evidence(id),
            PRIMARY KEY (object_type, object_id, evidence_id)
        )""",
        "CREATE INDEX relations_target ON relations(target_id)",
        "CREATE INDEX term_senses_page ON term_senses(page_id)",
        "CREATE INDEX rules_scope ON rules(scope_page_id)",
        "CREATE INDEX join_path_tables_page ON join_path_tables(page_id)",
        "CREATE INDEX object_evidence_evidence ON object_evidence(evidence_id)",
    ],
    # 3: full-text search. Derived data: `buildlog reindex` rebuilds it from the tables above.
    # trigram matches substrings, which also covers Korean words with particles attached.
    [
        """CREATE VIRTUAL TABLE search_index USING fts5(
            page_id UNINDEXED, title, aliases, summary, body, extra, tokenize = 'trigram'
        )""",
    ],
    # 4: external research requests (Antigravity). A failed or interrupted request stays
    # resumable; the experience it belongs to is already committed and never lost.
    [
        """CREATE TABLE research_requests (
            id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES runs(id),
            page_id TEXT NOT NULL REFERENCES pages(id),
            question TEXT NOT NULL,
            context TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            error_kind TEXT,
            last_error TEXT,
            conversation_id TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX research_requests_run ON research_requests(run_id)",
        "CREATE INDEX research_requests_page ON research_requests(page_id)",
    ],
    # 5: run stages received → normalized → planned → committed with preserved failures.
    # A failed run keeps its stage and error; a retry with the same key reuses the row.
    [
        "ALTER TABLE runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE runs ADD COLUMN error_kind TEXT",
        "ALTER TABLE runs ADD COLUMN plan_summary_json TEXT",
    ],
]
RUN_STAGES = ("received", "normalized", "planned", "committed")
RESEARCH_STATUSES = {"pending", "running", "succeeded", "failed"}
SCHEMA_VERSION = len(MIGRATIONS)
# Versions whose migration also needs the search index rebuilt from existing rows.
REINDEX_AFTER = {3}


def default_store_path() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "Buildlog" / "buildlog.sqlite3"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    text = value if isinstance(value, str) else to_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    """Matching key for expressions and titles: NFKC, case-folded, single spaces."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def connect(store: Path) -> sqlite3.Connection:
    store.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def write(store: Path) -> Iterator[sqlite3.Connection]:
    """One locked write transaction: commits on success, rolls back everything on any error."""
    with closing(connect(store)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def initialize(store: Path) -> dict[str, Any]:
    """Create the store or apply pending migrations. Never deletes existing data."""
    with write(store) as connection:
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
        if any(current < version <= SCHEMA_VERSION for version in REINDEX_AFTER):
            from .index import rebuild  # local import: index depends on this module

            rebuild(connection)
        if current != SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
    return {"store": str(store), "schema_version": SCHEMA_VERSION}


def add_revision(connection: sqlite3.Connection, object_type: str, object_id: str, version: int,
                 snapshot: dict[str, Any], reason: str, timestamp: str, new_id: str) -> None:
    connection.execute(
        """INSERT INTO revisions(id, object_type, object_id, version, snapshot_json, reason, actor, changed_at)
           VALUES (?, ?, ?, ?, ?, ?, 'buildlog', ?)""",
        (new_id, object_type, object_id, version, to_json(snapshot), reason, timestamp),
    )
