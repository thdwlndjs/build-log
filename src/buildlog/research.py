"""External research through the Antigravity CLI (agy), with resumable requests.

Responsibilities are split: agy searches and proposes findings; Buildlog validates them and
stores each one as a web source plus evidence on the page the question supports. Deciding
how a finding changes a page's conclusion stays with the host, through a normal update.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import BuildlogError, NotFoundError, ValidationError, to_json

DEFAULT_TIMEOUT = 240  # seconds agy may spend on one question
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 8
ACCESS_LEVELS = {"fetched", "search_snippet", "from_memory"}
ACCESS_LABELS = {"fetched": "원문 확인", "search_snippet": "검색 결과 요약만 확인", "from_memory": "모델 기억(원문 미확인)"}
ERROR_HINTS = {
    "not_installed": "agy를 찾을 수 없다. 설치 경로를 BUILDLOG_AGY 또는 --agy로 지정한다.",
    "auth": "agy 로그인이 필요하다. 터미널에서 agy.exe를 실행해 로그인한 뒤 다시 실행한다.",
    "permission": "agy가 허용되지 않은 도구(명령 실행 등)를 쓰려다 거부됐다. 질문을 좁혀 다시 실행한다.",
    "timeout": "시간 안에 끝나지 않았다. --timeout을 늘리거나 질문을 좁혀 다시 실행한다.",
    "output": "agy 출력이 약속한 JSON 형식이 아니었다. 다시 실행한다.",
    "no_findings": "근거를 찾지 못했다. 질문을 바꾸거나 맥락을 보강해 다시 실행한다.",
    "process": "agy가 비정상 종료했다. 메시지를 확인하고 다시 실행한다.",
}

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "quote": {"type": "string"},
                    "relevance": {"type": "string"},
                    "access": {"type": "string", "enum": sorted(ACCESS_LEVELS)},
                },
                "required": ["url", "title", "quote", "relevance", "access"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["findings", "notes"],
}

PROMPT = """Research task for a developer's knowledge base. Answer only from sources you find now.

Rules:
- Use only your web search and web page reading tools. Do not run shell commands. Do not read or write local files.
- Prefer primary sources: official documentation, release notes, vendor knowledge base, standards, source repositories.
- quote: copy a short passage exactly as it appears in the source, in its original language. Do not paraphrase inside quote.
- access: "fetched" only if you opened and read the page itself; "search_snippet" if you saw the text only in search results; "from_memory" if you did not see it in this session.
- url: prefer the source page's own address (for example https://antigravity.google/docs/...) over a search-engine or grounding redirect link whenever you can see it. Do not spend extra steps resolving redirects; return the link you have.
- Never invent a URL. If you find nothing reliable, return an empty findings list and say why in notes.
- relevance: one sentence on how the finding bears on the question.
- Return at most {max_findings} findings.

Question:
{question}

