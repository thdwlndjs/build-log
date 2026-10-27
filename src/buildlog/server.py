"""Local review server: graph UI and JSON API over the same SQLite store.

Security model (local only):
- binds to a loopback address only;
- every request must carry a Host header naming this server (blocks DNS rebinding);
- every change (PATCH/POST) needs the per-process session token that is embedded in the page,
  and a matching Origin when the browser sends one;
- the page loads only bundled assets (CSP 'self'); Markdown is sanitized in the browser.
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import db
from .db import BuildlogError, ConflictError, NotFoundError, ValidationError
from .browse import list_pages, list_sessions, list_sources, session_detail, source_detail
from .graph import graph, list_runs, object_detail
from .update import add_feedback, merge_pages, set_feedback_status, unlink_relation, update_object

WEB = Path(__file__).resolve().parent / "web"
MAX_BODY = 1024 * 1024
ROUTE_TYPES = {"pages": "page", "relations": "relation", "terms": "term", "rules": "rule", "paths": "join_path"}
_ID = r"([0-9A-Za-z-]{1,80})"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class BuildlogServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
        if not _loopback(host):
            raise ValidationError(f"refusing to bind to {host}: the review server is local only (use 127.0.0.1)")
        if not store.exists():
            raise NotFoundError(f"store does not exist: {store}")
        db.initialize(store)
        self.store = store
        self.token = secrets.token_urlsafe(24)
        super().__init__((host, port), Handler)
        self.host, self.port = self.server_address[0], self.server_address[1]
        self.allowed_hosts = {f"{self.host}:{self.port}", f"localhost:{self.port}", f"127.0.0.1:{self.port}"}

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


class Handler(BaseHTTPRequestHandler):
    server: BuildlogServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # keep the console quiet; errors go to the JSON body
        if os.environ.get("BUILDLOG_SERVER_LOG"):
            super().log_message(format, *args)

    # ------------------------------------------------------------ plumbing

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         # The one inline style Cytoscape 3.30.2 injects is allowed by its hash, nothing else.
                         "default-src 'self'; script-src 'self'; style-src 'self' 'sha256-pgvDUBa4IjFA2yuSJ2cqcyxmNYJMborsd0ORcRv9vw8='; "
                         "img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _check_host(self) -> None:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "unexpected Host header")

    def _check_write(self) -> None:
        if not secrets.compare_digest(self.headers.get("X-Buildlog-Token", ""), self.server.token):
            raise ApiError(HTTPStatus.FORBIDDEN, "missing or wrong session token; reload the page")
        origin = self.headers.get("Origin")
        if origin is not None and urlparse(origin).netloc not in self.server.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-origin change refused")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {error}") from error
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _dispatch(self, method: str) -> None:
        try:
            self._check_host()
            url = urlparse(self.path)
            path, params = unquote(url.path), {k: v[-1] for k, v in parse_qs(url.query).items()}
            if method in {"PATCH", "POST"}:
                self._check_write()
            if not path.startswith("/api/"):
                if method not in {"GET", "HEAD"}:
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
                return self._static(path)
            self._json(HTTPStatus.OK, self._api(method, path, params))
        except ApiError as error:
            self._json(error.status, {"error": str(error), "kind": "request"})
        except ConflictError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error), "kind": "conflict"})
        except NotFoundError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error), "kind": "not_found"})
        except BuildlogError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error), "kind": "validation"})
        except sqlite3.Error as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"store error: {error}", "kind": "store"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # ------------------------------------------------------------ static files

    def _static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", self.server.token)
            html = html.replace("{{STORE}}", str(self.server.store).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;"))
            return self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
        if not path.startswith("/static/"):
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        file = (WEB / path[len("/static/"):]).resolve()
        if WEB.resolve() not in file.parents or not file.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix == ".js":
            content_type = "text/javascript"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, file.read_bytes(), content_type)

    # ------------------------------------------------------------ API

    def _api(self, method: str, path: str, params: dict[str, str]) -> Any:
        store = self.server.store
        if method in {"GET", "HEAD"}:
            if path == "/api/health":
                return {"ok": True, "store": str(store), "pid": os.getpid()}
            if path == "/api/graph":
                caps = {k: int(params[k]) for k in ("cap_nodes", "cap_edges") if params.get(k, "").isdigit()}
                types = [t for t in params.get("types", "").split(",") if t] or None
                return graph(store, run_id=params.get("run") or None, focus=params.get("focus") or None,
                             q=params.get("q") or None, types=types, include_columns=params.get("columns") == "1", **caps)
            if path == "/api/runs":
                return list_runs(store)
            if path == "/api/search":
                from .search import query

                if not params.get("q"):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "q is required")
                found = query(store, params["q"], scope=params.get("scope") or None, max_bytes=10_000_000)
                return {k: found[k] for k in ("terms", "results", "out_of_scope", "rules", "terms_found")}
            if path == "/api/paths":
                from .search import paths

                for key in ("system", "from", "to"):
                    if not params.get(key):
                        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} is required")
                return paths(store, params["system"], params["from"], params["to"], params.get("purpose"))
            if path == "/api/pages":
                return list_pages(store, params.get("type") or None, params.get("status") or None, params.get("sort") or None,
                                  params.get("offset"), params.get("limit"))
            if path == "/api/sessions":
                return list_sessions(store)
            if path == "/api/sources":
                return list_sources(store, params.get("kind") or None, params.get("q") or None)
            m = re.fullmatch(rf"/api/sessions/{_ID}", path)
            if m:
                return session_detail(store, m.group(1))
            m = re.fullmatch(rf"/api/sources/{_ID}", path)
            if m:
                return source_detail(store, m.group(1))
            m = re.fullmatch(rf"/api/(objects|pages|relations|terms|rules|paths)/{_ID}", path)
            if m:
                detail = object_detail(store, m.group(2))
                expected = ROUTE_TYPES.get(m.group(1))
                if expected and detail["object_type"] != expected:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"{m.group(2)} is a {detail['object_type']}, not a {expected}")
                return detail
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown API path")
        body = self._body()
        m = re.fullmatch(rf"/api/(pages|relations|terms|rules|paths)/{_ID}", path)
        if method == "PATCH" and m:
            detail_type = object_detail(store, m.group(2))["object_type"]
            if detail_type != ROUTE_TYPES[m.group(1)]:
                raise ApiError(HTTPStatus.NOT_FOUND, f"{m.group(2)} is a {detail_type}")
            return update_object(store, m.group(2), _int(body, "expected_version"), body.get("patch") or {},
                                 body.get("reason") or "edited in the review UI", feedback_id=body.get("feedback_id"))
        m = re.fullmatch(rf"/api/relations/{_ID}/unlink", path)
        if method == "POST" and m:
            return unlink_relation(store, m.group(1), _int(body, "expected_version"), body.get("reason") or "",
                                   feedback_id=body.get("feedback_id"))
        if method == "POST" and path == "/api/feedback":
            return add_feedback(store, str(body.get("target_id") or ""), _int(body, "target_version"), str(body.get("text") or ""))
        m = re.fullmatch(rf"/api/feedback/{_ID}/status", path)
        if method == "POST" and m:
            return set_feedback_status(store, m.group(1), str(body.get("status") or ""))
        if method == "POST" and path == "/api/merge":
            versions = body.get("expected_versions")
            if not isinstance(versions, dict) or not all(isinstance(v, int) for v in versions.values()):
                raise ApiError(HTTPStatus.BAD_REQUEST, "expected_versions must map page ids to integer versions")
            return merge_pages(store, body.get("source_ids") or [], str(body.get("target_id") or ""), versions,
                               body.get("reason") or "")
        raise ApiError(HTTPStatus.NOT_FOUND, "unknown API path")


def _int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer")
    return value


# ---------------------------------------------------------------- running


def start_in_thread(store: Path, host: str = "127.0.0.1", port: int = 0) -> BuildlogServer:
    """Start a server in a background thread (tests and embedding). Call .shutdown() to stop."""
    server = BuildlogServer(store, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def state_file(store: Path) -> Path:
    return store.parent / "serve.json"
