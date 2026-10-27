"""Starting, finding and stopping the review server of a store.

A running server records itself in `serve.json` next to the store. Other commands use it to tell
whether a graph address really exists before they report one.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Callable

from .db import NotFoundError, ValidationError
from .server import BuildlogServer, state_file


def _health(host: str, port: int, timeout: float = 0.7) -> dict[str, Any] | None:
    try:
        conn = HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/api/health", headers={"Host": f"{host}:{port}"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return json.loads(body) if response.status == 200 else None
    except (OSError, ValueError):
        return None


def _read_state(store: Path) -> dict[str, Any] | None:
    path = state_file(store)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def running_url(store: Path) -> str | None:
    """The address of a live server for exactly this store, or None."""
    state = _read_state(store)
    if not state:
        return None
    health = _health(state["host"], state["port"])
    if not health or Path(health.get("store", "")).resolve() != store.resolve():
        return None
    return f"http://{state['host']}:{state['port']}/"


def status(store: Path) -> dict[str, Any]:
    url = running_url(store)
    state = _read_state(store) if url else None
    return {"running": bool(url), "url": url, "pid": state.get("pid") if state else None, "store": str(store)}


def foreground(store: Path, host: str, port: int, open_browser: bool, announce: Callable[[dict[str, Any]], None]) -> int:
    if running_url(store):
        raise ValidationError(f"a review server for this store is already running at {running_url(store)}")
    server = BuildlogServer(store, host, port)
    state = {"pid": os.getpid(), "host": server.host, "port": server.port, "store": str(store),
             "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
    state_file(store).write_text(json.dumps(state), encoding="utf-8")
    announce({"url": server.url, "pid": os.getpid(), "store": str(store), "stop": "Ctrl+C, or serve --stop"})
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        current = _read_state(store)
        if current and current.get("pid") == os.getpid():
            state_file(store).unlink(missing_ok=True)
    return 0


def detach(store: Path, host: str, port: int, open_browser: bool) -> dict[str, Any]:
    url = running_url(store)
    if url is None:
        if not store.exists():
            raise NotFoundError(f"store does not exist: {store}")
        log = store.parent / "serve.log"
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        command = [sys.executable, "-m", "buildlog", "--store", str(store), "serve", "--host", host, "--port", str(port)]
        with open(log, "ab") as out:
            try:
                # Leave the caller's Windows job (an editor or agent session), or the server ends with it.
                subprocess.Popen(command, stdout=out, stderr=out, stdin=subprocess.DEVNULL, close_fds=True,
                                 creationflags=flags | (getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) if os.name == "nt" else 0))
            except OSError:   # the job does not allow breaking away: start it inside, as before
                subprocess.Popen(command, stdout=out, stderr=out, stdin=subprocess.DEVNULL, close_fds=True, creationflags=flags)
        deadline = time.monotonic() + 15
        while url is None and time.monotonic() < deadline:
            time.sleep(0.3)
            url = running_url(store)
        if url is None:
            raise ValidationError(f"the review server did not start; see {log}")
    if open_browser:
        webbrowser.open(url)
    return status(store)


def stop(store: Path) -> dict[str, Any]:
    state = _read_state(store)
    if not state or not running_url(store):
        state_file(store).unlink(missing_ok=True)
        return {"running": False, "stopped": False, "store": str(store)}
    os.kill(state["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while running_url(store) and time.monotonic() < deadline:
        time.sleep(0.2)
    state_file(store).unlink(missing_ok=True)
    return {"running": bool(running_url(store)), "stopped": True, "pid": state["pid"], "store": str(store)}
