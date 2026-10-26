"""Phase 2.3: one skill source installed for Claude Code and Codex."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buildlog.skills import MARKER, SOURCE, install


class SkillInstallTests(unittest.TestCase):
    def test_hosts_get_their_own_wording_and_the_same_program(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            targets = {"claude-code": Path(d) / "claude", "codex": Path(d) / "codex"}
            result = install(["claude-code", "codex"], python=r"C:\venv\python.exe", target_dirs=targets)
            claude = (targets["claude-code"] / "SKILL.md").read_text(encoding="utf-8")
            codex = (targets["codex"] / "SKILL.md").read_text(encoding="utf-8")
            for text in (claude, codex):
                self.assertTrue(text.startswith("---\nname: buildlog\n"))
                self.assertIn(r'"C:\venv\python.exe" -m buildlog query', text)
                self.assertIn(MARKER, text)
                self.assertNotIn("{{", text)
            self.assertIn("Claude Code conversation", claude)
            self.assertIn("Codex conversation", codex)
            self.assertIn("codex:<YYYY-MM-DD>", (targets["codex"] / "plan-format.md").read_text(encoding="utf-8"))
            self.assertEqual({f["action"] for h in result["hosts"] for f in h["files"]}, {"created"})

    def test_foreign_file_is_backed_up_and_reinstall_is_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "codex"
            target.mkdir()
            (target / "SKILL.md").write_text("---\nname: buildlog\n---\nold Phase 1 skill\n", encoding="utf-8")
            first = install(["codex"], target_dirs={"codex": target})
            skill = next(f for f in first["hosts"][0]["files"] if f["file"].endswith("SKILL.md"))
            self.assertEqual(skill["action"], "updated")
            self.assertIn("old Phase 1 skill", Path(skill["backup"]).read_text(encoding="utf-8"))
            second = install(["codex"], target_dirs={"codex": target})
            self.assertEqual({f["action"] for f in second["hosts"][0]["files"]}, {"unchanged"})

    def test_installed_files_are_recognised_and_not_backed_up_again(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "claude"
            install(["claude-code"], target_dirs={"claude-code": target})
            for name in ("SKILL.md", "plan-format.md"):
                self.assertIn(MARKER, (target / name).read_text(encoding="utf-8"))
                # Simulate an older installed version: it must be replaced without a backup.
                (target / name).write_text((target / name).read_text(encoding="utf-8") + "\nold\n", encoding="utf-8")
            again = install(["claude-code"], target_dirs={"claude-code": target})
            self.assertEqual([f["backup"] for f in again["hosts"][0]["files"]], [None, None])

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "claude"
            install(["claude-code"], target_dirs={"claude-code": target}, dry_run=True)
            self.assertFalse(target.exists())

    def test_source_is_in_the_repository(self) -> None:
        self.assertTrue((SOURCE / "SKILL.md").is_file())
        self.assertTrue((SOURCE / "plan-format.md").is_file())


if __name__ == "__main__":
    unittest.main()
