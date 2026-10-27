"""Phase 4: web evidence is checked against the real page (offline, with a fake fetcher)."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from buildlog.read import read_object, render_markdown
from buildlog.sources import check_quote, page_text, verify_sources
from buildlog.store import ingest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_research import plan_with_research  # noqa: E402

PAGE = "<html><head><style>x{}</style><script>var hidden='Optional time limit';</script></head><body><h1>Reference</h1>" \
       "<p>--print-timeout 0s Optional time limit for print mode (<code>0</code> waits until the turn completes).</p>" \
       "<p>Print mode runs a single prompt and exits without the interactive interface.</p></body></html>"
REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA"
REAL = "https://example.org/docs/cli/reference/"


def fake_fetch(url: str) -> dict[str, Any]:
    if url in (REDIRECT, REAL):
        return {"final_url": REAL, "status": 200, "text": page_text(PAGE)}
    return {"final_url": url, "status": None, "error": "URLError: offline"}


class QuoteCheckTests(unittest.TestCase):
    def test_exact_partial_absent(self) -> None:
        text = page_text(PAGE)
        self.assertNotIn("hidden", text)  # scripts and styles are not page text
        self.assertEqual(check_quote(text, "Optional time limit for print mode (0 waits until the turn completes).")["match"], "exact")
        self.assertEqual(check_quote(text, "**Optional time limit** for print mode")["match"], "exact")  # markdown emphasis ignored
        partial = check_quote(text, "Sets an optional time limit for print mode (0 waits until the turn completes).")
        self.assertEqual(partial["match"], "partial")
        self.assertEqual(check_quote(text, "Headless mode streams JSON events to stderr for every tool call")["match"], "absent")

    def test_gzip_decoding_path(self) -> None:
        # fetch() decodes gzip bodies; page_text must then see real words.
        body = gzip.decompress(gzip.compress(PAGE.encode("utf-8"))).decode("utf-8")
        self.assertIn("Print mode runs a single prompt", page_text(body))


class VerifySourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "v.sqlite3"
        saved = ingest(self.store, plan_with_research())
        self.page_id = saved["page_id"]
        # Store web evidence the way an older research run left it: redirect links, snippets, a duplicate.
        with closing(sqlite3.connect(self.store)) as c:
            for i, (url, quote) in enumerate([
                (REDIRECT, "Optional time limit for print mode (0 waits until the turn completes)."),
                (REDIRECT, "Sets an optional time limit for print mode (0 waits until the turn completes)."),
                (REAL, "Optional time limit for print mode (0 waits until the turn completes)."),
                ("https://unreachable.invalid/x", "Something"),
            ]):
                meta = json.dumps({"access": "search_snippet", "redirect_url": url == REDIRECT})
                c.execute("INSERT INTO sources(id, kind, locator, captured_at, accessible_excerpt, metadata_json, parse_status) VALUES (?, 'web', ?, 't', ?, ?, 'search_snippet')",
                          (f"s{i}", url, quote, meta))
                c.execute("INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind) VALUES (?, ?, ?, ?, 'reported')",
                          (f"e{i}", f"s{i}", "agy 조사 · 검색 결과 요약만 확인 · Ref · 검색 리디렉션 링크(원문 주소 아님) — relevance", quote))
                c.execute("INSERT INTO page_sources(page_id, evidence_id) VALUES (?, ?)", (self.page_id, f"e{i}"))
            c.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolves_checks_and_removes_duplicate_links(self) -> None:
        report = verify_sources(self.store, self.page_id, fetcher=fake_fetch)
        self.assertEqual(report["results"], {"exact": 2, "partial": 1, "unreadable": 1})
        self.assertEqual(report["duplicate_links_removed"], 1)  # same page, same quote, reached through the redirect and directly
        with closing(sqlite3.connect(self.store)) as c:
            rows = {r[0]: r[1:] for r in c.execute("SELECT e.id, s.locator, e.evidence_kind, e.section_locator FROM evidence e JOIN sources s ON s.id = e.source_id WHERE e.id LIKE 'e%'")}
            linked = {r[0] for r in c.execute("SELECT evidence_id FROM page_sources WHERE page_id = ?", (self.page_id,))}
        self.assertEqual(rows["e0"][:2], (REAL, "observed"))         # redirect resolved, verbatim quote → observed
        self.assertEqual(rows["e1"][:2], (REAL, "reported"))         # paraphrase stays second-hand
        self.assertIn("원문과 일부 일치", rows["e1"][2])
        self.assertNotIn("검색 리디렉션 링크", rows["e1"][2])
        self.assertEqual(rows["e3"][1], "reported")
        self.assertNotIn("e2", linked)                              # duplicate link removed, rows kept
        self.assertEqual(len(rows), 4)
        md = render_markdown(read_object(self.store, self.page_id))
        self.assertIn("원문 대조: 원문에서 인용 그대로 확인", md)
        self.assertIn("원문 대조: 원문과 일부 일치 (단어 일치", md)
        self.assertIn("원문 대조: 원문을 읽지 못함", md)

    def test_offline_recheck_keeps_the_earlier_result(self) -> None:
        verify_sources(self.store, self.page_id, fetcher=fake_fetch)
        with closing(sqlite3.connect(self.store)) as c:
            before = c.execute("SELECT s.locator, s.metadata_json, e.section_locator FROM sources s JOIN evidence e ON e.source_id = s.id WHERE e.id = 'e0'").fetchone()

        def offline(url: str) -> dict[str, Any]:
            return {"final_url": url, "status": None, "error": "URLError: [WinError 10013] socket access denied"}

        report = verify_sources(self.store, self.page_id, fetcher=offline)
        # e0 and e1 keep their result; e2's duplicate link was removed by the first check; e3 was never readable.
        self.assertEqual(report["kept_previous_on_failure"], 2)
        with closing(sqlite3.connect(self.store)) as c:
            after = c.execute("SELECT s.locator, s.metadata_json, e.section_locator FROM sources s JOIN evidence e ON e.source_id = s.id WHERE e.id = 'e0'").fetchone()
            linked = {r[0] for r in c.execute("SELECT evidence_id FROM page_sources WHERE page_id = ?", (self.page_id,))}
        self.assertEqual(after[0], REAL)                           # not reverted to the redirect link
        self.assertEqual(json.loads(after[1])["verification"], json.loads(before[1])["verification"])
        self.assertIn("WinError 10013", json.loads(after[1])["last_failed_check"]["error"])
        self.assertEqual(after[2], before[2])
        self.assertIn("e0", linked)

    def test_rerun_is_stable(self) -> None:
        verify_sources(self.store, self.page_id, fetcher=fake_fetch)
        again = verify_sources(self.store, self.page_id, fetcher=fake_fetch)
        self.assertEqual(again["duplicate_links_removed"], 0)
        with closing(sqlite3.connect(self.store)) as c:
            locator = c.execute("SELECT section_locator FROM evidence WHERE id = 'e1'").fetchone()[0]
        self.assertEqual(locator.count("Buildlog 원문 대조"), 1)


if __name__ == "__main__":
    unittest.main()
