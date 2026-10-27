"""Graph data for the review UI. Built from SQLite on every request; nothing is cached on disk."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import NotFoundError, ValidationError
from .read import read_object, render_document, render_markdown

DEFAULT_NODES, DEFAULT_EDGES = 150, 300
MAX_NODES, MAX_EDGES = 500, 1000


def _redirected(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT from_page_id FROM page_redirects")}


def _run_seeds(c: sqlite3.Connection, run_id: str) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"run not found: {run_id}")
    result = json.loads(row["result_json"]) if row["result_json"] else {}
    flags: dict[str, str] = {}
    for item in result.get("created", {}).get("pages", []):
        flags[item["id"]] = "created"
    for item in result.get("updated", []):
        if item.get("type") == "page":
            flags.setdefault(item["id"], "updated")
    for item in result.get("unchanged", []):
        if item.get("type") == "page":
            flags.setdefault(item["id"], "unchanged")
    for item in result.get("created", {}).get("research_requests", []):
        flags.setdefault(item["page_id"], "researched")
    info = {"id": row["id"], "idempotency_key": row["idempotency_key"], "status": row["status"], "stage": row["stage"],
            "research_status": row["research_status"], "updated_at": row["updated_at"]}
    return list(flags), flags, info


def latest_run(c: sqlite3.Connection) -> str | None:
    for row in c.execute("SELECT id, result_json FROM runs WHERE status = 'succeeded' ORDER BY updated_at DESC, rowid DESC"):
        result = json.loads(row["result_json"] or "{}")
        if result.get("created", {}).get("pages") or any(u.get("type") == "page" for u in result.get("updated", [])):
            return row["id"]
    return None


def list_runs(store: Path, limit: int = 30) -> dict[str, Any]:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        runs = []
        for row in c.execute("""SELECT id, idempotency_key, status, stage, research_status, updated_at, result_json FROM runs
                                ORDER BY updated_at DESC, rowid DESC LIMIT ?""", (limit,)):
            result = json.loads(row["result_json"] or "{}")
            pages = result.get("created", {}).get("pages", [])
            runs.append({"id": row["id"], "idempotency_key": row["idempotency_key"], "status": row["status"], "stage": row["stage"],
                         "research_status": row["research_status"], "updated_at": row["updated_at"],
                         "created_pages": len(pages), "first_title": pages[0]["title"] if pages else None})
    return {"runs": runs}


def graph(store: Path, run_id: str | None = None, focus: str | None = None, q: str | None = None,
          types: list[str] | None = None, include_columns: bool = False,
          cap_nodes: int = DEFAULT_NODES, cap_edges: int = DEFAULT_EDGES) -> dict[str, Any]:
    if not (1 <= cap_nodes <= MAX_NODES) or not (1 <= cap_edges <= MAX_EDGES):
        raise ValidationError(f"cap_nodes must be 1..{MAX_NODES} and cap_edges 1..{MAX_EDGES}")
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    run_info = None
    flags: dict[str, str] = {}
    with closing(db.connect(store)) as c:
        redirected = _redirected(c)
        if focus:
            page = c.execute("SELECT id FROM pages WHERE id = ?", (focus,)).fetchone()
            if page is None:
                raise NotFoundError(f"page not found: {focus}")
            seeds = [db.resolve_redirect(c, focus)]
            mode = "focus"
        elif q:
            from .search import query

            found = query(store, q, limit=20, max_bytes=10_000_000)
            seeds = [r["id"] for r in found["results"]] + [r["id"] for r in found["out_of_scope"]]
            mode = "search"
        else:
            run_id = run_id or latest_run(c)
            if run_id:
                seeds, flags, run_info = _run_seeds(c, run_id)
                mode = "run"
            else:
                seeds = [r["id"] for r in c.execute("SELECT id FROM pages ORDER BY updated_at DESC LIMIT 20")]
                mode = "recent"
        seeds = [db.resolve_redirect(c, s) for s in seeds]
        seeds = list(dict.fromkeys(s for s in seeds if s not in redirected))

        def page_rows(ids: list[str]) -> dict[str, sqlite3.Row]:
            if not ids:
                return {}
            marks = ",".join("?" * len(ids))
            return {r["id"]: r for r in c.execute(
                f"""SELECT p.id, p.title, p.type, p.status, p.version, p.summary_dirty, so.system_key
                    FROM pages p LEFT JOIN schema_objects so ON so.page_id = p.id WHERE p.id IN ({marks})""", ids)}

        neighbor_ids: list[str] = []
        if seeds:
            marks = ",".join("?" * len(seeds))
            for r in c.execute(f"""SELECT source_id, target_id FROM relations WHERE removed_at IS NULL
                                   AND (source_id IN ({marks}) OR target_id IN ({marks}))""", (*seeds, *seeds)):
                neighbor_ids += [r["source_id"], r["target_id"]]
            for r in c.execute(f"""SELECT index_page_id, child_page_id FROM index_members
                                   WHERE index_page_id IN ({marks}) OR child_page_id IN ({marks})""", (*seeds, *seeds)):
                neighbor_ids += [r["index_page_id"], r["child_page_id"]]
        ordered = list(dict.fromkeys([*seeds, *neighbor_ids]))
        rows = page_rows(ordered)
        seed_set = set(seeds)
        kept = []
        for page_id in ordered:
            row = rows.get(page_id)
            if row is None or page_id in redirected:
                continue
            if page_id not in seed_set:
                if row["type"] == "schema_column" and not include_columns:
                    continue
                if types and row["type"] not in types:
                    continue
            kept.append(row)
        dropped_nodes = max(0, len(kept) - cap_nodes)
        kept = kept[:cap_nodes]
        node_ids = {r["id"] for r in kept}
        nodes = [{"id": r["id"], "title": r["title"], "type": r["type"], "status": r["status"], "version": r["version"],
                  "summary_dirty": bool(r["summary_dirty"]), "system_key": r["system_key"], "seed": r["id"] in seed_set,
                  "flag": flags.get(r["id"])} for r in kept]

        edges = []
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            ids = tuple(node_ids)
            for r in c.execute(f"""SELECT id, source_id, target_id, type, status, version, explanation_md FROM relations
                                   WHERE removed_at IS NULL AND source_id IN ({marks}) AND target_id IN ({marks})
                                   ORDER BY type, id""", ids + ids):
                edges.append({"id": r["id"], "source": r["source_id"], "target": r["target_id"], "type": r["type"],
                              "status": r["status"], "version": r["version"], "label": r["explanation_md"][:160], "kind": "relation"})
            for r in c.execute(f"""SELECT index_page_id, child_page_id, position FROM index_members
                                   WHERE index_page_id IN ({marks}) AND child_page_id IN ({marks})""", ids + ids):
                edges.append({"id": f"index:{r['index_page_id']}:{r['child_page_id']}", "source": r["index_page_id"],
                              "target": r["child_page_id"], "type": "INDEX", "status": "extracted", "version": None,
                              "label": f"목차 {r['position']}", "kind": "index"})
        dropped_edges = max(0, len(edges) - cap_edges)
        edges = edges[:cap_edges]
    return {"mode": mode, "run": run_info, "seeds": seeds, "nodes": nodes, "edges": edges,
            "truncated": {"nodes": dropped_nodes, "edges": dropped_edges},
            "caps": {"nodes": cap_nodes, "edges": cap_edges}}


def object_detail(store: Path, object_id: str) -> dict[str, Any]:
    """Everything the side panel shows for one item: record, rendered Markdown, history, feedback."""
    record = read_object(store, object_id)
    kind = record["object_type"]
    obj_id = record["page"]["id"] if kind == "page" else record[kind]["id"]
    with closing(db.connect(store)) as c:
        revisions = [dict(r) for r in c.execute(
            """SELECT id, version, reason, changed_at FROM revisions WHERE object_id = ? ORDER BY version DESC LIMIT 20""", (obj_id,))]
        feedback = [dict(r) for r in c.execute(
            """SELECT id, target_version, feedback_text, status, resulting_revision, created_at FROM feedback
               WHERE target_id = ? ORDER BY created_at DESC""", (obj_id,))]
        merged_from = [dict(r) for r in c.execute(
            """SELECT r.from_page_id AS id, p.title, r.merged_at FROM page_redirects r JOIN pages p ON p.id = r.from_page_id
               WHERE r.to_page_id = ?""", (obj_id,))] if kind == "page" else []
    return {"object_type": kind, "id": obj_id, "record": record, "markdown": render_markdown(record),
            "document_md": render_document(record) if kind == "page" else None,
            "revisions": revisions, "feedback": feedback, "merged_from": merged_from}
