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
from . import db
from .index import rebuild
from .research import DEFAULT_TIMEOUT, ResearchFailed, add_request, render_research, run_research
from .search import (
    DEFAULT_CANDIDATES, DEFAULT_LIMIT, DEFAULT_MAX_BYTES, DEFAULT_MAX_RELATED, BudgetTooSmall, neighbors, paths, query,
    render_neighbors, render_paths,
)

ENVELOPE_VERSION = 1
# Exit codes (spec §9): 0 ok, 2 invalid input, 3 conflict, 4 not found, 5 external research failure, 6 store failure.
EXIT_INPUT, EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_RESEARCH, EXIT_STORE = 2, 3, 4, 5, 6


def _store(value: str | None) -> Path:
    # --store > BUILDLOG_STORE > config file > default
    from .config import resolve_store

    return resolve_store(value)


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
    query_parser = commands.add_parser("query", help="find stored knowledge for a question and return agent-ready Markdown")
    query_parser.add_argument("text", nargs="?", help="the question; or pass --input")
    query_parser.add_argument("--input", help='UTF-8 JSON file or - : {"query", "terms", "scope", "limit", "max_bytes"}')
    query_parser.add_argument("--term", action="append", default=[], help="extra search term (repeatable): error codes, synonyms, table names")
    query_parser.add_argument("--scope", help="system key (e.g. demo_mes) or environment word (e.g. Windows)")
    query_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    query_parser.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATES)
    query_parser.add_argument("--max-related", type=int, default=DEFAULT_MAX_RELATED)
    query_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    query_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    neighbors_parser = commands.add_parser("neighbors")
    neighbors_parser.add_argument("page_id")
    neighbors_parser.add_argument("--type")
    neighbors_parser.add_argument("--direction", choices=("out", "in", "both"), default="both")
    neighbors_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    paths_parser = commands.add_parser("paths")
    paths_parser.add_argument("--system", required=True)
    paths_parser.add_argument("--from", dest="from_table", required=True)
    paths_parser.add_argument("--to", dest="to_table", required=True)
    paths_parser.add_argument("--purpose")
    paths_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    commands.add_parser("reindex", help="rebuild the search index from stored records")
    research_parser = commands.add_parser("research", help="run pending external research with Antigravity (agy)")
    research_parser.add_argument("--run", dest="run_id", help="run the unfinished research requests of this ingest run")
    research_parser.add_argument("--page", dest="page_id", help="run the unfinished requests of this page, or attach --question to it")
    research_parser.add_argument("--request", dest="request_ids", action="append", default=[], help="request id (repeatable)")
    research_parser.add_argument("--question", help="new question to attach to --page and run now")
    research_parser.add_argument("--context", default="", help="short context for --question")
    research_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds agy may spend per question")
    research_parser.add_argument("--agy", help="path to agy.exe (default: BUILDLOG_AGY, then the config file, then %%LOCALAPPDATA%%\\agy\\bin\\agy.exe)")
    research_parser.add_argument("--retry-succeeded", action="store_true", help="also re-run requests that already succeeded")
    research_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    skill_parser = commands.add_parser("install-skill", help="install the Buildlog skill for Claude Code and/or Codex")
    skill_parser.add_argument("--host", choices=("claude-code", "codex", "all"), default="all")
    skill_parser.add_argument("--dry-run", action="store_true")
    config_parser = commands.add_parser("config", help="show or change shared settings (store, agy)")
    config_parser.add_argument("action", choices=("show", "set", "unset"))
    config_parser.add_argument("key", nargs="?", choices=("store", "agy"))
    config_parser.add_argument("value", nargs="?")
    runs_parser = commands.add_parser("runs", help="list save runs with their stage, status and preserved errors")
    runs_parser.add_argument("--status", choices=("running", "failed", "succeeded"))
    runs_parser.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request_id = str(uuid.uuid4())
    try:
        store = _store(args.store)
        if args.command == "config":
            from .config import set_value, show

            if args.action == "show":
                data = show(args.store)
            elif args.action == "set":
                if not args.key or not args.value:
                    raise ValidationError("config set needs a key and a value")
                data = set_value(args.key, args.value)
            else:
                if not args.key:
                    raise ValidationError("config unset needs a key")
                data = set_value(args.key, None)
            _write(sys.stdout, _envelope(request_id, "ok", data))
            return 0
        if args.command == "runs":
            from .runs import list_runs

            data, warnings = list_runs(store, args.status, args.limit), []
            _write(sys.stdout, _envelope(request_id, "ok", data, warnings))
            return 0
        if args.command == "init":
            data, warnings = initialize(store), []
        elif args.command == "ingest":
            data = ingest(store, _load_json(args.input))
            # Saved, but part of the input was not read: never report that as plain "ok".
            status = "partial" if data.get("status") == "partial" else "ok"
            _write(sys.stdout, _envelope(request_id, status, data, data.get("warnings", [])))
            return 0
        elif args.command == "read":
            record = read_object(store, args.object_id)
            if args.format == "markdown":
                _write(sys.stdout, render_markdown(record))
                return 0
            data, warnings = record, []
        elif args.command == "query":
            spec: dict[str, Any] = _load_json(args.input) if args.input else {}
            if not isinstance(spec, dict):
                raise ValidationError("query input must be a JSON object")
            text = spec.get("query", args.text) or ""
            data = query(store, text, terms=list(spec.get("terms", [])) + args.term, scope=spec.get("scope", args.scope),
                         limit=spec.get("limit", args.limit), candidates=spec.get("candidates", args.candidates),
                         max_related=spec.get("max_related", args.max_related), max_bytes=spec.get("max_bytes", args.max_bytes))
            if args.format == "markdown":
                _write(sys.stdout, data["markdown"])
                return 0
            warnings = []
        elif args.command == "neighbors":
            data, warnings = neighbors(store, args.page_id, args.type, args.direction), []
            if args.format == "markdown":
                _write(sys.stdout, render_neighbors(data))
                return 0
        elif args.command == "paths":
            data, warnings = paths(store, args.system, args.from_table, args.to_table, args.purpose), []
            if args.format == "markdown":
                _write(sys.stdout, render_paths(data))
                return 0
        elif args.command == "research":
            request_ids = list(args.request_ids)
            if args.question:
                if not args.page_id:
                    raise ValidationError("--question needs --page")
                request_ids.append(add_request(store, args.page_id, args.question, args.context)["id"])
            data = run_research(store, run_id=args.run_id, page_id=None if args.question else args.page_id,
                                request_ids=request_ids or None, agy=[args.agy] if args.agy else None,
                                timeout=args.timeout, retry_succeeded=args.retry_succeeded)
            if args.format == "markdown":
                _write(sys.stdout, render_research(data))
                return 0
            warnings = []
        elif args.command == "install-skill":
            from .skills import HOSTS, install

            hosts = sorted(HOSTS) if args.host == "all" else [args.host]
            data, warnings = install(hosts, dry_run=args.dry_run), []
        elif args.command == "reindex":
            if not store.exists():
                raise NotFoundError(f"store does not exist: {store}")
            initialize(store)
            with db.write(store) as connection:
                data, warnings = {"store": str(store), "pages_indexed": rebuild(connection)}, []
        else:
            patch = _load_json(args.patch)
            if not isinstance(patch, dict):
                raise ValidationError("patch input must be a JSON object")
            data, warnings = update_object(store, args.object_id, args.expected_version, patch, args.reason), []
        _write(sys.stdout, _envelope(request_id, "ok", data, warnings))
        return 0
    except ResearchFailed as error:
        # Exit 5: external research failed. The saved experience is untouched; the summary says what to resume.
        if getattr(args, "format", "json") == "markdown":
            _write(sys.stdout, render_research(error.data))
        _write(sys.stderr, _envelope(request_id, "error", error.data, error=str(error)))
        return EXIT_RESEARCH
    except BudgetTooSmall as error:
        _write(sys.stderr, _envelope(request_id, "error", error.data, error=str(error)))
        return EXIT_INPUT
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
