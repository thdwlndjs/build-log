"""Phase 2.1: schema, terms, relations, join paths and rules."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from buildlog import db
from buildlog.store import ConflictError, ValidationError, ingest, initialize, read_object, render_markdown, update_object

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_mes"


def demo_plan() -> dict[str, Any]:
    return {
        "idempotency_key": "test:demo-mes",
        "sources": [
            {"ref": "ddl", "kind": "sql_ddl", "dialect": "postgresql", "system_key": "demo_mes", "path": str(DEMO / "schema.sql")},
            {"ref": "crm", "kind": "schema_json", "path": str(DEMO / "crm_schema.json")},
            {"ref": "guide", "kind": "markdown", "path": str(DEMO / "operations_guide.md")},
            {"ref": "notes", "kind": "text", "path": str(DEMO / "team_notes.txt")},
            {"ref": "readings", "kind": "sample_csv", "system_key": "demo_mes", "table": "sensor_reading",
             "path": str(DEMO / "samples" / "sensor_reading.csv")},
            {"ref": "employees", "kind": "sample_json", "system_key": "demo_mes", "table": "employee",
             "path": str(DEMO / "samples" / "employee.json")},
        ],
        "evidence": [
            {"ref": "e-period", "source": "guide", "evidence_kind": "observed",
             "excerpt": "sensor_reading을 조회할 때는 반드시 reading_time 기간 조건을 건다. 한 번에 31일을 넘기지 않는다."},
            {"ref": "e-outage", "source": "guide", "evidence_kind": "observed",
             "excerpt": "기간 조건 없이 조회하면 전체 파티션을 읽어 운영 DB가 멈춘 적이 있다."},
            {"ref": "e-fanout", "source": "guide", "evidence_kind": "observed",
             "excerpt": "equipment와 sensor_reading을 함께 조인한 뒤 capacity_kw를 합계하면 측정 건수만큼 용량이 중복 합산된다."},
            {"ref": "e-site-biz", "source": "guide", "evidence_kind": "observed",
             "excerpt": "site.biz_no는 공장(사업장) 자체의 사업자등록번호다."},
            {"ref": "e-vendor-biz", "source": "guide", "evidence_kind": "observed",
             "excerpt": "equipment.vendor_biz_no는 설비를 납품한 공급업체의 사업자등록번호다."},
            {"ref": "e-crm-biz", "source": "crm", "evidence_kind": "observed",
             "excerpt": "고객사 사업자번호. 하이픈 포함 12자리(000-00-00000)"},
            {"ref": "e-history", "source": "guide", "evidence_kind": "observed",
             "excerpt": "설비 마스터 변경 이력은 보관하지 않는다. equipment는 수정 시 현재 값을 덮어쓴다."},
            {"ref": "e-operator", "source": "notes", "evidence_kind": "reported",
             "excerpt": "operator_code에는 employee.emp_code 값을 넣는다고 들었다. 하지만 FK는 걸려 있지 않다."},
            {"ref": "e-orphan", "source": "readings", "evidence_kind": "observed", "excerpt": "103,2026-10-01T00:00:00+09:00,80.1,E0999"},
            {"ref": "e-stream", "source": "notes", "evidence_kind": "observed",
             "excerpt": "센서 데이터를 실시간으로 받는 스트리밍 API가 있는지는 아직 확인하지 못했다."},
        ],
        "pages": [
            {"ref": "guide-page", "type": "source", "title": "데모 MES 데이터 사용 가이드",
             "summary_md": "센서 조회 기간 규칙, 사업장 집계 주의점, 사업자번호의 두 의미, 변경 이력 부재.",
             "body_md": "운영 가이드 요약.", "evidence": ["e-period", "e-fanout"]},
            {"ref": "toc", "type": "index", "title": "데모 MES 목차", "summary_md": "데모 MES 지식 목차", "body_md": "목차"},
        ],
        "index_members": [
            {"index": "toc", "child": "guide-page", "position": 1},
            {"index": "toc", "child": "demo_mes:sensor_reading", "position": 2},
        ],
        "terms": [
            {"expression": "사업자번호", "scope": "demo_mes:site.biz_no", "page": "demo_mes:site.biz_no",
             "definition_md": "공장(사업장) 자체의 사업자등록번호. 하이픈 없는 10자리.", "aliases": ["biz_no", "사업자등록번호"],
             "evidence": ["e-site-biz"], "status": "extracted"},
            {"expression": "사업자번호", "scope": "demo_mes:equipment.vendor_biz_no", "page": "demo_mes:equipment.vendor_biz_no",
             "definition_md": "설비를 납품한 공급업체의 사업자등록번호. 사업장 번호와 다르다.", "aliases": ["vendor_biz_no"],
             "evidence": ["e-vendor-biz"], "status": "extracted"},
            {"expression": "사업자번호", "scope": "demo_crm:crm.customer.biz_no", "page": "demo_crm:crm.customer.biz_no",
             "definition_md": "고객사의 사업자번호. 하이픈 포함 12자리 형식이라 MES 값과 그대로 비교할 수 없다.",
             "evidence": ["e-crm-biz"], "status": "extracted"},
        ],
        "relations": [
            {"source": "demo_mes:sensor_reading", "target": "demo_mes:employee", "type": "JOINS_TO",
             "explanation_md": "operator_code = emp_code로 연결된다고 전해 들었다. FK는 없고 고아 코드가 있다.",
             "evidence": ["e-operator", "e-orphan"], "status": "inferred"},
            {"source": "guide-page", "target": "demo_mes:sensor_reading", "type": "SUPPORTS",
             "explanation_md": "센서 조회 규칙의 출처 문서.", "evidence": ["e-period"], "status": "extracted"},
        ],
        "join_paths": [
            {"system_key": "demo_mes", "title": "사업장별 센서 측정값", "purpose": "사업장 단위로 측정값을 본다",
             "steps": [
                 {"from_table": "site", "from_columns": ["id"], "to_table": "equipment", "to_columns": ["site_id"], "join_kind": "inner"},
                 {"from_table": "equipment", "from_columns": ["id"], "to_table": "sensor_reading", "to_columns": ["equipment_id"], "join_kind": "inner"},
             ],
             "conditions_md": "sensor_reading.reading_time 기간 조건 필수(최대 31일).",
             "caveats_md": "equipment → sensor_reading은 1:N이다. 이 경로에서 capacity_kw를 합계하면 측정 건수만큼 중복 합산된다. 설비 단위로 먼저 집계한다.",
             "evidence": ["e-fanout", "e-period"], "status": "extracted"},
            {"system_key": "demo_mes", "title": "측정 작업자", "purpose": "측정값의 작업자 이름을 본다",
             "steps": [{"from_table": "sensor_reading", "from_columns": ["operator_code"], "to_table": "employee",
                        "to_columns": ["emp_code"], "join_kind": "left", "evidence": ["e-operator"]}],
             "caveats_md": "FK가 없고 employee에 없는 코드가 있어 INNER JOIN이면 행이 빠진다.",
             "evidence": ["e-operator", "e-orphan"], "status": "inferred"},
        ],
        "rules": [
            {"scope": "demo_mes:sensor_reading", "kind": "requirement", "instruction_md": "reading_time 기간 조건을 반드시 건다. 최대 31일.",
             "reason_md": "기간 없이 조회하면 전체 파티션을 읽어 운영 DB가 멈춘 적이 있다.",
             "alternative_md": "장기 추세는 daily_equipment_avg 뷰를 쓴다.", "evidence": ["e-period", "e-outage"], "status": "extracted"},
            {"scope": "demo_mes:sensor_reading", "kind": "prohibition", "instruction_md": "reading_time 기간 조건 없이 전체 조회하지 않는다.",
             "reason_md": "운영 DB 장애 이력.", "evidence": ["e-outage"], "status": "extracted"},
            {"scope": "demo_mes:equipment", "kind": "unsupported_operation", "confirmation": "confirmed",
             "instruction_md": "설비 마스터 변경 이력(과거 시점 값) 조회 기능은 없다.",
             "alternative_md": "변경 요청서(ITSM 티켓)를 확인한다.", "evidence": ["e-history"], "status": "extracted"},
            {"scope": "demo_mes:sensor_reading", "kind": "unsupported_operation", "confirmation": "unconfirmed",
             "instruction_md": "실시간 스트리밍 API 존재 여부는 확인되지 않았다. 있다고 가정하지 않는다.",
             "evidence": ["e-stream"], "status": "inferred"},
        ],
    }


def experience_plan() -> dict[str, Any]:
    return {
        "idempotency_key": "test:config",
        "sources": [{"ref": "chat", "kind": "conversation", "locator": "test conversation", "accessible_excerpt": "PATH 문제"}],
        "evidence": [{"ref": "e1", "source": "chat", "evidence_kind": "observed", "excerpt": "which: no agy"}],
        "pages": [{"ref": "p", "type": "experience", "title": "CLI가 PATH에 없음", "summary_md": "절대 경로로 해결",
                   "body_md": "## 문제\n\nPATH에 없다.", "evidence": ["e1"]}],
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "k.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def query(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def page_by_title(self, title: str) -> str:
        return self.query("SELECT id FROM pages WHERE title = ?", title)[0]["id"]

    def markdown(self, object_id: str) -> str:
        return render_markdown(read_object(self.store, object_id))


class DemoKnowledgeTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.result = ingest(self.store, demo_plan())

    def test_ddl_is_read_with_honest_partial_status(self) -> None:
        ddl = next(s for s in self.result["sources"] if s["ref"] == "ddl")
        self.assertEqual(ddl["parse_status"], "partial")
        messages = [d for d in ddl["diagnostics"] if d["severity"] == "unsupported"]
        self.assertEqual(len(messages), 1)
        self.assertIn("CREATE VIEW", messages[0]["text"])
        self.assertTrue(any("DDL read partially" in w for w in self.result["warnings"]))

    def test_tables_columns_and_foreign_keys(self) -> None:
        tables = self.query("SELECT object_name FROM schema_objects WHERE object_type = 'table' ORDER BY object_name")
        self.assertEqual([r[0] for r in tables], ["customer", "employee", "equipment", "sales_order", "sensor_reading", "site"])
        fk = self.query("""SELECT r.status FROM relations r JOIN pages a ON a.id = r.source_id JOIN pages b ON b.id = r.target_id
                           WHERE a.title = 'sensor_reading (demo_mes)' AND b.title = 'equipment (demo_mes)' AND r.type = 'JOINS_TO'""")
        self.assertEqual([r[0] for r in fk], ["extracted"])
        md = self.markdown(self.page_by_title("sensor_reading (demo_mes)"))
        self.assertIn("| `equipment_id` | integer | NOT NULL | PK, FK→equipment |", md)
        self.assertIn("해석하지 않은 테이블 옵션: `PARTITION BY RANGE ( reading_time )`", md)

    def test_explicit_inferred_and_unknown_are_kept_apart(self) -> None:
        md = self.markdown(self.page_by_title("sensor_reading (demo_mes)"))
        self.assertIn("—JOINS_TO→ employee (demo_mes)", md)
        self.assertIn("[추정]", md)
        self.assertIn("외래키로 연결된다", md)
        # schema_json columns without nullable stay unknown instead of being guessed.
        crm = self.markdown(self.page_by_title("crm.customer (demo_crm)"))
        self.assertIn("| `biz_no` | varchar(12) | 미확인 |", crm)

    def test_same_term_has_context_specific_meanings(self) -> None:
        rows = self.query("SELECT scope FROM term_senses WHERE expression = '사업자번호' ORDER BY scope")
        self.assertEqual(len(rows), 3)
        site_md = self.markdown(self.page_by_title("site (demo_mes)"))
        self.assertIn("공장(사업장) 자체의 사업자등록번호", site_md)
        self.assertNotIn("공급업체", site_md)
        equipment_md = self.markdown(self.page_by_title("equipment (demo_mes)"))
        self.assertIn("공급업체의 사업자등록번호", equipment_md)

    def test_three_table_join_path_with_fanout_caveat(self) -> None:
        path_id = self.result["created"]["join_paths"][0]["id"]
        record = read_object(self.store, path_id)["join_path"]
        self.assertEqual([s["cardinality"] for s in record["steps"]], ["1:N", "1:N"])
        self.assertEqual([s["cardinality_basis"] for s in record["steps"]], ["schema", "schema"])
        self.assertEqual([s["foreign_key"] for s in record["steps"]], ["backward", "backward"])
        md = self.markdown(self.page_by_title("equipment (demo_mes)"))
        self.assertIn("### 사업장별 센서 측정값", md)
        self.assertIn("관계 수: 1:N (스키마 키 근거)", md)
        self.assertIn("측정 건수만큼 중복 합산", md)

    def test_join_without_foreign_key_shows_its_basis(self) -> None:
        path_id = self.result["created"]["join_paths"][1]["id"]
        md = self.markdown(path_id)
        self.assertIn("FK 없음 — 업무상 연결", md)
        self.assertIn("관계 수: 미확인", md)
        self.assertIn("연결 컬럼 NULL: NULL 가능 (스키마 키 근거)", md)

    def test_rules_reach_the_table_markdown(self) -> None:
        md = self.markdown(self.page_by_title("sensor_reading (demo_mes)"))
        rules_at = md.index("## 필수 조건·금칙·미지원 연산")
        self.assertLess(rules_at, md.index("## 조인 경로"))
        self.assertIn("**필수 조건** — reading_time 기간 조건을 반드시 건다", md)
        self.assertIn("**금칙** — reading_time 기간 조건 없이 전체 조회하지 않는다.", md)
        self.assertIn("미지원 연산 (미확인 — 있는지 없는지 확인되지 않음)", md)
        # A column page carries the rules of its table too.
        column_md = self.markdown(self.page_by_title("sensor_reading.reading_time"))
        self.assertIn("**금칙**", column_md)

    def test_confirmed_absence_of_change_history(self) -> None:
        md = self.markdown(self.page_by_title("equipment (demo_mes)"))
        self.assertIn("**미지원 연산 (부재 확인됨)** — 설비 마스터 변경 이력", md)
        self.assertIn("대안: 변경 요청서(ITSM 티켓)를 확인한다.", md)

    def test_evidence_keeps_source_lines(self) -> None:
        row = self.query("SELECT start_line, end_line FROM evidence WHERE excerpt LIKE 'site.biz_no는%'")[0]
        lines = (DEMO / "operations_guide.md").read_text(encoding="utf-8").split("\n")
        self.assertIn("site.biz_no는", lines[row["start_line"] - 1])

    def test_samples_are_redacted_and_checked(self) -> None:
        content = self.query("SELECT content FROM sources WHERE kind = 'sample_json'")[0][0]
        self.assertNotIn("$2b$12$demo", content)
        self.assertIn("[REDACTED]", content)
        self.assertTrue(any("REDACTED" in w for w in self.result["warnings"]))

    def test_index_lists_children(self) -> None:
        md = self.markdown(self.page_by_title("데모 MES 목차"))
        self.assertIn("1. 데모 MES 데이터 사용 가이드", md)
        self.assertIn("2. sensor_reading (demo_mes)", md)

    def test_same_plan_again_is_idempotent(self) -> None:
        before = self.query("SELECT COUNT(*) FROM pages")[0][0]
        again = ingest(self.store, demo_plan())
        self.assertTrue(again["reused"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM pages")[0][0], before)

    def test_same_schema_with_new_key_reuses_objects(self) -> None:
        plan = demo_plan()
        plan["idempotency_key"] = "test:demo-mes-2"
        plan["pages"], plan["index_members"] = [], []
        plan["relations"] = [r for r in plan["relations"] if r["source"] != "guide-page"]
        before = {t: self.query(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("pages", "relations", "term_senses", "rules", "join_paths")}
        result = ingest(self.store, plan)
        after = {t: self.query(f"SELECT COUNT(*) FROM {t}")[0][0] for t in before}
        self.assertEqual(before, after)
        self.assertFalse(result["created"])

    def test_changed_schema_needs_explicit_replace(self) -> None:
        changed = (DEMO / "schema.sql").read_text(encoding="utf-8").replace("  vendor_biz_no char(10)\n", "  vendor_code varchar(20)\n")
        plan = {"idempotency_key": "test:ddl-v2",
                "sources": [{"ref": "ddl", "kind": "sql_ddl", "dialect": "postgresql", "system_key": "demo_mes", "content": changed}]}
        with self.assertRaises(ConflictError) as raised:
            ingest(self.store, plan)
        self.assertIn("SCHEMA_CONFLICT", str(raised.exception))
        plan["idempotency_key"] = "test:ddl-v3"
        plan["sources"][0]["replace_existing_schema"] = True
        ingest(self.store, plan)
        old = self.query("SELECT status FROM pages WHERE title = 'equipment.vendor_biz_no'")[0][0]
        self.assertEqual(old, "deprecated")
        self.assertEqual(self.query("SELECT version FROM pages WHERE title = 'equipment (demo_mes)'")[0][0], 2)
        self.assertEqual(len(self.query("SELECT 1 FROM pages WHERE title = 'equipment.vendor_code'")), 1)

    def test_term_conflict_and_versioned_update(self) -> None:
        plan = demo_plan()
        plan["idempotency_key"] = "test:term-change"
        plan["terms"][0]["definition_md"] = "다른 정의"
        with self.assertRaises(ConflictError):
            ingest(self.store, plan)
        term_id = self.query("SELECT id FROM term_senses WHERE scope = 'demo_mes:site.biz_no'")[0][0]
        result = update_object(self.store, term_id, 1, {"definition_md": "고친 정의"}, "user correction")
        self.assertEqual(result["version"], 2)
        with self.assertRaises(ConflictError):
            update_object(self.store, term_id, 1, {"definition_md": "늦은 수정"}, "stale")
        self.assertIn("고친 정의", self.markdown(self.page_by_title("site (demo_mes)")))

    def test_index_cycle_is_rejected(self) -> None:
        plan = {"idempotency_key": "test:cycle",
                "pages": [{"ref": "sub", "type": "index", "title": "하위 목차", "summary_md": "s", "body_md": "b"}],
                "index_members": [{"index": "sub", "child": self.page_by_title("데모 MES 목차"), "position": 1},
                                  {"index": self.page_by_title("데모 MES 목차"), "child": "sub", "position": 3}]}
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("cycle", str(raised.exception))
        self.assertEqual(len(self.query("SELECT 1 FROM pages WHERE title = '하위 목차'")), 0)


class ValidationTests(Base):
    def test_excerpt_must_exist_in_source(self) -> None:
        plan = demo_plan()
        plan["evidence"][0]["excerpt"] = "가이드에 없는 문장"
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("not found in source", str(raised.exception))
        self.assertEqual(self.query("SELECT COUNT(*) FROM pages")[0][0], 0)
        self.assertEqual([tuple(r) for r in self.query("SELECT stage, status FROM runs")], [("received", "failed")])

    def test_cardinality_without_basis_is_rejected(self) -> None:
        plan = demo_plan()
        plan["join_paths"][1]["steps"][0]["cardinality"] = "N:1"
        plan["join_paths"][1]["steps"][0]["evidence"] = []
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("not backed by the stored keys", str(raised.exception))

    def test_cardinality_contradicting_schema_is_rejected(self) -> None:
        plan = demo_plan()
        plan["join_paths"][0]["steps"][1]["cardinality"] = "N:1"
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("contradicts the stored schema", str(raised.exception))

    def test_unbacked_step_cannot_be_called_extracted(self) -> None:
        plan = demo_plan()
        plan["join_paths"][1]["status"] = "extracted"
        plan["join_paths"][1]["steps"][0]["evidence"] = []
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("no foreign key backs", str(raised.exception))

    def test_confirmed_absence_needs_direct_evidence(self) -> None:
        plan = demo_plan()
        plan["evidence"].append({"ref": "e-guess", "source": "notes", "evidence_kind": "inferred", "excerpt": "문서에 언급이 없다"})
        plan["rules"][3]["confirmation"] = "confirmed"
        plan["rules"][3]["evidence"] = ["e-guess"]
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("not proof", str(raised.exception))

    def test_failure_late_in_plan_rolls_back_schema_too(self) -> None:
        plan = demo_plan()
        plan["rules"][0]["scope"] = "demo_mes:no_such_table"
        with self.assertRaises(ValidationError):
            ingest(self.store, plan)
        # The reference is checked while planning against the store: nothing is kept, and the
        # failure stays on the run at the stage it reached.
        for table in ("pages", "sources", "evidence", "relations", "term_senses", "rules", "join_paths", "search_index"):
            self.assertEqual(self.query(f"SELECT COUNT(*) FROM {table}")[0][0], 0, table)
        run = self.query("SELECT stage, status, error_kind, error FROM runs")[0]
        self.assertEqual((run["stage"], run["status"], run["error_kind"]), ("normalized", "failed", "validation"))
        self.assertIn("no_such_table", run["error"])

    def test_ambiguous_table_across_schemas(self) -> None:
        sql = "CREATE TABLE a.t (id int PRIMARY KEY);\nCREATE TABLE b.t (id int PRIMARY KEY);\n"
        plan = {"idempotency_key": "test:ambiguous",
                "sources": [{"ref": "ddl", "kind": "sql_ddl", "dialect": "postgresql", "system_key": "s", "content": sql},
                            {"ref": "doc", "kind": "text", "content": "t는 중요하다"}],
                "evidence": [{"ref": "e", "source": "doc", "evidence_kind": "observed", "excerpt": "t는 중요하다"}],
                "rules": [{"scope": "s:t", "kind": "requirement", "instruction_md": "x", "reason_md": "y", "evidence": ["e"], "status": "extracted"}]}
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("several schemas", str(raised.exception))
        plan["rules"][0]["scope"] = "s:b.t"
        ingest(self.store, plan)

    def test_dialect_must_be_stated(self) -> None:
        plan = {"idempotency_key": "k", "sources": [{"ref": "d", "kind": "sql_ddl", "system_key": "s", "content": "CREATE TABLE t (id int);"}]}
        with self.assertRaises(ValidationError):
            ingest(self.store, plan)

    def test_schema_pages_cannot_be_written_by_hand(self) -> None:
        plan = experience_plan()
        plan["pages"][0]["type"] = "schema_table"
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan)
        self.assertIn("schema pages come from", str(raised.exception))


class GeneralExperienceTests(Base):
    def test_config_experience_gets_no_database_sections(self) -> None:
        result = ingest(self.store, experience_plan())
        md = self.markdown(result["page_id"])
        for heading in ("## 테이블 구조", "## 조인 경로", "## 용어 정의", "## 필수 조건·금칙·미지원 연산"):
            self.assertNotIn(heading, md)
        self.assertEqual(self.query("SELECT COUNT(*) FROM schema_objects")[0][0], 0)


class MigrationFromV1Tests(Base):
    def test_v1_store_upgrades_and_keeps_records(self) -> None:
        # Build a store exactly as Phase 1 left it: only migration 1, schema_version 1.
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for statement in db.MIGRATIONS[0]:
                c.execute(statement)
            c.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
            c.execute("INSERT INTO pages VALUES ('p1', 'experience', '옛 기록', 's', 'b', 'active', 1, 't', 't')")
            c.commit()
        self.assertEqual(initialize(self.store)["schema_version"], db.SCHEMA_VERSION)
        self.assertIn("# 옛 기록", self.markdown("p1"))
        ingest(self.store, demo_plan())


if __name__ == "__main__":
    unittest.main()
