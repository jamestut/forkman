"""End-to-end tests for Forkman's Git history operations.

Each test drives ``python -m forkman`` in an isolated repository.  The
fixtures use local bare remotes, so these tests exercise real Git refs and
merge/rebase behavior without relying on a network service.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class WorkflowTest(unittest.TestCase):
    def test_release_keeps_adjustment_that_becomes_empty(self) -> None:
        patch = self.create_patch("custom/already-applied", "upstream", "feature.txt", "feature\n")
        self.run_cli("integrate")
        self.run_cli("release", "redundant", "--commit", patch)
        self.assertEqual(
            self.git(self.repo, "show", "-s", "--format=%s", "release/redundant").stdout.strip(),
            "custom/already-applied change",
        )
        self.assertEqual(
            self.git(self.repo, "diff", "--name-only", "release/redundant^", "release/redundant").stdout,
            "",
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forkman-test-")
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
        (self.seed / "upstream.txt").write_text("upstream v1\n")
        self.git(self.seed, "add", "upstream.txt")
        self.git(self.seed, "commit", "-m", "upstream base")
        self.base = self.git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.git(self.root, "clone", "--bare", str(self.seed), str(self.upstream))
        self.git(self.root, "init", "--bare", str(self.origin))

        self.create_working_repo("work")
        self.run_cli("init")
        self.config_commit = self.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.run_cli("status")
        self.run_cli("validate")

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
        )
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed in {cwd}:\n{result.stdout}\n{result.stderr}")
        return result

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SOURCE_ROOT), env.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [os.environ.get("PYTHON", "python"), "-m", "forkman", *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(
                f"forkman {' '.join(args)} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def run_crashing_cli(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SOURCE_ROOT), env.get("PYTHONPATH", "")]
        )
        return subprocess.run(
            [os.environ.get("PYTHON", "python"), "-c", script, *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def create_working_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Forkman Test")
        self.git(repo, "config", "user.email", "forkman@example.test")
        self.git(repo, "remote", "add", "upstream", str(self.upstream))
        self.git(repo, "fetch", "upstream")
        self.git(repo, "reset", "--hard", "upstream/main")
        self.git(repo, "remote", "add", "origin", str(self.origin))
        return repo

    def commit_file(self, path: str, content: str, message: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git(self.repo, "add", path)
        self.git(self.repo, "commit", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def create_patch(self, name: str, parent: str, path: str, content: str) -> str:
        parent_ref = "upstream/main" if parent == "upstream" else parent
        self.git(self.repo, "checkout", "-b", name, parent_ref)
        tip = self.commit_file(path, content, f"{name} change")
        self.git(self.repo, "checkout", "main")
        return tip

    def has_ancestor(self, older: str, newer: str) -> bool:
        return self.git(self.repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def branch_tip(self, name: str) -> str:
        return self.git(self.repo, "rev-parse", f"refs/heads/{name}").stdout.strip()

    def test_init_creates_one_config_commit_and_sibling_merge_order(self) -> None:
        config = yaml.safe_load((self.repo / "forkman.yaml").read_text())
        self.assertEqual(
            config,
            {
                "upstream": {"remote": "upstream", "branch": "main"},
                "fork": {"remote": "origin"},
                "integration": {"branch": "main"},
            },
        )
        self.assertEqual(self.git(self.repo, "rev-parse", f"{self.config_commit}^").stdout.strip(), self.base)

        a = self.create_patch("custom/a", "upstream", "a.txt", "A\n")
        b = self.create_patch("custom/b", "upstream", "b.txt", "B\n")
        self.run_cli("integrate")

        # Both patches are merged as separate first-parent steps, in a stable
        # order, and the patch tips are the second parents of those merges.
        merges = self.git(self.repo, "rev-list", "--first-parent", "--merges", "main").stdout.splitlines()
        self.assertEqual(len(merges), 2)
        merge_second_parents = [
            self.git(self.repo, "rev-parse", f"{merge}^2").stdout.strip()
            for merge in reversed(merges)
        ]
        self.assertCountEqual(merge_second_parents, [a, b])
        self.assertEqual((self.repo / "a.txt").read_text(), "A\n")
        self.assertEqual((self.repo / "b.txt").read_text(), "B\n")
        config_history = self.git(self.repo, "log", "main", "--format=%H", "--", "forkman.yaml").stdout.splitlines()
        self.assertEqual(len(config_history), 1)

    def test_update_rebases_patch_stack_and_rebuilds_integration(self) -> None:
        self.create_patch("custom/base", "upstream", "base-patch.txt", "base patch\n")
        child = self.create_patch("custom/child", "custom/base", "child-patch.txt", "child patch\n")
        self.run_cli("integrate")

        # Advance the upstream remote independently, then update the local
        # stack. The patch order and contents survive while the new upstream
        # commit becomes the root of each rewritten patch history.
        (self.seed / "upstream.txt").write_text("upstream v2\n")
        self.git(self.seed, "add", "upstream.txt")
        self.git(self.seed, "commit", "-m", "upstream update")
        self.git(self.seed, "push", str(self.upstream), "main")
        new_upstream = self.git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.run_cli("update", "--fetch")

        self.assertTrue(self.has_ancestor(new_upstream, self.branch_tip("custom/base")))
        self.assertTrue(self.has_ancestor(self.branch_tip("custom/base"), self.branch_tip("custom/child")))
        self.assertNotEqual(child, self.branch_tip("custom/child"))
        self.assertTrue(self.has_ancestor(new_upstream, self.branch_tip("main")))
        self.assertEqual((self.repo / "upstream.txt").read_text(), "upstream v2\n")
        self.assertEqual((self.repo / "base-patch.txt").read_text(), "base patch\n")
        self.assertEqual((self.repo / "child-patch.txt").read_text(), "child patch\n")

    def test_reparenting_patch_replays_its_descendants(self) -> None:
        alternate = self.create_patch("custom/alternate", "upstream", "alternate.txt", "alternate\n")
        self.create_patch("custom/base", "upstream", "base.txt", "base\n")
        old_child = self.create_patch("custom/child", "custom/base", "child.txt", "child\n")
        self.run_cli("integrate")

        self.run_cli("rebase", "custom/base", "--onto", "custom/alternate")

        new_base = self.branch_tip("custom/base")
        new_child = self.branch_tip("custom/child")
        self.assertTrue(self.has_ancestor(alternate, new_base))
        self.assertTrue(self.has_ancestor(new_base, new_child))
        self.assertNotEqual(new_child, old_child)
        self.assertEqual((self.repo / "alternate.txt").read_text(), "alternate\n")
        self.assertEqual((self.repo / "base.txt").read_text(), "base\n")
        self.assertEqual((self.repo / "child.txt").read_text(), "child\n")

    def test_release_dependency_exclusion_and_explicit_adjustment_commit(self) -> None:
        self.create_patch("custom/base", "upstream", "base.txt", "base\n")
        self.create_patch("custom/child", "custom/base", "child.txt", "child\n")
        self.create_patch("custom/independent", "upstream", "independent.txt", "independent\n")
        self.run_cli("integrate")

        refused = self.run_cli("release", "bad", "--exclude", "custom/base", check=False)
        self.assertNotEqual(refused.returncode, 0, "excluding a parent should fail when a child remains selected")
        self.assertFalse((self.repo / "release-bad.txt").exists())

        self.git(self.repo, "checkout", "-b", "dev/release-adjustment", "main")
        adjustment = self.commit_file("adjustment.txt", "release fix\n", "release adjustment")
        self.git(self.repo, "checkout", "main")
        self.run_cli(
            "release", "2.4", "--exclude", "custom/base", "--exclude-dependents", "--commit", adjustment
        )
        release = "release/2.4"
        self.assertEqual(self.git(self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{release}", check=False).returncode, 0)
        tree = self.git(self.repo, "ls-tree", "-r", "--name-only", release).stdout.splitlines()
        self.assertIn("independent.txt", tree)
        self.assertNotIn("base.txt", tree)
        self.assertNotIn("child.txt", tree)
        self.assertIn("adjustment.txt", tree)
        self.assertFalse(self.has_ancestor(self.branch_tip("main"), self.branch_tip(release)))

    def test_delete_refuses_parent_and_allows_leaf(self) -> None:
        self.create_patch("custom/parent", "upstream", "parent.txt", "parent\n")
        self.create_patch("custom/child", "custom/parent", "child.txt", "child\n")
        self.run_cli("integrate")
        original_parent = self.branch_tip("custom/parent")

        refused = self.run_cli("delete", "custom/parent", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.branch_tip("custom/parent"), original_parent)

        self.run_cli("delete", "custom/child")
        self.assertNotEqual(self.git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/custom/child", check=False).returncode, 0)

    def test_promotion_from_integration_uses_explicit_exclusive_boundary(self) -> None:
        self.create_patch("custom/existing", "upstream", "existing.txt", "existing\n")
        self.run_cli("integrate")
        start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/new-work", "main")
        dev_tip = self.commit_file("new-work.txt", "new work\n", "development work")
        self.git(self.repo, "checkout", "main")
        self.run_cli("promote", "dev/new-work", "--new", "custom/new-work", "--from", start)

        promoted = self.branch_tip("custom/new-work")
        self.assertEqual((self.repo / "new-work.txt").read_text(), "new work\n")
        self.assertTrue(self.has_ancestor(self.base, promoted))
        self.assertFalse(self.has_ancestor(start, promoted), "the integration fork point must not enter patch ancestry")
        self.assertNotEqual(promoted, dev_tip, "promotion should replay development work onto the patch base")

    def test_promotion_update_replays_dev_work_onto_existing_patch(self) -> None:
        self.create_patch("custom/existing", "upstream", "existing.txt", "existing\n")
        self.run_cli("integrate")
        original_patch = self.branch_tip("custom/existing")
        start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/update-existing", "main")
        dev_tip = self.commit_file("refresh.txt", "refreshed\n", "development refresh")
        self.git(self.repo, "checkout", "main")

        self.run_cli("promote", "dev/update-existing", "--update", "custom/existing", "--from", start)

        updated = self.branch_tip("custom/existing")
        self.assertNotEqual(updated, original_patch)
        self.assertNotEqual(updated, dev_tip)
        self.assertTrue(self.has_ancestor(self.base, updated))
        self.assertFalse(self.has_ancestor(start, updated))
        self.assertEqual((self.repo / "existing.txt").read_text(), "existing\n")
        self.assertEqual((self.repo / "refresh.txt").read_text(), "refreshed\n")

    def test_update_existing_patch_replays_dependent_child(self) -> None:
        self.create_patch("custom/parent", "upstream", "parent.txt", "parent\n")
        old_child = self.create_patch("custom/child", "custom/parent", "child.txt", "child\n")
        self.run_cli("integrate")
        start = self.branch_tip("main")
        old_parent = self.branch_tip("custom/parent")
        self.git(self.repo, "checkout", "-b", "dev/parent-update", "main")
        self.commit_file("parent-followup.txt", "followup\n", "parent follow-up")
        self.git(self.repo, "checkout", "main")

        self.run_cli("promote", "dev/parent-update", "--update", "custom/parent", "--from", start)

        new_parent = self.branch_tip("custom/parent")
        new_child = self.branch_tip("custom/child")
        self.assertNotEqual(new_parent, old_parent)
        self.assertNotEqual(new_child, old_child)
        self.assertTrue(self.has_ancestor(new_parent, new_child))
        self.assertEqual((self.repo / "parent-followup.txt").read_text(), "followup\n")
        self.assertEqual((self.repo / "child.txt").read_text(), "child\n")

    def test_promotion_before_and_after_places_patches_in_stack_order(self) -> None:
        self.create_patch("custom/a", "upstream", "a.txt", "a\n")
        self.create_patch("custom/b", "upstream", "b.txt", "b\n")
        self.run_cli("integrate")

        for name, anchor, position in (
            ("x", "custom/a", "--after"),
            ("y", "custom/x", "--after"),
        ):
            start = self.branch_tip("main")
            dev = f"dev/{name}"
            self.git(self.repo, "checkout", "-b", dev, "main")
            self.commit_file(f"{name}.txt", f"{name}\n", f"development {name}")
            self.git(self.repo, "checkout", "main")
            self.run_cli(
                "promote", dev, "--new", f"custom/{name}", "--from", start,
                position, anchor,
            )

        # Also exercise --before against a later stack entry by inserting z
        # immediately before b. The first-parent merge sequence is the public
        # record of patch order.
        start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/z", "main")
        self.commit_file("z.txt", "z\n", "development z")
        self.git(self.repo, "checkout", "main")
        self.run_cli(
            "promote", "dev/z", "--new", "custom/z", "--from", start,
            "--before", "custom/b",
        )
        merges = self.git(self.repo, "rev-list", "--first-parent", "--merges", "main").stdout.splitlines()
        order = [
            self.git(self.repo, "for-each-ref", "--format=%(refname:short)", "--contains", self.git(self.repo, "rev-parse", f"{merge}^2").stdout.strip(), "refs/heads/custom").stdout.splitlines()
            for merge in reversed(merges)
        ]
        labels = [next((ref.rsplit("/", 1)[-1] for ref in refs if ref.startswith("custom/")), "?") for refs in order]
        self.assertEqual(labels, ["a", "x", "y", "z", "b"])

    def test_promotion_reads_nondefault_integration_from_config(self) -> None:
        previous_repo = self.repo
        self.repo = self.create_working_repo("custom-integration")
        try:
            self.run_cli("init", "--integration", "fork-main")
            config = yaml.safe_load(self.git(self.repo, "show", "fork-main:forkman.yaml").stdout)
            self.assertEqual(config["integration"]["branch"], "fork-main")
            self.create_patch("custom/base", "upstream", "base.txt", "base\n")
            self.run_cli("integrate")
            start = self.branch_tip("fork-main")
            self.git(self.repo, "checkout", "-b", "dev/from-fork-main", "fork-main")
            self.commit_file("promoted.txt", "from configured integration\n", "dev change")
            self.git(self.repo, "checkout", "fork-main")

            self.run_cli("promote", "dev/from-fork-main", "--new", "custom/promoted", "--from", start)

            tip = self.branch_tip("custom/promoted")
            self.assertFalse(self.has_ancestor(start, tip))
            self.assertTrue(self.has_ancestor(self.base, tip))
            promoted_file = self.git(self.repo, "show", "fork-main:promoted.txt").stdout
            self.assertEqual(promoted_file, "from configured integration\n")
            self.assertTrue(self.has_ancestor(tip, self.branch_tip("fork-main")))
        finally:
            self.repo = previous_repo

    def test_rebase_conflict_can_continue_after_resolution(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/conflict", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        self.git(self.repo, "checkout", "main")

        self.git(self.seed, "checkout", "-b", "upstream-change")
        (self.seed / "shared.txt").write_text("upstream side\n")
        self.git(self.seed, "add", "shared.txt")
        self.git(self.seed, "commit", "-m", "upstream conflicting change")
        self.git(self.seed, "push", str(self.upstream), "HEAD:main")
        new_base = self.git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.git(self.repo, "fetch", "upstream")

        result = self.run_cli("rebase", "custom/conflict", "--onto", "upstream", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.repo / ".git" / "forkman-state.json").exists())
        (self.repo / "shared.txt").write_text("resolved\n")
        self.git(self.repo, "add", "shared.txt")
        self.run_cli("continue")

        tip = self.branch_tip("custom/conflict")
        self.assertTrue(self.has_ancestor(new_base, tip))
        self.assertEqual((self.repo / "shared.txt").read_text(), "resolved\n")
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())

    def create_conflicting_patches(self) -> tuple[str, str]:
        a = self.create_patch("custom/a", "upstream", "shared.txt", "patch A\n")
        b = self.create_patch("custom/b", "upstream", "shared.txt", "patch B\n")
        return a, b

    def test_integration_conflict_can_continue_after_resolution(self) -> None:
        a, b = self.create_conflicting_patches()
        main_before = self.branch_tip("main")

        result = self.run_cli("integrate", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.repo / ".git" / "forkman-state.json").exists())
        self.assertEqual(self.branch_tip("main"), main_before, "managed refs must stay unpublished while conflicted")
        (self.repo / "shared.txt").write_text("resolved A and B\n")
        self.git(self.repo, "add", "shared.txt")
        self.run_cli("continue")

        self.assertEqual((self.repo / "shared.txt").read_text(), "resolved A and B\n")
        self.assertEqual(self.branch_tip("custom/a"), a)
        self.assertEqual(self.branch_tip("custom/b"), b)
        self.assertNotEqual(self.branch_tip("main"), main_before)
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.run_cli("validate")

    def test_integration_conflict_abort_preserves_all_managed_tips(self) -> None:
        self.create_conflicting_patches()
        main_before = self.branch_tip("main")
        patch_tips = {name: self.branch_tip(name) for name in ("custom/a", "custom/b")}

        result = self.run_cli("integrate", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.repo / ".git" / "forkman-state.json").exists())
        self.run_cli("abort")

        self.assertEqual(self.branch_tip("main"), main_before)
        self.assertEqual({name: self.branch_tip(name) for name in patch_tips}, patch_tips)
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.assertFalse((self.repo / "shared.txt").exists())

    def test_rebase_abort_restores_original_patch_tip(self) -> None:
        self.git(self.repo, "checkout", "-b", "custom/conflict", "upstream/main")
        self.commit_file("shared.txt", "patch side\n", "patch change")
        original_tip = self.branch_tip("custom/conflict")
        original_main = self.branch_tip("main")
        self.git(self.repo, "checkout", "main")

        self.git(self.seed, "checkout", "-b", "upstream-change")
        (self.seed / "shared.txt").write_text("upstream side\n")
        self.git(self.seed, "add", "shared.txt")
        self.git(self.seed, "commit", "-m", "upstream conflicting change")
        self.git(self.seed, "push", str(self.upstream), "HEAD:main")
        self.git(self.repo, "fetch", "upstream")
        result = self.run_cli("rebase", "custom/conflict", "--onto", "upstream", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.run_cli("abort")

        self.assertEqual(self.branch_tip("custom/conflict"), original_tip)
        self.assertEqual(self.branch_tip("main"), original_main)
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.assertFalse((self.repo / "shared.txt").exists())

    def test_init_adopts_existing_no_ff_patch_merges(self) -> None:
        previous_repo = self.repo
        self.repo = self.create_working_repo("manual-integration")
        try:
            a = self.create_patch("custom/a", "upstream", "a.txt", "a\n")
            self.git(self.repo, "merge", "--no-ff", "--no-edit", "-m", "manual merge a", "custom/a")
            b = self.create_patch("custom/b", "upstream", "b.txt", "b\n")
            self.git(self.repo, "merge", "--no-ff", "--no-edit", "-m", "manual merge b", "custom/b")

            self.run_cli("init")

            config_commits = self.git(self.repo, "log", "main", "--format=%H", "--", "forkman.yaml").stdout.splitlines()
            self.assertEqual(len(config_commits), 1)
            self.assertEqual(self.git(self.repo, "rev-parse", f"{config_commits[0]}^").stdout.strip(), self.base)
            merges = self.git(self.repo, "rev-list", "--first-parent", "--merges", "main").stdout.splitlines()
            self.assertEqual(len(merges), 2)
            second_parents = [
                self.git(self.repo, "rev-parse", f"{merge}^2").stdout.strip()
                for merge in reversed(merges)
            ]
            self.assertEqual(second_parents, [a, b])
            self.run_cli("validate")
        finally:
            self.repo = previous_repo

    def test_rebase_dev_branch_directly_onto_custom_with_from(self) -> None:
        custom_tip = self.create_patch("custom/target", "upstream", "target.txt", "target\n")
        start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/direct-rebase", "main")
        self.commit_file("dev-only.txt", "development\n", "dev change")
        dev_before = self.branch_tip("dev/direct-rebase")
        self.git(self.repo, "checkout", "main")

        self.run_cli("rebase", "dev/direct-rebase", "--onto", "custom/target", "--from", start)

        tip = self.branch_tip("dev/direct-rebase")
        self.assertNotEqual(tip, dev_before)
        self.assertTrue(self.has_ancestor(custom_tip, tip))
        self.assertFalse(self.has_ancestor(start, tip))
        self.assertEqual(self.git(self.repo, "show", "dev/direct-rebase:dev-only.txt").stdout, "development\n")

    def test_push_lease_rejects_remote_advanced_after_last_fetch(self) -> None:
        self.run_cli("push", "main")
        self.git(self.repo, "fetch", "origin")
        self.create_patch("custom/push-test", "upstream", "patch.txt", "local patch\n")
        self.run_cli("integrate")

        outsider = self.root / "outsider"
        self.git(self.root, "clone", "--branch", "main", str(self.origin), str(outsider))
        self.git(outsider, "config", "user.name", "Other Contributor")
        self.git(outsider, "config", "user.email", "other@example.test")
        (outsider / "remote-change.txt").write_text("advanced remotely\n")
        self.git(outsider, "add", "remote-change.txt")
        self.git(outsider, "commit", "-m", "advance origin independently")
        self.git(outsider, "push", "origin", "main")
        remote_tip = self.git(outsider, "rev-parse", "HEAD").stdout.strip()

        rejected = self.run_cli("push", "main", check=False)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.git(self.root, "ls-remote", str(self.origin), "refs/heads/main").stdout.split()[0], remote_tip)

    def test_continue_retries_rebase_crashed_before_git_started(self) -> None:
        self.create_patch("custom/existing", "upstream", "existing.txt", "existing\n")
        self.run_cli("integrate")
        integration_start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/recovery-before-rebase", "main")
        self.commit_file("recovered.txt", "recovered\n", "development recovery")
        self.git(self.repo, "checkout", "main")
        crash_script = """
