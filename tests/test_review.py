"""Phase 3: edits, unlinking, feedback, merges, the review server and its process control."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from buildlog.db import ConflictError, ValidationError
from buildlog.graph import graph
from buildlog.read import read_object, render_markdown
from buildlog.search import neighbors, query
from buildlog.server import start_in_thread
from buildlog.store import ingest
from buildlog.update import add_feedback, list_feedback, merge_pages, unlink_relation, update_object

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parents[0] / "src"
sys.path.insert(0, str(TESTS))
from test_knowledge import demo_plan  # noqa: E402


def duplicates_plan() -> dict[str, Any]:
    return {
        "idempotency_key": "test:duplicates",
        "sources": [{"ref": "c", "kind": "conversation", "locator": "test", "accessible_excerpt": "중복"}],
        "evidence": [{"ref": "ea", "source": "c", "evidence_kind": "observed", "excerpt": "A에서 본 출력"},
                     {"ref": "eb", "source": "c", "evidence_kind": "observed", "excerpt": "B에서 본 출력"}],
        "pages": [
            {"ref": "a", "type": "experience", "title": "중복 A: 빌드 캐시 오류", "summary_md": "캐시 삭제로 해결", "body_md": "## 문제\n\nA", "evidence": ["ea"]},
            {"ref": "b", "type": "experience", "title": "중복 B: 빌드 캐시 오류(재발)", "summary_md": "캐시 삭제로 해결", "body_md": "## 문제\n\nB", "evidence": ["eb"]},
            {"ref": "x", "type": "concept", "title": "빌드 캐시", "summary_md": "개념", "body_md": "개념 설명", "evidence": ["ea"]},
            {"ref": "toc", "type": "index", "title": "빌드 목차", "summary_md": "목차", "body_md": "목차"},
        ],
        "index_members": [{"index": "toc", "child": "b", "position": 1}, {"index": "toc", "child": "a", "position": 2},
                          {"index": "toc", "child": "x", "position": 3}],
        "terms": [{"expression": "캐시 무효화", "scope": "test:build", "page": "b", "definition_md": "정의", "evidence": ["eb"], "status": "extracted"}],
        "relations": [
            {"source": "a", "target": "x", "type": "USES", "explanation_md": "A uses", "evidence": ["ea"], "status": "extracted"},
            {"source": "b", "target": "x", "type": "USES", "explanation_md": "B uses", "evidence": ["eb"], "status": "extracted"},
            {"source": "b", "target": "a", "type": "RELATED_TO", "explanation_md": "same problem", "evidence": ["eb"], "status": "inferred"},
            {"source": "x", "target": "b", "type": "SUPPORTS", "explanation_md": "concept supports B", "evidence": ["eb"], "status": "inferred"},
        ],
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "r.sqlite3"
        self.demo = ingest(self.store, demo_plan())
        self.dups = ingest(self.store, duplicates_plan())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def page(self, title: str) -> sqlite3.Row:
        return self.q("SELECT * FROM pages WHERE title = ?", title)[0]

    def relation(self, source: str, rel_type: str) -> sqlite3.Row:
        return self.q("""SELECT r.* FROM relations r JOIN pages p ON p.id = r.source_id WHERE p.title = ? AND r.type = ?""", source, rel_type)[0]


class EditTests(Base):
    def test_body_only_edit_marks_summary_dirty_until_summary_changes(self) -> None:
        p = self.page("중복 A: 빌드 캐시 오류")
        result = update_object(self.store, p["id"], 1, {"body_md": "## 문제\n\n새 본문 zebrabody"}, "review")
        self.assertTrue(result["summary_dirty"])
        md = render_markdown(read_object(self.store, p["id"]))
        self.assertIn("요약이 최근 본문 수정 전 내용이다", md)
        self.assertEqual(query(self.store, "zebrabody")["results"][0]["id"], p["id"])  # search sees the new body at once
        result = update_object(self.store, p["id"], 2, {"summary_md": "새 요약"}, "review")
        self.assertFalse(result["summary_dirty"])
        self.assertNotIn("요약이 최근 본문 수정 전 내용이다", render_markdown(read_object(self.store, p["id"])))

    def test_relation_type_and_direction(self) -> None:
        rel = self.relation("중복 B: 빌드 캐시 오류(재발)", "RELATED_TO")
        update_object(self.store, rel["id"], 1, {"type": "SUPERSEDES", "reverse": True}, "B is older")
        row = self.q("SELECT * FROM relations WHERE id = ?", rel["id"])[0]
        self.assertEqual((row["type"], row["source_id"], row["target_id"]), ("SUPERSEDES", rel["target_id"], rel["source_id"]))
        # 빌드 캐시 —SUPPORTS→ B turned into B —USES→ 빌드 캐시 would duplicate an existing relation.
        supports = self.relation("빌드 캐시", "SUPPORTS")
        with self.assertRaises(ConflictError):
            update_object(self.store, supports["id"], 1, {"type": "USES", "reverse": True}, "dup")

    def test_term_aliases_are_replaced(self) -> None:
        term = self.q("SELECT * FROM term_senses WHERE scope = 'test:build'")[0]
        update_object(self.store, term["id"], 1, {"aliases": ["cache bust", "무효화"]}, "aliases")
        aliases = sorted(r[0] for r in self.q("SELECT expression FROM aliases WHERE term_sense_id = ?", term["id"]))
        self.assertEqual(aliases, ["cache bust", "무효화", "캐시 무효화"])


class UnlinkTests(Base):
    def test_unlink_hides_relation_keeps_row_and_is_not_restored(self) -> None:
        guide = self.page("데모 MES 데이터 사용 가이드")
        rel = self.q("SELECT * FROM relations WHERE source_id = ? AND type = 'SUPPORTS'", guide["id"])[0]
        unlink_relation(self.store, rel["id"], 1, "출처 관계가 아니다")
        self.assertNotIn("—SUPPORTS→", render_markdown(read_object(self.store, guide["id"])))
        self.assertEqual(neighbors(self.store, guide["id"], "SUPPORTS")["neighbors"], [])
        self.assertIn("연결 해제됨", render_markdown(read_object(self.store, rel["id"])))
        row = self.q("SELECT removed_reason, version FROM relations WHERE id = ?", rel["id"])[0]
        self.assertEqual((row["removed_reason"], row["version"]), ("출처 관계가 아니다", 2))
        self.assertEqual(self.q("SELECT COUNT(*) FROM object_evidence WHERE object_id = ?", rel["id"])[0][0] > 0, True)
        with self.assertRaises(ValidationError):
            update_object(self.store, rel["id"], 2, {"explanation_md": "x"}, "edit removed")
        self.assertEqual(neighbors(self.store, guide["id"], "SUPPORTS")["neighbors"], [])

    def test_unlinked_foreign_key_is_not_brought_back_by_reingest(self) -> None:
        table = self.page("sensor_reading (demo_mes)")
        fk = self.q("SELECT * FROM relations WHERE source_id = ? AND type = 'JOINS_TO' AND status = 'extracted'", table["id"])[0]
        unlink_relation(self.store, fk["id"], 1, "검토 결과 이 연결은 쓰지 않는다")
        ddl = demo_plan()["sources"][0]
        result = ingest(self.store, {"idempotency_key": "test:ddl-again", "sources": [ddl]})
        self.assertTrue(any("was unlinked" in w for w in result["warnings"]))
        self.assertEqual(self.q("SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND type = 'JOINS_TO' AND removed_at IS NULL",
                                fk["source_id"], fk["target_id"])[0][0], 0)

    def test_stale_unlink_conflicts(self) -> None:
        rel = self.relation("중복 A: 빌드 캐시 오류", "USES")
        update_object(self.store, rel["id"], 1, {"explanation_md": "changed"}, "edit")
        with self.assertRaises(ConflictError):
            unlink_relation(self.store, rel["id"], 1, "stale")


class FeedbackTests(Base):
    def test_feedback_is_linked_to_the_revision_that_addresses_it(self) -> None:
        p = self.page("중복 A: 빌드 캐시 오류")
        fb = add_feedback(self.store, p["id"], 1, "원인이 캐시가 아니라 권한이다")
        with self.assertRaises(ConflictError):
            add_feedback(self.store, p["id"], 9, "old version")
        update_object(self.store, p["id"], 1, {"body_md": "권한 문제"}, "피드백 반영", feedback_id=fb["id"])
        row = list_feedback(self.store, p["id"])["feedback"][0]
        revision = self.q("SELECT object_id, version FROM revisions WHERE id = ?", row["resulting_revision"])[0]
        self.assertEqual((row["status"], revision["object_id"], revision["version"]), ("addressed", p["id"], 2))


class MergeTests(Base):
    def merge(self) -> dict[str, Any]:
        a, b = self.page("중복 A: 빌드 캐시 오류"), self.page("중복 B: 빌드 캐시 오류(재발)")
        return merge_pages(self.store, [b["id"]], a["id"], {a["id"]: a["version"], b["id"]: b["version"]}, "같은 문제")

    def test_merge_moves_references_and_keeps_sources(self) -> None:
        a, b = self.page("중복 A: 빌드 캐시 오류"), self.page("중복 B: 빌드 캐시 오류(재발)")
        report = self.merge()
        # B→A RELATED_TO becomes a self-loop and is dropped; B —USES→ 빌드 캐시 folds into A's; 빌드 캐시 —SUPPORTS→ B moves to A.
        self.assertEqual((report["relations_dropped_as_self_loop"], report["relations_folded"], report["relations_moved"]), (1, 1, 1))
        uses = self.q("SELECT id FROM relations WHERE source_id = ? AND type = 'USES' AND removed_at IS NULL", a["id"])
        self.assertEqual(len(uses), 1)
        evidence = {r[0] for r in self.q("SELECT e.excerpt FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id WHERE oe.object_id = ?", uses[0]["id"])}
        self.assertEqual(evidence, {"A에서 본 출력", "B에서 본 출력"})
        self.assertEqual(self.q("SELECT page_id FROM term_senses WHERE scope = 'test:build'")[0][0], a["id"])
        # B's place in the index is folded into A, which was already there.
        self.assertEqual({r[0] for r in self.q("SELECT child_page_id FROM index_members WHERE index_page_id = ?", self.page("빌드 목차")["id"])},
                         {a["id"], self.page("빌드 캐시")["id"]})
        page_evidence = {r[0] for r in self.q("SELECT e.excerpt FROM page_sources ps JOIN evidence e ON e.id = ps.evidence_id WHERE ps.page_id = ?", a["id"])}
        self.assertIn("B에서 본 출력", page_evidence)
        record = read_object(self.store, b["id"])
        self.assertEqual((record["page"]["id"], record["redirected_from"]), (a["id"], b["id"]))
        self.assertEqual(self.q("SELECT status FROM pages WHERE id = ?", b["id"])[0][0], "superseded")
        self.assertNotIn(b["id"], [r["id"] for r in query(self.store, "빌드 캐시 오류")["results"]])
        self.assertNotIn(b["id"], [n["id"] for n in graph(self.store, focus=a["id"])["nodes"]])
        # A plan that still names B lands on A.
        plan = {"idempotency_key": "test:after-merge",
                "sources": [{"ref": "c", "kind": "conversation", "locator": "t", "accessible_excerpt": "x"}],
                "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "follow-up"}],
                "pages": [{"ref": "n", "type": "experience", "title": "후속", "summary_md": "s", "body_md": "b", "evidence": ["e"]}],
                "relations": [{"source": "n", "target": b["id"], "type": "RELATED_TO", "explanation_md": "follow-up", "evidence": ["e"], "status": "inferred"}],
                "index_members": [{"index": self.page("빌드 목차")["id"], "child": "n", "position": 9}]}
        ingest(self.store, plan)
        self.assertEqual(self.q("SELECT target_id FROM relations WHERE explanation_md = 'follow-up'")[0][0], a["id"])

    def test_merge_needs_current_versions_and_rejects_schema_pages(self) -> None:
        a, b = self.page("중복 A: 빌드 캐시 오류"), self.page("중복 B: 빌드 캐시 오류(재발)")
        with self.assertRaises(ConflictError):
            merge_pages(self.store, [b["id"]], a["id"], {a["id"]: a["version"], b["id"]: 99}, "x")
        table = self.page("site (demo_mes)")
        with self.assertRaises(ValidationError):
            merge_pages(self.store, [b["id"]], table["id"], {table["id"]: 1, b["id"]: 1}, "x")
        self.assertEqual(self.q("SELECT COUNT(*) FROM page_redirects")[0][0], 0)


class ServerTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.server = start_in_thread(self.store)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        conn = HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.server.port}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def test_guards(self) -> None:
        p = self.page("중복 A: 빌드 캐시 오류")
        body = {"expected_version": 1, "patch": {"title": "hacked"}}
        self.assertEqual(self.call("GET", "/api/health", headers={"Host": "attacker.test"})[0], 403)
        self.assertEqual(self.call("PATCH", f"/api/pages/{p['id']}", body)[0], 403)
        self.assertEqual(self.call("PATCH", f"/api/pages/{p['id']}", body, {"X-Buildlog-Token": "wrong"})[0], 403)
        self.assertEqual(self.call("PATCH", f"/api/pages/{p['id']}", body,
                                   {"X-Buildlog-Token": self.server.token, "Origin": "http://attacker.test"})[0], 403)
        self.assertEqual(self.call("GET", "/static/../update.py")[0], 404)
        self.assertEqual(self.page("중복 A: 빌드 캐시 오류")["title"], "중복 A: 빌드 캐시 오류")

    def test_route_type_and_errors(self) -> None:
        rel = self.relation("중복 A: 빌드 캐시 오류", "USES")
        token = {"X-Buildlog-Token": self.server.token}
        status, data = self.call("PATCH", f"/api/pages/{rel['id']}", {"expected_version": 1, "patch": {"explanation_md": "x"}}, token)
        self.assertEqual(status, 404)
        status, data = self.call("PATCH", f"/api/relations/{rel['id']}", {"expected_version": 5, "patch": {"explanation_md": "x"}}, token)
        self.assertEqual((status, data["kind"]), (409, "conflict"))
        status, data = self.call("GET", "/api/graph?cap_nodes=3")
        self.assertEqual(len(data["nodes"]), 3)
        self.assertGreater(data["truncated"]["nodes"], 0)

    def test_page_and_bundled_assets(self) -> None:
        status, raw = self.call("GET", "/")
        html = raw.decode("utf-8")
        self.assertIn(self.server.token, html)
        self.assertNotIn("https://", html)  # no CDN: every asset is local
        for asset in ("app.js", "app.css", "vendor/cytoscape-3.30.2.min.js", "vendor/marked-12.0.2.min.js", "vendor/purify-3.1.6.min.js",
                      "vendor/lucide-0.460.0.svg"):
            self.assertEqual(self.call("GET", f"/static/{asset}")[0], 200, asset)


class WorkspaceDetailTests(Base):
    """The workspace shows title, summary, relations, index and evidence in their own places (spec §10.2);
    document_md carries the rest, and the CLI Markdown keeps everything."""

    def test_document_markdown_holds_the_page_content(self) -> None:
        table = self.page("equipment (demo_mes)")
        _, detail = self.call("GET", f"/api/objects/{table['id']}")
        doc = detail["document_md"]
        for kept in ("## 테이블 구조", "capacity_kw", "## 필수 조건·금칙·미지원 연산"):
            self.assertIn(kept, doc)
        for elsewhere in ("# equipment (demo_mes)", "## 요약", "## 관계", "## 상위 목차", "## 근거"):
            self.assertNotIn(elsewhere, doc)
        for kept in ("# equipment (demo_mes)", "## 요약", "## 테이블 구조", "## 상위 목차"):
            self.assertIn(kept, detail["markdown"])
        evidence = detail["record"]["evidence"]
        self.assertTrue(evidence and all(e["source_id"] for e in evidence))
        relation = self.q("SELECT id FROM relations LIMIT 1")[0]["id"]
        self.assertIsNone(self.call("GET", f"/api/objects/{relation}")[1]["document_md"])

    def call(self, method: str, path: str) -> tuple[int, Any]:
        server = start_in_thread(self.store)
        try:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=10)
            conn.request(method, path, headers={"Host": f"127.0.0.1:{server.port}"})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            return response.status, body
        finally:
            server.shutdown()
            server.server_close()


class ServeProcessTests(unittest.TestCase):
    def test_detach_status_ingest_url_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "s.sqlite3"
            ingest(store, duplicates_plan())
            env = {**os.environ, "PYTHONPATH": str(SRC)}

            def cli(*args: str) -> dict[str, Any]:
                done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(store), *args], capture_output=True, env=env, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
                return json.loads(done.stdout.decode("utf-8"))["data"]

            port = 8899
            try:
                started = cli("serve", "--detach", "--port", str(port))
                self.assertTrue(started["running"])
                self.assertEqual(started["url"], f"http://127.0.0.1:{port}/")
                plan = Path(d) / "p.json"
                p = duplicates_plan()
                p["terms"] = []  # the same term in the same scope would conflict with the first save
                p["idempotency_key"] = "test:dup-2"
                plan.write_text(json.dumps(p), encoding="utf-8")
                saved = cli("ingest", "--input", str(plan))
                self.assertEqual(saved["graph"]["url"], f"http://127.0.0.1:{port}/?run={saved['run_id']}")
            finally:
                stopped = cli("serve", "--stop")
            self.assertFalse(stopped["running"])
            self.assertFalse(cli("serve", "--status")["running"])
            p["idempotency_key"] = "test:dup-3"
            plan.write_text(json.dumps(p), encoding="utf-8")
            self.assertIsNone(cli("ingest", "--input", str(plan))["graph"]["url"])  # no address without a server

    def test_two_stores_in_one_folder_have_their_own_server(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            stores = {name: Path(d) / f"{name}.sqlite3" for name in ("a", "b")}
            for store in stores.values():
                ingest(store, duplicates_plan())
            env = {**os.environ, "PYTHONPATH": str(SRC)}

            def cli(name: str, *args: str) -> dict[str, Any]:
                done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(stores[name]), *args],
                                      capture_output=True, env=env, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
                return json.loads(done.stdout.decode("utf-8"))["data"]

            try:
                self.assertTrue(cli("a", "serve", "--detach", "--port", "8897")["running"])
                self.assertTrue(cli("b", "serve", "--detach", "--port", "8898")["running"])
                self.assertEqual(cli("a", "serve", "--status")["url"], "http://127.0.0.1:8897/")   # not b's
                self.assertTrue(cli("a", "serve", "--stop")["stopped"])
                self.assertFalse(cli("a", "serve", "--status")["running"])
                self.assertEqual(cli("b", "serve", "--status")["url"], "http://127.0.0.1:8898/")   # b keeps running
            finally:
                cli("a", "serve", "--stop")
                cli("b", "serve", "--stop")

    def test_a_port_in_use_is_not_shared(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "s.sqlite3"
            ingest(store, duplicates_plan())
            first = start_in_thread(store)
            try:
                with self.assertRaises(OSError):
                    start_in_thread(store, port=first.port)
            finally:
                first.shutdown()
                first.server_close()


if __name__ == "__main__":
    unittest.main()
