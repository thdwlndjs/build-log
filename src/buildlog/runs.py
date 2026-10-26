"""Save-run lifecycle: received → normalized → planned → committed.

Each stage change before `committed` is its own small committed transaction, so a failure
leaves a visible record: the last stage reached, the error kind, and the message. The data
itself is written only in the final transaction, together with `committed`.

A retry with the same idempotency key reuses the failed (or interrupted) run row and starts
again from `received`. Resuming external research is a different thing: it continues the
research requests of an already committed run (see research.py).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import ConflictError, NotFoundError, ValidationError, to_json


def error_kind(error: BaseException) -> str:
    if isinstance(error, ConflictError):
        return "conflict"
    if isinstance(error, NotFoundError):
        return "not_found"
    if isinstance(error, ValidationError):
        return "validation"
    if isinstance(error, sqlite3.Error):
        return "store"
    return "internal"


def receive(store: Path, key: str) -> dict[str, Any]:
    """Record the attempt. Returns the run row; a committed run is returned untouched."""
    ts = db.now()
    with db.write(store) as c:
        row = c.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            run_id = str(uuid.uuid4())
            c.execute(
                """INSERT INTO runs(id, idempotency_key, input_hash, stage, status, research_status, attempts,
                                    created_at, updated_at)
                   VALUES (?, ?, '', 'received', 'running', 'not_requested', 1, ?, ?)""",
                (run_id, key, ts, ts),
            )
            return dict(c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
        if row["status"] == "succeeded":
            return dict(row)
        # A failed or interrupted earlier attempt: this is a save retry on the same row.
        c.execute(
            """UPDATE runs SET stage = 'received', status = 'running', attempts = attempts + 1, error = NULL,
                               error_kind = NULL, updated_at = ? WHERE id = ? AND status != 'succeeded'""",
            (ts, row["id"]),
        )
        return dict(c.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone())


def advance(store: Path, run_id: str, stage: str, input_hash: str | None = None, summary: dict[str, Any] | None = None) -> None:
    with db.write(store) as c:
        c.execute(
            """UPDATE runs SET stage = ?, input_hash = COALESCE(?, input_hash),
                               plan_summary_json = COALESCE(?, plan_summary_json), updated_at = ?
               WHERE id = ? AND status = 'running'""",
            (stage, input_hash, to_json(summary) if summary is not None else None, db.now(), run_id),
        )


def fail(store: Path, run_id: str, error: BaseException) -> None:
    """Keep the failure on the run. Never touches a run that another attempt already committed."""
    try:
        with db.write(store) as c:
            c.execute(
                """UPDATE runs SET status = 'failed', error_kind = ?, error = ?, updated_at = ?
                   WHERE id = ? AND status != 'succeeded'""",
                (error_kind(error), str(error)[:2000], db.now(), run_id),
            )
    except sqlite3.Error:
        pass  # the original error matters more than a failed failure record


def list_runs(store: Path, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    if not store.exists():
        raise NotFoundError(f"store does not exist: {store}")
    if not isinstance(limit, int) or limit < 1:
        raise ValidationError("limit must be a positive integer")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        sql = """SELECT id, idempotency_key, stage, status, attempts, error_kind, error, research_status,
                        plan_summary_json, created_at, updated_at FROM runs"""
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        rows = [dict(r) for r in c.execute(sql, args + (limit,))]
    for row in rows:
        row["plan_summary"] = json.loads(row.pop("plan_summary_json")) if row["plan_summary_json"] else None
    return {"store": str(store), "runs": rows}
