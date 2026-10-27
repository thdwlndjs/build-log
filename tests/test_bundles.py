"""Phase 5 contract: every page belongs to a bundle (index). spec §3 and §6."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from buildlog.db import ValidationError
from buildlog.store import ingest

PENDING = unittest.expectedFailure  # Phase 5-2

DDL = """CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customer(id), amount NUMERIC);
"""


def base(key: str, pages: list[dict[str, Any]], members: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "idempotency_key": key,
        "sources": [{"ref": "c", "kind": "conversation", "locator": "test", "accessible_excerpt": "묶음"}],
        "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "빌드 캐시 오류"}],
        "pages": pages,
        "index_members": members or [],
    }
    plan.update(extra)
    return plan


def experience(ref: str, title: str) -> dict[str, Any]:
    return {"ref": ref, "type": "experience", "title": title, "summary_md": "요약", "body_md": "## 문제\n\n본문", "evidence": ["e"]}


def index(ref: str, title: str) -> dict[str, Any]:
    return {"ref": ref, "type": "index", "title": title, "summary_md": "목차", "body_md": "목차"}


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.store = self.dir / "b.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def page_id(self, title: str) -> str:
        return self.q("SELECT id FROM pages WHERE title = ?", title)[0]["id"]

    @PENDING
    def test_new_page_without_bundle_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, base("k:orphan", [experience("p", "소속 없는 경험")]))
        self.assertIn("BUNDLE_REQUIRED", str(raised.exception))
        self.assertIn("소속 없는 경험", str(raised.exception))
        self.assertEqual(self.q("SELECT COUNT(*) FROM pages")[0][0], 0)  # nothing was stored
        run = self.q("SELECT status FROM runs WHERE idempotency_key = 'k:orphan'")[0]
        self.assertEqual(run["status"], "failed")

    def test_new_top_level_index_with_its_member_is_accepted(self) -> None:
        result = ingest(self.store, base("k:top", [experience("p", "경험"), index("toc", "새 목차")],
                                         [{"index": "toc", "child": "p", "position": 1}]))
        self.assertFalse([w for w in result["warnings"] if "BUNDLE" in w])
        # The index has no parent: it is a top-level bundle, which needs no parent of its own.
        self.assertEqual(self.q("SELECT COUNT(*) FROM index_members WHERE child_page_id = ?", self.page_id("새 목차"))[0][0], 0)

    def test_new_page_can_join_an_existing_index(self) -> None:
        ingest(self.store, base("k:first", [experience("p", "경험"), index("toc", "목차")], [{"index": "toc", "child": "p", "position": 1}]))
        toc = self.page_id("목차")
        ingest(self.store, base("k:second", [experience("n", "후속 경험")], [{"index": toc, "child": "n", "position": 2}]))
        self.assertEqual(len(self.q("SELECT 1 FROM index_members WHERE index_page_id = ?", toc)), 2)

    def test_nested_index_is_a_member_of_its_parent(self) -> None:
        ingest(self.store, base("k:nested", [experience("p", "경험"), index("top", "상위 목차"), index("sub", "하위 목차")],
                                [{"index": "top", "child": "sub", "position": 1}, {"index": "sub", "child": "p", "position": 1}]))
        self.assertEqual(self.q("SELECT index_page_id FROM index_members WHERE child_page_id = ?", self.page_id("하위 목차"))[0][0],
                         self.page_id("상위 목차"))

    @PENDING
    def test_schema_tables_need_a_bundle_and_columns_do_not(self) -> None:
        ddl = self.dir / "shop.sql"
        ddl.write_text(DDL, encoding="utf-8")
        source = {"ref": "ddl", "kind": "sql_ddl", "dialect": "postgresql", "system_key": "shop", "path": str(ddl)}

        def plan(key: str, members: list[dict[str, Any]]) -> dict[str, Any]:
            p = base(key, [index("toc", "쇼핑 목차")], members)
            p["sources"].append(source)
            return p

        with self.assertRaises(ValidationError) as raised:
            ingest(self.store, plan("k:schema-missing", [{"index": "toc", "child": "shop:customer", "position": 1}]))
        message = str(raised.exception)
        self.assertIn("BUNDLE_REQUIRED", message)
        self.assertIn("orders", message)
        self.assertNotIn("customer_id", message)  # columns belong to their table through PART_OF

        ingest(self.store, plan("k:schema-ok", [{"index": "toc", "child": "shop:customer", "position": 1},
                                                {"index": "toc", "child": "shop:orders", "position": 2}]))
        self.assertGreater(self.q("SELECT COUNT(*) FROM pages WHERE type = 'schema_column'")[0][0], 0)

    @PENDING
    def test_updating_an_unbundled_existing_page_warns(self) -> None:
        ingest(self.store, base("k:legacy", [experience("p", "예전 경험"), index("toc", "목차")], [{"index": "toc", "child": "p", "position": 1}]))
        page = self.q("SELECT id, version FROM pages WHERE title = '예전 경험'")[0]
        with closing(sqlite3.connect(self.store)) as c:  # a page saved before bundles were required
            c.execute("DELETE FROM index_members WHERE child_page_id = ?", (page["id"],))
            c.commit()
        result = ingest(self.store, base("k:update", [{"id": page["id"], "expected_version": page["version"], "body_md": "## 문제\n\n갱신",
                                                      "reason": "후속", "evidence": ["e"]}]))
        self.assertTrue(any("BUNDLE_MISSING" in w and page["id"] in w for w in result["warnings"]))
        self.assertEqual(self.q("SELECT version FROM pages WHERE id = ?", page["id"])[0][0], page["version"] + 1)


if __name__ == "__main__":
    unittest.main()
