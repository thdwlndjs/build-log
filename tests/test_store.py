from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from buildlog.store import (
    SCHEMA_VERSION,
    ConflictError,
    NotFoundError,
    ValidationError,
    ingest,
    initialize,
    read_page,
    render_markdown,
    update_page,
)

SRC = Path(__file__).resolve().parents[1] / "src"


def sample_plan() -> dict[str, object]:
    return {
        "idempotency_key": "phase-1-test",
        "page": {
            "type": "experience",
            "title": "SQLite FTS5 확인",
            "summary_md": "로컬 Python 환경에서 FTS5를 확인했다.",
            "body_md": "## Problem\n\nFTS5 지원 여부를 확인했다.\n\n## Mitigation\n\n가상 테이블 생성으로 검증했다.",
            "status": "active",
        },
        "source": {
            "kind": "conversation",
            "locator": "current-session",
            "accessible_excerpt": "SQLite FTS5 생성이 성공했다.",
        },
        "evidence": [{"section_locator": "environment-check", "excerpt": "FTS5 available", "evidence_kind": "observed"}],
    }


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "knowledge.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ingest_read_and_markdown(self) -> None:
        result = ingest(self.store, sample_plan())
        record = read_page(self.store, result["page_id"])
        rendered = render_markdown(record)
        self.assertFalse(result["reused"])
        self.assertIn("# SQLite FTS5 확인", rendered)
        self.assertIn("FTS5 available", rendered)

    def test_same_input_is_idempotent(self) -> None:
        first = ingest(self.store, sample_plan())
        second = ingest(self.store, sample_plan())
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertTrue(second["reused"])

    def test_missing_capture_time_does_not_change_idempotency_hash(self) -> None:
        with patch("buildlog.store._now", side_effect=["2026-10-26T00:00:00Z", "2026-10-26T00:01:00Z"]):
            first = ingest(self.store, sample_plan())
            second = ingest(self.store, sample_plan())
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertTrue(second["reused"])

    def test_same_key_with_different_input_conflicts(self) -> None:
        ingest(self.store, sample_plan())
        changed = sample_plan()
        changed["page"] = {**changed["page"], "title": "변경된 제목"}  # type: ignore[index]
        with self.assertRaises(ConflictError):
            ingest(self.store, changed)

    def test_stale_update_is_rejected_and_revision_is_created(self) -> None:
        created = ingest(self.store, sample_plan())
        updated = update_page(self.store, created["page_id"], 1, {"title": "갱신된 제목"}, "correct title")
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ConflictError):
            update_page(self.store, created["page_id"], 1, {"title": "다시 갱신"}, "stale update")

    def test_invalid_input_does_not_create_database_rows(self) -> None:
        invalid = sample_plan()
        invalid["page"] = {"title": "불완전"}
        with self.assertRaises(ValidationError):
            ingest(self.store, invalid)
        self.assertFalse(self.store.exists())

    def test_credentials_are_rejected(self) -> None:
        invalid = sample_plan()
        invalid["api_key"] = "do-not-store"
        with self.assertRaises(ValidationError):
            ingest(self.store, invalid)

    def test_invalid_input_leaves_existing_store_untouched(self) -> None:
        created = ingest(self.store, sample_plan())
        before = _row_counts(self.store)
        invalid = sample_plan()
        invalid["idempotency_key"] = "another-key"
        invalid["page"] = {"title": "불완전"}
        with self.assertRaises(ValidationError):
            ingest(self.store, invalid)
        self.assertEqual(_row_counts(self.store), before)
        self.assertEqual(read_page(self.store, created["page_id"])["page"]["version"], 1)

    def test_failure_mid_write_rolls_back_every_row(self) -> None:
        ingest(self.store, sample_plan())
        before = _row_counts(self.store)
        plan = sample_plan()
        plan["idempotency_key"] = "rollback-test"
        plan["evidence"] = [
            {"section_locator": "a", "excerpt": "first", "evidence_kind": "observed"},
            {"section_locator": "b", "excerpt": "second", "evidence_kind": "observed"},
        ]
        # The second evidence row reuses the first one's id, so its insert fails
        # after the run, page, source and first evidence rows were written.
        ids = [uuid.UUID(int=i) for i in (1, 2, 3, 4, 4)]
        with patch("buildlog.store.uuid.uuid4", side_effect=ids):
            with self.assertRaises(sqlite3.IntegrityError):
                ingest(self.store, plan)
        self.assertEqual(_row_counts(self.store), before)
        # The key was not consumed by the failed attempt.
        self.assertFalse(ingest(self.store, plan)["reused"])

    def test_update_keeps_every_revision(self) -> None:
        created = ingest(self.store, sample_plan())
        update_page(self.store, created["page_id"], 1, {"body_md": "## Mitigation\n\n수정됨"}, "follow-up result")
        with closing(sqlite3.connect(self.store)) as connection:
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM revisions WHERE object_id = ? ORDER BY version", (created["page_id"],)
            )]
        self.assertEqual(versions, [1, 2])
        self.assertIn("수정됨", read_page(self.store, created["page_id"])["page"]["body_md"])

    def test_read_missing_page_and_missing_store(self) -> None:
        with self.assertRaises(NotFoundError):
            read_page(self.store, "nope")
        self.assertFalse(self.store.exists())
        ingest(self.store, sample_plan())
        with self.assertRaises(NotFoundError):
            read_page(self.store, "nope")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "knowledge.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_initialize_is_repeatable_and_keeps_data(self) -> None:
        created = ingest(self.store, sample_plan())
        self.assertEqual(initialize(self.store)["schema_version"], SCHEMA_VERSION)
        initialize(self.store)
        self.assertEqual(read_page(self.store, created["page_id"])["page"]["title"], "SQLite FTS5 확인")

    def test_newer_store_schema_is_rejected(self) -> None:
        initialize(self.store)
        with closing(sqlite3.connect(self.store)) as connection:
            connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
            connection.commit()
        with self.assertRaises(ValidationError):
            initialize(self.store)


