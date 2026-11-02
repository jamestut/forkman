"""Focused recovery tests for interrupted Forkman operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ContinueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forkman-continue-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        self.upstream = self.root / "upstream.git"
        self.origin = self.root / "origin.git"
        self.repo = self.root / "work"
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Forkman Test")
        self.git(self.seed, "config", "user.email", "forkman@example.test")
        (self.seed / "base.txt").write_text("base\n")
        self.git(self.seed, "add", "base.txt")
        self.git(self.seed, "commit", "-m", "upstream base")
        self.git(self.root, "clone", "--bare", str(self.seed), str(self.upstream))
        self.git(self.root, "init", "--bare", str(self.origin))
        self.repo.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.name", "Forkman Test")
        self.git(self.repo, "config", "user.email", "forkman@example.test")
        self.git(self.repo, "remote", "add", "upstream", str(self.upstream))
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "fetch", "upstream")
        self.git(self.repo, "reset", "--hard", "upstream/main")
        self.run_cli("init")

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(SOURCE_ROOT), env.get("PYTHONPATH", "")])
        result = subprocess.run(
            [os.environ.get("PYTHON", "python"), "-m", "forkman", *args], cwd=self.repo,
            env=env, text=True, capture_output=True, check=False,
        )
        if check and result.returncode:
            self.fail(f"forkman {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def run_crashing_cli(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(SOURCE_ROOT), env.get("PYTHONPATH", "")])
        return subprocess.run(
            [os.environ.get("PYTHON", "python"), "-c", script, *args], cwd=self.repo,
            env=env, text=True, capture_output=True, check=False,
        )

    def commit_file(self, path: str, text: str, message: str) -> str:
        (self.repo / path).write_text(text)
        self.git(self.repo, "add", path)
        self.git(self.repo, "commit", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def branch_tip(self, name: str) -> str:
        return self.git(self.repo, "rev-parse", f"refs/heads/{name}").stdout.strip()

    def test_unresolved_continue_keeps_operation_paused(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/conflict", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")
        (self.seed / "shared.txt").write_text("upstream side\n")
        self.git(self.seed, "add", "shared.txt")
        self.git(self.seed, "commit", "-m", "upstream conflicting change")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")

        interrupted = self.run_cli("rebase", "custom/conflict", "--onto", "upstream", check=False)
        self.assertNotEqual(interrupted.returncode, 0)
        state_path = self.repo / ".git" / "forkman-state.json"
        state_before = state_path.read_bytes()

        still_unresolved = self.run_cli("continue", check=False)

        self.assertNotEqual(still_unresolved.returncode, 0)
        self.assertTrue(state_path.exists())
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertTrue((self.repo / ".git" / "rebase-merge").exists())
        self.assertIn("Paused:", self.run_cli("status").stdout)


    def test_continue_recovers_cherry_pick_finished_before_state_advance(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/patch", "upstream/main")
        self.commit_file("patch.txt", "patch\n", "patch change")
        self.git(self.repo, "checkout", "main")
        crash_script = """
import os, sys
import forkman.engine as engine
original = engine.perform
def crash(git, action, active):
    result = original(git, action, active)
    if action['kind'] == 'merge':
        os._exit(90)
    return result
