"""Phase 4: export / import round trips, Markdown edits, conflicts, backup and restore."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from buildlog.db import ConflictError, ValidationError
from buildlog.portability import backup, export, import_export, restore
from buildlog.read import read_object, render_markdown
from buildlog.search import query
from buildlog.store import ingest
from buildlog.update import merge_pages, unlink_relation, update_object

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parents[0] / "src"
sys.path.insert(0, str(TESTS))
from test_knowledge import demo_plan  # noqa: E402
from test_review import duplicates_plan  # noqa: E402

DATA_TABLES = ["pages", "sources", "evidence", "page_sources", "revisions", "runs", "term_senses", "aliases", "relations",
               "index_members", "schema_objects", "join_paths", "join_path_tables", "rules", "object_evidence",
               "research_requests", "feedback", "page_redirects"]


def dump(store: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(store)) as c:
        return {t: sorted(map(tuple, c.execute(f"SELECT * FROM {t}"))) for t in DATA_TABLES}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.store = self.dir / "a.sqlite3"
        ingest(self.store, demo_plan())
        ingest(self.store, duplicates_plan())
        # Leave some review history in the store too.
        with closing(sqlite3.connect(self.store)) as c:
            self.a = c.execute("SELECT id FROM pages WHERE title = '중복 A: 빌드 캐시 오류'").fetchone()[0]
            self.b = c.execute("SELECT id FROM pages WHERE title = '중복 B: 빌드 캐시 오류(재발)'").fetchone()[0]
            self.rel = c.execute("SELECT id FROM relations WHERE explanation_md = 'A uses'").fetchone()[0]
        unlink_relation(self.store, self.rel, 1, "review")
        merge_pages(self.store, [self.b], self.a, {self.a: 1, self.b: 1}, "dup")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class RoundTripTests(Base):
    def test_full_round_trip_reproduces_every_row(self) -> None:
        out = self.dir / "export"
        report = export(self.store, out)
        self.assertGreater(report["pages"], 20)
        self.assertTrue((out / "manifest.json").exists())
        target = self.dir / "b.sqlite3"
        result = import_export(target, out)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(dump(target), dump(self.store))
        self.assertEqual(render_markdown(read_object(target, self.a)), render_markdown(read_object(self.store, self.a)))
        self.assertEqual([r["id"] for r in query(target, "사업자번호", scope="demo_mes")["results"]],
                         [r["id"] for r in query(self.store, "사업자번호", scope="demo_mes")["results"]])
        again = import_export(target, out)  # importing the same export twice changes nothing
        self.assertEqual((again["inserted"], again["updated"], again["markdown_edits"]), ({}, {}, []))

    def test_markdown_edit_becomes_a_new_version(self) -> None:
        out = self.dir / "export"
        export(self.store, out)
        page_file = out / "pages" / f"{self.a}.md"
        text = page_file.read_text(encoding="utf-8")
        self.assertIn("<!-- buildlog:body -->", text)
        page_file.write_text(text.replace("<!-- buildlog:body -->\n", "<!-- buildlog:body -->\n파일에서 고친 본문 첫 줄 zebrafile\n"), encoding="utf-8")
        version = read_object(self.store, self.a)["page"]["version"]
        result = import_export(self.store, out)
        self.assertEqual(result["markdown_edits"], [{"page_id": self.a, "fields": ["body_md"], "version": version + 1}])
        page = read_object(self.store, self.a)["page"]
        self.assertTrue(page["body_md"].startswith("파일에서 고친 본문 첫 줄"))
        self.assertEqual(page["summary_dirty"], 1)
        self.assertEqual(query(self.store, "zebrafile")["results"][0]["id"], self.a)  # 3+ character body words are searchable

    def test_change_after_export_is_a_conflict_and_nothing_is_applied(self) -> None:
        out = self.dir / "export"
        export(self.store, out)
        page_file = out / "pages" / f"{self.a}.md"
        page_file.write_text(page_file.read_text(encoding="utf-8").replace("<!-- buildlog:body -->\n", "<!-- buildlog:body -->\nfile edit\n"),
                             encoding="utf-8")
        version = read_object(self.store, self.a)["page"]["version"]
        update_object(self.store, self.a, version, {"summary_md": "changed in the store"}, "meanwhile")
        before = dump(self.store)
        with self.assertRaises(ConflictError):
            import_export(self.store, out)
        self.assertEqual(dump(self.store), before)

    def test_dry_run_and_tampered_manifest(self) -> None:
        out = self.dir / "export"
        export(self.store, out)
        target = self.dir / "c.sqlite3"
        report = import_export(target, out, dry_run=True)
        self.assertGreater(report["inserted"]["pages"], 0)
        self.assertEqual(dump(target)["pages"], [])
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        manifest["tables"]["pages"][0]["title"] = "tampered"
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValidationError):
            import_export(target, out)

    def test_scoped_export_brings_what_belongs_to_the_pages(self) -> None:
        with closing(sqlite3.connect(self.store)) as c:
            table = c.execute("SELECT id FROM pages WHERE title = 'sensor_reading (demo_mes)'").fetchone()[0]
        out = self.dir / "scoped"
        report = export(self.store, out, [table, self.a])
        self.assertIn("schema_objects", report["rows"])
        target = self.dir / "d.sqlite3"
        result = import_export(target, out)
        self.assertEqual(result["conflicts"], [])
        md = render_markdown(read_object(target, table))
        self.assertIn("reading_time 기간 조건을 반드시 건다", md)  # rule scoped to the table came along
        self.assertIn("| `operator_code` |", md)
        with closing(sqlite3.connect(target)) as c:
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_export_refuses_to_clear_unrelated_folders(self) -> None:
        folder = self.dir / "notes"
        folder.mkdir()
        (folder / "keep.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(ValidationError):
            export(self.store, folder, force=True)
        self.assertTrue((folder / "keep.txt").exists())


class BackupRestoreTests(Base):
    def test_backup_then_restore_brings_back_the_old_state(self) -> None:
        copy = self.dir / "backup.sqlite3"
        result = backup(self.store, copy)
        self.assertEqual(result["integrity"], "ok")
        snapshot = dump(self.store)
        update_object(self.store, self.a, read_object(self.store, self.a)["page"]["version"], {"title": "after backup"}, "x")
        restored = restore(self.store, copy)
        self.assertEqual(dump(self.store), snapshot)
        self.assertTrue(Path(restored["previous_store_saved_as"]).exists())  # the replaced store was kept
        self.assertEqual(query(self.store, "빌드 캐시 오류")["results"][0]["id"], self.a)  # index rebuilt

    def test_cli_backup_restore_and_export_import(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC)}

        def cli(*args: str) -> dict[str, Any]:
            done = subprocess.run([sys.executable, "-m", "buildlog", *args], capture_output=True, env=env, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
            return json.loads(done.stdout.decode("utf-8"))["data"]

        self.assertEqual(cli("--store", str(self.store), "backup", "--out", str(self.dir / "b1.sqlite3"))["integrity"], "ok")
        cli("--store", str(self.store), "export", "--out", str(self.dir / "ex"))
        imported = cli("--store", str(self.dir / "fresh.sqlite3"), "import", "--in", str(self.dir / "ex"))
        self.assertEqual(imported["conflicts"], [])
        restored = cli("--store", str(self.dir / "fresh.sqlite3"), "restore", "--from", str(self.dir / "b1.sqlite3"))
        self.assertEqual(restored["integrity"], "ok")


if __name__ == "__main__":
    unittest.main()
