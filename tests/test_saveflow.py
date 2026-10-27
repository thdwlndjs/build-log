"""Phase 4 save flow: what was looked up before saving, follow-ups, concepts, and index updates."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from buildlog.db import ValidationError
from buildlog.read import read_object, render_markdown
from buildlog.search import query
from buildlog.store import ingest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_review import duplicates_plan  # noqa: E402


def follow_up(key: str, prior: dict[str, Any] | None, link_to: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "idempotency_key": key,
        "sources": [{"ref": "c", "kind": "conversation", "locator": "test", "accessible_excerpt": "후속"}],
        "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "캐시를 지워도 재발"}],
        "pages": [{"ref": "n", "type": "experience", "title": "빌드 캐시 오류 재발: 권한 문제", "summary_md": "캐시 삭제로는 재발, 권한 수정으로 해결",
                   "body_md": "## 문제\n\n재발", "evidence": ["e"]}],
    }
    if link_to:
        plan["relations"] = [{"source": "n", "target": link_to, "type": "SUPERSEDES", "explanation_md": "원인 정정", "evidence": ["e"], "status": "extracted"}]
    if prior is not None:
        plan["prior_check"] = prior
    plan.update(extra or {})
    return plan


class SaveFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "f.sqlite3"
        ingest(self.store, duplicates_plan())
        with closing(sqlite3.connect(self.store)) as c:
            self.a = c.execute("SELECT id FROM pages WHERE title = '중복 A: 빌드 캐시 오류'").fetchone()[0]
            self.concept = c.execute("SELECT id FROM pages WHERE title = '빌드 캐시'").fetchone()[0]
            self.toc = c.execute("SELECT id, version FROM pages WHERE title = '빌드 목차'").fetchone()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_lookup_is_reported(self) -> None:
        result = ingest(self.store, follow_up("k1", None, None))
        self.assertTrue(any("prior_check missing" in w for w in result["warnings"]))

    def test_follow_up_with_concept_and_index_update(self) -> None:
        prior = {"query": "빌드 캐시 오류 재발", "terms": ["빌드 캐시", "권한"], "candidates": [self.a], "decision": "follow_up",
                 "reason": "같은 증상의 후속 시도이며 원인이 달랐다"}
        extra = {
            "relations": [
                {"source": "n", "target": self.a, "type": "SUPERSEDES", "explanation_md": "원인 정정", "evidence": ["e"], "status": "extracted"},
                {"source": "n", "target": self.concept, "type": "USES", "explanation_md": "캐시 개념", "evidence": ["e"], "status": "extracted"},
            ],
            "index_members": [{"index": self.toc[0], "child": "n", "position": 2}],
        }
        extra["pages"] = follow_up("k", None, None)["pages"] + [
            {"id": self.toc[0], "expected_version": self.toc[1], "summary_md": "빌드 캐시 오류와 재발(권한 원인) 기록", "reason": "새 후속 기록"}]
        result = ingest(self.store, follow_up("k2", prior, None, extra))
        self.assertEqual([w for w in result["warnings"] if "prior_check" in w], [])
        self.assertEqual(result["prior_check"]["decision"], "follow_up")
        self.assertEqual(result["prior_check"]["candidates"], [self.a])
        new_id = next(p["id"] for p in result["created"]["pages"] if p.get("ref") == "n")
        md = render_markdown(read_object(self.store, new_id))
        self.assertIn("—SUPERSEDES→ 중복 A", md)
        self.assertIn("—USES→ 빌드 캐시", md)
        self.assertIn("상위 목차", md)
        toc_md = render_markdown(read_object(self.store, self.toc[0]))
        self.assertIn("재발(권한 원인)", toc_md)
        # The next lookup brings the new record together with the one it corrects.
        found = query(self.store, "빌드 캐시 오류 재발")
        self.assertIn(new_id, [r["id"] for r in found["results"]])
        self.assertIn("—SUPERSEDES→", found["markdown"])

    def test_decision_mismatch_and_unknown_candidate(self) -> None:
        prior = {"query": "q", "candidates": [self.a], "decision": "update", "reason": "r"}
        result = ingest(self.store, follow_up("k3", prior, None))
        self.assertTrue(any("says update" in w for w in result["warnings"]))
        bad = {"query": "q", "candidates": ["00000000-0000-0000-0000-000000000000"], "decision": "new", "reason": "r"}
        with self.assertRaises(ValidationError):
            ingest(self.store, follow_up("k4", bad, None))

    def test_update_instead_of_duplicate(self) -> None:
        prior = {"query": "빌드 캐시 오류", "candidates": [self.a], "decision": "update", "reason": "같은 기록에 결과만 추가"}
        plan = {"idempotency_key": "k5", "prior_check": prior,
                "sources": [{"ref": "c", "kind": "conversation", "locator": "t", "accessible_excerpt": "x"}],
                "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "다시 확인"}],
                "pages": [{"id": self.a, "expected_version": 1, "body_md": "## 문제\n\nA\n\n## 후속\n\n다시 확인함", "summary_md": "캐시 삭제로 해결(재확인)",
                           "reason": "후속 결과", "evidence": ["e"]}]}
        before = self.count_pages()
        result = ingest(self.store, plan)
        self.assertEqual(self.count_pages(), before)
        self.assertEqual([u["id"] for u in result["updated"]], [self.a])
        self.assertEqual([w for w in result["warnings"] if "prior_check" in w], [])

    def count_pages(self) -> int:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
