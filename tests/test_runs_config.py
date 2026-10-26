"""Run stages and recovery, shared settings, and samples embedded in schema_json."""
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
from unittest.mock import patch

from buildlog import config, ingest as ingest_module
from buildlog.db import ValidationError
from buildlog.research import run_research
from buildlog.store import ingest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_research import plan_with_research  # noqa: E402


def experience(key: str = "k1", title: str = "경험") -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "sources": [{"ref": "c", "kind": "conversation", "locator": "test", "accessible_excerpt": "x"}],
        "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "관찰"}],
        "pages": [{"ref": "p", "type": "experience", "title": title, "summary_md": "s", "body_md": "b", "evidence": ["e"]}],
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.store = self.dir / "r.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def run_row(self, key: str) -> sqlite3.Row:
        return self.rows("SELECT * FROM runs WHERE idempotency_key = ?", key)[0]

    def data_count(self) -> int:
        return sum(self.rows(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("pages", "sources", "evidence", "revisions", "search_index"))


class RunStageTests(Base):
    def test_success_passes_every_stage(self) -> None:
        result = ingest(self.store, experience())
        run = self.run_row("k1")
        self.assertEqual((run["stage"], run["status"], run["attempts"]), ("committed", "succeeded", 1))
        self.assertEqual(json.loads(run["plan_summary_json"])["create"], {"pages": 1})
        self.assertEqual(result["status"], "complete")

    def test_failure_while_committing_keeps_record_and_no_data(self) -> None:
        calls = {"n": 0}
        original = ingest_module._Apply.run

        def flaky(self: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 2:  # the planning pass succeeds, the writing pass fails
                raise sqlite3.OperationalError("disk I/O error (simulated)")
            return original(self)

        with patch.object(ingest_module._Apply, "run", flaky):
            with self.assertRaises(sqlite3.OperationalError):
                ingest(self.store, experience())
        run = self.run_row("k1")
        self.assertEqual((run["stage"], run["status"], run["error_kind"]), ("planned", "failed", "store"))
        self.assertIn("simulated", run["error"])
        self.assertEqual(self.data_count(), 0)
        retried = ingest(self.store, experience())
        self.assertEqual(retried["run_id"], run["id"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages")[0][0], 1)
        self.assertEqual(self.run_row("k1")["attempts"], 2)

    def test_interrupted_run_is_retried_not_duplicated(self) -> None:
        ingest(self.store, experience())
        # Simulate a process killed after planning, before commit, on a different key.
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("""INSERT INTO runs(id, idempotency_key, input_hash, stage, status, research_status, attempts, created_at, updated_at)
                         VALUES ('dead', 'k2', '', 'planned', 'running', 'not_requested', 1, 't', 't')""")
            c.commit()
        result = ingest(self.store, experience("k2", "두 번째"))
        self.assertEqual(result["run_id"], "dead")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages")[0][0], 2)
        self.assertEqual(tuple(self.rows("SELECT stage, status, attempts FROM runs WHERE id = 'dead'")[0]), ("committed", "succeeded", 2))

    def test_committed_run_is_never_marked_failed(self) -> None:
        ingest(self.store, experience())
        broken = experience()
        broken["pages"][0]["title"] = ""
        with self.assertRaises(ValidationError):
            ingest(self.store, broken)
        run = self.run_row("k1")
        self.assertEqual((run["status"], run["error"]), ("succeeded", None))

    def test_failed_save_can_be_retried_with_corrected_input(self) -> None:
        broken = experience()
        broken["evidence"][0]["source"] = "missing"
        with self.assertRaises(ValidationError):
            ingest(self.store, broken)
        self.assertEqual(self.run_row("k1")["status"], "failed")
        result = ingest(self.store, experience())
        self.assertFalse(result["reused"])
        again = ingest(self.store, experience())
        self.assertTrue(again["reused"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages")[0][0], 1)

    def test_research_resume_needs_a_committed_run(self) -> None:
        plan = plan_with_research()
        plan["pages"][0]["title"] = ""
        with self.assertRaises(ValidationError):
            ingest(self.store, plan)
        run_id = self.run_row("test:research")["id"]
        with self.assertRaises(ValidationError) as raised:
            run_research(self.store, run_id=run_id, agy=["unused"], timeout=30)
        self.assertIn("not committed", str(raised.exception))

    def test_runs_command_lists_failures(self) -> None:
        broken = experience("bad")
        broken["pages"][0]["type"] = "schema_table"
        with self.assertRaises(ValidationError):
            ingest(self.store, broken)
        done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(self.store), "runs", "--status", "failed"],
                              capture_output=True, env={**os.environ, "PYTHONPATH": str(SRC)})
        runs = json.loads(done.stdout.decode("utf-8"))["data"]["runs"]
        self.assertEqual([(r["idempotency_key"], r["stage"], r["error_kind"]) for r in runs], [("bad", "received", "validation")])


class ConcurrencyTests(Base):
    def test_same_key_from_parallel_processes_commits_once(self) -> None:
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps(experience("parallel")), encoding="utf-8")
        ingest(self.store, experience("warm-up"))  # create the store first so every process opens the same file
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        procs = [subprocess.Popen([sys.executable, "-m", "buildlog", "--store", str(self.store), "ingest", "--input", str(plan)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(6)]
        outputs = [p.communicate(timeout=120) for p in procs]
        codes = [p.returncode for p in procs]
        self.assertEqual(codes, [0] * 6, [o[1].decode("utf-8", "replace") for o in outputs])
        reused = [json.loads(o[0].decode("utf-8"))["data"]["reused"] for o in outputs]
        self.assertEqual(reused.count(False), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages WHERE title = '경험'")[0][0], 2)  # warm-up + parallel
        self.assertEqual(self.rows("SELECT COUNT(*) FROM runs WHERE idempotency_key = 'parallel'")[0][0], 1)
        self.assertEqual(self.run_row("parallel")["status"], "succeeded")

    def test_different_keys_from_parallel_processes_all_commit(self) -> None:
        ingest(self.store, experience("warm-up"))
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        procs = []
        for i in range(5):
            path = self.dir / f"p{i}.json"
            path.write_text(json.dumps(experience(f"key-{i}", f"동시 {i}")), encoding="utf-8")
            procs.append(subprocess.Popen([sys.executable, "-m", "buildlog", "--store", str(self.store), "ingest", "--input", str(path)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
        for p in procs:
            p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages WHERE title LIKE '동시 %'")[0][0], 5)


class ConfigTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.dir / "config.toml"
        self.saved_env = {k: os.environ.pop(k, None) for k in ("BUILDLOG_CONFIG", "BUILDLOG_STORE", "BUILDLOG_AGY", "BUILDLOG_AGY_COMMAND")}
        os.environ["BUILDLOG_CONFIG"] = str(self.config)

    def tearDown(self) -> None:
        for key, value in self.saved_env.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        super().tearDown()

    def test_order_option_env_config_default(self) -> None:
        self.assertEqual(config.resolve("store")[1], "default")
        self.config.write_text("store = 'from-config.sqlite3'\nagy = 'C:\\tools\\agy.exe'\n", encoding="utf-8")
        value, origin = config.resolve("store")
        self.assertEqual(Path(value), self.dir / "from-config.sqlite3")  # relative to the config file
        self.assertTrue(origin.startswith("config"))
        self.assertEqual(config.resolve_agy(), [r"C:\tools\agy.exe"])
        os.environ["BUILDLOG_STORE"] = str(self.dir / "from-env.sqlite3")
        os.environ["BUILDLOG_AGY"] = r"C:\env\agy.exe"
        self.assertEqual(config.resolve("store"), (str(self.dir / "from-env.sqlite3"), "env BUILDLOG_STORE"))
        self.assertEqual(config.resolve_agy(), [r"C:\env\agy.exe"])
        self.assertEqual(config.resolve("store", str(self.dir / "opt.sqlite3"))[1], "option")
        self.assertEqual(config.resolve_agy(r"C:\opt\agy.exe"), [r"C:\opt\agy.exe"])

    def test_invalid_config_is_an_input_error(self) -> None:
        self.config.write_text("stor = 'typo'\n", encoding="utf-8")
        with self.assertRaises(ValidationError):
            config.resolve("store")
        done = subprocess.run([sys.executable, "-m", "buildlog", "init"], capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(SRC)})
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown keys", json.loads(done.stderr.decode("utf-8"))["error"])

    def test_cli_uses_config_store_and_config_commands(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        store = self.dir / "shared" / "kb.sqlite3"
        done = subprocess.run([sys.executable, "-m", "buildlog", "config", "set", "store", str(store)], capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps(experience()), encoding="utf-8")
        done = subprocess.run([sys.executable, "-m", "buildlog", "ingest", "--input", str(plan)], capture_output=True, env=env)
        self.assertEqual(json.loads(done.stdout.decode("utf-8"))["data"]["store"], str(store))
        shown = json.loads(subprocess.run([sys.executable, "-m", "buildlog", "config", "show"], capture_output=True, env=env)
                           .stdout.decode("utf-8"))["data"]
        self.assertEqual(shown["store"]["value"], str(store))
        subprocess.run([sys.executable, "-m", "buildlog", "config", "unset", "store"], capture_output=True, env=env)
        self.assertEqual(config.resolve("store")[1], "default")


def schema_with_samples(rows: int = 3, table: str = "item", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    sample = {"table": table, "rows": [{"id": i, "name": f"n{i}", "api_token": "secret-value"} for i in range(rows)]}
    sample.update(extra or {})
    return {
        "system_key": "inv",
        "tables": [{"name": "item", "columns": [{"name": "id", "nullable": False}, {"name": "name"}, {"name": "api_token"}],
                    "primary_key": ["id"]}],
        "samples": [sample, {"table": "item", "format": "csv", "content": "id,name\n9,csv-row\n"}],
    }


class EmbeddedSampleTests(Base):
    def plan(self, schema: dict[str, Any], key: str = "s1") -> dict[str, Any]:
        return {"idempotency_key": key, "sources": [{"ref": "schema", "kind": "schema_json", "content": json.dumps(schema)}]}

    def test_samples_use_the_sample_path(self) -> None:
        result = ingest(self.store, self.plan(schema_with_samples()))
        self.assertEqual(result["status"], "complete")
        derived = [s for s in result["sources"] if s.get("from_source") == "schema"]
        self.assertEqual([(s["ref"], s["kind"], s["parse_status"]) for s in derived],
                         [("schema#samples[0]", "sample_json", "complete"), ("schema#samples[1]", "sample_csv", "complete")])
        schema_text = self.rows("SELECT content FROM sources WHERE kind = 'schema_json'")[0][0]
        self.assertNotIn("n0", schema_text)  # rows are not kept twice
        sample_text = self.rows("SELECT content FROM sources WHERE kind = 'sample_json'")[0][0]
        self.assertNotIn("secret-value", sample_text)
        self.assertIn("[REDACTED]", sample_text)
        linked = self.rows("""SELECT COUNT(*) FROM page_sources ps JOIN pages p ON p.id = ps.page_id
                              JOIN evidence e ON e.id = ps.evidence_id WHERE p.title = 'item (inv)' AND e.section_locator LIKE 'sample of item%'""")
        self.assertEqual(linked[0][0], 2)

    def test_truncation_is_partial_and_extra_rows_are_not_kept(self) -> None:
        result = ingest(self.store, self.plan(schema_with_samples(rows=60)))
        self.assertEqual(result["status"], "partial")
        self.assertIn("schema#samples[0]", result["partial_sources"])
        text = self.rows("SELECT content FROM sources WHERE kind = 'sample_json'")[0][0]
        self.assertIn('"n49"', text)
        self.assertNotIn('"n50"', text)
        everything = " ".join(r[0] or "" for r in self.rows("SELECT content FROM sources"))
        self.assertNotIn('"n55"', everything)

    def test_unknown_table_and_fields_are_partial(self) -> None:
        result = ingest(self.store, self.plan(schema_with_samples(table="ghost", extra={"note": "x"})))
        self.assertEqual(result["status"], "partial")
        sample = next(s for s in result["sources"] if s["ref"] == "schema#samples[0]")
        messages = " ".join(d["message"] for d in sample["diagnostics"])
        self.assertIn("fields not read", messages)
        self.assertIn("not linked", messages)
        stored = self.rows("SELECT parse_status FROM sources WHERE kind = 'sample_json'")[0][0]
        self.assertEqual(stored, "partial")

    def test_unknown_schema_fields_are_reported(self) -> None:
        schema = schema_with_samples()
        schema["owner"] = "team-a"
        result = ingest(self.store, self.plan(schema))
        self.assertEqual(result["status"], "partial")
        self.assertIn("schema", result["partial_sources"])

    def test_plain_sample_truncation_is_partial_too(self) -> None:
        ingest(self.store, self.plan(schema_with_samples(rows=1)))
        csv_rows = "id,name\n" + "".join(f"{i},r{i}\n" for i in range(70))
        result = ingest(self.store, {"idempotency_key": "s2", "sources": [
            {"ref": "csv", "kind": "sample_csv", "system_key": "inv", "table": "item", "content": csv_rows}]})
        self.assertEqual(result["status"], "partial")

    def test_cli_reports_partial_in_the_envelope(self) -> None:
        plan = self.dir / "p.json"
        plan.write_text(json.dumps(self.plan(schema_with_samples(rows=60))), encoding="utf-8")
        done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(self.store), "ingest", "--input", str(plan)],
                              capture_output=True, env={**os.environ, "PYTHONPATH": str(SRC)})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout.decode("utf-8"))["status"], "partial")


if __name__ == "__main__":
    unittest.main()
