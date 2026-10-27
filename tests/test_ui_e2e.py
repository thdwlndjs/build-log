"""Phase 5 end-to-end: the workspace screen (spec §10.2) in real Chromium (Playwright), checked with the CLI.

Every change is made by clicking in the browser; every check of stored data runs the real CLI in a separate
process against the same SQLite store, so the screen, the API, the store, `read` and `query` are all exercised.
The Phase 3 review-screen checks for features the workspace does not connect yet are listed in
docs/workbench-menu-draft.md §3; their server side stays covered by test_review.
Skipped when Playwright or its Chromium is not installed (pip install playwright; python -m playwright install chromium).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parents[0] / "src"
sys.path.insert(0, str(TESTS))

try:
    from playwright.sync_api import Page, expect, sync_playwright

    with sync_playwright() as _p:
        _browser = _p.chromium.launch()
        _browser.close()
    PLAYWRIGHT = True
except Exception:  # noqa: BLE001 - any failure means the browser cannot run here
    PLAYWRIGHT = False

from buildlog.server import start_in_thread  # noqa: E402
from buildlog.store import ingest  # noqa: E402
from test_knowledge import demo_plan  # noqa: E402
from test_review import duplicates_plan  # noqa: E402

SCREENSHOTS = os.environ.get("BUILDLOG_E2E_SCREENSHOTS")


@unittest.skipUnless(PLAYWRIGHT, "Playwright with Chromium is not installed")
class WorkspaceEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.store = Path(cls.temp_dir.name) / "ui.sqlite3"
        cls.demo_run = ingest(cls.store, demo_plan())
        cls.dup_run = ingest(cls.store, duplicates_plan())
        cls.server = start_in_thread(cls.store)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        # A new page is a new browser context: no tabs remembered from another test.
        self.page: Page = self.browser.new_page(viewport={"width": 1400, "height": 900})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        # A 409 answer to a stale save is expected and is shown to the user; the browser also logs it.
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "(Conflict)" not in m.text else None)

    def tearDown(self) -> None:
        if SCREENSHOTS:
            self.page.screenshot(path=str(Path(SCREENSHOTS) / f"{self._testMethodName}.png"))
        self.page.close()
        self.assertEqual(self.errors, [], "browser console errors")

    # ------------------------------------------------------------ helpers

    def cli(self, *args: str) -> str:
        done = subprocess.run([sys.executable, "-m", "buildlog", "--store", str(self.store), *args], capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(SRC)}, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return done.stdout.decode("utf-8")

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def page_id(self, title: str) -> str:
        return self.q("SELECT id FROM pages WHERE title = ?", title)[0]["id"]

    def goto(self, query: str = "") -> None:
        self.page.goto(self.server.url + query)
        self.page.wait_for_selector("body[data-ready=true]")

    def open_doc(self, title: str) -> str:
        page_id = self.page_id(title)
        self.goto(f"?tab=doc:{page_id}")
        self.expect_doc(title)
        return page_id

    def expect_doc(self, title: str) -> None:
        expect(self.page.locator("#doc-title")).to_have_text(title)
        page_id = self.page_id(title)
        # The local graph follows the document: its current node is this page.
        self.wait_until("id => !!window.buildlogLocalCy && window.buildlogLocalCy.nodes('.current').map(n => n.id()).join() === id", page_id)

    def wait_until(self, script: str, arg: Any = None, timeout: float = 5.0) -> None:
        # page.wait_for_function compiles strings in the page, which the screen's CSP (no eval) refuses.
        deadline = time.monotonic() + timeout
        while not self.page.evaluate(script, arg):
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for: {script} ({arg})")
            self.page.wait_for_timeout(50)

    def info(self, name: str) -> None:
        self.page.locator(f".w-detail-tabs button[data-info='{name}']").click()

    def expect_status(self, text: str) -> None:
        expect(self.page.locator("[data-status]")).to_contain_text(text)

    def cli_update(self, page_id: str, patch: dict[str, Any], reason: str) -> None:
        version = self.q("SELECT version FROM pages WHERE id = ?", page_id)[0][0]
        path = Path(self.temp_dir.name) / f"patch-{self._testMethodName}.json"
        path.write_text(json.dumps(patch), encoding="utf-8")
        self.cli("update", page_id, "--expected-version", str(version), "--patch", str(path), "--reason", reason)

    # ------------------------------------------------------------ scenarios

    def test_document_and_local_graph_are_shown_together(self) -> None:
        a = self.open_doc("중복 A: 빌드 캐시 오류")
        expect(self.page.locator(".w-paper .w-kind")).to_have_text("경험")
        expect(self.page.locator(".w-paper .w-summary")).to_have_text("캐시 삭제로 해결")
        expect(self.page.locator(".w-paper .w-body")).to_contain_text("A")
        paper = self.page.locator(".w-paper").bounding_box()
        graph = self.page.locator("#local-graph").bounding_box()
        self.assertTrue(paper and graph and paper["width"] > 300 and graph["height"] > 150)
        self.assertGreater(graph["x"], paper["x"] + paper["width"] - 1)   # side by side, graph on the right
        nodes = set(self.page.evaluate("window.buildlogLocalCy.nodes().map(n => n.id())"))
        self.assertEqual(nodes, {a, self.page_id("빌드 캐시"), self.page_id("중복 B: 빌드 캐시 오류(재발)"), self.page_id("빌드 목차")})
        expect(self.page.locator(".w-related button[data-open]")).to_have_count(3)
        expect(self.page.locator(".w-tab.active")).to_contain_text("중복 A")
        self.expect_status("문서 · 경험 · v1")

    def test_selecting_a_graph_node_opens_that_document(self) -> None:
        self.open_doc("중복 A: 빌드 캐시 오류")
        concept = self.page_id("빌드 캐시")
        self.page.evaluate(f"window.buildlogLocalCy.getElementById('{concept}').emit('tap')")
        self.expect_doc("빌드 캐시")
        self.assertIn(f"tab=doc:{concept}", self.page.url)
        expect(self.page.locator(".w-tab")).to_have_count(2)                   # the first document stays open as a tab
        nodes = set(self.page.evaluate("window.buildlogLocalCy.nodes().map(n => n.id())"))
        self.assertIn(self.page_id("중복 B: 빌드 캐시 오류(재발)"), nodes)

    def test_related_documents_and_back_forward_and_direct_link(self) -> None:
        a = self.open_doc("중복 A: 빌드 캐시 오류")
        toc = self.page_id("빌드 목차")
        self.page.locator(f".w-related button[data-open='{toc}']").click()
        self.expect_doc("빌드 목차")
        self.page.go_back()
        self.expect_doc("중복 A: 빌드 캐시 오류")
        self.page.go_forward()
        self.expect_doc("빌드 목차")
        self.page.reload()
        self.page.wait_for_selector("body[data-ready=true]")
        self.expect_doc("빌드 목차")
        expect(self.page.locator(".w-tab")).to_have_count(2)                   # open tabs survive a reload
        fresh = self.browser.new_page()
        try:
            fresh.goto(f"{self.server.url}?tab=doc:{a}")
            expect(fresh.locator("#doc-title")).to_have_text("중복 A: 빌드 캐시 오류")
            fresh.goto(f"{self.server.url}?open={toc}")                         # older review links still open the page
            expect(fresh.locator("#doc-title")).to_have_text("빌드 목차")
        finally:
            fresh.close()

    def test_info_history_and_evidence(self) -> None:
        self.open_doc("중복 A: 빌드 캐시 오류")
        dl = self.page.locator(".w-details dl")
        expect(dl).to_contain_text("경험")
        expect(dl).to_contain_text("3개 문서")
        expect(dl).to_contain_text("v1")
        self.info("이력")
        expect(self.page.locator(".w-details .w-detail-line")).to_have_count(1)
        self.info("근거")
        expect(self.page.locator(".w-details .w-evidence")).to_contain_text("A에서 본 출력")
        expect(self.page.locator(".w-details .w-evidence button[data-source]")).to_contain_text("대화 · test")

    def test_body_edit_is_versioned_and_reaches_the_cli(self) -> None:
        page_id = self.open_doc("빌드 캐시")
        self.page.click("button[data-edit]")
        editor = self.page.locator("textarea[data-editor]")
        expect(editor).to_have_value("개념 설명")
        editor.fill("빌드 산출물을 재사용하려고 저장해 두는 중간 결과. 무효화 조건이 핵심이다. e2ebody")
        self.page.click("button[data-save]")
        self.expect_status("저장했습니다 (v2)")
        expect(self.page.locator(".w-paper .w-body")).to_contain_text("e2ebody")
        expect(self.page.locator("[data-summary-dirty]")).to_be_visible()
        self.info("이력")
        expect(self.page.locator(".w-details .w-detail-line[data-version='2']")).to_contain_text("작업 공간에서 본문 수정")
        self.assertIn("e2ebody", self.cli("read", page_id))
        query = Path(self.temp_dir.name) / "q.json"
        query.write_text(json.dumps({"query": "무효화 조건 e2ebody", "terms": ["e2ebody"]}), encoding="utf-8")
        self.assertIn(page_id, self.cli("query", "--input", str(query)))

    def test_concurrent_change_is_not_overwritten(self) -> None:
        page_id = self.open_doc("빌드 목차")
        self.page.click("button[data-edit]")
        self.cli_update(page_id, {"body_md": "CLI에서 먼저 고침"}, "concurrent")
        self.page.locator("textarea[data-editor]").fill("브라우저 늦은 수정")
        self.page.click("button[data-save]")
        self.expect_status("VERSION_CONFLICT")
        expect(self.page.locator("textarea[data-editor]")).to_have_value("브라우저 늦은 수정")   # nothing typed is lost
        md = self.cli("read", page_id)
        self.assertIn("CLI에서 먼저 고침", md)
        self.assertNotIn("브라우저 늦은 수정", md)
        self.page.click("button[data-cancel-edit]")
        expect(self.page.locator(".w-paper .w-body")).to_contain_text("CLI에서 먼저 고침")

    def test_markdown_is_sanitized(self) -> None:
        page_id = self.page_id("중복 B: 빌드 캐시 오류(재발)")
        self.cli_update(page_id, {"body_md": "위험한 본문 <img src=x onerror=\"document.body.dataset.pwned=1\"> <script>document.body.dataset.pwned=2</script>"},
                        "xss check")
        self.open_doc("중복 B: 빌드 캐시 오류(재발)")
        expect(self.page.locator(".w-paper .w-body")).to_contain_text("<img src=x")   # shown as text
        self.assertIsNone(self.page.evaluate("document.body.dataset.pwned || null"))
        self.assertEqual(self.page.locator(".w-paper img").count(), 0)

    def test_first_visit_opens_the_latest_save_and_panels_toggle(self) -> None:
        self.goto()
        expect(self.page.locator("#doc-title")).not_to_be_empty()
        seeds = {r["id"] for r in self.q("SELECT id FROM pages")}
        self.assertIn(self.page.url.split("tab=doc:")[1], seeds)
        self.page.click("button[data-toggle-left]")
        expect(self.page.locator("#ww-side")).to_be_hidden()
        self.page.click("button[data-toggle-context]")
        expect(self.page.locator(".w-context")).to_have_count(0)
        self.page.click("button[data-toggle-context]")
        expect(self.page.locator("#local-graph")).to_be_visible()

    def test_screens_of_later_steps_say_so_and_tabs_close(self) -> None:
        self.open_doc("빌드 목차")
        self.page.click("button.l-graph-entry")
        expect(self.page.locator(".w-content > .w-empty")).to_contain_text("5-5")
        self.page.click("button[data-settings]")
        expect(self.page.locator(".w-content > .w-empty")).to_contain_text("5-5")
        expect(self.page.locator(".w-tab")).to_have_count(3)
        self.page.locator(".w-tab", has_text="전체 그래프").locator("button[data-close]").click()
        expect(self.page.locator(".w-tab")).to_have_count(2)
        self.goto(f"?run={self.dup_run['run_id']}")                              # the link a save hands out
        expect(self.page.locator(".w-tab.active")).to_contain_text("전체 그래프")

    # ------------------------------------------------------------ Phase 5-4: explorer, list, search, sources

    def test_document_list_filter_and_open(self) -> None:
        self.open_doc("빌드 목차")
        self.page.click("button[data-list]")
        rows = self.page.locator(".w-flat .w-row")
        expect(rows.first).to_be_visible()
        total = self.q("SELECT COUNT(*) FROM pages WHERE type != 'schema_column'")[0][0]
        expect(rows).to_have_count(total)                                  # fewer than one page of 50
        self.page.locator(".w-filters button[data-filter='테이블']").click()
        expect(rows).to_have_count(self.q("SELECT COUNT(*) FROM pages WHERE type = 'schema_table'")[0][0])
        expect(self.page.locator(".w-filters button.active")).to_have_text("테이블")
        self.page.locator(".w-flat .w-row", has_text="sensor_reading (demo_mes)").click()
        self.expect_doc("sensor_reading (demo_mes)")
        expect(self.page.locator(".w-paper .w-body")).to_contain_text("reading_time")   # the table's rule reaches the page

    def test_search_dialog_opens_a_document(self) -> None:
        self.open_doc("빌드 목차")
        self.page.click("button[data-search]")
        self.page.locator("input[data-query]").fill("빌드 캐시 오류")
        result = self.page.locator(".w-search-dialog .w-row", has_text="중복 A: 빌드 캐시 오류")
        expect(result).to_be_visible()
        result.click()
        expect(self.page.locator(".w-search-dialog")).to_have_count(0)
        self.expect_doc("중복 A: 빌드 캐시 오류")
        self.page.click("button[data-search]")
        self.page.locator("input[data-query]").fill("zzz없는기록zzz")
        expect(self.page.locator(".w-search-dialog [data-results]")).to_contain_text("찾은 기록이 없습니다")
        self.page.keyboard.press("Escape")
        expect(self.page.locator(".w-search-dialog")).to_have_count(0)

    def test_explorer_sessions_open_documents_and_session_tab(self) -> None:
        self.open_doc("중복 A: 빌드 캐시 오류")
        tree = self.page.locator(".l-tree-body")
        toc = self.page_id("빌드 목차")
        expect(tree.locator(f"button[data-session='{toc}']")).to_be_visible()
        expect(tree.locator(f"button[data-session='{self.page_id('데모 MES 목차')}']")).to_be_visible()
        expect(tree.locator(f"button[data-open='{self.page_id('중복 A: 빌드 캐시 오류')}']")).to_have_class("selected")
        tree.locator(f"button[data-open='{self.page_id('빌드 캐시')}']").click()
        self.expect_doc("빌드 캐시")
        self.page.locator("[data-navigator-filter]").fill("데모")                 # the filter keeps only matching sessions
        expect(tree.locator("button[data-session]")).to_have_count(1)
        self.page.locator("[data-navigator-filter]").fill("")
        tree.locator(f"button[data-session='{toc}']").click()
        expect(self.page.locator("#session-title")).to_have_text("빌드 목차")
        expect(self.page.locator(".w-paper .w-row")).to_have_count(4)             # the index itself and its 3 members
        expect(self.page.locator(".w-context dl")).to_contain_text("4개")
        self.page.locator(".w-paper button[data-source]").first.click()
        expect(self.page.locator("#source-title")).to_have_text("test")
        self.page.go_back()
        expect(self.page.locator("#session-title")).to_have_text("빌드 목차")

    def test_source_tab_shows_what_was_stored(self) -> None:
        self.open_doc("빌드 목차")
        self.page.locator(".l-navtabs button[data-navigator='sources']").click()
        self.page.locator(".l-tree-body button.l-source", has_text="operations_guide.md").click()
        expect(self.page.locator("#source-title")).to_have_text("operations_guide.md")
        expect(self.page.locator("[data-source-text]")).to_contain_text("reading_time 기간 조건을 건다")
        expect(self.page.locator(".w-context dl")).to_contain_text("문서 소스")
        self.page.locator(".w-paper .w-inline-link", has_text="데모 MES 데이터 사용 가이드").click()
        self.expect_doc("데모 MES 데이터 사용 가이드")
        # A conversation was never stored in full: only its saved excerpt and quotes are shown.
        self.page.locator(".l-tree-body button.l-source", has_text="test").first.click()
        expect(self.page.locator("[data-no-text]")).to_be_visible()
        expect(self.page.locator(".w-paper")).to_contain_text("A에서 본 출력")

    def test_evidence_opens_its_source(self) -> None:
        self.open_doc("중복 A: 빌드 캐시 오류")
        self.info("근거")
        self.page.locator(".w-details .w-evidence button[data-source]").first.click()
        expect(self.page.locator("#source-title")).to_have_text("test")
        expect(self.page.locator(".w-paper")).to_contain_text("중복 A: 빌드 캐시 오류")   # linked knowledge
        self.assertIn("tab=source:", self.page.url)


if __name__ == "__main__":
    unittest.main()