engine.perform = crash
from forkman.cli import main
main(sys.argv[1:])
"""

        crashed = self.run_crashing_cli(crash_script, "integrate")
        self.assertEqual(crashed.returncode, 90)
        self.assertTrue((self.repo / ".git" / "forkman-state.json").exists())
        self.run_cli("continue")

        self.assertIn("patch.txt", self.git(self.repo, "ls-tree", "-r", "--name-only", "main").stdout.splitlines())
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())

    def test_manual_cherry_pick_continue_then_forkman_continue(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/patch", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")

        self.git(self.repo, "checkout", "-b", "dev/adjustment", "upstream/main")
        adjustment = self.commit_file("shared.txt", "adjustment side\n", "release adjustment")
        self.git(self.repo, "checkout", "main")
        interrupted = self.run_cli("release", "manual-continue", "--commit", adjustment, check=False)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue((self.repo / ".git" / "CHERRY_PICK_HEAD").exists())

        (self.repo / "shared.txt").write_text("resolved release\n")
        self.git(self.repo, "add", "shared.txt")
        self.git(self.repo, "-c", "core.editor=true", "cherry-pick", "--continue")
        self.run_cli("continue")

        release = self.branch_tip("release/manual-continue")
        self.assertEqual(self.git(self.repo, "show", f"{release}:shared.txt").stdout, "resolved release\n")
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())


    def test_rebase_can_continue_through_two_conflicts(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/series", "upstream/main")
        self.commit_file("shared.txt", "patch one\n", "patch one")
        self.commit_file("shared.txt", "patch two\n", "patch two")
        self.git(self.repo, "checkout", "main")
        (self.seed / "shared.txt").write_text("upstream\n")
        self.git(self.seed, "add", "shared.txt")
        self.git(self.seed, "commit", "-m", "upstream conflicting change")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")

        first = self.run_cli("rebase", "custom/series", "--onto", "upstream", check=False)
        self.assertNotEqual(first.returncode, 0)
        (self.repo / "shared.txt").write_text("resolved one\n")
        self.git(self.repo, "add", "shared.txt")
        second = self.run_cli("continue", check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("could not apply", second.stderr + second.stdout)
        (self.repo / "shared.txt").write_text("resolved two\n")
        self.git(self.repo, "add", "shared.txt")
        self.run_cli("continue")

        self.assertEqual(self.git(self.repo, "show", "custom/series:shared.txt").stdout, "resolved two\n")
        self.assertEqual(self.git(self.repo, "rev-list", "--count", "upstream/main..custom/series").stdout.strip(), "2")


    def test_empty_conflict_resolution_can_continue_rebase(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/conflict", "upstream/main")
        original = self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")
        (self.seed / "shared.txt").write_text("upstream side\n")
        self.git(self.seed, "add", "shared.txt")
        self.git(self.seed, "commit", "-m", "upstream conflicting change")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")

        interrupted = self.run_cli("rebase", "custom/conflict", "--onto", "upstream", check=False)
        self.assertNotEqual(interrupted.returncode, 0)
        (self.repo / "shared.txt").write_text("upstream side\n")
        self.git(self.repo, "add", "shared.txt")

        continued = self.run_cli("continue", check=False)

        self.assertEqual(continued.returncode, 0, continued.stderr)
        tip = self.branch_tip("custom/conflict")
        self.assertNotEqual(tip, original)
        self.assertEqual(self.git(self.repo, "rev-list", "--count", "upstream/main..custom/conflict").stdout.strip(), "1")
        self.assertEqual(self.git(self.repo, "diff", "--name-only", f"{tip}^", tip).stdout, "")

    def test_empty_conflict_resolution_can_continue_cherry_pick(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/patch", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")

        self.git(self.repo, "checkout", "-b", "dev/adjustment", "upstream/main")
        adjustment = self.commit_file("shared.txt", "adjustment side\n", "release adjustment")
        self.git(self.repo, "checkout", "main")
        interrupted = self.run_cli("release", "empty-resolution", "--commit", adjustment, check=False)
        self.assertNotEqual(interrupted.returncode, 0)
        (self.repo / "shared.txt").write_text("patch side\n")
        self.git(self.repo, "add", "shared.txt")

        continued = self.run_cli("continue", check=False)

        self.assertEqual(continued.returncode, 0, continued.stderr)
        release = self.branch_tip("release/empty-resolution")
        self.assertEqual(self.git(self.repo, "show", "-s", "--format=%s", release).stdout.strip(), "release adjustment")
        self.assertEqual(self.git(self.repo, "diff", "--name-only", f"{release}^", release).stdout, "")

    def test_manual_resolution_commit_with_custom_message_is_not_recognized(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/patch", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")

        self.git(self.repo, "checkout", "-b", "dev/adjustment", "upstream/main")
        adjustment = self.commit_file("shared.txt", "adjustment side\n", "release adjustment")
        self.git(self.repo, "checkout", "main")
        interrupted = self.run_cli("release", "manual-commit", "--commit", adjustment, check=False)
        self.assertNotEqual(interrupted.returncode, 0)
        (self.repo / "shared.txt").write_text("resolved release\n")
        self.git(self.repo, "add", "shared.txt")
        self.git(self.repo, "commit", "-m", "resolved release adjustment")

        continued = self.run_cli("continue", check=False)

        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.assertEqual(self.git(self.repo, "show", "release/manual-commit:shared.txt").stdout, "resolved release\n")


if __name__ == "__main__":
    unittest.main()
