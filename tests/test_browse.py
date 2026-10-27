"""Phase 5 contract: read-only APIs for the workspace screen. spec §10.1 and §10.3.

"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from buildlog.db import SCHEMA_VERSION
from buildlog.server import start_in_thread
from buildlog.store import ingest
from buildlog.update import merge_pages


GUIDE = "# 빌드 가이드\n\n캐시는 매일 지운다.\n"
DDL = """CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customer(id), amount NUMERIC);
"""
CONVERSATION = "Claude Code conversation, 2026-10-27, test project, 빌드 캐시"


def store_plan(directory: Path) -> dict[str, Any]:
    (directory / "guide.md").write_text(GUIDE, encoding="utf-8")
    (directory / "shop.sql").write_text(DDL, encoding="utf-8")
    page = lambda ref, kind, title, summary, ev: {  # noqa: E731
        "ref": ref, "type": kind, "title": title, "summary_md": summary, "body_md": f"## 본문\n\n{title}", "evidence": ev}
    return {
        "idempotency_key": "test:browse",
        "sources": [
            {"ref": "conv", "kind": "conversation", "locator": CONVERSATION, "accessible_excerpt": "대화 발췌: 빌드 캐시 오류"},
            {"ref": "guide", "kind": "markdown", "path": str(directory / "guide.md")},
            {"ref": "ddl", "kind": "sql_ddl", "dialect": "postgresql", "system_key": "shop", "path": str(directory / "shop.sql")},
        ],
        "evidence": [
            {"ref": "e1", "source": "conv", "evidence_kind": "observed", "excerpt": "빌드 캐시 오류를 봤다"},
            {"ref": "e2", "source": "guide", "evidence_kind": "observed", "excerpt": "캐시는 매일 지운다."},
        ],
        "pages": [
            page("exp1", "experience", "캐시 오류", "캐시를 지워 해결\n둘째 줄", ["e1"]),
            page("exp2", "experience", "권한 오류", "권한을 고쳐 해결", ["e1"]),
            page("c1", "concept", "빌드 캐시", "빌드 결과를 재사용하는 저장소", ["e2"]),
            {"ref": "toc", "type": "index", "title": "빌드 목차", "summary_md": "빌드 문제 모음", "body_md": "목차"},
            {"ref": "sub", "type": "index", "title": "하위 목차", "summary_md": "권한 문제", "body_md": "목차"},
        ],
        "index_members": [
            {"index": "toc", "child": "exp1", "position": 1},
            {"index": "toc", "child": "sub", "position": 2},
            {"index": "toc", "child": "shop:customer", "position": 3},
            {"index": "toc", "child": "shop:orders", "position": 4},
            {"index": "toc", "child": "c1", "position": 5},
            {"index": "sub", "child": "exp2", "position": 1},
        ],
        "terms": [{"expression": "캐시 키", "scope": "test:build", "page": "exp2", "definition_md": "캐시를 찾는 키",
                   "evidence": ["e2"], "status": "extracted"}],
        "relations": [{"source": "exp1", "target": "c1", "type": "USES", "explanation_md": "캐시 개념", "evidence": ["e1"], "status": "extracted"}],
    }


class BrowseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        directory = Path(cls.temp_dir.name)
        cls.store = directory / "browse.sqlite3"
        ingest(cls.store, store_plan(directory))
        ids = cls.ids()
        # A later save, merged away: the duplicate must disappear from lists.
        ingest(cls.store, {
            "idempotency_key": "test:browse-dup",
            "sources": [{"ref": "c", "kind": "conversation", "locator": "test", "accessible_excerpt": "중복"}],
            "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "같은 캐시 오류"}],
            "pages": [{"ref": "d", "type": "experience", "title": "캐시 오류(중복)", "summary_md": "중복", "body_md": "중복", "evidence": ["e"]}],
            "index_members": [{"index": ids["빌드 목차"], "child": "d", "position": 6}],
        })
        ids = cls.ids()
        merge_pages(cls.store, [ids["캐시 오류(중복)"]], ids["캐시 오류"], {ids["캐시 오류(중복)"]: 1, ids["캐시 오류"]: 1}, "중복")
        # A page saved before bundles were required: simulate by removing its membership.
        with closing(sqlite3.connect(cls.store)) as c:
            c.execute("DELETE FROM index_members WHERE child_page_id = ?", (ids["빌드 캐시"],))
            c.commit()
        cls.ids_by_title = cls.ids()
        cls.server = start_in_thread(cls.store)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp_dir.cleanup()

    @classmethod
    def ids(cls) -> dict[str, str]:
        with closing(sqlite3.connect(cls.store)) as c:
            return {title: pid for pid, title in c.execute("SELECT id, title FROM pages")}

    def q(self, sql: str, *args: Any) -> list[Any]:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute(sql, args).fetchall()

    def get(self, path: str) -> tuple[int, Any, bytes]:
        conn = HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.server.port}"})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw), raw
        except ValueError:
            return response.status, None, raw

    def ok(self, path: str) -> Any:
        status, data, raw = self.get(path)
        self.assertEqual(status, 200, raw[:300])
        return data

    def source_id(self, kind: str) -> str:
        return self.q("SELECT id FROM sources WHERE kind = ? ORDER BY rowid LIMIT 1", kind)[0][0]

    # ------------------------------------------------------------ /api/pages

    def test_pages_list_shape_and_defaults(self) -> None:
        data = self.ok("/api/pages")
        titles = [i["title"] for i in data["items"]]
        self.assertEqual((data["offset"], data["limit"]), (0, 50))
        self.assertNotIn("캐시 오류(중복)", titles)                      # merged away
        self.assertFalse([i for i in data["items"] if i["type"] == "schema_column"])  # columns only on request
        self.assertEqual(data["total"], len(data["items"]))
        self.assertGreater(data["counts"]["schema_column"], 0)            # counts ignore the filter
        item = next(i for i in data["items"] if i["title"] == "캐시 오류")
        self.assertEqual(set(item), {"id", "title", "type", "status", "version", "updated_at", "summary_line", "summary_dirty"})
        self.assertEqual(item["summary_line"], "캐시를 지워 해결")
        stamps = [i["updated_at"] for i in data["items"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_pages_filter_sort_and_paging(self) -> None:
        experiences = self.ok("/api/pages?type=experience&sort=title")
        self.assertEqual([i["title"] for i in experiences["items"]], ["권한 오류", "캐시 오류"])
        self.assertEqual(experiences["total"], 2)
        page = self.ok("/api/pages?type=experience&sort=title&offset=1&limit=1")
        self.assertEqual([i["title"] for i in page["items"]], ["캐시 오류"])
        self.assertEqual(page["total"], 2)
        columns = self.ok("/api/pages?type=schema_column")
        self.assertTrue(columns["items"])
        for bad in ("type=nope", "status=nope", "sort=nope", "limit=0", "limit=201", "offset=-1"):
            self.assertEqual(self.get(f"/api/pages?{bad}")[0], 400, bad)

    # ------------------------------------------------------------ /api/sessions

    def test_sessions_are_top_level_indexes_with_ordered_members(self) -> None:
        data = self.ok("/api/sessions")
        self.assertEqual([s["title"] for s in data["sessions"]], ["빌드 목차"])   # 하위 목차 has a parent
        session = data["sessions"][0]
        self.assertEqual([c["title"] for c in session["children"]],
                         ["캐시 오류", "하위 목차", "customer (shop)", "orders (shop)"])
        self.assertEqual([c["position"] for c in session["children"]], sorted(c["position"] for c in session["children"]))
        self.assertNotIn(session["id"], [c["id"] for c in session["children"]])
        self.assertEqual(session["summary_line"], "빌드 문제 모음")

    def test_unbundled_pages_are_reported_not_hidden(self) -> None:
        unbundled = self.ok("/api/sessions")["unbundled"]
        self.assertEqual([(u["title"], u["type"]) for u in unbundled], [("빌드 캐시", "concept")])  # columns belong to tables

    def test_session_detail(self) -> None:
        toc = self.ids_by_title["빌드 목차"]
        listed = self.ok("/api/sessions")["sessions"][0]
        detail = self.ok(f"/api/sessions/{toc}")
        self.assertEqual(detail["summary_md"], "빌드 문제 모음")
        self.assertEqual(detail["parents"], [])
        self.assertEqual(detail["children"][0]["summary_line"], "캐시를 지워 해결")
        kinds = {s["kind"] for s in detail["sources"]}
        self.assertIn("conversation", kinds)
        self.assertIn("sql_ddl", kinds)
        self.assertEqual(listed["source_count"], len(detail["sources"]))
        sub = self.ok(f"/api/sessions/{self.ids_by_title['하위 목차']}")
        self.assertEqual(sub["parents"], [{"id": toc, "title": "빌드 목차"}])
        self.assertEqual(self.get(f"/api/sessions/{self.ids_by_title['캐시 오류']}")[0], 404)  # not an index

    # ------------------------------------------------------------ /api/sources

    def test_sources_list(self) -> None:
        sources = self.ok("/api/sources")["sources"]
        by_kind = {s["kind"]: s for s in sources}
        self.assertEqual(by_kind["markdown"]["title"], "guide.md")
        self.assertEqual(by_kind["markdown"]["kind_label"], "문서")
        self.assertEqual(by_kind["sql_ddl"]["kind_label"], "DDL")
        self.assertEqual(by_kind["conversation"]["kind_label"], "대화")
        conv = next(s for s in sources if s["locator"] == CONVERSATION)
        self.assertEqual(conv["title"], CONVERSATION)
        self.assertEqual(conv["page_count"], 3)            # 캐시 오류, 권한 오류, and 빌드 캐시 through the USES relation
        self.assertEqual(set(conv), {"id", "kind", "kind_label", "title", "locator", "captured_at", "parse_status", "page_count"})
        self.assertEqual([s["kind"] for s in self.ok("/api/sources?kind=markdown")["sources"]], ["markdown"])
        self.assertEqual([s["title"] for s in self.ok("/api/sources?q=GUIDE")["sources"]], ["guide.md"])
        self.assertEqual(self.get("/api/sources?kind=nope")[0], 400)

    def test_file_source_detail_has_text_and_linked_pages(self) -> None:
        detail = self.ok(f"/api/sources/{self.source_id('markdown')}")
        self.assertEqual(detail["text"], GUIDE)
        self.assertFalse(detail["text_truncated"])
        self.assertIsNone(detail["verification"])
        titles = {p["title"] for p in detail["pages"]}
        self.assertIn("빌드 캐시", titles)    # evidence linked to the page
        self.assertIn("권한 오류", titles)    # evidence linked to a term on the page
        self.assertIn("캐시는 매일 지운다.", [e["excerpt"] for e in detail["evidence"]])

    def test_conversation_source_shows_only_what_was_stored(self) -> None:
        conv = self.q("SELECT id FROM sources WHERE locator = ?", CONVERSATION)[0][0]
        detail = self.ok(f"/api/sources/{conv}")
        self.assertIsNone(detail["text"])
        self.assertEqual(detail["excerpt"], "대화 발췌: 빌드 캐시 오류")
        self.assertEqual([e["excerpt"] for e in detail["evidence"]], ["빌드 캐시 오류를 봤다"])
        self.assertEqual({p["title"] for p in detail["pages"]}, {"캐시 오류", "권한 오류", "빌드 캐시"})
        self.assertEqual(self.get("/api/sources/00000000-0000-0000-0000-000000000000")[0], 404)

    # ------------------------------------------------------------ /api/settings, /api/graph?mode=all

    def test_settings_are_read_only_and_carry_no_token(self) -> None:
        status, data, raw = self.get("/api/settings")
        self.assertEqual(status, 200, raw[:300])
        self.assertEqual(data["store"], str(self.store))
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(set(data["config"]), {"file", "store", "agy"})
        self.assertEqual(set(data["config"]["agy"]), {"value", "origin"})
        self.assertNotIn(self.server.token.encode(), raw)

    def test_graph_all_mode(self) -> None:
        data = self.ok("/api/graph?mode=all")
        self.assertEqual(data["mode"], "all")
        titles = {n["title"] for n in data["nodes"]}
        self.assertTrue({"캐시 오류", "권한 오류", "빌드 캐시", "빌드 목차", "하위 목차"} <= titles)
        self.assertNotIn("캐시 오류(중복)", titles)
        self.assertFalse([n for n in data["nodes"] if n["type"] == "schema_column"])
        with_columns = self.ok("/api/graph?mode=all&columns=1")
        self.assertTrue([n for n in with_columns["nodes"] if n["type"] == "schema_column"])


if __name__ == "__main__":
    unittest.main()
