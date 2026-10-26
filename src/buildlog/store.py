"""Public entry points of the Buildlog store (kept stable for the CLI and tests)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import (
    SCHEMA_VERSION, BuildlogError, ConflictError, NotFoundError, ValidationError, default_store_path, initialize,
)
from .ingest import ingest
from .read import read_object, read_page, render_markdown
from .update import update_object


def update_page(store: Path, page_id: str, expected_version: int, patch: dict[str, Any], reason: str) -> dict[str, Any]:
    return update_object(store, page_id, expected_version, patch, reason)


__all__ = [
    "SCHEMA_VERSION", "BuildlogError", "ConflictError", "NotFoundError", "ValidationError", "default_store_path",
    "initialize", "ingest", "read_object", "read_page", "render_markdown", "update_object", "update_page",
]
