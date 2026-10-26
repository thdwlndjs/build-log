"""Shared settings for the CLI and every host skill.

Resolution order for each value: command option > environment variable > config file > default.
The config file is `%APPDATA%\\Buildlog\\config.toml` unless BUILDLOG_CONFIG names another file.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from .db import ValidationError

KEYS = {
    "store": "SQLite store path",
    "agy": "path to agy.exe (Antigravity CLI)",
}
ENV = {"store": "BUILDLOG_STORE", "agy": "BUILDLOG_AGY"}
AGY_COMMAND_ENV = "BUILDLOG_AGY_COMMAND"  # JSON list, for wrappers such as ["python", "fake_agy.py"]


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def config_path() -> Path:
    configured = os.environ.get("BUILDLOG_CONFIG")
    return Path(configured).expanduser() if configured else _appdata() / "Buildlog" / "config.toml"


def defaults() -> dict[str, str]:
    return {"store": str(_localappdata() / "Buildlog" / "buildlog.sqlite3"),
            "agy": str(_localappdata() / "agy" / "bin" / "agy.exe")}


def _expand(value: str, base: Path) -> str:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return str(path if path.is_absolute() else (base / path))


def load_config(path: Path | None = None) -> dict[str, str]:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ValidationError(f"config file {path} is not valid TOML: {error}") from error
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise ValidationError(f"config file {path} has unknown keys {unknown}; allowed: {sorted(KEYS)}")
    values = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"config file {path}: {key} must be a non-empty string")
        values[key] = _expand(value, path.parent)
    return values


def resolve(key: str, option: str | None = None) -> tuple[str, str]:
    """Return (value, where it came from)."""
    if option:
        return str(Path(option).expanduser()), "option"
    env = os.environ.get(ENV[key])
    if env:
        return str(Path(os.path.expandvars(env)).expanduser()), f"env {ENV[key]}"
    config = load_config()
    if key in config:
        return config[key], f"config {config_path()}"
    return defaults()[key], "default"


def resolve_store(option: str | None = None) -> Path:
    return Path(resolve("store", option)[0])


def resolve_agy(option: str | None = None) -> list[str]:
    if option:
        return [str(Path(option).expanduser())]
    command = os.environ.get(AGY_COMMAND_ENV)
    if command:
        try:
            value = json.loads(command)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{AGY_COMMAND_ENV} must be a JSON list of strings") from error
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"{AGY_COMMAND_ENV} must be a JSON list of strings")
        return value
    return [resolve("agy")[0]]


def show(store_option: str | None = None) -> dict[str, Any]:
    store, store_from = resolve("store", store_option)
    agy, agy_from = resolve("agy")
    if os.environ.get(AGY_COMMAND_ENV):
        agy, agy_from = " ".join(resolve_agy()), f"env {AGY_COMMAND_ENV}"
    path = config_path()
    return {"config_file": str(path), "config_exists": path.exists(),
            "store": {"value": store, "from": store_from}, "agy": {"value": agy, "from": agy_from},
            "order": "option > environment > config file > default"}


def _toml_string(value: str) -> str:
    # Literal strings keep Windows backslashes as they are.
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def set_value(key: str, value: str | None) -> dict[str, Any]:
    if key not in KEYS:
        raise ValidationError(f"unknown config key {key!r}; allowed: {sorted(KEYS)}")
    path = config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        load_config(path)  # refuse to rewrite a file that is already invalid
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    if value is None:
        raw.pop(key, None)
    else:
        if not value.strip():
            raise ValidationError("value must be a non-empty string")
        raw[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Buildlog settings shared by the CLI, Claude Code and Codex.",
             "# Order: command option > environment variable > this file > default."]
    lines += [f"{k} = {_toml_string(v)}" for k, v in sorted(raw.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return show()
