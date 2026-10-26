from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .store import BuildlogError, ConflictError, NotFoundError, ValidationError, default_store_path, ingest, initialize, read_page, render_markdown, update_page


def _store(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_store_path()


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _output(data: dict[str, Any]) -> None:
    _write_stdout(json.dumps({"schema_version": 1, "status": "ok", "data": data}, ensure_ascii=False) + "\n")


def _write_stdout(value: str) -> None:
    sys.stdout.buffer.write(value.encode("utf-8"))


def _write_stderr(value: str) -> None:
    sys.stderr.buffer.write(value.encode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="buildlog")
    parser.add_argument("--store", help="SQLite database path; defaults to the user-local Buildlog store")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("--input", required=True, help="UTF-8 JSON file, or - for stdin")
    read_parser = commands.add_parser("read")
    read_parser.add_argument("page_id")
    read_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    update_parser = commands.add_parser("update")
    update_parser.add_argument("page_id")
    update_parser.add_argument("--expected-version", required=True, type=int)
    update_parser.add_argument("--patch", required=True, help="UTF-8 JSON file, or - for stdin")
    update_parser.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args.store)
    try:
        if args.command == "init":
            _output(initialize(store))
        elif args.command == "ingest":
            _output(ingest(store, _load_json(args.input)))
        elif args.command == "read":
            record = read_page(store, args.page_id)
            if args.format == "markdown":
                _write_stdout(render_markdown(record))
            else:
                _output(record)
        elif args.command == "update":
            patch = _load_json(args.patch)
            if not isinstance(patch, dict):
                raise ValidationError("patch input must be a JSON object")
            _output(update_page(store, args.page_id, args.expected_version, patch, args.reason))
        return 0
    except ConflictError as error:
        return _error(error, 3)
    except NotFoundError as error:
        return _error(error, 4)
    except (BuildlogError, json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        return _error(error, 2)
    except sqlite3.Error as error:
        return _error(f"store error: {error}", 5)


def _error(error: object, code: int) -> int:
    payload = {"schema_version": 1, "status": "error", "error": str(error)}
    _write_stderr(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
