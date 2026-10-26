"""Stand-in for agy.exe in tests. Replays outputs recorded from the real agy CLI (2026-10-26).

Mode comes from the file named by FAKE_AGY_MODE_FILE (so a test can change it between calls).
The call is checked against Buildlog's contract: print mode, JSON output, a schema file,
plan mode, no permission bypass, and an empty working directory.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCHEMA_ECHO = {"type": "object", "properties": {"findings": {"type": "array"}, "notes": {"type": "string"}}}

SUCCESS = {
    "findings": [
        {"access": "search_snippet", "quote": "The trigram tokenizer extends FTS5 to support substring matching in general, instead of the usual token matching.",
         "relevance": "States that the trigram tokenizer supports general substring matching.", "title": "SQLite FTS5 Extension", "url": "https://www.sqlite.org/fts5.html"},
        {"access": "fetched", "quote": "FTS5 tables that use the trigram tokenizer also support indexed GLOB and LIKE pattern matching.",
         "relevance": "Indexed LIKE matching is available.", "title": "SQLite FTS5 Extension", "url": "https://www.sqlite.org/fts5.html"},
    ],
    "notes": "Official SQLite documentation.",
}


def envelope(**extra: object) -> str:
    base = {"conversation_id": "fake-conversation", "status": "SUCCESS", "response": "", "duration_seconds": 1.0, "num_turns": 1,
            "json_schema": SCHEMA_ECHO, "usage": {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 2}}
    base.update(extra)
    return json.dumps(base)


def main() -> int:
    args = sys.argv[1:]
    problems = []
    for flag in ("--print", "--output-format", "--json-schema", "--mode", "--print-timeout"):
        if flag not in args:
            problems.append(f"missing {flag}")
    if "--dangerously-skip-permissions" in args:
        problems.append("permission bypass used")
    if "--mode" in args and args[args.index("--mode") + 1] != "plan":
        problems.append("not plan mode")
    if "--json-schema" in args and not Path(args[args.index("--json-schema") + 1]).is_file():
        problems.append("schema file missing")
    extra_files = [p.name for p in Path.cwd().iterdir() if p.suffix != ".json"]
    if extra_files:
        problems.append(f"working directory not empty: {extra_files}")
    if problems:
        sys.stderr.write("fake agy contract violation: " + "; ".join(problems))
        return 99

    log = os.environ.get("FAKE_AGY_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"prompt": args[args.index("--print") + 1], "cwd": os.getcwd()}) + "\n")
    mode = Path(os.environ["FAKE_AGY_MODE_FILE"]).read_text(encoding="utf-8").strip()
    out, err, code = "", "", 0
    if mode == "success":
        out = envelope(response=json.dumps(SUCCESS), structured_output=SUCCESS)
    elif mode == "denied":  # recorded: a tool needed "command" permission, headless mode auto-denied it
        out = envelope(denied_actions=[{"action": "command", "display_name": "RunCommand"}])
        err = ('jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, '
               "so it was auto-denied.")
    elif mode == "timeout":  # recorded: exit 0, status SUCCESS, empty response, message only on stderr
        out = envelope(duration_seconds=0)
        err = "[agy] print timeout after 10s with turn in progress; returning partial output"
    elif mode == "auth":
        err, code = "You are currently not signed in. Run agy to sign in.", 1
    elif mode == "notjson":
        out = "Welcome to the Antigravity CLI"
    elif mode == "empty":
        payload = {"findings": [], "notes": "No reliable source found."}
        out = envelope(response=json.dumps(payload), structured_output=payload)
    elif mode == "badurl":
        payload = {"findings": [
            {"access": "fetched", "quote": "q", "relevance": "r", "title": "t", "url": "not a url"},
            {"access": "guessed", "quote": "q", "relevance": "r", "title": "t", "url": "https://example.org/a"},
            {"access": "fetched", "quote": "Valid quote.", "relevance": "r", "title": "Example", "url": "https://example.org/doc"},
        ], "notes": ""}
        out = envelope(response=json.dumps(payload), structured_output=payload)
    elif mode == "redirect":  # recorded shape: grounding redirect links instead of source addresses
        payload = {"findings": [
            {"access": "search_snippet", "quote": "Sets an optional time limit for print mode (0 waits until the turn completes).",
             "relevance": "r", "title": "Google Antigravity Documentation - CLI Reference",
             "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEJxQctVXHPyTFaggSC6DKVSx"},
        ], "notes": ""}
        out = envelope(response=json.dumps(payload), structured_output=payload)
    else:
        err, code = f"unknown fake mode {mode}", 98
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
