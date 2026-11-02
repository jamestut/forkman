"""Tests for building releases against an explicitly selected upstream base."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ReleaseBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forkman-release-base-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        self.upstream = self.root / "upstream.git"
        self.origin = self.root / "origin.git"
        self.repo = self.root / "work"
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.configure(self.seed)
        (self.seed / "version.txt").write_text("v1\n")
        (self.seed / "upstream.txt").write_text("base\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "upstream v1")
        self.v1 = self.git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.git(self.seed, "tag", "-a", "v1", "-m", "release v1")
        self.git(self.root, "clone", "--bare", str(self.seed), str(self.upstream))
        self.git(self.root, "init", "--bare", str(self.origin))
        self.repo.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.configure(self.repo)
        self.git(self.repo, "remote", "add", "upstream", str(self.upstream))
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "fetch", "upstream", "--tags")
        self.git(self.repo, "reset", "--hard", "upstream/main")
        self.run_cli("init")

    def configure(self, cwd: Path) -> None:
        self.git(cwd, "config", "user.name", "Forkman Test")
        self.git(cwd, "config", "user.email", "forkman@example.test")

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(SOURCE_ROOT), env.get("PYTHONPATH", "")])
        result = subprocess.run(
            [os.environ.get("PYTHON", "python"), "-m", "forkman", *args],
            cwd=self.repo, env=env, text=True, capture_output=True, check=False,
        )
        if check and result.returncode:
            self.fail(f"forkman {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def commit_file(self, cwd: Path, path: str, content: str, message: str) -> str:
        (cwd / path).write_text(content)
        self.git(cwd, "add", path)
        self.git(cwd, "commit", "-m", message)
        return self.git(cwd, "rev-parse", "HEAD").stdout.strip()

    def tip(self, branch: str) -> str:
        return self.git(self.repo, "rev-parse", f"refs/heads/{branch}").stdout.strip()

    def test_release_base_replays_patch_ranges_in_integration_order(self) -> None:
        # Advance upstream after the tagged source point, including a version
        # change and unrelated additions which must not leak into this release.
        (self.seed / "version.txt").write_text("v2\n")
        (self.seed / "upstream-only.txt").write_text("main-only\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "upstream v2 and unrelated file")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")
        current_main = self.git(self.repo, "rev-parse", "upstream/main").stdout.strip()

        self.git(self.repo, "checkout", "-b", "custom/parent", "upstream/main")
        parent = self.commit_file(self.repo, "parent.txt", "parent patch\n", "parent patch")
        self.git(self.repo, "checkout", "-b", "custom/child", "custom/parent")
        child = self.commit_file(self.repo, "child.txt", "child patch\n", "child patch")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")

        originals = {"custom/parent": parent, "custom/child": child, "main": self.tip("main")}
        self.run_cli("release", "v1-series", "--base", "v1")

        release = "release/v1-series"
        files = set(self.git(self.repo, "ls-tree", "-r", "--name-only", release).stdout.splitlines())
        self.assertIn("parent.txt", files)
        self.assertIn("child.txt", files)
        self.assertNotIn("upstream-only.txt", files)
        self.assertEqual(self.git(self.repo, "show", f"{release}:version.txt").stdout, "v1\n")
        self.assertEqual(self.git(self.repo, "show", f"{release}:upstream.txt").stdout, "base\n")

        # Both patch commits are rebuilt as no-ff merges from the selected base.
        first_parent = self.git(self.repo, "rev-list", "--first-parent", release).stdout.splitlines()
        self.assertIn(self.v1, first_parent)
        self.assertEqual(
            self.git(self.repo, "rev-list", "--first-parent", "--merges", "--count", f"{self.v1}..{release}").stdout.strip(),
            "2",
        )
        merges = self.git(
            self.repo, "rev-list", "--first-parent", "--merges", "--reverse", f"{self.v1}..{release}"
        ).stdout.splitlines()
        first_patch_tree = set(self.git(self.repo, "ls-tree", "-r", "--name-only", f"{merges[0]}^2").stdout.splitlines())
        second_patch_tree = set(self.git(self.repo, "ls-tree", "-r", "--name-only", f"{merges[1]}^2").stdout.splitlines())
        self.assertIn("parent.txt", first_patch_tree)
        self.assertNotIn("child.txt", first_patch_tree)
        self.assertIn("parent.txt", second_patch_tree)
        self.assertIn("child.txt", second_patch_tree)
        for name, original in originals.items():
            self.assertEqual(self.tip(name), original)
            self.assertNotEqual(self.git(self.repo, "merge-base", release, original).stdout.strip(), original)
        self.assertEqual(self.git(self.repo, "rev-parse", "upstream/main").stdout.strip(), current_main)
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")

    def test_conflicting_release_base_leaves_selected_refs_and_checkout_intact(self) -> None:
        self.commit_file(self.seed, "version.txt", "v2\n", "upstream v2")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")
        self.git(self.repo, "checkout", "-b", "custom/version", "upstream/main")
        custom_tip = self.commit_file(self.repo, "version.txt", "custom version\n", "custom version")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")
        main_tip = self.tip("main")
        upstream_tip = self.git(self.repo, "rev-parse", "upstream/main").stdout.strip()

        paused = self.run_cli("release", "conflict", "--base", "v1", check=False)

        self.assertNotEqual(paused.returncode, 0)
        self.assertIn("conflict", paused.stderr.lower() + paused.stdout.lower())
        state_path = self.repo / ".git" / "forkman-state.json"
        self.assertTrue(state_path.exists(), "a replay conflict must leave resumable Forkman state")
        self.assertTrue((self.repo / ".git" / "rebase-merge").exists())
        self.assertTrue((self.repo / "version.txt").read_text().startswith("<<<<<<<"))
        self.assertEqual(self.git(self.repo, "merge-base", "--is-ancestor", "v1", "HEAD", check=False).returncode, 0)
        self.assertEqual(self.tip("custom/version"), custom_tip)
        self.assertEqual(self.tip("main"), main_tip)
        self.assertEqual(self.git(self.repo, "rev-parse", "upstream/main").stdout.strip(), upstream_tip)
        self.assertNotEqual(self.git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/release/conflict", check=False).returncode, 0)

        (self.repo / "version.txt").write_text("custom version\n")
        self.git(self.repo, "add", "version.txt")
        self.run_cli("continue")

        self.assertFalse(state_path.exists())
        self.assertEqual(self.git(self.repo, "show", "release/conflict:version.txt").stdout, "custom version\n")
        self.assertEqual(self.tip("custom/version"), custom_tip)
        self.assertEqual(self.tip("main"), main_tip)
        self.assertEqual(self.git(self.repo, "rev-parse", "upstream/main").stdout.strip(), upstream_tip)
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout.strip(), main_tip)
        self.assertEqual(self.git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/release/conflict", check=False).returncode, 0)

    def test_release_base_excludes_dependency_tree_and_replays_adjustment(self) -> None:
        self.git(self.seed, "checkout", "main")
        self.commit_file(self.seed, "version.txt", "v2\n", "upstream v2")
        self.commit_file(self.seed, "upstream-only.txt", "main-only\n", "upstream unrelated file")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.git(self.repo, "fetch", "upstream")

        self.git(self.repo, "checkout", "-b", "custom/parent", "upstream/main")
        self.commit_file(self.repo, "parent.txt", "parent patch\n", "parent patch")
        self.git(self.repo, "checkout", "-b", "custom/child", "custom/parent")
        self.commit_file(self.repo, "child.txt", "child patch\n", "child patch")
        self.git(self.repo, "checkout", "-b", "custom/independent", "upstream/main")
        self.commit_file(self.repo, "independent.txt", "independent patch\n", "independent patch")
        self.git(self.repo, "checkout", "main")
        self.run_cli("integrate")
        patch_tips = {name: self.tip(name) for name in ("custom/parent", "custom/child", "custom/independent")}
        main_tip = self.tip("main")
        upstream_tip = self.git(self.repo, "rev-parse", "upstream/main").stdout.strip()

        self.git(self.repo, "checkout", "-b", "dev/v1-adjustment", "v1")
        adjustment = self.commit_file(self.repo, "adjustment.txt", "release adjustment\n", "release adjustment")
        self.git(self.repo, "checkout", "main")
        refused = self.run_cli("release", "excluded", "--base", "v1", "--exclude", "custom/parent", check=False)
        self.assertNotEqual(refused.returncode, 0, "excluding a parent while keeping its child must fail")
        self.assertEqual(self.tip("main"), main_tip)

        self.run_cli(
            "release", "excluded", "--base", "v1", "--exclude", "custom/parent",
            "--exclude-dependents", "--commit", adjustment,
        )

        release = "release/excluded"
        files = set(self.git(self.repo, "ls-tree", "-r", "--name-only", release).stdout.splitlines())
        self.assertIn("independent.txt", files)
        self.assertIn("adjustment.txt", files)
        self.assertNotIn("parent.txt", files)
        self.assertNotIn("child.txt", files)
        self.assertNotIn("upstream-only.txt", files)
        self.assertEqual(self.git(self.repo, "show", f"{release}:version.txt").stdout, "v1\n")
        self.assertEqual(self.git(self.repo, "show", "-s", "--format=%s", release).stdout.strip(), "release adjustment")
        for name, tip in patch_tips.items():
            self.assertEqual(self.tip(name), tip)
        self.assertEqual(self.tip("main"), main_tip)
        self.assertEqual(self.git(self.repo, "rev-parse", "upstream/main").stdout.strip(), upstream_tip)
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")


if __name__ == "__main__":
    unittest.main()
