"""Backup, restore, export to Markdown + manifest, and import back.

Export writes one Markdown file per page and a manifest with every stored row, from a single
consistent snapshot. Import matches rows by UUID only (never by file order or title), checks
references and versions, and applies everything in one transaction. A page's summary and body may
be edited in its Markdown file; on import such an edit becomes a new version of that page.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db
from .db import ConflictError, NotFoundError, SCHEMA_VERSION, ValidationError, sha256, to_json
from .index import rebuild, reindex

FORMAT = "buildlog-export"
FORMAT_VERSION = 1
# Insert order respects foreign keys; keys identify a row across stores.
TABLES: list[tuple[str, tuple[str, ...]]] = [
    ("pages", ("id",)),
    ("sources", ("id",)),
    ("evidence", ("id",)),
    ("page_sources", ("page_id", "evidence_id")),
    ("revisions", ("id",)),
    ("runs", ("id",)),
    ("term_senses", ("id",)),
    ("aliases", ("normalized_expression", "term_sense_id")),
    ("relations", ("id",)),
    ("index_members", ("index_page_id", "child_page_id")),
    ("schema_objects", ("page_id",)),
    ("join_paths", ("id",)),
    ("join_path_tables", ("join_path_id", "page_id")),
    ("rules", ("id",)),
    ("object_evidence", ("object_type", "object_id", "evidence_id")),
    ("research_requests", ("id",)),
    ("feedback", ("id",)),
    ("page_redirects", ("from_page_id",)),
]
VERSIONED = {"pages", "term_senses", "relations", "join_paths", "rules"}
SUMMARY_MARK = "<!-- buildlog:summary -->"
BODY_MARK = "<!-- buildlog:body -->"
END_MARK = "<!-- buildlog:end — 아래는 참고용으로 다시 만든 내용이며 가져오기에서 읽지 않습니다 -->"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _integrity(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
        result = c.execute("PRAGMA integrity_check").fetchone()[0]
        version = c.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0] if version else 0
    return {"integrity": result, "schema_version": int(version[0]) if version else None, "pages": pages}


# ---------------------------------------------------------------- backup / restore


def backup(store: Path, out: Path, force: bool = False) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    if out.exists() and not force:
        raise ValidationError(f"{out} exists; pass --force to replace it")
    if out.resolve() == store.resolve():
        raise ValidationError("backup target is the store itself")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    with closing(db.connect(store)) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)  # consistent even while other processes write
    check = _integrity(tmp)
    if check["integrity"] != "ok":
        tmp.unlink(missing_ok=True)
        raise ValidationError(f"backup failed the integrity check: {check['integrity']}")
    tmp.replace(out)
    return {"store": str(store), "backup": str(out), "bytes": out.stat().st_size, **check}


def restore(store: Path, source: Path) -> dict[str, Any]:
    from .serving import running_url

    if not source.exists():
        raise NotFoundError(f"backup does not exist: {source}")
    if source.resolve() == store.resolve():
        raise ValidationError("restore source is the store itself")
    check = _integrity(source)
    if check["integrity"] != "ok":
        raise ValidationError(f"backup failed the integrity check: {check['integrity']}")
    if check["schema_version"] is None or check["schema_version"] > SCHEMA_VERSION:
        raise ValidationError(f"backup schema version {check['schema_version']} cannot be restored by this Buildlog ({SCHEMA_VERSION})")
    if running_url(store):
        raise ValidationError("a review server is using this store; stop it first (serve --stop)")
    saved = None
    if store.exists():
        saved = store.with_name(f"{store.stem}.pre-restore-{_stamp()}{store.suffix}")
        backup(store, saved)
    store.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(store)) as dst:
        src.backup(dst)
    db.initialize(store)  # bring an older backup up to the current schema
    with db.write(store) as c:
        rebuild(c)
    return {"store": str(store), "restored_from": str(source), "previous_store_saved_as": str(saved) if saved else None,
            **_integrity(store)}


# ---------------------------------------------------------------- export


def _snapshot(store: Path) -> sqlite3.Connection:
    memory = sqlite3.connect(":memory:")
    with closing(db.connect(store)) as src:
        src.backup(memory)
    memory.row_factory = sqlite3.Row
    return memory


def _rows(c: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _scope(rows: dict[str, list[dict[str, Any]]], page_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Keep the given pages and everything that belongs to them."""
    pages = set(page_ids)
    known = {r["id"] for r in rows["pages"]}
    missing = pages - known
    if missing:
        raise NotFoundError(f"pages not found: {sorted(missing)}")
    # Columns of a kept table travel with it.
    for so in rows["schema_objects"]:
        if so["object_type"] == "table" and so["page_id"] in pages:
            pages |= {c["page_id"] for c in rows["schema_objects"]
                      if c["object_type"] == "column" and c["norm_key"].startswith(so["norm_key"] + ".")}
    out: dict[str, list[dict[str, Any]]] = {t: [] for t, _ in TABLES}
    out["pages"] = [r for r in rows["pages"] if r["id"] in pages]
    out["page_sources"] = [r for r in rows["page_sources"] if r["page_id"] in pages]
    out["term_senses"] = [r for r in rows["term_senses"] if r["page_id"] in pages]
    terms = {r["id"] for r in out["term_senses"]}
    out["aliases"] = [r for r in rows["aliases"] if r["term_sense_id"] in terms]
    out["relations"] = [r for r in rows["relations"] if r["source_id"] in pages and r["target_id"] in pages]
    out["index_members"] = [r for r in rows["index_members"] if r["index_page_id"] in pages and r["child_page_id"] in pages]
    out["schema_objects"] = [r for r in rows["schema_objects"] if r["page_id"] in pages]
    path_tables: dict[str, set[str]] = {}
    for r in rows["join_path_tables"]:
        path_tables.setdefault(r["join_path_id"], set()).add(r["page_id"])
    paths = {p for p, t in path_tables.items() if t <= pages}
    out["join_paths"] = [r for r in rows["join_paths"] if r["id"] in paths]
    out["join_path_tables"] = [r for r in rows["join_path_tables"] if r["join_path_id"] in paths]
    out["rules"] = [r for r in rows["rules"] if r["scope_page_id"] in pages]
    kept = {("term", t) for t in terms} | {("relation", r["id"]) for r in out["relations"]} | \
           {("join_path", p) for p in paths} | {("rule", r["id"]) for r in out["rules"]}
    out["object_evidence"] = [r for r in rows["object_evidence"] if (r["object_type"], r["object_id"]) in kept]
    evidence = {r["evidence_id"] for r in out["page_sources"]} | {r["evidence_id"] for r in out["object_evidence"]}
    out["evidence"] = [r for r in rows["evidence"] if r["id"] in evidence]
    sources = {r["source_id"] for r in out["evidence"]}
    out["sources"] = [r for r in rows["sources"] if r["id"] in sources]
    objects = pages | terms | {r["id"] for r in out["relations"]} | paths | {r["id"] for r in out["rules"]}
    out["revisions"] = [r for r in rows["revisions"] if r["object_id"] in objects]
    out["research_requests"] = [dict(r, run_id=None) for r in rows["research_requests"] if r["page_id"] in pages]
    out["feedback"] = [r for r in rows["feedback"] if r["target_id"] in objects]
    out["page_redirects"] = [r for r in rows["page_redirects"] if r["from_page_id"] in pages and r["to_page_id"] in pages]
    return out


