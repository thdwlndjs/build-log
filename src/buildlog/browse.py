"""Read-only lists for the workspace screen (spec §10.3): pages, sessions (top-level indexes) and sources.

Nothing here writes to the store. A page merged into another (page_redirects) is left out of every list.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path, PureWindowsPath
from typing import Any

from . import db
from .db import PAGE_STATUSES, PAGE_TYPES, NotFoundError, ValidationError
from .plan import SOURCE_KINDS

KIND_LABELS = {"conversation": "대화", "web": "웹", "markdown": "문서", "text": "문서", "document": "문서",
               "sql_ddl": "DDL", "schema_json": "스키마", "sample_json": "샘플", "sample_csv": "샘플"}
FILE_KINDS = {"markdown", "text", "sql_ddl", "schema_json", "sample_json", "sample_csv"}
MAX_TEXT = 200_000
NOT_MERGED = "p.id NOT IN (SELECT from_page_id FROM page_redirects)"

# (source, page) pairs: evidence on the page itself, or on a term / rule / relation / join path that belongs
# to the page (the term's page, the rule's scope, both ends of a linked relation, the tables a path passes).
SOURCE_PAGES = """
    SELECT e.source_id, ps.page_id FROM page_sources ps JOIN evidence e ON e.id = ps.evidence_id
    UNION SELECT e.source_id, t.page_id FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
          JOIN term_senses t ON oe.object_type = 'term' AND t.id = oe.object_id WHERE t.page_id IS NOT NULL
    UNION SELECT e.source_id, r.scope_page_id FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
          JOIN rules r ON oe.object_type = 'rule' AND r.id = oe.object_id
    UNION SELECT e.source_id, rel.source_id FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
          JOIN relations rel ON oe.object_type = 'relation' AND rel.id = oe.object_id WHERE rel.removed_at IS NULL
    UNION SELECT e.source_id, rel.target_id FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
          JOIN relations rel ON oe.object_type = 'relation' AND rel.id = oe.object_id WHERE rel.removed_at IS NULL
    UNION SELECT e.source_id, jt.page_id FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
          JOIN join_path_tables jt ON oe.object_type = 'join_path' AND jt.join_path_id = oe.object_id