Context from the stored experience (for orientation only; do not treat it as a source):
{context}
"""


class ResearchFailed(BuildlogError):
    def __init__(self, message: str, data: dict[str, Any]) -> None:
        super().__init__(message)
        self.data = data


class AgyError(Exception):
    def __init__(self, kind: str, message: str, conversation_id: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.conversation_id = conversation_id


def default_agy() -> list[str]:
    """agy command from the shared settings (environment > config file > default)."""
    from .config import resolve_agy

    return resolve_agy(None)


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+\.[^\s]+$", url))


# Search grounding links point at a redirect service, not at the source; they can expire.
_REDIRECT_RE = re.compile(r"^https?://(?:vertexaisearch\.cloud\.google\.com/grounding-api-redirect/|www\.google\.[^/]+/url\?)", re.I)


def is_redirect(url: str) -> bool:
    return bool(_REDIRECT_RE.match(url))


def parse_agy_output(stdout: bytes, stderr: bytes, returncode: int) -> tuple[list[dict[str, Any]], str, str | None, list[str]]:
    """Return (findings, notes, conversation_id, warnings) or raise AgyError. Never trusts exit code alone."""
    err = stderr.decode("utf-8", "replace")
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise AgyError("output", f"agy printed more than {MAX_OUTPUT_BYTES} bytes")
    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if re.search(r"not signed in|sign in|log ?in|unauthori[sz]ed|auth", err, re.I):
            raise AgyError("auth", err.strip()[:500] or "agy is not signed in") from None
        if returncode != 0:
            raise AgyError("process", f"agy exited with {returncode}: {err.strip()[:500]}") from None
        raise AgyError("output", f"agy output is not JSON: {stdout[:200]!r}") from None
    if not isinstance(envelope, dict):
        raise AgyError("output", "agy output is not a JSON object")
    conversation_id = envelope.get("conversation_id")
    status = str(envelope.get("status", ""))
    if re.search(r"auth|login|sign", status, re.I):
        raise AgyError("auth", f"agy status {status}: {err.strip()[:300]}", conversation_id)
    if re.search(r"timeout|time_out|deadline", status, re.I):
        raise AgyError("timeout", f"agy status {status}", conversation_id)
    structured = envelope.get("structured_output")
    if structured is None and isinstance(envelope.get("response"), str) and envelope["response"].strip():
        try:
            structured = json.loads(envelope["response"])
        except json.JSONDecodeError:
            structured = None
    denied = envelope.get("denied_actions") or []
    # agy reports its own print timeout only on stderr, with exit 0 and status SUCCESS.
    timed_out = bool(re.search(r"print timeout|timed out|timeout after", err, re.I))
    if structured is None:
        if timed_out:
            raise AgyError("timeout", err.strip()[:300], conversation_id)
        if denied:
            names = ", ".join(str(d.get("display_name") or d.get("action")) for d in denied if isinstance(d, dict))
            raise AgyError("permission", f"agy produced no result; denied actions: {names}. {err.strip()[:300]}", conversation_id)
        if status.upper() != "SUCCESS":
            raise AgyError("process", f"agy status {status or 'missing'}: {err.strip()[:300]}", conversation_id)
        raise AgyError("output", "agy returned no structured output", conversation_id)
    if not isinstance(structured, dict) or not isinstance(structured.get("findings"), list):
        raise AgyError("output", "agy output does not match the findings schema", conversation_id)

    findings, warnings = [], []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(structured["findings"][:MAX_FINDINGS]):
        if not isinstance(item, dict):
            warnings.append(f"finding {index} dropped: not an object")
            continue
        url = str(item.get("url", "")).strip()
        quote = " ".join(str(item.get("quote", "")).split())
        access = item.get("access")
        if not _valid_url(url):
            warnings.append(f"finding {index} dropped: invalid url {url[:80]!r}")
            continue
        if not quote:
            warnings.append(f"finding {index} dropped: empty quote")
            continue
        if access not in ACCESS_LEVELS:
            warnings.append(f"finding {index} dropped: unknown access {access!r}")
            continue
        if (url, quote) in seen:
            continue
        seen.add((url, quote))
        findings.append({"url": url, "title": " ".join(str(item.get("title", "")).split())[:300] or url,
                         "quote": quote[:1500], "relevance": " ".join(str(item.get("relevance", "")).split())[:500],
                         "access": access, "redirect_url": is_redirect(url)})
    redirects = sum(f["redirect_url"] for f in findings)
    if redirects:
        warnings.append(f"{redirects} finding(s) carry a search redirect link instead of the source address; the link may expire")
    if denied:
        warnings.append(f"agy reported {len(denied)} denied action(s) but still returned findings")
    if timed_out:
        warnings.append("agy hit its time limit and returned partial output; findings may be incomplete")
    if not findings:
        raise AgyError("no_findings", "agy returned no usable findings: " + str(structured.get("notes", ""))[:300], conversation_id)
    return findings, str(structured.get("notes", ""))[:1000], conversation_id, warnings


def call_agy(question: str, context: str, agy: list[str], timeout: int) -> tuple[list[dict[str, Any]], str, str | None, list[str]]:
    prompt = PROMPT.format(question=question, context=context or "(none)", max_findings=MAX_FINDINGS)
    with tempfile.TemporaryDirectory(prefix="buildlog-research-") as workdir:
        schema = Path(workdir) / "findings.schema.json"
        schema.write_text(json.dumps(FINDINGS_SCHEMA), encoding="utf-8")
        command = [*agy, "--print", prompt, "--output-format", "json", "--json-schema", str(schema),
                   "--mode", "plan", "--print-timeout", f"{timeout}s"]
        try:
            # An empty working directory keeps agy away from any project files.
            done = subprocess.run(command, capture_output=True, cwd=workdir, timeout=timeout + 60, stdin=subprocess.DEVNULL)
        except FileNotFoundError as error:
            raise AgyError("not_installed", f"agy not found: {agy[0]}") from error
        except OSError as error:  # e.g. the path is not an executable
            raise AgyError("not_installed", f"agy cannot be started ({agy[0]}): {error}") from error
        except subprocess.TimeoutExpired as error:
            raise AgyError("timeout", f"agy did not finish within {timeout + 60}s") from error
    return parse_agy_output(done.stdout, done.stderr, done.returncode)


# ---------------------------------------------------------------- requests


def _requests(c: Any, run_id: str | None, page_id: str | None, request_ids: list[str] | None, include_done: bool) -> list[dict[str, Any]]:
    clauses, args = [], []
    if run_id:
        clauses.append("run_id = ?")
        args.append(run_id)
    if page_id:
        clauses.append("page_id = ?")
        args.append(page_id)
    if request_ids:
        clauses.append(f"id IN ({','.join('?' * len(request_ids))})")
        args += request_ids
    if not include_done:
        clauses.append("status != 'succeeded'")
    sql = "SELECT * FROM research_requests" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at, rowid"
    return [dict(r) for r in c.execute(sql, args)]


def _refresh_run_status(c: Any, run_id: str | None) -> None:
    if not run_id:
        return
    statuses = {r[0] for r in c.execute("SELECT status FROM research_requests WHERE run_id = ?", (run_id,))}
    if not statuses:
        value = "not_requested"
    elif statuses == {"succeeded"}:
        value = "succeeded"
    elif "failed" in statuses:
        value = "failed"
    else:
        value = "pending"
    c.execute("UPDATE runs SET research_status = ?, updated_at = ? WHERE id = ?", (value, db.now(), run_id))


def add_request(store: Path, page_id: str, question: str, context: str = "") -> dict[str, Any]:
    if not question or not question.strip():
        raise ValidationError("question must be a non-empty string")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    with db.write(store) as c:
        if c.execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
            raise NotFoundError(f"page not found: {page_id}")
        request_id, ts = str(uuid.uuid4()), db.now()
        c.execute("""INSERT INTO research_requests(id, run_id, page_id, question, context, status, attempts, created_at, updated_at)
                     VALUES (?, NULL, ?, ?, ?, 'pending', 0, ?, ?)""", (request_id, page_id, question.strip(), context, ts, ts))
    return {"id": request_id, "page_id": page_id, "question": question.strip(), "status": "pending"}


def _store_findings(c: Any, request: dict[str, Any], findings: list[dict[str, Any]], notes: str, conversation_id: str | None) -> list[str]:
    evidence_ids = []
    ts = db.now()
    for finding in findings:
        check = finding.get("verification") or {}
        address = check.get("final_url") or finding["url"]
        exists = c.execute(
            """SELECT 1 FROM page_sources ps JOIN evidence e ON e.id = ps.evidence_id JOIN sources s ON s.id = e.source_id
               WHERE ps.page_id = ? AND s.kind = 'web' AND s.locator = ? AND e.excerpt = ?""",
            (request["page_id"], address, finding["quote"]),
        ).fetchone()
        if exists:
            continue
        source_id, evidence_id = str(uuid.uuid4()), str(uuid.uuid4())
        metadata = {"title": finding["title"], "access": finding["access"], "relevance": finding["relevance"],
                    "question": request["question"], "research_request_id": request["id"], "agy_conversation_id": conversation_id,
                    "original_url": finding["url"], "redirect_url": address != finding["url"] or finding.get("redirect_url", False)}
        if check:
            metadata["verification"] = check
        c.execute("""INSERT INTO sources(id, kind, locator, captured_at, accessible_excerpt, metadata_json, parse_status)
                     VALUES (?, 'web', ?, ?, ?, ?, ?)""",
                  (source_id, address, ts, finding["quote"][:500], to_json(metadata), finding["access"]))
        # Observed only when Buildlog itself found the quote verbatim in the page. A researcher's claim
        # alone, a snippet, or a paraphrase stays second-hand.
        kind = "observed" if check.get("match") == "exact" else "reported"
        locator = f"agy 조사 · {ACCESS_LABELS[finding['access']]} · {finding['title']}"
        if check.get("match"):
            from .sources import MATCH_LABELS

            locator += f" · Buildlog 원문 대조: {MATCH_LABELS[check['match']]}"
        elif metadata["redirect_url"]:
            locator += " · 검색 리디렉션 링크(원문 주소 아님)"
        if finding["relevance"]:
            locator += f" — {finding['relevance']}"
        c.execute("""INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind) VALUES (?, ?, ?, ?, ?)""",
                  (evidence_id, source_id, locator[:600], finding["quote"], kind))
        c.execute("INSERT INTO page_sources(page_id, evidence_id) VALUES (?, ?)", (request["page_id"], evidence_id))
        evidence_ids.append(evidence_id)
    return evidence_ids


def run_research(store: Path, run_id: str | None = None, page_id: str | None = None, request_ids: list[str] | None = None,
                 agy: list[str] | None = None, timeout: int = DEFAULT_TIMEOUT, retry_succeeded: bool = False) -> dict[str, Any]:
    """Run every unfinished request selected by run, page, or ids. Resumes failed and interrupted ones."""
    if not (run_id or page_id or request_ids):
        raise ValidationError("select requests with a run id, a page id, or request ids")
    if not isinstance(timeout, int) or timeout < 10:
        raise ValidationError("timeout must be at least 10 seconds")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    agy = agy or default_agy()
    with closing(db.connect(store)) as c:
        if run_id:
            run = c.execute("SELECT status, stage FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"run not found: {run_id}")
            if run["status"] != "succeeded":
                # Research resumes requests of a saved experience; a failed save is retried with ingest instead.
                raise ValidationError(f"run {run_id} is not committed (stage {run['stage']}, status {run['status']}); "
                                      "retry the save with the same idempotency_key first")
        todo = _requests(c, run_id, page_id, request_ids, include_done=retry_succeeded)
        already = [r for r in _requests(c, run_id, page_id, request_ids, include_done=True) if r["status"] == "succeeded"]
    results = []
    for request in todo:
        with db.write(store) as c:
            c.execute("UPDATE research_requests SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                      (db.now(), request["id"]))
        try:
            findings, notes, conversation_id, warnings = call_agy(request["question"], request["context"], agy, timeout)
        except AgyError as error:
            # A repeat of an already-answered request keeps its earlier findings and status;
            # only the new error is recorded.
            status = "succeeded" if request["result_json"] else "failed"
            with db.write(store) as c:
                c.execute("""UPDATE research_requests SET status = ?, error_kind = ?, last_error = ?, conversation_id = ?,
                             updated_at = ? WHERE id = ?""",
                          (status, error.kind, str(error)[:1000], error.conversation_id, db.now(), request["id"]))
                _refresh_run_status(c, request["run_id"])
            results.append({"id": request["id"], "page_id": request["page_id"], "question": request["question"], "status": "failed",
                            "error_kind": error.kind, "error": str(error)[:500], "hint": ERROR_HINTS[error.kind]})
            continue
        # Check every finding against its page before writing (network work stays outside the transaction).
        if os.environ.get("BUILDLOG_OFFLINE") != "1":
            from .sources import verify_finding

            cache: dict[str, Any] = {}
            for finding in findings:
                finding["verification"] = verify_finding(finding["url"], finding["quote"], cache=cache)
        with db.write(store) as c:
            evidence_ids = _store_findings(c, request, findings, notes, conversation_id)
            result = {"findings": findings, "notes": notes, "evidence_ids": evidence_ids, "warnings": warnings}
            c.execute("""UPDATE research_requests SET status = 'succeeded', error_kind = NULL, last_error = NULL, conversation_id = ?,
                         result_json = ?, updated_at = ? WHERE id = ?""",
                      (conversation_id, to_json(result), db.now(), request["id"]))
            _refresh_run_status(c, request["run_id"])
        results.append({"id": request["id"], "page_id": request["page_id"], "question": request["question"], "status": "succeeded",
                        "findings": findings, "notes": notes, "new_evidence": len(evidence_ids), "warnings": warnings})
    summary = {"store": str(store), "run_id": run_id, "page_id": page_id, "results": results,
               "already_succeeded": [{"id": r["id"], "question": r["question"]} for r in already if r["id"] not in {x["id"] for x in results}]}
    if any(r["status"] == "failed" for r in results):
        failed = [r for r in results if r["status"] == "failed"]
        raise ResearchFailed(
            f"RESEARCH_FAILED: {len(failed)} of {len(results)} request(s) failed ({', '.join(sorted({r['error_kind'] for r in failed}))}); "
            "the saved experience is kept and the failed requests can be resumed with the same command", summary)
    return summary


def render_research(summary: dict[str, Any]) -> str:
    lines = ["# 조사 결과", ""]
    if not summary["results"] and not summary["already_succeeded"]:
        lines.append("실행할 조사 요청이 없다.")
    for r in summary["results"]:
        lines += [f"## {r['question']}", "", f"- 요청: `{r['id']}` · 대상 페이지: `{r['page_id']}` · 상태: {r['status']}"]
        if r["status"] == "failed":
            lines += [f"- 실패 원인({r['error_kind']}): {r['error']}", f"- 조치: {r['hint']}"]
        else:
            lines.append(f"- 새로 연결한 근거: {r['new_evidence']}건")
            for f in r["findings"]:
                check = f.get("verification") or {}
                if check.get("match"):
                    from .sources import MATCH_LABELS

                    url = check.get("final_url") or f["url"]
                    note = f" · 원문 대조: {MATCH_LABELS[check['match']]}"
                else:
                    url = f["url"]
                    note = " · 검색 리디렉션 링크(원문 주소 아님)" if f.get("redirect_url") else ""
                lines.append(f"- [{f['title']}]({url}) · {ACCESS_LABELS[f['access']]}{note}: \"{f['quote']}\" — {f['relevance']}")
            if r["notes"]:
                lines.append(f"- 조사 메모: {r['notes']}")
            for w in r["warnings"]:
                lines.append(f"- 경고: {w}")
        lines.append("")
    for r in summary["already_succeeded"]:
        lines.append(f"- 이미 완료된 요청: {r['question']} (`{r['id']}`)")
    return "\n".join(lines).rstrip("\n") + "\n"