def _page_file(page: dict[str, Any], rendered: str) -> str:
    front = ["---", f"buildlog_id: {page['id']}", f"type: {page['type']}", f"version: {page['version']}",
             f"status: {page['status']}", f"title: {json.dumps(page['title'], ensure_ascii=False)}", "---", ""]
    return "\n".join(front + [
        "<!-- 요약과 본문은 고쳐서 `buildlog import`로 다시 가져올 수 있습니다. 표시 줄은 지우지 마세요. -->",
        SUMMARY_MARK, page["summary_md"], BODY_MARK, page["body_md"], END_MARK, "", rendered,
    ])


def export(store: Path, out_dir: Path, page_ids: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    db.initialize(store)
    if out_dir.exists() and any(out_dir.iterdir()):
        if not force:
            raise ValidationError(f"{out_dir} is not empty; pass --force to replace an earlier export there")
        if not (out_dir / "manifest.json").exists():
            raise ValidationError(f"{out_dir} does not hold a Buildlog export; refusing to clear it")
        shutil.rmtree(out_dir)
    snap = _snapshot(store)
    try:
        rows = {table: _rows(snap, table) for table, _ in TABLES}
        if page_ids:
            rows = _scope(rows, page_ids)
        from .read import _page_record, render_markdown

        files: dict[str, Any] = {}
        (out_dir / "pages").mkdir(parents=True, exist_ok=True)
        for page in rows["pages"]:
            rendered = render_markdown(_page_record(snap, dict(page)))
            name = f"pages/{page['id']}.md"
            (out_dir / name).write_text(_page_file(page, rendered), encoding="utf-8", newline="\n")
            files[page["id"]] = {"file": name, "version": page["version"], "title_sha256": sha256(page["title"]),
                                 "summary_sha256": sha256(page["summary_md"]), "body_sha256": sha256(page["body_md"]),
                                 "status": page["status"]}
    finally:
        snap.close()
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "schema_version": SCHEMA_VERSION,
                "exported_at": db.now(), "scope": page_ids or "all", "pages": files, "tables": rows}
    manifest["tables_sha256"] = sha256(rows)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"store": str(store), "out": str(out_dir), "pages": len(files),
            "rows": {t: len(r) for t, r in rows.items() if r}, "scope": manifest["scope"]}


