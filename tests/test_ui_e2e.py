"""Phase 3 end-to-end: real Chromium (Playwright) edits through the review UI, checked with the CLI.

Every change is made by clicking in the browser; every check runs the real CLI in a separate process
against the same SQLite store, so the UI, the API, the store, `read` and `query` are all exercised.
Skipped when Playwright or its Chromium is not installed (pip install playwright; python -m playwright install chromium).
"""
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
class ReviewUiEndToEnd(unittest.TestCase):
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

    def open_ui(self, query: str = "", run: str = "dup") -> None:
        if not query:
            query = f"?run={(self.dup_run if run == 'dup' else self.demo_run)['run_id']}"
        self.page.goto(self.server.url + query)
        self.page.wait_for_selector("body[data-ready=true]")

    def open_node(self, title: str) -> None:
        self.page.locator(f"#node-list button[data-node-id='{self.page_id(title)}']").click()
        expect(self.page.locator("#panel-title")).to_have_text(title)

    def tab(self, key: str) -> None:
        self.page.locator(f"#panel .tabs button[data-tab={key}]").click()

    def expect_status(self, text: str) -> None:
        expect(self.page.locator("#status")).to_contain_text(text)

    # ------------------------------------------------------------ scenarios

    def test_graph_shows_latest_save_and_page_markdown(self) -> None:
        self.open_ui()
        self.assertGreater(self.page.evaluate("window.buildlogCy.nodes().length"), 0)
        expect(self.page.locator("#graph-info")).to_contain_text("저장 실행")
        expect(self.page.locator("#node-list")).to_contain_text("중복 A: 빌드 캐시 오류")
        self.open_node("중복 A: 빌드 캐시 오류")
        expect(self.page.locator("#panel .md")).to_contain_text("캐시 삭제로 해결")
        # Clicking a node on the canvas opens it too.
        target = self.page_id("빌드 캐시")
        self.page.evaluate(f"window.buildlogCy.getElementById('{target}').emit('tap')")
        expect(self.page.locator("#panel-title")).to_have_text("빌드 캐시")

    def test_body_edit_reaches_cli_read_and_query(self) -> None:
        self.open_ui()
        self.open_node("중복 A: 빌드 캐시 오류")
        self.tab("edit")
        self.page.fill("#panel textarea[name=body_md]", "## 문제\n\n브라우저에서 고친 본문 e2ebodyedit")
        self.page.click("button[data-action=save-page]")
        self.expect_status("저장했습니다 (v2)")
        self.expect_status("요약 갱신이 필요")
        page_id = self.page_id("중복 A: 빌드 캐시 오류")
        md = self.cli("read", page_id)
        self.assertIn("브라우저에서 고친 본문 e2ebodyedit", md)
        self.assertIn("요약이 최근 본문 수정 전 내용이다", md)
        found = self.cli("query", "e2ebodyedit")
        self.assertIn(f"`{page_id}` v2", found)
        expect(self.page.locator("#node-list")).to_contain_text("요약 갱신 필요")

    def test_relation_and_definition_edits_reach_cli(self) -> None:
        self.open_ui(run="demo")
        self.open_node("sensor_reading (demo_mes)")
        self.tab("relations")
        item = self.page.locator("#panel .item", has_text="employee (demo_mes)").first
        rel_id = item.get_attribute("data-relation-id")
        item.locator("button[data-action=edit-relation]").click()
        item.locator("textarea[name=explanation_md]").fill("operator_code = emp_code. 검토자가 고친 설명 e2erel")
        item.locator("button[data-action=save-relation]").click()
        self.expect_status("관계를 저장했습니다 (v2)")
        self.assertIn("검토자가 고친 설명 e2erel", self.cli("read", rel_id))
        self.assertIn("검토자가 고친 설명 e2erel", self.cli("read", self.page_id("sensor_reading (demo_mes)")))

        self.open_node("site (demo_mes)")
        self.tab("knowledge")
        term = self.page.locator("#panel .item[data-term-id]").first
        term.locator("button[data-action=edit]").click()
        term.locator("textarea[name=definition_md]").fill("공장 자체의 사업자등록번호(검토 반영 e2edef)")
        term.locator("button[data-action=save]").click()
        self.expect_status("저장했습니다 (v2)")
        self.assertIn("검토 반영 e2edef", self.cli("read", self.page_id("site (demo_mes)")))
        self.assertIn("검토 반영 e2edef", self.cli("query", "사업자번호", "--scope", "demo_mes"))

    def test_unlink_keeps_history_and_evidence(self) -> None:
        self.open_ui(run="demo")
        self.open_node("데모 MES 데이터 사용 가이드")
        self.tab("relations")
        item = self.page.locator("#panel .item", has_text="SUPPORTS").first
        rel_id = item.get_attribute("data-relation-id")
        evidence_before = self.q("SELECT COUNT(*) FROM object_evidence WHERE object_id = ?", rel_id)[0][0]
        item.locator("button[data-action=unlink-relation]").click()
        item.locator("input[name=reason]").fill("출처 관계가 아니다")
        item.locator("button[data-action=confirm-unlink]").click()
        self.expect_status("관계를 해제했습니다")
        guide = self.page_id("데모 MES 데이터 사용 가이드")
        self.assertNotIn(rel_id, self.cli("read", guide))
        self.assertIn("연결 해제됨", self.cli("read", rel_id))
        self.assertEqual(self.q("SELECT COUNT(*) FROM object_evidence WHERE object_id = ?", rel_id)[0][0], evidence_before)
        self.assertEqual([r["reason"] for r in self.q("SELECT reason FROM revisions WHERE object_id = ? ORDER BY version", rel_id)],
                         ["created", "unlinked: 출처 관계가 아니다"])
        self.assertEqual(self.page.evaluate(f"window.buildlogCy.getElementById('{rel_id}').length"), 0)

    def test_merge_keeps_references_and_sources(self) -> None:
        self.open_ui(f"?run={self.dup_run['run_id']}")
        self.open_node("중복 A: 빌드 캐시 오류")
        self.tab("merge")
        self.page.fill("#panel input[name=merge-search]", "빌드 캐시 오류")
        self.page.click("button[data-action=merge-search]")
        b_id = self.page_id("중복 B: 빌드 캐시 오류(재발)")
        self.page.locator(f"#panel [data-candidate-id='{b_id}'] input").check()
        self.page.fill("#panel input[name=reason]", "같은 문제의 재발 기록")
        self.page.click("button[data-action=confirm-merge]")
        self.expect_status("병합했습니다")
        a_id = self.page_id("중복 A: 빌드 캐시 오류")
        merged = self.cli("read", b_id)
        self.assertIn("# 중복 A: 빌드 캐시 오류", merged)
        self.assertIn(f"`{b_id}`은 이 페이지로 병합됐다", merged)
        self.assertIn("B에서 본 출력", merged)  # B's evidence is now A's
        self.assertEqual(self.q("SELECT page_id FROM term_senses WHERE scope = 'test:build'")[0][0], a_id)
        self.assertNotIn(b_id, self.cli("query", "빌드 캐시 오류"))
        expect(self.page.locator("#node-list")).not_to_contain_text("중복 B")

    def test_feedback_then_edit_marks_it_addressed(self) -> None:
        self.open_ui()
        self.open_node("빌드 캐시")
        self.tab("feedback")
        self.page.fill("#panel textarea[name=feedback]", "개념 설명이 너무 짧다")
        self.page.click("button[data-action=send-feedback]")
        self.expect_status("피드백을 기록했습니다")
        fb = self.q("SELECT * FROM feedback WHERE feedback_text = '개념 설명이 너무 짧다'")[0]
        self.tab("edit")
        self.page.fill("#panel textarea[name=body_md]", "빌드 산출물을 재사용하려고 저장해 두는 중간 결과. 무효화 조건이 핵심이다.")
        self.page.fill("#panel textarea[name=summary_md]", "빌드 중간 결과 저장소")
        self.page.select_option("#panel select[name=feedback]", fb["id"])
        self.page.click("button[data-action=save-page]")
        self.expect_status("저장했습니다")
        row = self.q("SELECT status, resulting_revision FROM feedback WHERE id = ?", fb["id"])[0]
        self.assertEqual(row["status"], "addressed")
        self.assertIsNotNone(row["resulting_revision"])

    def test_concurrent_change_is_not_overwritten(self) -> None:
        self.open_ui()
        self.open_node("빌드 목차")
        self.tab("edit")
        page_id = self.page_id("빌드 목차")
        version = self.q("SELECT version FROM pages WHERE id = ?", page_id)[0][0]
        patch = Path(self.temp_dir.name) / "patch.json"
        patch.write_text(json.dumps({"summary_md": "CLI에서 먼저 고침"}), encoding="utf-8")
        self.cli("update", page_id, "--expected-version", str(version), "--patch", str(patch), "--reason", "concurrent")
        self.page.fill("#panel textarea[name=summary_md]", "브라우저 늦은 수정")
        self.page.click("button[data-action=save-page]")
        self.expect_status("VERSION_CONFLICT")
        md = self.cli("read", page_id)
        self.assertIn("CLI에서 먼저 고침", md)
        self.assertNotIn("브라우저 늦은 수정", md)

    def test_join_path_is_highlighted(self) -> None:
        self.open_ui(run="demo")
        self.open_node("equipment (demo_mes)")
        self.tab("knowledge")
        self.page.locator("#panel .item[data-path-id]", has_text="사업장별 센서 측정값").locator("button[data-action=highlight-path]").click()
        self.assertEqual(self.page.evaluate("window.buildlogCy.nodes('.hl').length"), 3)
        self.assertGreaterEqual(self.page.evaluate("window.buildlogCy.edges('.hl').length"), 2)

    def test_markdown_is_sanitized(self) -> None:
        page_id = self.page_id("빌드 캐시")
        version = self.q("SELECT version FROM pages WHERE id = ?", page_id)[0][0]
        patch = Path(self.temp_dir.name) / "xss.json"
        patch.write_text(json.dumps({"body_md": "위험한 본문 <img src=x onerror=\"document.body.dataset.pwned=1\"> <script>document.body.dataset.pwned=2</script>"}),
                         encoding="utf-8")
        self.cli("update", page_id, "--expected-version", str(version), "--patch", str(patch), "--reason", "xss check")
        self.open_ui()
        self.open_node("빌드 캐시")
        expect(self.page.locator("#panel .md")).to_contain_text("<img src=x")  # shown as text
        self.assertIsNone(self.page.evaluate("document.body.dataset.pwned || null"))
        self.assertEqual(self.page.locator("#panel .md img").count(), 0)


if __name__ == "__main__":
    unittest.main()
