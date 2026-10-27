"""Install the Buildlog skill for each host from the single source in skills/buildlog/."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import ValidationError

SOURCE = Path(__file__).resolve().parents[2] / "skills" / "buildlog"
MARKER = "installed by `buildlog install-skill`"

HOSTS: dict[str, dict[str, str]] = {
    "claude-code": {
        "HOST_NAME": "Claude Code",
        "KEY_PREFIX": "claude-code",
        "WRITE_FILE": "use your file-writing tool (not shell echo, which can break Korean text) and put the file in your scratchpad or temp directory.",
    },
    "codex": {
        "HOST_NAME": "Codex",
        "KEY_PREFIX": "codex",
        "WRITE_FILE": "write the file with apply_patch, or with PowerShell `Set-Content -Encoding utf8` / Python, in `$env:TEMP`. Do not use `echo` or `>` redirection in Windows PowerShell 5.1, which writes UTF-16.",
    },
}


def host_dir(host: str) -> Path:
    if host == "claude-code":
        return Path.home() / ".claude" / "skills" / "buildlog"
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "skills" / "buildlog"


def render(text: str, host: str, python: str) -> str:
    values = {**HOSTS[host], "PYTHON": python}
    rendered = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values[m.group(1)], text)
    left = re.findall(r"\{\{[^}]*\}\}", rendered)
    if left:
        raise ValidationError(f"unreplaced placeholders in skill source: {sorted(set(left))}")
    return rendered


def host_description(rendered: str, host: str, source: Path) -> str:
    """Swap in `description.<host>.txt` as the SKILL.md frontmatter description, if that file exists.

    Codex lists skills with a shortened description (cut near 650 characters in observed
    sessions), so it gets its own text with the "use when" part first. Hosts without such a
    file keep the description from SKILL.md unchanged.
    """
    override = source / f"description.{host}.txt"
    if not override.is_file():
        return rendered
    text = " ".join(override.read_text(encoding="utf-8").split())
    if not text:
        raise ValidationError(f"{override.name} is empty")
    head, sep, body = rendered.partition("\n---\n")  # frontmatter ends at the second '---'
    new_head, count = re.subn(r"^description: .*$", lambda _: f"description: {text}", head, count=1, flags=re.M)
    if count != 1 or not sep:
        raise ValidationError("SKILL.md frontmatter has no description line to replace")
    return new_head + sep + body


def install(hosts: list[str], python: str | None = None, source: Path = SOURCE, dry_run: bool = False,
            target_dirs: dict[str, Path] | None = None) -> dict[str, Any]:
    unknown = [h for h in hosts if h not in HOSTS]
    if unknown:
        raise ValidationError(f"unknown host(s) {unknown}; choose from {sorted(HOSTS)}")
    if not (source / "SKILL.md").is_file():
        raise ValidationError(f"skill source not found at {source}; run from the Buildlog repository checkout")
    python = python or sys.executable
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for host in hosts:
        target = (target_dirs or {}).get(host) or host_dir(host)
        files = []
        for src in sorted(source.glob("*.md")):
            rendered = render(src.read_text(encoding="utf-8"), host, python)
            if src.name == "SKILL.md":
                rendered = host_description(rendered, host, source)
            dest = target / src.name
            action, backup = "created", None
            if dest.exists():
                current = dest.read_text(encoding="utf-8", errors="replace")
                if current == rendered:
                    action = "unchanged"
                else:
                    action = "updated"
                    if MARKER not in current:
                        # Someone else wrote this file; keep it before replacing.
                        backup = dest.with_name(f"{dest.name}.bak-{stamp}")
            if not dry_run and action != "unchanged":
                target.mkdir(parents=True, exist_ok=True)
                if backup is not None:
                    backup.write_bytes(dest.read_bytes())
                dest.write_text(rendered, encoding="utf-8", newline="\n")
            files.append({"file": str(dest), "action": action, "backup": str(backup) if backup else None})
        results.append({"host": host, "dir": str(target), "files": files})
    return {"python": python, "source": str(source), "dry_run": dry_run, "hosts": results}
