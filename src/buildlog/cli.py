from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from .store import (
    BuildlogError, ConflictError, NotFoundError, ValidationError, default_store_path, ingest, initialize,
    read_object, render_markdown, update_object,
)

ENVELOPE_VERSION = 1
# Exit codes (spec §9): 0 ok, 2 invalid input, 3 conflict, 4 not found, 5 external research failure, 6 store failure.
EXIT_INPUT, EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_STORE = 2, 3, 4, 6


def _store(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_store_path()


def _load_json(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def _write(stream: Any, value: str) -> None:
    stream.buffer.write(value.encode("utf-8"))
    stream.flush()


def _envelope(request_id: str, status: str, data: Any = None, warnings: list[str] | None = None, error: str | None = None) -> str:
    payload = {"schema_version": ENVELOPE_VERSION, "request_id": request_id, "status": status, "data": data,
               "warnings": warnings or [], "error": error}
    return json.dumps(payload, ensure_ascii=False) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="buildlog")
    parser.add_argument("--store", help="SQLite database path; defaults to the user-local Buildlog store")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("--input", required=True, help="UTF-8 JSON save plan file, or - for stdin")
    read_parser = commands.add_parser("read")
    read_parser.add_argument("object_id", help="id of a page, term, relation, join path, or rule")
    read_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    update_parser = commands.add_parser("update")
    update_parser.add_argument("object_id", help="id of a page, term, relation, join path, or rule")
    update_parser.add_argument("--expected-version", required=True, type=int)
    update_parser.add_argument("--patch", required=True, help="UTF-8 JSON file, or - for stdin")
    update_parser.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args.store)
    request_id = str(uuid.uuid4())
    try:
        if args.command == "init":
            data, warnings = initialize(store), []
        elif args.command == "ingest":
            data = ingest(store, _load_json(args.input))
            warnings = data.get("warnings", [])
        elif args.command == "read":
            record = read_object(store, args.object_id)
            if args.format == "markdown":
                _write(sys.stdout, render_markdown(record))
                return 0
            data, warnings = record, []
        else:
            patch = _load_json(args.patch)
            if not isinstance(patch, dict):
                raise ValidationError("patch input must be a JSON object")
            data, warnings = update_object(store, args.object_id, args.expected_version, patch, args.reason), []
        _write(sys.stdout, _envelope(request_id, "ok", data, warnings))
        return 0
    except ConflictError as error:
        return _fail(request_id, error, EXIT_CONFLICT)
    except NotFoundError as error:
        return _fail(request_id, error, EXIT_NOT_FOUND)
    except (BuildlogError, json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        return _fail(request_id, error, EXIT_INPUT)
    except sqlite3.Error as error:
        return _fail(request_id, f"store error: {error}", EXIT_STORE)


def _fail(request_id: str, error: object, code: int) -> int:
    _write(sys.stderr, _envelope(request_id, "error", error=str(error)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