"""


def summary_line(markdown: str | None) -> str:
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= 160 else line[:159] + "…"
    return ""


def _open(store: Path) -> sqlite3.Connection:
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    return db.connect(store)


def _int(value: Any, name: str, default: int, low: int, high: int | None = None) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer") from None
    if number < low or (high is not None and number > high):
        raise ValidationError(f"{name} must be {low}..{high}" if high is not None else f"{name} must be at least {low}")
    return number


# ---------------------------------------------------------------- pages


def list_pages(store: Path, page_type: str | None = None, status: str | None = None, sort: str | None = None,
               offset: Any = None, limit: Any = None) -> dict[str, Any]:
    if page_type and page_type not in PAGE_TYPES:
        raise ValidationError(f"type must be one of {sorted(PAGE_TYPES)}")
    if status and status not in PAGE_STATUSES:
        raise ValidationError(f"status must be one of {sorted(PAGE_STATUSES)}")
    sort = sort or "updated"
    if sort not in ("updated", "title"):
        raise ValidationError("sort must be updated or title")
    offset_n = _int(offset, "offset", 0, 0)
    limit_n = _int(limit, "limit", 50, 1, 200)
    where, args = [NOT_MERGED], []
    if page_type:
        where.append("p.type = ?")
        args.append(page_type)
    else:
        where.append("p.type != 'schema_column'")   # columns are read from their table unless asked for
    if status:
        where.append("p.status = ?")
        args.append(status)
    order = "p.updated_at DESC, p.rowid DESC" if sort == "updated" else "p.title COLLATE NOCASE, p.title, p.id"
    with closing(_open(store)) as c:
        condition = " AND ".join(where)
        total = c.execute(f"SELECT COUNT(*) FROM pages p WHERE {condition}", args).fetchone()[0]
        rows = c.execute(f"""SELECT p.id, p.title, p.type, p.status, p.version, p.updated_at, p.summary_md, p.summary_dirty
                             FROM pages p WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?""", (*args, limit_n, offset_n)).fetchall()
        counts = {r[0]: r[1] for r in c.execute(f"SELECT p.type, COUNT(*) FROM pages p WHERE {NOT_MERGED} GROUP BY p.type")}
    items = [{"id": r["id"], "title": r["title"], "type": r["type"], "status": r["status"], "version": r["version"],
              "updated_at": r["updated_at"], "summary_line": summary_line(r["summary_md"]), "summary_dirty": bool(r["summary_dirty"])}
             for r in rows]
    return {"total": total, "offset": offset_n, "limit": limit_n, "counts": counts, "items": items}


# ---------------------------------------------------------------- sessions


def _children(c: sqlite3.Connection, index_id: str) -> list[sqlite3.Row]:
    return c.execute(f"""SELECT p.id, p.title, p.type, p.status, p.summary_md, m.position FROM index_members m
                         JOIN pages p ON p.id = m.child_page_id WHERE m.index_page_id = ? AND {NOT_MERGED}
                         ORDER BY m.position, p.title""", (index_id,)).fetchall()


def _sources_of(c: sqlite3.Connection, page_ids: list[str]) -> list[sqlite3.Row]:
    """Sources linked to these pages (same routes as SOURCE_PAGES), filtered by page first so the cost
    follows the pages asked for, not the size of the store."""
    if not page_ids:
        return []
    m = ",".join("?" * len(page_ids))
    # CROSS JOIN keeps the page-side table first, so each branch starts from the page index instead of
    # scanning every piece of evidence of that kind (the planner has no statistics to choose by).
    return c.execute(f"""SELECT s.id, s.kind, s.locator, s.metadata_json FROM sources s WHERE s.id IN (
            SELECT e.source_id FROM page_sources ps CROSS JOIN evidence e ON e.id = ps.evidence_id WHERE ps.page_id IN ({m})
            UNION SELECT e.source_id FROM term_senses t CROSS JOIN object_evidence oe ON oe.object_type = 'term' AND oe.object_id = t.id
                  CROSS JOIN evidence e ON e.id = oe.evidence_id WHERE t.page_id IN ({m})
            UNION SELECT e.source_id FROM rules r CROSS JOIN object_evidence oe ON oe.object_type = 'rule' AND oe.object_id = r.id
                  CROSS JOIN evidence e ON e.id = oe.evidence_id WHERE r.scope_page_id IN ({m})
            UNION SELECT e.source_id FROM relations rel CROSS JOIN object_evidence oe ON oe.object_type = 'relation' AND oe.object_id = rel.id
                  CROSS JOIN evidence e ON e.id = oe.evidence_id WHERE rel.removed_at IS NULL AND rel.source_id IN ({m})
            UNION SELECT e.source_id FROM relations rel CROSS JOIN object_evidence oe ON oe.object_type = 'relation' AND oe.object_id = rel.id
                  CROSS JOIN evidence e ON e.id = oe.evidence_id WHERE rel.removed_at IS NULL AND rel.target_id IN ({m})
            UNION SELECT e.source_id FROM join_path_tables jt CROSS JOIN object_evidence oe ON oe.object_type = 'join_path' AND oe.object_id = jt.join_path_id
                  CROSS JOIN evidence e ON e.id = oe.evidence_id WHERE jt.page_id IN ({m}))
          ORDER BY s.captured_at DESC, s.rowid DESC""", [*page_ids] * 6).fetchall()


def unbundled_pages(c: sqlite3.Connection) -> list[dict[str, Any]]:
    """Pages that belong to no bundle (spec §3): not in any index; a column without its table."""
    rows = c.execute(f"""SELECT p.id, p.title, p.type FROM pages p WHERE {NOT_MERGED} AND (
                           (p.type NOT IN ('index', 'schema_column') AND p.id NOT IN (SELECT child_page_id FROM index_members))
                           OR (p.type = 'schema_column' AND p.id NOT IN (
                            SELECT source_id FROM relations WHERE type = 'PART_OF' AND removed_at IS NULL)))
                         ORDER BY p.title, p.id""").fetchall()
    return [{"id": r["id"], "title": r["title"], "type": r["type"]} for r in rows]


def list_sessions(store: Path) -> dict[str, Any]:
    with closing(_open(store)) as c:
        tops = c.execute(f"""SELECT p.id, p.title, p.summary_md, p.version, p.updated_at FROM pages p
                             WHERE p.type = 'index' AND {NOT_MERGED}
                             AND p.id NOT IN (SELECT child_page_id FROM index_members)
                             ORDER BY p.title, p.id""").fetchall()
        sessions = []
        for top in tops:
            children = _children(c, top["id"])
            sessions.append({
                "id": top["id"], "title": top["title"], "summary_line": summary_line(top["summary_md"]),
                "version": top["version"], "updated_at": top["updated_at"],
                "children": [{"id": r["id"], "title": r["title"], "type": r["type"], "position": r["position"]} for r in children],
                "source_count": len(_sources_of(c, [top["id"], *(r["id"] for r in children)])),
            })
        return {"sessions": sessions, "unbundled": unbundled_pages(c)}


def session_detail(store: Path, index_id: str) -> dict[str, Any]:
    with closing(_open(store)) as c:
        row = c.execute("SELECT id FROM pages WHERE id = ?", (index_id,)).fetchone()
        page = c.execute("SELECT * FROM pages WHERE id = ?", (db.resolve_redirect(c, index_id),)).fetchone() if row else None
        if page is None or page["type"] != "index":
            raise NotFoundError(f"session not found (no index page): {index_id}")
        children = _children(c, page["id"])
        parents = c.execute(f"""SELECT p.id, p.title FROM index_members m JOIN pages p ON p.id = m.index_page_id
                                WHERE m.child_page_id = ? AND {NOT_MERGED} ORDER BY p.title""", (page["id"],)).fetchall()
        sources = _sources_of(c, [page["id"], *(r["id"] for r in children)])
    return {
        "id": page["id"], "title": page["title"], "summary_md": page["summary_md"], "version": page["version"],
        "updated_at": page["updated_at"], "parents": [{"id": p["id"], "title": p["title"]} for p in parents],
        "children": [{"id": r["id"], "title": r["title"], "type": r["type"], "status": r["status"],
                      "summary_line": summary_line(r["summary_md"]), "position": r["position"]} for r in children],
        "sources": [_source_head(s) for s in sources],
    }


# ---------------------------------------------------------------- sources


def _meta(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except ValueError:
        return {}


def source_title(row: sqlite3.Row) -> str:
    meta = _meta(row)
    locator = row["locator"] or ""
    if row["kind"] == "web" and meta.get("title"):
        return str(meta["title"])
    if row["kind"] in FILE_KINDS:
        return PureWindowsPath(str(meta.get("path") or locator)).name or locator
    return locator


def _source_head(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "kind": row["kind"], "kind_label": KIND_LABELS.get(row["kind"], row["kind"]), "title": source_title(row)}


def list_sources(store: Path, kind: str | None = None, q: str | None = None) -> dict[str, Any]:
    if kind and kind not in SOURCE_KINDS:
        raise ValidationError(f"kind must be one of {sorted(SOURCE_KINDS)}")
    with closing(_open(store)) as c:
        rows = c.execute("SELECT * FROM sources" + (" WHERE kind = ?" if kind else "") + " ORDER BY captured_at DESC, rowid DESC",
                         (kind,) if kind else ()).fetchall()
        counts = {r[0]: r[1] for r in c.execute(
            f"SELECT sp.source_id, COUNT(DISTINCT sp.page_id) FROM ({SOURCE_PAGES}) sp JOIN pages p ON p.id = sp.page_id "
            f"WHERE {NOT_MERGED} GROUP BY sp.source_id")}
    needle = (q or "").strip().casefold()
    out = []
    for row in rows:
        title = source_title(row)
        if needle and needle not in title.casefold() and needle not in (row["locator"] or "").casefold():
            continue
        out.append({**_source_head(row), "locator": row["locator"], "captured_at": row["captured_at"],
                    "parse_status": row["parse_status"], "page_count": counts.get(row["id"], 0)})
    return {"sources": out}


def source_detail(store: Path, source_id: str) -> dict[str, Any]:
    with closing(_open(store)) as c:
        row = c.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"source not found: {source_id}")
        evidence = c.execute("""SELECT id, excerpt, section_locator, evidence_kind FROM evidence WHERE source_id = ?
                                ORDER BY rowid""", (source_id,)).fetchall()
        pages = c.execute(f"""SELECT DISTINCT p.id, p.title, p.type FROM ({SOURCE_PAGES}) sp JOIN pages p ON p.id = sp.page_id
                              WHERE sp.source_id = ? AND {NOT_MERGED} ORDER BY p.title, p.id""", (source_id,)).fetchall()
    text = row["content"]
    try:
        diagnostics = json.loads(row["diagnostics_json"] or "[]")
    except ValueError:
        diagnostics = []
    return {
        **_source_head(row), "locator": row["locator"], "captured_at": row["captured_at"], "parse_status": row["parse_status"],
        "diagnostics": diagnostics,
        "text": text[:MAX_TEXT] if text is not None else None,
        "text_truncated": text is not None and len(text) > MAX_TEXT,
        "excerpt": row["accessible_excerpt"],
        "evidence": [{"id": e["id"], "excerpt": e["excerpt"], "section_locator": e["section_locator"], "evidence_kind": e["evidence_kind"]}
                     for e in evidence],
        "verification": _meta(row).get("verification") if row["kind"] == "web" else None,
        "pages": [{"id": p["id"], "title": p["title"], "type": p["type"]} for p in pages],
    }