import os, sys
import forkman.engine as engine
original = engine.perform
def crash(git, action, active):
    if action['kind'] == 'rebase':
        os._exit(90)
    return original(git, action, active)
engine.perform = crash
from forkman.cli import main
main(sys.argv[1:])
"""

        crashed = self.run_crashing_cli(
            crash_script, "rebase", "dev/recovery-before-rebase", "--onto", "upstream",
            "--from", integration_start,
        )
        self.assertEqual(crashed.returncode, 90)
        self.assertTrue((self.repo / ".git" / "forkman-state.json").exists())
        self.run_cli("continue")

        tip = self.branch_tip("dev/recovery-before-rebase")
        self.assertFalse(self.has_ancestor(integration_start, tip))
        self.assertEqual(self.git(self.repo, "show", "dev/recovery-before-rebase:recovered.txt").stdout, "recovered\n")
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())

    def test_continue_verifies_config_commit_after_crash_before_state_advance(self) -> None:
        self.create_patch("custom/recovery", "upstream", "recovery.txt", "patch\n")
        crash_script = """
import os, sys
import forkman.engine as engine
original = engine.perform
def crash(git, action, active):
    result = original(git, action, active)
    if action['kind'] == 'config':
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

        config_commits = self.git(self.repo, "log", "main", "--format=%H", "--", "forkman.yaml").stdout.splitlines()
        self.assertEqual(len(config_commits), 1)
        self.assertIn("recovery.txt", self.git(self.repo, "ls-tree", "-r", "--name-only", "main").stdout.splitlines())
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.run_cli("validate")

    def test_continue_recognizes_rebase_finished_before_state_advance(self) -> None:
        start = self.branch_tip("main")
        self.git(self.repo, "checkout", "-b", "dev/recovery-after-rebase", "main")
        self.commit_file("finished.txt", "finished\n", "development finished")
        original_dev = self.branch_tip("dev/recovery-after-rebase")
        self.git(self.repo, "checkout", "main")
        crash_script = """
import os, sys
import forkman.engine as engine
original = engine.perform
def crash(git, action, active):
    result = original(git, action, active)
    if action['kind'] == 'rebase':
        os._exit(90)
    return result
engine.perform = crash
from forkman.cli import main
main(sys.argv[1:])
"""

        crashed = self.run_crashing_cli(
            crash_script, "rebase", "dev/recovery-after-rebase", "--onto", "upstream", "--from", start
        )
        self.assertEqual(crashed.returncode, 90)
        self.run_cli("continue")

        tip = self.branch_tip("dev/recovery-after-rebase")
        self.assertNotEqual(tip, original_dev)
        self.assertFalse(self.has_ancestor(start, tip))
        self.assertEqual(self.git(self.repo, "show", "dev/recovery-after-rebase:finished.txt").stdout, "finished\n")

    def test_branch_discovery_ignores_tags_matching_branch_names(self) -> None:
        # Git's short ref formatter can return tag names when a tag collides
        # with a local branch. Forkman's stack model must use refs/heads.
        self.git(self.repo, "tag", "main", "upstream/main")
        self.git(self.repo, "tag", "custom/a", "upstream/main")
        self.create_patch("custom/a", "upstream", "tag-collision.txt", "patch branch\n")

        self.run_cli("integrate")
        self.run_cli("validate")

        self.assertEqual(self.git(self.repo, "show", "refs/heads/main:tag-collision.txt").stdout, "patch branch\n")
        self.assertNotEqual(self.git(self.repo, "rev-parse", "refs/heads/custom/a").stdout.strip(), self.base)

    def test_init_updates_fork_remote_and_keeps_one_config_commit(self) -> None:
        self.git(self.repo, "remote", "add", "fork", str(self.origin))

        self.run_cli("init", "--fork-remote", "fork")

        config = yaml.safe_load(self.git(self.repo, "show", "main:forkman.yaml").stdout)
        self.assertEqual(config["fork"]["remote"], "fork")
        config_commits = self.git(self.repo, "log", "main", "--format=%H", "--", "forkman.yaml").stdout.splitlines()
        self.assertEqual(len(config_commits), 1)
        self.assertEqual(self.git(self.repo, "rev-parse", f"{config_commits[0]}^").stdout.strip(), self.base)
        self.run_cli("validate")

    def test_dry_run_leaves_refs_and_worktree_untouched(self) -> None:
        patch = self.create_patch("custom/preview", "upstream", "preview.txt", "preview\n")
        before_main = self.branch_tip("main")
        self.run_cli("integrate", "--dry-run")
        self.assertEqual(self.branch_tip("main"), before_main)
        self.assertEqual(self.branch_tip("custom/preview"), patch)
        self.assertFalse((self.repo / ".git" / "forkman-state.json").exists())
        self.assertFalse((self.repo / "preview.txt").exists())

    def test_validate_rejects_direct_commit_on_integration(self) -> None:
        integration_before = self.branch_tip("main")
        direct = self.commit_file("manual.txt", "manual integration change\n", "manual integration change")

        result = self.run_cli("validate", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.branch_tip("main"), direct)
        self.assertNotEqual(integration_before, direct)


if __name__ == "__main__":
    unittest.main()
