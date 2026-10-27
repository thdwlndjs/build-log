"""Search index maintenance. The index is derived data and can always be rebuilt."""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable


def _related_pages(c: sqlite3.Connection, page_id: str) -> list[str]:
    """A table page also carries the terms and rules of its columns."""
    schema = c.execute("SELECT norm_key, object_type FROM schema_objects WHERE page_id = ?", (page_id,)).fetchone()
    pages = [page_id]
    if schema is not None and schema["object_type"] == "table":
        pages += [r["page_id"] for r in c.execute(
            "SELECT page_id FROM schema_objects WHERE object_type = 'column' AND substr(norm_key, 1, ?) = ?",
            (len(schema["norm_key"]) + 1, schema["norm_key"] + "."))]
    return pages


def _document(c: sqlite3.Connection, page: sqlite3.Row) -> tuple[str, str, str, str, str, str]:
    page_id = page["id"]
    pages = _related_pages(c, page_id)
    marks = ",".join("?" * len(pages))
    aliases = [r["expression"] for r in c.execute(
        f"""SELECT DISTINCT a.expression FROM aliases a JOIN term_senses t ON t.id = a.term_sense_id
            WHERE t.page_id IN ({marks}) AND t.status != 'rejected'""", tuple(pages))]
    extra: list[str] = []
    schema = c.execute("SELECT system_key, metadata_json, object_type FROM schema_objects WHERE page_id = ?", (page_id,)).fetchone()
    if schema is not None:
        meta = json.loads(schema["metadata_json"])
        extra.append(schema["system_key"])
        if schema["object_type"] == "table":
            extra.append(meta["name"])
            for column in meta["columns"]:
                extra.append(" ".join(filter(None, [column["name"], column.get("description")])))
        else:
            extra.append(f"{meta['table']} {meta['name']}")
    for row in c.execute(f"SELECT scope, definition_md FROM term_senses WHERE page_id IN ({marks}) AND status != 'rejected'", tuple(pages)):
        extra.append(f"{row['scope']} {row['definition_md']}")
    for row in c.execute(f"""SELECT condition_md, instruction_md, reason_md, alternative_md FROM rules
                             WHERE scope_page_id IN ({marks}) AND status != 'rejected'""", tuple(pages)):
        extra.append(" ".join(v for v in row if v))
    for row in c.execute(f"""SELECT DISTINCT j.title, j.purpose, j.conditions_md, j.caveats_md FROM join_paths j
                             JOIN join_path_tables jt ON jt.join_path_id = j.id
                             WHERE jt.page_id = ? AND j.status != 'rejected'""", (page_id,)):
        extra.append(" ".join(v for v in row if v))
    return page_id, page["title"], " ".join(aliases), page["summary_md"], page["body_md"], "\n".join(extra)


def _write(c: sqlite3.Connection, page: sqlite3.Row) -> None:
    c.execute("DELETE FROM search_index WHERE page_id = ?", (page["id"],))
    if c.execute("SELECT 1 FROM page_redirects WHERE from_page_id = ?", (page["id"],)).fetchone():
        return  # merged into another page: only the representative is searchable
    page_id, title, aliases, summary, body, extra = _document(c, page)
    c.execute("INSERT INTO search_index(page_id, title, aliases, summary, body, extra) VALUES (?, ?, ?, ?, ?, ?)",
              (page_id, title, aliases, summary, body, extra))


def reindex(c: sqlite3.Connection, page_ids: Iterable[str]) -> int:
    """Refresh the index rows of the given pages and the tables their columns belong to."""
    targets: set[str] = set()
    for page_id in page_ids:
        if not page_id:
            continue
        targets.add(page_id)
        column = c.execute("SELECT norm_key FROM schema_objects WHERE page_id = ? AND object_type = 'column'", (page_id,)).fetchone()
        if column is not None:
            table = c.execute("SELECT page_id FROM schema_objects WHERE object_type = 'table' AND norm_key = ?",
                              (column["norm_key"].rsplit(".", 1)[0],)).fetchone()
            if table is not None:
                targets.add(table["page_id"])
    for page_id in targets:
        page = c.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            c.execute("DELETE FROM search_index WHERE page_id = ?", (page_id,))
        else:
            _write(c, page)
    return len(targets)


def rebuild(c: sqlite3.Connection) -> int:
    c.execute("DELETE FROM search_index")
    count = 0
    for page in c.execute("SELECT * FROM pages").fetchall():
        _write(c, page)
        count += 1
    return count
