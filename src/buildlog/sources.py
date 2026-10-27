"""Check web evidence against the source itself: resolve redirects, fetch the page, find the quote.

Research tools often hand back search-redirect links and snippets that are paraphrased. Buildlog
follows the link to the real address, reads the page, and records whether the quoted text is
actually there. Only a quote found verbatim upgrades evidence to `observed`; anything else keeps
its original kind with an explicit result, so a paraphrase is never presented as a source quote.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import urllib.error
import urllib.request
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import db
from .db import NotFoundError, ValidationError, to_json

MAX_PAGE_BYTES = 3 * 1024 * 1024
DEFAULT_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; Buildlog/0.1 source check)"
MATCH_LABELS = {
    "exact": "원문에서 인용 그대로 확인",
    "partial": "원문과 일부 일치",
    "absent": "원문에서 찾지 못함(요약·의역일 수 있음)",
    "unreadable": "원문을 읽지 못함",
}
_WORD = re.compile(r"[\w][\w\-./]*", re.UNICODE)


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Follow redirects and return {final_url, status, text} or {final_url, error}."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate",
                                                   "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PAGE_BYTES + 1)
            final_url, status = response.geturl(), response.status
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            content_type = response.headers.get("Content-Type") or ""
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        return {"final_url": error.geturl() or url, "status": error.code, "error": f"HTTP {error.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"final_url": url, "status": None, "error": f"{type(error).__name__}: {error}"}
    if len(raw) > MAX_PAGE_BYTES:
        return {"final_url": final_url, "status": status, "error": f"page larger than {MAX_PAGE_BYTES} bytes"}
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
        elif encoding not in ("", "identity"):
            return {"final_url": final_url, "status": status, "error": f"unsupported content encoding {encoding}"}
    except (OSError, zlib.error) as error:
        return {"final_url": final_url, "status": status, "error": f"cannot decode body: {error}"}
    if not any(t in content_type for t in ("html", "text", "xml", "json")) and content_type:
        return {"final_url": final_url, "status": status, "error": f"not a text page ({content_type})"}
    return {"final_url": final_url, "status": status, "text": page_text(raw.decode(charset, "replace"))}


def page_text(markup: str) -> str:
    markup = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    markup = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _norm(text: str) -> str:
    # Quotes from research often carry Markdown emphasis or code ticks that the page does not.
    text = re.sub(r"[*_`]+", "", text)
    text = " ".join(text.split())
    # Removing inline tags leaves spaces inside brackets and before punctuation: "( <code>0</code> waits".
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}.,;:!?])", r"\1", text)
    return text.casefold()


def check_quote(text: str, quote: str) -> dict[str, Any]:
    page, q = _norm(text), _norm(quote)
    if not q:
        return {"match": "absent", "coverage": 0.0, "longest_run": 0}
    if q in page or q.rstrip(".") in page:
        return {"match": "exact", "coverage": 1.0, "longest_run": len(_WORD.findall(q))}
    words = _WORD.findall(q)
    page_words = set(_WORD.findall(page))
    significant = [w for w in words if len(w) >= 3]
    coverage = sum(w in page_words for w in significant) / len(significant) if significant else 0.0
    longest = 0
    for start in range(len(words)):  # longest run of consecutive quote words present as a phrase
        for end in range(len(words), start + longest, -1):
            if " ".join(words[start:end]) in page:
                longest = end - start
                break
    match = "partial" if coverage >= 0.8 and longest >= 6 else "absent"
    return {"match": match, "coverage": round(coverage, 2), "longest_run": longest}


def verify_finding(url: str, quote: str, fetcher: Callable[[str], dict[str, Any]] = fetch,
                   cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    cache = cache if cache is not None else {}
    if url not in cache:
        cache[url] = fetcher(url)
    page = cache[url]
    result: dict[str, Any] = {"final_url": page["final_url"], "http_status": page.get("status"), "checked_at": db.now()}
    if "text" not in page:
        return result | {"match": "unreadable", "error": page.get("error")}
    return result | check_quote(page["text"], quote)


def verify_sources(store: Path, page_id: str | None = None, all_pages: bool = False,
                   fetcher: Callable[[str], dict[str, Any]] = fetch) -> dict[str, Any]:
    """Re-check stored web evidence: real address, quote match, duplicate links."""
    if not (page_id or all_pages):
        raise ValidationError("give a page id or --all")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        sql = """SELECT ps.page_id, e.id AS evidence_id, e.excerpt, e.evidence_kind, s.id AS source_id, s.locator, s.metadata_json
                 FROM page_sources ps JOIN evidence e ON e.id = ps.evidence_id JOIN sources s ON s.id = e.source_id
                 WHERE s.kind = 'web'""" + (" AND ps.page_id = ?" if page_id else "") + " ORDER BY e.rowid"
        rows = [dict(r) for r in c.execute(sql, (page_id,) if page_id else ())]
    if page_id and not rows:
        with closing(db.connect(store)) as c:
            if c.execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
                raise NotFoundError(f"page not found: {page_id}")
    cache: dict[str, dict[str, Any]] = {}
    checks = []
    for row in rows:
        meta = json.loads(row["metadata_json"] or "{}")
        original = meta.get("original_url") or row["locator"]
        checks.append((row, meta, original, verify_finding(original, row["excerpt"], fetcher, cache)))
    counts: dict[str, int] = {}
    duplicates = kept_previous = 0
    with db.write(store) as c:
        seen: set[tuple[str, str, str]] = set()
        for row, meta, original, check in checks:
            previous = meta.get("verification") or {}
            if check["match"] == "unreadable" and check.get("http_status") is None:
                # The page could not be reached this time (offline, sandbox, timeout). That says nothing
                # about the source: keep the address and result of the last check that did reach it.
                meta = meta | {"original_url": original, "last_failed_check": {"checked_at": check["checked_at"], "error": check.get("error")}}
                if previous and previous.get("match") != "unreadable":
                    c.execute("UPDATE sources SET metadata_json = ? WHERE id = ?", (to_json(meta), row["source_id"]))
                    kept_previous += 1
                    counts["kept_previous"] = counts.get("kept_previous", 0) + 1
                    key = (row["page_id"], row["locator"], _norm(row["excerpt"]))
                    seen.add(key)
                    continue
                check = check | {"final_url": row["locator"]}
            counts[check["match"]] = counts.get(check["match"], 0) + 1
            meta = meta | {"original_url": original, "verification": check}
            meta.pop("last_failed_check", None) if check["match"] != "unreadable" else None
            if check["final_url"] != original:
                meta["redirect_url"] = True
            c.execute("UPDATE sources SET locator = ?, metadata_json = ? WHERE id = ?", (check["final_url"], to_json(meta), row["source_id"]))
            if check["match"] == "exact" and row["evidence_kind"] != "observed":
                c.execute("UPDATE evidence SET evidence_kind = 'observed' WHERE id = ?", (row["evidence_id"],))
            # The stored location text was written before the check; say what the check found instead.
            label = f" · Buildlog 원문 대조: {MATCH_LABELS[check['match']]}"
            current = c.execute("SELECT section_locator FROM evidence WHERE id = ?", (row["evidence_id"],)).fetchone()[0]
            updated = re.sub(r" · (검색 리디렉션 링크\(원문 주소 아님\)|Buildlog 원문 대조: [^—·]*?)(?= —|$)", "", current)
            updated = updated.replace(" — ", label + " — ", 1) if " — " in updated else updated + label
            c.execute("UPDATE evidence SET section_locator = ? WHERE id = ?", (updated[:600], row["evidence_id"]))
            key = (row["page_id"], check["final_url"], _norm(row["excerpt"]))
            if key in seen:
                # The same quote from the same page is already linked; keep the first link only.
                c.execute("DELETE FROM page_sources WHERE page_id = ? AND evidence_id = ?", (row["page_id"], row["evidence_id"]))
                duplicates += 1
            seen.add(key)
    return {"store": str(store), "checked": len(checks), "results": counts, "duplicate_links_removed": duplicates,
            "kept_previous_on_failure": kept_previous, "pages_fetched": len(cache),
            "unreadable": [u for u, p in cache.items() if "text" not in p]}