# ---------------------------------------------------------------- import


def _parse_page_file(text: str, where: str) -> dict[str, Any]:
    m = re.match(r"---\n(.*?)\n---\n", text.replace("\r\n", "\n"), re.S)
    if not m:
        raise ValidationError(f"{where}: missing front matter")
    front: dict[str, Any] = {}
    for line in m.group(1).split("\n"):
        key, _, value = line.partition(": ")
        front[key.strip()] = value.strip()
    rest = text.replace("\r\n", "\n")[m.end():]
    try:
        summary = rest.split(SUMMARY_MARK + "\n", 1)[1].split("\n" + BODY_MARK + "\n", 1)
        body = summary[1].split("\n" + END_MARK, 1)[0]
    except IndexError as error:
        raise ValidationError(f"{where}: the buildlog:summary / body / end markers were removed or reordered") from error
    try:
        title = json.loads(front.get("title", '""'))
    except json.JSONDecodeError as error:
        raise ValidationError(f"{where}: title must stay a quoted string") from error
    return {"id": front.get("buildlog_id"), "version": int(front.get("version", "0") or 0), "status": front.get("status"),
            "title": title, "summary_md": summary[0], "body_md": body}


def _key(row: dict[str, Any], key: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row[k] for k in key)


def import_export(store: Path, in_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    manifest_path = in_dir / "manifest.json"
    if not manifest_path.exists():
        raise NotFoundError(f"no manifest.json in {in_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"manifest.json is not valid JSON: {error}") from error
    if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise ValidationError("not a Buildlog export manifest (format or format_version differs)")
    if int(manifest.get("schema_version", 0)) > SCHEMA_VERSION:
        raise ValidationError(f"export schema version {manifest['schema_version']} is newer than this Buildlog ({SCHEMA_VERSION})")
    rows: dict[str, list[dict[str, Any]]] = manifest.get("tables") or {}
    if sha256(rows) != manifest.get("tables_sha256"):
        raise ValidationError("manifest tables were changed after export (hash mismatch); edit pages through their Markdown files")
    unknown = set(rows) - {t for t, _ in TABLES}
    if unknown:
        raise ValidationError(f"manifest has unknown tables {sorted(unknown)}")

    # Edits made in the page files.
    edits: dict[str, dict[str, Any]] = {}
    for page_id, info in (manifest.get("pages") or {}).items():
        path = in_dir / info["file"]
        if not path.exists():
            raise ValidationError(f"{info['file']} listed in the manifest is missing")
        parsed = _parse_page_file(path.read_text(encoding="utf-8"), info["file"])
        if parsed["id"] != page_id or parsed["version"] != info["version"]:
            raise ValidationError(f"{info['file']}: buildlog_id/version in the front matter must not be changed")
        changes = {}
        if sha256(parsed["title"]) != info["title_sha256"]:
            changes["title"] = parsed["title"]
        if sha256(parsed["summary_md"]) != info["summary_sha256"]:
            changes["summary_md"] = parsed["summary_md"]
        if sha256(parsed["body_md"]) != info["body_sha256"]:
            changes["body_md"] = parsed["body_md"]
        if parsed["status"] != info["status"]:
            if parsed["status"] not in db.PAGE_STATUSES:
                raise ValidationError(f"{info['file']}: status must be one of {sorted(db.PAGE_STATUSES)}")
            changes["status"] = parsed["status"]
        if changes:
            edits[page_id] = changes

    db.initialize(store)
    report: dict[str, Any] = {"store": str(store), "from": str(in_dir), "dry_run": dry_run, "inserted": {}, "updated": {},
                              "unchanged": {}, "kept_existing": [], "markdown_edits": [], "conflicts": []}
    touched: set[str] = set()
    with closing(db.connect(store)) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            for table, key in TABLES:
                columns = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
                for row in rows.get(table, []):
                    row = {k: row.get(k) for k in columns}
                    where = " AND ".join(f"{k} = ?" for k in key)
                    current = c.execute(f"SELECT * FROM {table} WHERE {where}", _key(row, key)).fetchone()
                    if current is None:
                        try:
                            c.execute(f"INSERT INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                      tuple(row[k] for k in columns))
                        except sqlite3.IntegrityError as error:
                            report["conflicts"].append(f"{table} {_key(row, key)}: {error}")
                            continue
                        report["inserted"][table] = report["inserted"].get(table, 0) + 1
                        if table == "pages":
                            touched.add(row["id"])
                        continue
                    current = dict(current)
                    if current == row:
                        report["unchanged"][table] = report["unchanged"].get(table, 0) + 1
                        continue
                    if table in VERSIONED:
                        if row["version"] > current["version"]:
                            sets = [k for k in columns if k not in key]
                            c.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in sets)} WHERE {where}",
                                      tuple(row[k] for k in sets) + _key(row, key))
                            report["updated"][table] = report["updated"].get(table, 0) + 1
                            touched.add(row.get("id") if table == "pages" else row.get("page_id") or row.get("scope_page_id") or "")
                        else:
                            report["conflicts"].append(
                                f"{table} {row['id']}: the store has version {current['version']} that differs from the exported version "
                                f"{row['version']}; it changed after this export")
                    else:
                        report["kept_existing"].append(f"{table} {_key(row, key)}")
            # Page edits from Markdown become new versions, only on top of the exported version.
            for page_id, changes in edits.items():
                info = manifest["pages"][page_id]
                current = c.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
                if current is None or current["version"] != info["version"]:
                    report["conflicts"].append(f"page {page_id}: edited in Markdown at version {info['version']}, "
                                               f"but the store has version {current['version'] if current else 'none'}")
                    continue
                values = dict(current) | changes
                if "body_md" in changes and "summary_md" not in changes:
                    values["summary_dirty"] = 1
                elif "summary_md" in changes:
                    values["summary_dirty"] = 0
                version = current["version"] + 1
                ts = db.now()
                c.execute("""UPDATE pages SET title = ?, summary_md = ?, body_md = ?, status = ?, summary_dirty = ?, version = ?,
                             updated_at = ? WHERE id = ?""",
                          (values["title"], values["summary_md"], values["body_md"], values["status"], values["summary_dirty"],
                           version, ts, page_id))
                snapshot = {k: v for k, v in values.items() if k != "created_at"} | {"version": version, "updated_at": ts}
                db.add_revision(c, "page", page_id, version, snapshot, "imported Markdown edit", ts, str(uuid.uuid4()))
                report["markdown_edits"].append({"page_id": page_id, "fields": sorted(changes), "version": version})
                touched.add(page_id)
            problems = c.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                report["conflicts"] += [f"missing reference: {tuple(p)}" for p in problems[:20]]
            if report["conflicts"]:
                raise ConflictError("IMPORT_CONFLICT: " + "; ".join(report["conflicts"][:10]))
            # Imported terms, rules and paths change what their pages are found by: rebuild the derived index.
            rebuild(c)
            report["pages_touched"] = len([t for t in touched if t])
            if dry_run:
                c.rollback()
            else:
                c.commit()
        except BaseException:
            c.rollback()
            raise
    return report
