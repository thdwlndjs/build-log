"""Phase 2.2: search, neighbors, join paths, and the query Markdown."""
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
from buildlog.index import rebuild
from buildlog.search import BudgetTooSmall, neighbors, paths, query, query_terms
from buildlog.store import ingest, update_object

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_knowledge import demo_plan  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SRC = Path(__file__).resolve().parents[1] / "src"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SearchCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.store = Path(cls.temp_dir.name) / "s.sqlite3"
        ingest(cls.store, demo_plan())
        ingest(cls.store, load("experiences.json"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def page_id(self, title: str) -> str:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute("SELECT id FROM pages WHERE title = ?", (title,)).fetchone()[0]


class RegressionSetTests(SearchCorpus):
    def test_cases(self) -> None:
        report = []
        for case in load("search_cases.json")["cases"]:
            with self.subTest(case["name"]):
                result = query(self.store, case["query"], terms=case.get("terms"), scope=case.get("scope"),
                               limit=case.get("limit", 5))
                titles = [r["title"] for r in result["results"]]
                report.append(f"{case['name']}: {titles[:3]}")
                markdown = result["markdown"]
                if case.get("expect_no_results"):
                    self.assertEqual(titles, [])
                    self.assertIn("관련 기록을 찾지 못했다", markdown)
                    continue
                if "expect_first" in case:
                    self.assertEqual(titles[0] if titles else None, case["expect_first"])
                for title in case.get("expect_in_results", []):
                    self.assertIn(title, titles)
                for text in case.get("expect_markdown", []):
                    self.assertIn(text, markdown)
                for title in case.get("expect_out_of_scope", []):
                    self.assertIn(title, [r["title"] for r in result["out_of_scope"]])
                    self.assertNotIn(title, titles)
                rules = [r["instruction_md"] for r in result["rules"]]
                for text in case.get("expect_rules", []):
                    self.assertTrue(any(text in r for r in rules), f"rule {text!r} missing from {rules}")
                    self.assertIn(text, markdown)
                found = {t["scope"]: t["in_scope"] for t in result["terms_found"]}
                for scope in case.get("expect_terms_in_scope", []):
                    self.assertIs(found.get(scope), True)
                for scope in case.get("expect_terms_other_scope", []):
                    self.assertIs(found.get(scope), False)
        print("\n" + "\n".join(report))


class QueryBehaviourTests(SearchCorpus):
    def test_rules_come_first_and_survive_omission(self) -> None:
        full = query(self.store, "사업장별 센서 측정값", scope="demo_mes", terms=["sensor_reading", "equipment"], limit=3)
        self.assertGreaterEqual(len(full["results"]), 2)
        md = full["markdown"]
        self.assertLess(md.index("## 반드시 지킬 규칙"), md.index("## 기록 1."))
        first_record = md.index("## 기록 2.")
        # A budget that fits the first record but not the rest omits whole records, never their rules.
        tight = query(self.store, "사업장별 센서 측정값", scope="demo_mes", terms=["sensor_reading", "equipment"], limit=3,
                      max_bytes=len(md[:first_record].encode("utf-8")) + 600)
        self.assertTrue(tight["omitted"])
        self.assertIn("## 생략한 기록", tight["markdown"])
        self.assertLessEqual(tight["bytes"], len(md[:first_record].encode("utf-8")) + 600)
        for rule in full["rules"]:
            self.assertIn(rule["instruction_md"], tight["markdown"])

    def test_join_path_tables_bring_their_rules(self) -> None:
        result = query(self.store, "설비 용량", scope="demo_mes", terms=["capacity_kw"], limit=1)
        self.assertEqual(result["results"][0]["title"], "equipment.capacity_kw")
        self.assertIn("### 사업장별 센서 측정값", result["markdown"])
        self.assertIn("reading_time 기간 조건을 반드시 건다", result["markdown"])
        self.assertIn("함께 전달한 조인 경로가 지나는 테이블", result["markdown"])

    def test_body_headings_nest_under_the_record(self) -> None:
        md = query(self.store, "ORA-12541")["markdown"]
        self.assertIn("\n### 환경\n", md)
        self.assertNotIn("\n## 환경\n", md)

    def test_budget_too_small_is_reported_not_truncated(self) -> None:
        with self.assertRaises(BudgetTooSmall) as raised:
            query(self.store, "sensor_reading", scope="demo_mes", max_bytes=400)
        self.assertGreater(raised.exception.data["needed_bytes"], 400)
        self.assertTrue(raised.exception.data["record_ids"])

    def test_markdown_alone_explains_the_record(self) -> None:
        md = query(self.store, "ORA-12541")["markdown"]
        for text in ("### 문제", "### 환경", "### Mitigation", "### RCA 상태", "ORA-12541: TNS:no listener", "### 근거"):
            self.assertIn(text, md)

    def test_short_terms_use_titles_and_aliases(self) -> None:
        result = query(self.store, "pip")
        self.assertTrue(result["results"])
        alias = query(self.store, "작업자 코드")
        self.assertIn("sensor_reading.operator_code", [r["title"] for r in alias["results"]])

    def test_fts_syntax_in_question_is_literal(self) -> None:
        # FTS operators and stray quotes are searched as plain text, never parsed as query syntax.
        result = query(self.store, 'NEAR("zz" yy) * "unbalanced', terms=['"quoted', "zz:yy*"])
        self.assertIn('"quoted', result["terms"])
        self.assertEqual(result["results"], [])

    def test_particles_are_stripped(self) -> None:
        self.assertEqual(query_terms("파티션을 리스너가"), ["파티션", "리스너"])

    def test_update_is_searchable_immediately(self) -> None:
        page_id = self.page_id("WSL에서 docker 명령이 없다고 나옴")
        update_object(self.store, page_id, 1, {"body_md": "## 문제\n\nWSL에서 docker 없음. 재부팅 후 zebracorn 표시."}, "test")
        self.assertEqual(query(self.store, "zebracorn")["results"][0]["id"], page_id)

    def test_rebuild_matches_incremental_index(self) -> None:
        with closing(sqlite3.connect(self.store)) as c:
            before = sorted(c.execute("SELECT page_id, title, aliases, summary, body, extra FROM search_index").fetchall())
            c.row_factory = sqlite3.Row
            rebuild(c)
            c.row_factory = None
            after = sorted(c.execute("SELECT page_id, title, aliases, summary, body, extra FROM search_index").fetchall())
            c.rollback()
        self.assertEqual(before, after)


class PathAndNeighborTests(SearchCorpus):
    def test_stored_path_comes_first(self) -> None:
        result = paths(self.store, "demo_mes", "site", "sensor_reading")
        self.assertEqual([p["title"] for p in result["stored"]], ["사업장별 센서 측정값"])
        self.assertEqual(result["candidates"], [])

    def test_candidates_are_marked_as_estimates(self) -> None:
        from buildlog.search import render_paths

        result = paths(self.store, "demo_mes", "employee", "equipment")
        self.assertFalse(result["stored"])
        hops = [[e["to_name"] for e in trail] for trail in result["candidates"]]
        self.assertIn(["site", "equipment"], hops)
        self.assertIn(["sensor_reading", "equipment"], hops)
        statuses = {tuple(e["status"] for e in trail) for trail in result["candidates"]}
        self.assertIn(("inferred", "extracted"), statuses)
        self.assertIn("**추정 후보**", render_paths(result))
        self.assertLessEqual(max(len(t) for t in result["candidates"]), 4)

    def test_neighbors(self) -> None:
        page_id = self.page_id("sensor_reading (demo_mes)")
        out = neighbors(self.store, page_id, "JOINS_TO", "out")
        self.assertEqual(sorted(n["other_title"] for n in out["neighbors"]), ["employee (demo_mes)", "equipment (demo_mes)"])
        incoming = neighbors(self.store, page_id, direction="in")
        self.assertTrue(all(n["direction"] == "in" for n in incoming["neighbors"]))


class CliQueryTests(SearchCorpus):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([sys.executable, "-m", "buildlog", "--store", str(self.store), *args],
                              capture_output=True, env={**os.environ, "PYTHONPATH": str(SRC)})

    def test_query_markdown_and_budget_error(self) -> None:
        done = self.run_cli("query", "Git Bash 한글 깨짐")
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8"))
        self.assertIn("# Buildlog 조회 결과", done.stdout.decode("utf-8"))
        tight = self.run_cli("query", "sensor_reading", "--max-bytes", "300")
        self.assertEqual(tight.returncode, 2)
        error = json.loads(tight.stderr.decode("utf-8"))
        self.assertIn("BUDGET_TOO_SMALL", error["error"])
        self.assertIn("needed_bytes", error["data"])

    def test_query_from_json_input(self) -> None:
        spec = Path(self.temp_dir.name) / "q.json"
        spec.write_text(json.dumps({"query": "인증서 오류", "terms": ["CERTIFICATE_VERIFY_FAILED"], "scope": "프록시"}, ensure_ascii=False), encoding="utf-8")
        done = self.run_cli("query", "--input", str(spec), "--format", "json")
        data = json.loads(done.stdout.decode("utf-8"))["data"]
        self.assertEqual(data["results"][0]["title"], "사내 프록시에서 pip 인증서 오류: 루트 인증서 지정으로 해결")

    def test_paths_and_reindex_commands(self) -> None:
        done = self.run_cli("paths", "--system", "demo_mes", "--from", "site", "--to", "sensor_reading")
        self.assertIn("사업장별 센서 측정값", done.stdout.decode("utf-8"))
        done = self.run_cli("reindex")
        self.assertEqual(json.loads(done.stdout.decode("utf-8"))["status"], "ok")


class MigrationTests(unittest.TestCase):
    def test_v2_store_gets_index_on_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "v2.sqlite3"
            with closing(sqlite3.connect(store)) as c:
                c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                for version in (0, 1):
                    for statement in db.MIGRATIONS[version]:
                        c.execute(statement)
                c.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
                c.execute("INSERT INTO pages VALUES ('p1', 'experience', 'quokka 문제', 's', 'b', 'active', 1, 't', 't')")
                c.commit()
            self.assertEqual(query(store, "quokka")["results"][0]["id"], "p1")


if __name__ == "__main__":
    unittest.main()
