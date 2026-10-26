"""Apply a normalized save plan to the store in one transaction."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import db
from .db import ConflictError, NotFoundError, ValidationError, normalize_text, to_json
from .index import reindex
from .plan import normalize_plan

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def new_id() -> str:
    return str(uuid.uuid4())


def ingest(store: Path, raw_plan: Any) -> dict[str, Any]:
    plan, input_hash = normalize_plan(raw_plan)
    db.initialize(store)
    # The idempotency check runs inside the locked transaction so two concurrent
    # saves of the same key cannot both write.
    with db.write(store) as connection:
        existing = connection.execute(
            "SELECT input_hash, result_json FROM runs WHERE idempotency_key = ?", (plan["idempotency_key"],)
        ).fetchone()
        if existing is not None:
            result = json.loads(existing["result_json"])
            if existing["input_hash"] != input_hash and not _matches_legacy_hash(connection, existing["input_hash"], result, raw_plan):
                raise ConflictError(
                    f"IDEMPOTENCY_CONFLICT: key was used with different input (existing run {result.get('run_id')})"
                )
            result["reused"] = True
            return result
        return _Apply(connection, store, plan, input_hash).run()


def _matches_legacy_hash(connection: sqlite3.Connection, stored_hash: str, result: dict[str, Any], raw_plan: Any) -> bool:
    # Early Phase-1 development stores hashed the plan after filling in captured_at.
    # Accept only that exact legacy shape so those records stay idempotent.
    if not isinstance(raw_plan, dict) or "page" not in raw_plan or "page_id" not in result:
        return False
    from .plan import _legacy_plan

    legacy = _legacy_plan(raw_plan)
    source = connection.execute(
        """SELECT s.captured_at FROM sources s JOIN evidence e ON e.source_id = s.id
           JOIN page_sources ps ON ps.evidence_id = e.id WHERE ps.page_id = ? LIMIT 1""",
        (result["page_id"],),
    ).fetchone()
    if source is not None:
        legacy["source"]["captured_at"] = source["captured_at"]
    return stored_hash == db.sha256(legacy)


def _fold(value: str | None) -> str:
    return (value or "").casefold()


class _Apply:
    def __init__(self, connection: sqlite3.Connection, store: Path, plan: dict[str, Any], input_hash: str) -> None:
        self.c = connection
        self.store = store
        self.plan = plan
        self.input_hash = input_hash
        self.ts = db.now()
        self.run_id = new_id()
        self.page_refs: dict[str, str] = {}
        self.source_ids: dict[str, str] = {}
        self.evidence_ids: dict[str, str] = {}
        self.created: dict[str, list[dict[str, Any]]] = {}
        self.updated: list[dict[str, Any]] = []
        self.reused: list[dict[str, Any]] = []
        self.warnings: list[str] = list(plan["warnings"])
        self.touched: set[str] = set()  # pages whose search index rows must be refreshed

    # ------------------------------------------------------------ helpers

    def _record(self, kind: str, item: dict[str, Any]) -> None:
        self.created.setdefault(kind, []).append(item)

    def _revision(self, object_type: str, object_id: str, version: int, snapshot: dict[str, Any], reason: str) -> None:
        db.add_revision(self.c, object_type, object_id, version, snapshot, reason, self.ts, new_id())

    def _link_evidence(self, object_type: str, object_id: str, evidence_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            if object_type == "page":
                self.c.execute("INSERT OR IGNORE INTO page_sources(page_id, evidence_id) VALUES (?, ?)", (object_id, evidence_id))
            else:
                self.c.execute(
                    "INSERT OR IGNORE INTO object_evidence(object_type, object_id, evidence_id) VALUES (?, ?, ?)",
                    (object_type, object_id, evidence_id),
                )

    def _evidence(self, refs: list[str]) -> list[str]:
        return [self.evidence_ids[ref] for ref in refs]

    def _add_evidence(self, source_id: str, locator: str, excerpt: str, kind: str = "observed",
                      start: int | None = None, end: int | None = None) -> str:
        evidence_id = new_id()
        self.c.execute(
            """INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind, start_line, end_line)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (evidence_id, source_id, locator, excerpt, kind, start, end),
        )
        return evidence_id

    def _insert_page(self, page_type: str, title: str, summary: str, body: str, status: str) -> str:
        page_id = new_id()
        self.c.execute(
            """INSERT INTO pages(id, type, title, summary_md, body_md, status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (page_id, page_type, title, summary, body, status, self.ts, self.ts),
        )
        self.touched.add(page_id)
        snapshot = {"id": page_id, "type": page_type, "title": title, "summary_md": summary, "body_md": body,
                    "status": status, "version": 1, "updated_at": self.ts}
        self._revision("page", page_id, 1, snapshot, "created")
        return page_id

    def _update_page(self, page_id: str, expected_version: int | None, changes: dict[str, Any], reason: str) -> int:
        page = self.c.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            raise NotFoundError(f"page not found: {page_id}")
        if expected_version is not None and page["version"] != expected_version:
            raise ConflictError(f"VERSION_CONFLICT: page {page_id} expected {expected_version}, current {page['version']}")
        values = dict(page) | changes
        version = page["version"] + 1
        self.c.execute(
            """UPDATE pages SET title = ?, summary_md = ?, body_md = ?, status = ?, version = ?, updated_at = ? WHERE id = ?""",
            (values["title"], values["summary_md"], values["body_md"], values["status"], version, self.ts, page_id),
        )
        snapshot = {k: values[k] for k in ("id", "type", "title", "summary_md", "body_md", "status")} | {"version": version, "updated_at": self.ts}
        self._revision("page", page_id, version, snapshot, reason)
        self.touched.add(page_id)
        self.updated.append({"type": "page", "id": page_id, "version": version, "title": values["title"]})
        return version

    # ------------------------------------------------------------ reference resolution

    def _tables(self, system_key: str) -> list[sqlite3.Row]:
        prefix = _fold(system_key) + ":"
        return self.c.execute(
            "SELECT * FROM schema_objects WHERE object_type = 'table' AND substr(norm_key, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()

    def table(self, system_key: str, name: str, where: str) -> sqlite3.Row:
        """Find a stored table by 'name' or 'schema.name' within a system."""
        parts = name.split(".")
        if len(parts) > 2:
            raise ValidationError(f"{where}: table reference {name!r} has too many parts")
        schema = parts[0] if len(parts) == 2 else None
        table_name = parts[-1]
        rows = [r for r in self._tables(system_key) if _fold(json.loads(r["metadata_json"])["name"]) == _fold(table_name)
                and (schema is None or _fold(r["schema_name"]) == _fold(schema))]
        if not rows:
            raise ValidationError(f"{where}: table {name!r} is not known in system {system_key!r}")
        if len(rows) > 1:
            schemas = sorted(r["schema_name"] or "(none)" for r in rows)
            raise ValidationError(f"{where}: table {name!r} exists in several schemas of {system_key!r} {schemas}; qualify it as schema.table")
        return rows[0]

    def page_id(self, ref: str, where: str) -> str:
        if ref in self.page_refs:
            return self.page_refs[ref]
        if _UUID_RE.match(ref):
            row = self.c.execute("SELECT id FROM pages WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise ValidationError(f"{where}: page {ref} does not exist")
            return row["id"]
        if ":" in ref:
            system_key, rest = ref.split(":", 1)
            parts = rest.split(".")
            candidates: list[tuple[str, str | None]] = []  # (table ref, column)
            if len(parts) == 1:
                candidates = [(parts[0], None)]
            elif len(parts) == 2:
                candidates = [(rest, None), (parts[0], parts[1])]
            elif len(parts) == 3:
                candidates = [(f"{parts[0]}.{parts[1]}", parts[2])]
            found: list[str] = []
            errors: list[str] = []
            for table_ref, column in candidates:
                try:
                    table = self.table(system_key, table_ref, where)
                except ValidationError as error:
                    errors.append(str(error))
                    continue
                if column is None:
                    found.append(table["page_id"])
                    continue
                row = self.c.execute(
                    "SELECT page_id FROM schema_objects WHERE object_type = 'column' AND norm_key = ?",
                    (table["norm_key"] + "." + _fold(column),),
                ).fetchone()
                if row is not None:
                    found.append(row["page_id"])
                else:
                    errors.append(f"{where}: column {column!r} is not in table {table_ref!r}")
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ValidationError(f"{where}: reference {ref!r} is ambiguous; use system:schema.table.column")
            raise ValidationError(errors[-1] if errors else f"{where}: unknown reference {ref!r}")
        raise ValidationError(f"{where}: unknown reference {ref!r} (use a ref from this plan, a page id, or system:table[.column])")

    # ------------------------------------------------------------ steps

    def run(self) -> dict[str, Any]:
        self.c.execute(
            """INSERT INTO runs(id, idempotency_key, input_hash, stage, status, research_status, created_at, updated_at)
               VALUES (?, ?, ?, 'normalized', 'running', 'not_requested', ?, ?)""",
            (self.run_id, self.plan["idempotency_key"], self.input_hash, self.ts, self.ts),
        )
        for source in self.plan["sources"]:
            self._source(source)
        for item in self.plan["evidence"]:
            self.evidence_ids[item["ref"]] = self._add_evidence(
                self.source_ids[item["source"]], item["section_locator"], item["excerpt"], item["evidence_kind"],
                item["start_line"], item["end_line"],
            )
        for source in self.plan["sources"]:
            if source["schema"] is not None:
                self._schema(source)
        for source in self.plan["sources"]:
            if source["rows"] is not None:
                self._sample(source)
        for i, page in enumerate(self.plan["pages"]):
            self._page(page, f"pages[{i}]")
        for i, member in enumerate(self.plan["index_members"]):
            self._index_member(member, f"index_members[{i}]")
        for i, term in enumerate(self.plan["terms"]):
            self._term(term, f"terms[{i}]")
        for i, relation in enumerate(self.plan["relations"]):
            self._relation(relation, f"relations[{i}]")
        for i, path in enumerate(self.plan["join_paths"]):
            self._join_path(path, f"join_paths[{i}]")
        for i, rule in enumerate(self.plan["rules"]):
            self._rule(rule, f"rules[{i}]")
        reindex(self.c, self.touched)
        return self._finish()

    def _finish(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "run_id": self.run_id,
            "store": str(self.store),
            "reused": False,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.reused,
            "sources": [
                {"ref": s["ref"], "id": self.source_ids[s["ref"]], "kind": s["kind"], "parse_status": s["parse_status"],
                 "diagnostics": s["diagnostics"]}
                for s in self.plan["sources"]
            ],
            "warnings": self.warnings,
        }
        host_pages = [p for p in self.created.get("pages", []) if p.get("ref") is not None]
        if len(host_pages) == 1:
            result["page_id"] = host_pages[0]["id"]
            result["version"] = 1
        self.c.execute(
            "UPDATE runs SET stage = 'committed', status = 'succeeded', result_json = ?, updated_at = ? WHERE id = ?",
            (to_json(result), self.ts, self.run_id),
        )
        return result

    def _source(self, source: dict[str, Any]) -> None:
        source_id = new_id()
        self.c.execute(
            """INSERT INTO sources(id, kind, locator, captured_at, accessible_excerpt, content, content_hash,
                                   metadata_json, parse_status, diagnostics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, source["kind"], source["locator"], source["captured_at"] or self.ts, source["accessible_excerpt"],
             source["content"], source["content_hash"], to_json(source["metadata"]) if source["metadata"] else None,
             source["parse_status"], to_json(source["diagnostics"]) if source["diagnostics"] else None),
        )
        self.source_ids[source["ref"]] = source_id

    # ------------------------------------------------------------ schema

    def _table_excerpt(self, source: dict[str, Any], table: dict[str, Any]) -> tuple[str, str, int | None, int | None]:
        span = table.get("span")
        if span and source["content"]:
            lines = source["content"].split("\n")[span["start_line"] - 1:span["end_line"]]
            excerpt = "\n".join(lines).strip()
            if len(excerpt) > 600:
                excerpt = excerpt[:600] + " …"
            return f"lines {span['start_line']}-{span['end_line']}", excerpt, span["start_line"], span["end_line"]
        pointer = table.get("pointer") or "table"
        return f"json {pointer}", f"table {table['name']}", None, None

    def _schema(self, source: dict[str, Any]) -> None:
        system_key = source["schema"]["system_key"]
        source_id = self.source_ids[source["ref"]]
        table_evidence: dict[str, str] = {}
        table_meta: list[tuple[str, dict[str, Any]]] = []
        for table in source["schema"]["tables"]:
            locator, excerpt, start, end = self._table_excerpt(source, table)
            evidence_id = self._add_evidence(source_id, locator, excerpt, "observed", start, end)
            page_id, meta = self._table(system_key, table, source, evidence_id)
            table_evidence[page_id] = evidence_id
            table_meta.append((page_id, meta))
        # Foreign keys become JOINS_TO relations with status extracted, grouped per table pair.
        pairs: dict[tuple[str, str], list[str]] = {}
        for page_id, meta in table_meta:
            column_names = {_fold(c["name"]) for c in meta["columns"]}
            for fk in meta["foreign_keys"]:
                where = f"{source['ref']}: table {meta['name']} foreign key {fk['columns']}"
                missing = [c for c in fk["columns"] if _fold(c) not in column_names]
                if missing:
                    raise ValidationError(f"{where}: columns {missing} are not in the table")
                target_ref = f"{fk['target_schema']}.{fk['target_table']}" if fk.get("target_schema") else fk["target_table"]
                target = self.table(system_key, target_ref, where)
                target_meta = json.loads(target["metadata_json"])
                target_columns = {_fold(c["name"]) for c in target_meta["columns"]}
                missing = [c for c in fk["target_columns"] if _fold(c) not in target_columns]
                if missing:
                    raise ValidationError(f"{where}: target columns {missing} are not in {target_ref}")
                text = f"`{meta['name']}({', '.join(fk['columns'])})` → `{target_meta['name']}({', '.join(fk['target_columns'])})` (FK)"
                pairs.setdefault((page_id, target["page_id"]), []).append(text)
        for (from_id, to_id), texts in pairs.items():
            self._upsert_relation(from_id, to_id, "JOINS_TO", "외래키로 연결된다: " + "; ".join(texts), "extracted",
                                  [table_evidence[from_id]], f"{source['ref']} foreign keys")

    def _table(self, system_key: str, table: dict[str, Any], source: dict[str, Any], evidence_id: str) -> tuple[str, dict[str, Any]]:
        schema_name = table["schema"] or ""
        norm_key = f"{_fold(system_key)}:{_fold(schema_name)}.{_fold(table['name'])}"
        meta = {k: table.get(k) for k in ("schema", "name", "description", "columns", "primary_key", "unique",
                                           "foreign_keys", "checks", "indexes", "options_raw")}
        meta["system_key"] = system_key
        column_names = {_fold(c["name"]) for c in meta["columns"]}
        for key_name, cols in [("primary_key", meta["primary_key"])] + [("unique", u) for u in meta["unique"]]:
            missing = [c for c in cols if _fold(c) not in column_names]
            if missing:
                raise ValidationError(f"{source['ref']}: table {table['name']} {key_name} columns {missing} are not in the table")
        title = f"{schema_name + '.' if schema_name else ''}{table['name']} ({system_key})"
        description = table.get("description")
        summary = description or f"{system_key} 시스템의 {table['name']} 테이블. 설명이 원문에 없다."
        body = description or "(원문에 테이블 설명 없음)"
        existing = self.c.execute(
            "SELECT * FROM schema_objects WHERE norm_key = ? AND object_type = 'table'", (norm_key,)
        ).fetchone()
        if existing is None:
            page_id = self._insert_page("schema_table", title, summary, body, "active")
            self.c.execute(
                """INSERT INTO schema_objects(page_id, system_key, schema_name, object_name, object_type, norm_key, metadata_json)
                   VALUES (?, ?, ?, ?, 'table', ?, ?)""",
                (page_id, system_key, schema_name, table["name"], norm_key, to_json(meta)),
            )
            self._link_evidence("page", page_id, [evidence_id])
            self._record("pages", {"id": page_id, "type": "schema_table", "title": title})
            for column in meta["columns"]:
                self._column(system_key, schema_name, table["name"], norm_key, page_id, column, evidence_id)
            return page_id, meta

        page_id = existing["page_id"]
        self._link_evidence("page", page_id, [evidence_id])
        if json.loads(existing["metadata_json"]) == json.loads(to_json(meta)):
            self.reused.append({"type": "page", "id": page_id, "title": title})
            return page_id, meta
        if not source["replace_existing_schema"]:
            page = self.c.execute("SELECT version FROM pages WHERE id = ?", (page_id,)).fetchone()
            raise ConflictError(
                f"SCHEMA_CONFLICT: table {title} differs from the stored definition (page {page_id}, version {page['version']}); "
                "set replace_existing_schema: true on the source to update it"
            )
        self.c.execute("UPDATE schema_objects SET metadata_json = ? WHERE page_id = ?", (to_json(meta), page_id))
        self._update_page(page_id, None, {"title": title, "summary_md": summary, "body_md": body, "status": "active"},
                          f"schema re-ingested from {source['locator']}")
        stored_columns = {
            r["norm_key"]: r for r in self.c.execute(
                "SELECT * FROM schema_objects WHERE object_type = 'column' AND substr(norm_key, 1, ?) = ?",
                (len(norm_key) + 1, norm_key + "."),
            )
        }
        seen: set[str] = set()
        for column in meta["columns"]:
            column_key = f"{norm_key}.{_fold(column['name'])}"
            seen.add(column_key)
            row = stored_columns.get(column_key)
            if row is None:
                self._column(system_key, schema_name, table["name"], norm_key, page_id, column, evidence_id)
            elif json.loads(row["metadata_json"]) != json.loads(to_json(self._column_meta(table["name"], column))):
                self.c.execute("UPDATE schema_objects SET metadata_json = ? WHERE page_id = ?",
                               (to_json(self._column_meta(table["name"], column)), row["page_id"]))
                title_c, summary_c, body_c = self._column_text(table["name"], column)
                self._update_page(row["page_id"], None, {"title": title_c, "summary_md": summary_c, "body_md": body_c, "status": "active"},
                                  f"schema re-ingested from {source['locator']}")
                self._link_evidence("page", row["page_id"], [evidence_id])
        for column_key, row in stored_columns.items():
            if column_key not in seen:
                self._update_page(row["page_id"], None, {"status": "deprecated"},
                                  f"column no longer in schema from {source['locator']}")
        return page_id, meta

    @staticmethod
    def _column_meta(table_name: str, column: dict[str, Any]) -> dict[str, Any]:
        return {"table": table_name, **{k: column.get(k) for k in ("name", "type", "nullable", "description", "default", "generated")}}

    @staticmethod
    def _column_text(table_name: str, column: dict[str, Any]) -> tuple[str, str, str]:
        title = f"{table_name}.{column['name']}"
        description = column.get("description")
        type_text = column.get("type") or "타입 미상"
        summary = f"{description} ({type_text})" if description else f"{table_name} 테이블의 {column['name']} 컬럼 ({type_text}). 설명이 원문에 없다."
        return title, summary, description or "(원문에 컬럼 설명 없음)"

    def _column(self, system_key: str, schema_name: str, table_name: str, table_key: str, table_page: str,
                column: dict[str, Any], evidence_id: str) -> None:
        title, summary, body = self._column_text(table_name, column)
        page_id = self._insert_page("schema_column", title, summary, body, "active")
        self.c.execute(
            """INSERT INTO schema_objects(page_id, system_key, schema_name, object_name, object_type, norm_key, metadata_json)
               VALUES (?, ?, ?, ?, 'column', ?, ?)""",
            (page_id, system_key, schema_name, f"{table_name}.{column['name']}", f"{table_key}.{_fold(column['name'])}",
             to_json(self._column_meta(table_name, column))),
        )
        self._link_evidence("page", page_id, [evidence_id])
        self._upsert_relation(page_id, table_page, "PART_OF", f"`{column['name']}` 컬럼은 `{table_name}` 테이블에 속한다.",
                              "extracted", [evidence_id], "schema column", record=False)

    def _sample(self, source: dict[str, Any]) -> None:
        meta = source["metadata"]
        where = f"source {source['ref']}"
        try:
            table = self.table(meta["system_key"], meta["table"], where)
        except ValidationError as error:
            self.warnings.append(f"{error}; the sample was stored but not linked to a table")
            return
        table_meta = json.loads(table["metadata_json"])
        known = {_fold(c["name"]) for c in table_meta["columns"]}
        seen = {_fold(k) for row in source["rows"] for k in row}
        unknown = sorted(seen - known)
        if unknown:
            self.warnings.append(f"{where}: sample columns {unknown} are not in table {meta['table']}")
        header = source["content"].split("\n", 1)[0] if source["kind"] == "sample_csv" else f"{len(source['rows'])} rows"
        evidence_id = self._add_evidence(self.source_ids[source["ref"]], f"sample of {meta['table']} ({len(source['rows'])} rows)",
                                         header, "observed", 1 if source["kind"] == "sample_csv" else None,
                                         1 if source["kind"] == "sample_csv" else None)
        self._link_evidence("page", table["page_id"], [evidence_id])

    # ------------------------------------------------------------ host knowledge

    def _page(self, page: dict[str, Any], where: str) -> None:
        evidence_ids = self._evidence(page["evidence"])
        if page["id"] is not None:
            page_id = self.page_id(page["id"], where)
            changes = {k: page[k] for k in ("title", "summary_md", "body_md", "status") if k in page}
            if changes:
                self._update_page(page_id, page["expected_version"], changes, page["reason"])
            else:
                current = self.c.execute("SELECT version FROM pages WHERE id = ?", (page_id,)).fetchone()
                if current["version"] != page["expected_version"]:
                    raise ConflictError(f"VERSION_CONFLICT: page {page_id} expected {page['expected_version']}, current {current['version']}")
            self._link_evidence("page", page_id, evidence_ids)
            if page.get("ref"):
                self.page_refs[page["ref"]] = page_id
            return
        page_id = self._insert_page(page["type"], page["title"], page["summary_md"], page["body_md"], page["status"])
        self._link_evidence("page", page_id, evidence_ids)
        self.page_refs[page["ref"]] = page_id
        self._record("pages", {"id": page_id, "ref": page["ref"], "type": page["type"], "title": page["title"]})

    def _index_member(self, member: dict[str, Any], where: str) -> None:
        index_id = self.page_id(member["index"], where)
        child_id = self.page_id(member["child"], where)
        index_type = self.c.execute("SELECT type FROM pages WHERE id = ?", (index_id,)).fetchone()["type"]
        if index_type != "index":
            raise ValidationError(f"{where}: {member['index']!r} is a {index_type} page, not an index page")
        if index_id == child_id:
            raise ValidationError(f"{where}: an index cannot contain itself")
        if self.c.execute("SELECT 1 FROM index_members WHERE index_page_id = ? AND child_page_id = ?", (index_id, child_id)).fetchone():
            raise ValidationError(f"{where}: {member['child']!r} is already in index {member['index']!r}")
        # Reject a cycle: the new child must not (transitively) contain the index.
        frontier, visited = [child_id], set()
        while frontier:
            current = frontier.pop()
            if current == index_id:
                raise ValidationError(f"{where}: adding {member['child']!r} to {member['index']!r} would create an index cycle")
            if current in visited:
                continue
            visited.add(current)
            frontier.extend(r["child_page_id"] for r in self.c.execute(
                "SELECT child_page_id FROM index_members WHERE index_page_id = ?", (current,)))
        self.c.execute("INSERT INTO index_members(index_page_id, child_page_id, position) VALUES (?, ?, ?)",
                       (index_id, child_id, member["position"]))
        self._record("index_members", {"index": index_id, "child": child_id, "position": member["position"]})

    def _term(self, term: dict[str, Any], where: str) -> None:
        page_id = self.page_id(term["page"], where) if term["page"] else None
        normalized = normalize_text(term["expression"])
        evidence_ids = self._evidence(term["evidence"])
        if term["id"] is not None:
            existing = self.c.execute("SELECT * FROM term_senses WHERE id = ?", (term["id"],)).fetchone()
            if existing is None:
                raise NotFoundError(f"{where}: term {term['id']} not found")
            if existing["version"] != term["expected_version"]:
                raise ConflictError(f"VERSION_CONFLICT: term {term['id']} expected {term['expected_version']}, current {existing['version']}")
        else:
            existing = self.c.execute(
                "SELECT * FROM term_senses WHERE normalized_expression = ? AND scope = ?", (normalized, term["scope"])
            ).fetchone()
        values = {"expression": term["expression"], "normalized_expression": normalized, "scope": term["scope"],
                  "definition_md": term["definition_md"], "page_id": page_id, "status": term["status"]}
        if existing is None:
            term_id = new_id()
            self.c.execute(
                """INSERT INTO term_senses(id, expression, normalized_expression, scope, definition_md, page_id, status, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (term_id, term["expression"], normalized, term["scope"], term["definition_md"], page_id, term["status"], self.ts, self.ts),
            )
            self._revision("term", term_id, 1, {"id": term_id, **values, "version": 1}, "created")
            self._record("terms", {"id": term_id, "expression": term["expression"], "scope": term["scope"]})
        else:
            term_id = existing["id"]
            same = all(existing[k] == v for k, v in values.items())
            if same:
                self.reused.append({"type": "term", "id": term_id, "expression": term["expression"], "scope": term["scope"]})
            elif term["id"] is None:
                raise ConflictError(
                    f"TERM_CONFLICT: {term['expression']!r} in scope {term['scope']!r} already has a different definition "
                    f"(term {term_id}, version {existing['version']}); pass id and expected_version to update it, or use a narrower scope"
                )
            else:
                version = existing["version"] + 1
                self.c.execute(
                    """UPDATE term_senses SET expression = ?, normalized_expression = ?, scope = ?, definition_md = ?, page_id = ?,
                       status = ?, version = ?, updated_at = ? WHERE id = ?""",
                    (term["expression"], normalized, term["scope"], term["definition_md"], page_id, term["status"], version, self.ts, term_id),
                )
                self._revision("term", term_id, version, {"id": term_id, **values, "version": version}, "updated by ingest")
                self.updated.append({"type": "term", "id": term_id, "version": version})
        for alias in [term["expression"], *term["aliases"]]:
            self.c.execute(
                "INSERT OR IGNORE INTO aliases(expression, normalized_expression, term_sense_id) VALUES (?, ?, ?)",
                (alias, normalize_text(alias), term_id),
            )
        self._link_evidence("term", term_id, evidence_ids)
        self.touched.add(page_id or "")
        if existing is not None and existing["page_id"]:
            self.touched.add(existing["page_id"])

    def _upsert_relation(self, source_id: str, target_id: str, rel_type: str, explanation: str, status: str,
                         evidence_ids: list[str], where: str, record: bool = True) -> str:
        if source_id == target_id:
            raise ValidationError(f"{where}: a relation needs two different pages")
        existing = self.c.execute(
            "SELECT * FROM relations WHERE source_id = ? AND target_id = ? AND type = ?", (source_id, target_id, rel_type)
        ).fetchone()
        if existing is not None:
            self._link_evidence("relation", existing["id"], evidence_ids)
            if existing["explanation_md"] != explanation or existing["status"] != status:
                self.warnings.append(
                    f"{where}: relation {existing['id']} ({rel_type}) already exists with a different explanation or status; "
                    "kept the stored one and added the new evidence. Use update to change it."
                )
            if record:
                self.reused.append({"type": "relation", "id": existing["id"], "relation_type": rel_type})
            return existing["id"]
        relation_id = new_id()
        self.c.execute(
            """INSERT INTO relations(id, source_id, target_id, type, explanation_md, status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (relation_id, source_id, target_id, rel_type, explanation, status, self.ts, self.ts),
        )
        self._revision("relation", relation_id, 1, {"id": relation_id, "source_id": source_id, "target_id": target_id,
                                                     "type": rel_type, "explanation_md": explanation, "status": status, "version": 1}, "created")
        self._link_evidence("relation", relation_id, evidence_ids)
        if record:
            self._record("relations", {"id": relation_id, "type": rel_type, "source": source_id, "target": target_id, "status": status})
        return relation_id

    def _relation(self, relation: dict[str, Any], where: str) -> None:
        self._upsert_relation(
            self.page_id(relation["source"], where), self.page_id(relation["target"], where), relation["type"],
            relation["explanation_md"], relation["status"], self._evidence(relation["evidence"]), where,
        )

    # ------------------------------------------------------------ join paths

    @staticmethod
    def _cols(meta: dict[str, Any], names: list[str], table: str, where: str) -> list[dict[str, Any]]:
        by_name = {_fold(c["name"]): c for c in meta["columns"]}
        missing = [n for n in names if _fold(n) not in by_name]
        if missing:
            raise ValidationError(f"{where}: columns {missing} are not in table {table}")
        return [by_name[_fold(n)] for n in names]

    @staticmethod
    def _is_key(meta: dict[str, Any], names: list[str]) -> bool:
        wanted = sorted(_fold(n) for n in names)
        keys = [meta.get("primary_key") or []] + list(meta.get("unique") or [])
        return any(key and sorted(_fold(k) for k in key) == wanted for key in keys)

    @staticmethod
    def _fk_matches(meta: dict[str, Any], columns: list[str], target_meta: dict[str, Any], target_columns: list[str]) -> bool:
        for fk in meta.get("foreign_keys") or []:
            if _fold(fk["target_table"]) != _fold(target_meta["name"]):
                continue
            if fk.get("target_schema") and _fold(fk["target_schema"]) != _fold(target_meta.get("schema")):
                continue
            pairs = sorted(zip(map(_fold, fk["columns"]), map(_fold, fk["target_columns"])))
            if pairs == sorted(zip(map(_fold, columns), map(_fold, target_columns))):
                return True
        return False

    def _join_path(self, path: dict[str, Any], where: str) -> None:
        system_key = path["system_key"]
        joined: list[str] = []
        table_pages: list[str] = []
        steps: list[dict[str, Any]] = []
        for i, step in enumerate(path["steps"]):
            sat = f"{where}.steps[{i}]"
            from_row = self.table(system_key, step["from_table"], sat)
            to_row = self.table(system_key, step["to_table"], sat)
            from_meta, to_meta = json.loads(from_row["metadata_json"]), json.loads(to_row["metadata_json"])
            if not joined:
                joined.append(from_row["page_id"])
            elif from_row["page_id"] not in joined:
                raise ValidationError(f"{sat}: from_table {step['from_table']!r} is not joined yet; steps must build on earlier tables")
            from_cols = self._cols(from_meta, step["from_columns"], step["from_table"], sat)
            to_cols = self._cols(to_meta, step["to_columns"], step["to_table"], sat)

            forward = self._fk_matches(from_meta, step["from_columns"], to_meta, step["to_columns"])
            backward = self._fk_matches(to_meta, step["to_columns"], from_meta, step["from_columns"])
            derived_card = "unknown"
            if forward and self._is_key(to_meta, step["to_columns"]):
                derived_card = "1:1" if self._is_key(from_meta, step["from_columns"]) else "N:1"
            elif backward and self._is_key(from_meta, step["from_columns"]):
                derived_card = "1:1" if self._is_key(to_meta, step["to_columns"]) else "1:N"
            flags = [c.get("nullable") for c in from_cols + to_cols]
            derived_null: Any = True if any(f is True for f in flags) else (False if all(f is False for f in flags) else "unknown")

            evidence_ids = self._evidence(step["evidence"])
            out = dict(step)
            out["from_table_id"], out["to_table_id"] = from_row["page_id"], to_row["page_id"]
            out["evidence"] = evidence_ids
            out["foreign_key"] = "forward" if forward else ("backward" if backward else None)
            for field, derived, label in (("cardinality", derived_card, "cardinality"), ("nullable", derived_null, "nullable")):
                given = step[field]
                if given == "unknown":
                    out[field] = derived
                    out[f"{field}_basis"] = "schema" if derived != "unknown" else None
                elif derived != "unknown":
                    if given != derived:
                        raise ValidationError(f"{sat}: {label} {given!r} contradicts the stored schema, which gives {derived!r}")
                    out[f"{field}_basis"] = "schema"
                elif not evidence_ids:
                    raise ValidationError(
                        f"{sat}: {label} {given!r} is not backed by the stored keys; cite step evidence or use \"unknown\""
                    )
                else:
                    out[f"{field}_basis"] = "evidence"
            if out["foreign_key"] is None and not evidence_ids and path["status"] in {"extracted", "verified"}:
                raise ValidationError(
                    f"{sat}: no foreign key backs {step['from_table']} → {step['to_table']}; cite step evidence or mark the path inferred"
                )
            steps.append(out)
            for page in (from_row["page_id"], to_row["page_id"]):
                if page not in table_pages:
                    table_pages.append(page)
            if to_row["page_id"] not in joined:
                joined.append(to_row["page_id"])

        normalized_title = normalize_text(path["title"])
        values = {"system_key": system_key, "title": path["title"], "normalized_title": normalized_title,
                  "purpose": path["purpose"], "steps_json": to_json(steps), "conditions_md": path["conditions_md"],
                  "caveats_md": path["caveats_md"], "status": path["status"]}
        existing = self.c.execute(
            "SELECT * FROM join_paths WHERE system_key = ? AND normalized_title = ?", (system_key, normalized_title)
        ).fetchone()
        path_evidence = self._evidence(path["evidence"])
        if existing is not None:
            def content(steps_json: str) -> list[dict[str, Any]]:
                # Evidence ids differ on every ingest; compare what the steps say.
                return [{k: v for k, v in step.items() if k != "evidence"} for step in json.loads(steps_json)]
            same = all(existing[k] == v for k, v in values.items() if k != "steps_json") and                 content(existing["steps_json"]) == content(values["steps_json"])
            if same:
                step_evidence = [e for s in steps for e in s["evidence"]]
                self._link_evidence("join_path", existing["id"], path_evidence + step_evidence)
                self.reused.append({"type": "join_path", "id": existing["id"], "title": path["title"]})
                return
            raise ConflictError(
                f"JOIN_PATH_CONFLICT: {path['title']!r} already exists in {system_key} with different content "
                f"(join path {existing['id']}, version {existing['version']}); update it or choose another title"
            )
        path_id = new_id()
        self.c.execute(
            """INSERT INTO join_paths(id, system_key, title, normalized_title, purpose, steps_json, conditions_md, caveats_md,
                                      status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (path_id, system_key, path["title"], normalized_title, path["purpose"], values["steps_json"],
             path["conditions_md"], path["caveats_md"], path["status"], self.ts, self.ts),
        )
        self.touched.update(table_pages)
        for page in table_pages:
            self.c.execute("INSERT INTO join_path_tables(join_path_id, page_id) VALUES (?, ?)", (path_id, page))
        self._revision("join_path", path_id, 1, {"id": path_id, **values, "version": 1}, "created")
        step_evidence = [e for s in steps for e in s["evidence"]]
        self._link_evidence("join_path", path_id, path_evidence + [e for e in step_evidence if e not in path_evidence])
        self._record("join_paths", {"id": path_id, "title": path["title"], "tables": len(table_pages), "status": path["status"]})

    # ------------------------------------------------------------ rules

    def _rule(self, rule: dict[str, Any], where: str) -> None:
        scope_id = self.page_id(rule["scope"], where)
        evidence_ids = self._evidence(rule["evidence"])
        values = {k: rule[k] for k in ("kind", "condition_md", "instruction_md", "reason_md", "alternative_md", "confirmation", "status")}
        for row in self.c.execute("SELECT * FROM rules WHERE scope_page_id = ? AND kind = ?", (scope_id, rule["kind"])):
            if normalize_text(row["instruction_md"]) == normalize_text(rule["instruction_md"]):
                if all(row[k] == v for k, v in values.items()):
                    self._link_evidence("rule", row["id"], evidence_ids)
                    self.reused.append({"type": "rule", "id": row["id"], "kind": rule["kind"]})
                    return
                raise ConflictError(
                    f"RULE_CONFLICT: {where} repeats the instruction of rule {row['id']} (version {row['version']}) "
                    "with different details; update that rule instead"
                )
        rule_id = new_id()
        self.c.execute(
            """INSERT INTO rules(id, scope_page_id, kind, condition_md, instruction_md, reason_md, alternative_md, confirmation,
                                 status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (rule_id, scope_id, rule["kind"], rule["condition_md"], rule["instruction_md"], rule["reason_md"],
             rule["alternative_md"], rule["confirmation"], rule["status"], self.ts, self.ts),
        )
        self._revision("rule", rule_id, 1, {"id": rule_id, "scope_page_id": scope_id, **values, "version": 1}, "created")
        self._link_evidence("rule", rule_id, evidence_ids)
        self.touched.add(scope_id)
        self._record("rules", {"id": rule_id, "kind": rule["kind"], "scope": scope_id})