class CliProcessTests(unittest.TestCase):
    """Each command runs in a fresh Python process, as the host skill runs it."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.store = self.dir / "knowledge.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "buildlog", "--store", str(self.store), *args],
            capture_output=True,
            cwd=self.dir,  # must not depend on the project directory
            env={**os.environ, "PYTHONPATH": str(SRC)},
        )

    def write_plan(self, plan: dict[str, object], name: str = "plan.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def test_save_then_read_across_processes(self) -> None:
        plan_path = self.write_plan(sample_plan())
        first = self.run_cli("ingest", "--input", str(plan_path))
        self.assertEqual(first.returncode, 0, first.stderr.decode("utf-8"))
        data = json.loads(first.stdout.decode("utf-8"))["data"]
        self.assertFalse(data["reused"])

        again = self.run_cli("ingest", "--input", str(plan_path))
        self.assertEqual(json.loads(again.stdout.decode("utf-8"))["data"]["page_id"], data["page_id"])

        read = self.run_cli("read", data["page_id"])
        self.assertEqual(read.returncode, 0)
        markdown = read.stdout.decode("utf-8")
        self.assertIn("# SQLite FTS5 확인", markdown)
        self.assertIn(data["page_id"], markdown)

    def test_error_exit_codes_and_json_errors(self) -> None:
        self.write_plan(sample_plan())
        self.run_cli("ingest", "--input", str(self.dir / "plan.json"))
        changed = sample_plan()
        changed["page"] = {**changed["page"], "title": "다른 제목"}  # type: ignore[index]
        conflict = self.run_cli("ingest", "--input", str(self.write_plan(changed, "changed.json")))
        self.assertEqual(conflict.returncode, 3)
        self.assertIn("IDEMPOTENCY_CONFLICT", conflict.stderr.decode("utf-8"))

        missing_file = self.run_cli("ingest", "--input", str(self.dir / "missing.json"))
        self.assertEqual(missing_file.returncode, 2)
        self.assertEqual(json.loads(missing_file.stderr.decode("utf-8"))["status"], "error")

        not_found = self.run_cli("read", "no-such-id")
        self.assertEqual(not_found.returncode, 4)


def _row_counts(store: Path) -> dict[str, int]:
    with closing(sqlite3.connect(store)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "pages", "sources", "evidence", "page_sources", "revisions")
        }
