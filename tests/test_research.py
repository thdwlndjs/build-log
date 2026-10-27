"""Phase 2.3: Antigravity research adapter (with a replaying fake agy)."""
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

from buildlog import db
from buildlog.plan import normalize_plan
from buildlog.read import read_object, render_markdown
from buildlog.research import ResearchFailed, run_research
from buildlog.store import ingest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SRC = Path(__file__).resolve().parents[1] / "src"
FAKE = [sys.executable, str(FIXTURES / "fake_agy.py")]


def plan_with_research() -> dict[str, Any]:
    return {
        "idempotency_key": "test:research",
        "sources": [{"ref": "chat", "kind": "conversation", "locator": "test", "accessible_excerpt": "검색 색인 설계"}],
        "evidence": [{"ref": "e1", "source": "chat", "evidence_kind": "observed", "excerpt": "trigram 색인으로 한국어 부분 일치"}],
        "pages": [{"ref": "p", "type": "experience", "title": "한국어 부분 일치 검색에 FTS5 trigram 사용",
                   "summary_md": "trigram으로 조사 붙은 한국어를 찾는다.", "body_md": "## 문제\n\n한국어 부분 일치.", "evidence": ["e1"]}],
        "research": [{"question": "Does the SQLite FTS5 trigram tokenizer support substring matching?", "page": "p",
                      "context": "Korean substring search in a local SQLite store"}],
    }


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.store = self.dir / "r.sqlite3"
        self.mode_file = self.dir / "mode.txt"
        self.log = self.dir / "agy.log"
        os.environ["FAKE_AGY_MODE_FILE"] = str(self.mode_file)
        os.environ["FAKE_AGY_LOG"] = str(self.log)
        os.environ["BUILDLOG_OFFLINE"] = "1"  # source checks are tested with a fake fetcher in test_sources
        self.saved = ingest(self.store, plan_with_research())
        self.page_id = self.saved["page_id"]
        self.run_id = self.saved["run_id"]

    def tearDown(self) -> None:
        os.environ.pop("FAKE_AGY_MODE_FILE", None)
        os.environ.pop("FAKE_AGY_LOG", None)
        os.environ.pop("BUILDLOG_OFFLINE", None)
        self.temp_dir.cleanup()

    def mode(self, value: str) -> None:
        self.mode_file.write_text(value, encoding="utf-8")

    def research(self, **kwargs: Any) -> dict[str, Any]:
        return run_research(self.store, run_id=kwargs.pop("run_id", self.run_id), agy=FAKE, timeout=30, **kwargs)

    def one(self, sql: str, *args: Any) -> Any:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute(sql, args).fetchone()

    def test_ingest_records_a_pending_request(self) -> None:
        self.assertEqual(self.saved["research_status"], "pending")
        self.assertEqual(self.one("SELECT research_status FROM runs WHERE id = ?", self.run_id)[0], "pending")
        md = render_markdown(read_object(self.store, self.page_id))
        self.assertIn("## 외부 조사", md)
        self.assertIn("끝나지 않은 조사가 있다", md)

    def test_success_links_sources_with_access_level(self) -> None:
        self.mode("success")
        summary = self.research()
        self.assertEqual(summary["results"][0]["status"], "succeeded")
        self.assertEqual(summary["results"][0]["new_evidence"], 2)
        with closing(sqlite3.connect(self.store)) as c:
            kinds = {r[0]: r[1] for r in c.execute(
                """SELECT e.excerpt, e.evidence_kind FROM evidence e JOIN sources s ON s.id = e.source_id WHERE s.kind = 'web'""")}
        # Only a quote Buildlog itself found verbatim in the page counts as observed. Source checks are
        # off in this offline test, so even the finding agy calls "fetched" stays second-hand.
        self.assertEqual(sorted(kinds.values()), ["reported", "reported"])
        self.assertEqual(self.one("SELECT research_status FROM runs WHERE id = ?", self.run_id)[0], "succeeded")
        md = render_markdown(read_object(self.store, self.page_id))
        self.assertIn("web — https://www.sqlite.org/fts5.html", md)
        self.assertIn("검색 결과 요약만 확인", md)
        self.assertNotIn("끝나지 않은 조사가 있다", md)

    def test_failure_keeps_experience_and_resumes(self) -> None:
        self.mode("denied")
        with self.assertRaises(ResearchFailed) as raised:
            self.research()
        self.assertEqual(raised.exception.data["results"][0]["error_kind"], "permission")
        self.assertEqual(self.one("SELECT research_status FROM runs WHERE id = ?", self.run_id)[0], "failed")
        md = render_markdown(read_object(self.store, self.page_id))
        self.assertIn("# 한국어 부분 일치 검색에 FTS5 trigram 사용", md)
        self.assertIn("실패", md)
        self.assertIn("buildlog research --page", md)
        self.mode("success")
        summary = self.research()
        self.assertEqual(summary["results"][0]["status"], "succeeded")
        self.assertEqual(self.one("SELECT attempts FROM research_requests")[0], 2)
        # Running again does nothing more: finished requests are not repeated.
        again = self.research()
        self.assertEqual(again["results"], [])
        self.assertEqual(len(again["already_succeeded"]), 1)

    def test_error_kinds(self) -> None:
        cases = {"timeout": "timeout", "auth": "auth", "notjson": "output", "empty": "no_findings"}
        for mode, kind in cases.items():
            with self.subTest(mode):
                self.mode(mode)
                with self.assertRaises(ResearchFailed) as raised:
                    self.research()
                self.assertEqual(raised.exception.data["results"][0]["error_kind"], kind)
        self.assertEqual(self.one("SELECT COUNT(*) FROM sources WHERE kind = 'web'")[0], 0)

    def test_agy_not_installed(self) -> None:
        with self.assertRaises(ResearchFailed) as raised:
            run_research(self.store, run_id=self.run_id, agy=[str(self.dir / "missing" / "agy.exe")], timeout=30)
        self.assertEqual(raised.exception.data["results"][0]["error_kind"], "not_installed")

    def test_invalid_findings_are_dropped(self) -> None:
        self.mode("badurl")
        summary = self.research()
        result = summary["results"][0]
        self.assertEqual([f["url"] for f in result["findings"]], ["https://example.org/doc"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_redirect_links_are_kept_but_labelled(self) -> None:
        self.mode("redirect")
        summary = self.research()
        result = summary["results"][0]
        self.assertTrue(result["findings"][0]["redirect_url"])
        self.assertTrue(any("redirect link" in w for w in result["warnings"]))
        md = render_markdown(read_object(self.store, self.page_id))
        self.assertIn("검색 리디렉션 링크(원문 주소 아님)", md)

    def test_failed_repeat_keeps_an_earlier_success(self) -> None:
        self.mode("success")
        self.research()
        self.mode("timeout")
        with self.assertRaises(ResearchFailed):
            self.research(retry_succeeded=True)
        row = self.one("SELECT status, error_kind FROM research_requests")
        self.assertEqual(tuple(row), ("succeeded", "timeout"))
        self.assertEqual(self.one("SELECT research_status FROM runs WHERE id = ?", self.run_id)[0], "succeeded")
        self.assertEqual(self.one("SELECT COUNT(*) FROM sources WHERE kind = 'web'")[0], 2)

    def test_retry_does_not_duplicate_evidence(self) -> None:
        self.mode("success")
        self.research()
        self.research(retry_succeeded=True)
        self.assertEqual(self.one("SELECT COUNT(*) FROM sources WHERE kind = 'web'")[0], 2)

    def test_prompt_carries_question_and_context_only(self) -> None:
        self.mode("success")
        self.research()
        call = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("Does the SQLite FTS5 trigram tokenizer support substring matching?", call["prompt"])
        self.assertIn("Korean substring search", call["prompt"])
        self.assertIn("Do not run shell commands", call["prompt"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())

    def test_question_attached_to_an_existing_page(self) -> None:
        self.mode("success")
        done = subprocess.run(
            [sys.executable, "-m", "buildlog", "--store", str(self.store), "research", "--page", self.page_id,
             "--question", "What does the trigram tokenizer support?", "--timeout", "30", "--format", "json"],
            capture_output=True, env={**os.environ, "PYTHONPATH": str(SRC), "BUILDLOG_AGY_COMMAND": json.dumps(FAKE)},
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        results = json.loads(done.stdout.decode("utf-8"))["data"]["results"]
        # Only the new question runs; the page's other pending request waits for its own run.
        self.assertEqual([r["question"] for r in results], ["What does the trigram tokenizer support?"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM research_requests")[0], 2)

    def test_non_executable_agy_is_a_research_failure(self) -> None:
        with self.assertRaises(ResearchFailed) as raised:
            run_research(self.store, run_id=self.run_id, agy=[str(FIXTURES / "fake_agy.py")], timeout=30)
        self.assertEqual(raised.exception.data["results"][0]["error_kind"], "not_installed")


class ResearchCliTests(unittest.TestCase):
    def test_exit_code_5_and_markdown_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "c.sqlite3"
            saved = ingest(store, plan_with_research())
            mode = Path(d) / "mode.txt"
            mode.write_text("denied", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(SRC), "FAKE_AGY_MODE_FILE": str(mode), "BUILDLOG_AGY_COMMAND": json.dumps(FAKE), "BUILDLOG_OFFLINE": "1"}
            done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(store), "research", "--run", saved["run_id"],
                                   "--timeout", "30"], capture_output=True, env=env)
            self.assertEqual(done.returncode, 5, done.stderr.decode("utf-8", "replace"))
            self.assertIn("RESEARCH_FAILED", json.loads(done.stderr.decode("utf-8"))["error"])
            self.assertIn("실패 원인(permission)", done.stdout.decode("utf-8"))
            mode.write_text("success", encoding="utf-8")
            done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(store), "research", "--run", saved["run_id"],
                                   "--timeout", "30"], capture_output=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
            self.assertIn("새로 연결한 근거: 2건", done.stdout.decode("utf-8"))


class CompatibilityTests(unittest.TestCase):
    def test_plans_without_research_keep_their_hash(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_knowledge import demo_plan

        plan, digest = normalize_plan(demo_plan())
        expected = db.sha256({k: v for k, v in plan.items() if k not in {"warnings", "research", "prior_check"}})
        self.assertEqual(digest, expected)


if __name__ == "__main__":
    unittest.main()
