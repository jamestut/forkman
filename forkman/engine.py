"""Resumable operations, prepared on temporary branches before publishing refs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from .git import Error, Git


def state_path(git: Git) -> Path:
    return git.git_dir / "forkman-state.json"


def save(git: Git, state: dict) -> None:
    path = state_path(git)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, path)


def read(git: Git) -> dict:
    try:
        return json.loads(state_path(git).read_text())
    except FileNotFoundError:
        raise Error("No Forkman operation is in progress.") from None


def check_idle(git: Git) -> None:
    if state_path(git).exists():
        raise Error("A Forkman operation is in progress. Use status, continue, or abort.")
    if not git.clean():
        raise Error("The worktree must be clean, including untracked files.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply", "REVERT_HEAD"):
        if (git.git_dir / marker).exists():
            raise Error("Finish or abort the existing Git operation first.")


class Plan:
    def __init__(self, git: Git, label: str):
        self.git = git
        self.id = uuid.uuid4().hex[:12]
        self.label = label
        self.actions: list[dict] = []
        self.updates: list[dict] = []
        self.temps: list[str] = []
        self.expected: dict[str, str] = {}
        self.parents: dict[str, str | None] | None = None
        self.baseline: str | None = None

    def temp(self, start: str) -> str:
        name = f"forkman-tmp/{self.id}/{len(self.temps)}"
        self.temps.append(name)
        self.actions.append({"kind": "checkout", "branch": name, "start": start})
        return name

    def action(self, kind: str, **kwargs) -> None:
        self.actions.append({"kind": kind, **kwargs})

    def update(self, branch: str, source: str | None) -> None:
        ref = "refs/heads/" + branch
        old = self.git.oid(ref) if self.git.exists(ref) else None
        self.updates.append({"ref": ref, "old": old, "source": source})

    def describe(self) -> None:
        print(self.label)
        for action in self.actions:
            kind = action["kind"]
            if kind == "rebase":
                print(f"  Rebase {action['name']} onto {action['onto_name']}")
            elif kind == "merge":
                print(f"  Merge {action['name']}")
            elif kind == "config":
                print("  Create the integration configuration commit")
            elif kind == "cherry-pick":
                print(f"  Replay release commit {action['commit']}")
        for update in self.updates:
            print(f"  {'Delete' if update['source'] is None else 'Update'} {update['ref'][11:]}")

    def execute(self, dry_run: bool = False) -> None:
        check_idle(self.git)
        self.describe()
        # A different worktree must never see its checked-out branch rewritten.
        affected = {u["ref"] for u in self.updates}
        porcelain = self.git.run("worktree", "list", "--porcelain", "-z")
        location = None
        for line in porcelain.split("\0"):
            if line.startswith("worktree "):
                location = Path(line[9:]).resolve()
            elif line.startswith("branch ") and line[7:] in affected:
                if location != self.git.root.resolve():
                    raise Error(f"{line[7:]} is checked out in another worktree: {location}")
        if dry_run:
            return
        original = self.git.branch()
        state = {
            "id": self.id, "label": self.label,
            "original": original, "original_oid": self.git.oid("HEAD"),
            "actions": self.actions, "updates": self.updates, "temps": self.temps,
            "expected": self.expected, "index": 0, "active": False, "published": False,
            "parents": self.parents, "baseline": self.baseline,
        }
        # Retain all input custom tips and every original target in Git itself.
        backups = dict(self.expected)
        backups.update({u["ref"]: u["old"] for u in self.updates if u["old"]})
        for ref, oid in backups.items():
            name = ref.removeprefix("refs/")
            self.git.run("update-ref", f"refs/forkman/backups/{self.id}/{name}", oid)
        try:
            with state_path(self.git).open("x") as handle:
                handle.write(json.dumps(state, indent=2) + "\n")
        except FileExistsError:
            raise Error("Another Forkman operation has started. Use status to inspect it.") from None
        resume(self.git)


def marker(git: Git, name: str) -> bool:
    return (git.git_dir / name).exists()


def empty_resolution(git: Git) -> bool:
    """A resolved index can intentionally have the same tree as HEAD."""
    return not any((git.run("ls-files", "--unmerged"),
                    git.run("diff", "--cached", "--name-only"),
                    git.run("diff", "--name-only")))


def remember_conflict(git: Git, state: dict) -> None:
    """Retain evidence of a stopped commit before a user resolves it manually."""
    if state["index"] >= len(state["actions"]):
        return
    action = state["actions"][state["index"]]
    ref = "REBASE_HEAD" if action["kind"] == "rebase" else "CHERRY_PICK_HEAD"
    if action["kind"] not in ("rebase", "cherry-pick") or not marker(git, ref):
        return
    action["conflict_head"] = git.oid("HEAD")
    action["conflict_commit"] = git.oid(ref)
    save(git, state)


def perform(git: Git, action: dict, active: bool) -> None:
    kind = action["kind"]
    if kind == "checkout":
        if git.exists("refs/heads/" + action["branch"]):
            git.run("switch", action["branch"])
        else:
            git.run("switch", "-c", action["branch"], action["start"])
    elif kind == "rebase":
        if active and (marker(git, "rebase-merge") or marker(git, "rebase-apply")):
            if (marker(git, "REBASE_HEAD") and empty_resolution(git)
                    and action.get("conflict_head") == git.oid("HEAD")
                    and action.get("conflict_commit") == git.oid("REBASE_HEAD")):
                # Git otherwise skips a conflicted commit resolved to no change,
                # even with --empty=keep. Preserve the patch's branch identity.
                git.run("commit", "--allow-empty", "-C", git.oid("REBASE_HEAD"))
            git.run("-c", "core.editor=true", "rebase", "--continue", env={"GIT_EDITOR": "true"})
        elif not active or git.oid("HEAD") == action.get("before_head"):
            git.run("rebase", "--no-update-refs", "--reapply-cherry-picks", "--empty=keep",
                    "--onto", action["onto"], action["base"], env={"GIT_EDITOR": "true"})
        else:
            # Reflog evidence distinguishes a completed rebase from an unrelated
            # commit or a manually aborted rebase after an interrupted command.
            entry = git.run("reflog", "show", "-1", "--format=%gs", "HEAD", check=False)
            if not entry.startswith("rebase (finish):") or not git.ancestor(action["onto"], "HEAD"):
                raise Error("Cannot verify that the interrupted rebase completed. Abort this operation and retry.")
    elif kind == "config":
        if active and git.oid("HEAD") != action.get("before_head"):
            parents = git.run("show", "-s", "--format=%P", "HEAD").split()
            content = git.run("show", "HEAD:forkman.yaml", check=False)
            if (parents != [action.get("before_head")] or content != action["text"].strip()
                    or git.run("show", "-s", "--format=%s", "HEAD") != "forkman: configuration"
                    or git.run("diff", "--name-only", "HEAD^", "HEAD").splitlines() not in ([], ["forkman.yaml"])):
                raise Error("Cannot verify the interrupted configuration commit. Abort this operation.")
            return
        # The upstream may track a symlink at this path; never write through it.
        path = git.root / "forkman.yaml"
        if path.is_symlink():
            path.unlink()
        path.write_text(action["text"].rstrip("\n") + "\n")
        git.run("add", "--", "forkman.yaml")
        git.run("commit", "--allow-empty", "-m", "forkman: configuration")
    elif kind == "merge":
        if active and marker(git, "MERGE_HEAD"):
            git.run("-c", "core.editor=true", "merge", "--continue", env={"GIT_EDITOR": "true"})
        elif not active or git.oid("HEAD") == action.get("before_head"):
            git.run("merge", "--no-ff", "--no-edit", "-m", "forkman: merge " + action["name"], action["ref"])
        else:
            parents = git.run("show", "-s", "--format=%P", "HEAD").split()
            if parents != [action.get("before_head"), git.oid(action["ref"])]:
                raise Error("Merge state disappeared without the expected merge commit. Abort this operation.")
    elif kind == "cherry-pick":
        if active and marker(git, "CHERRY_PICK_HEAD"):
            if empty_resolution(git):
                git.run("commit", "--allow-empty", "-C", git.oid("CHERRY_PICK_HEAD"))
                git.run("cherry-pick", "--quit")
            else:
                git.run("-c", "core.editor=true", "cherry-pick", "--continue", env={"GIT_EDITOR": "true"})
        elif not active or git.oid("HEAD") == action.get("before_head"):
            git.run("cherry-pick", "--allow-empty", "--keep-redundant-commits", action["commit"],
                    env={"GIT_EDITOR": "true"})
        else:
            parents = git.run("show", "-s", "--format=%P", "HEAD").split()
            message = git.run("show", "-s", "--format=%B", "HEAD")
            expected = git.run("show", "-s", "--format=%B", action["commit"])
            manual_resolution = (action.get("conflict_head") == action.get("before_head")
                                 and action.get("conflict_commit") == action["commit"])
            if parents != [action.get("before_head")] or (message != expected and not manual_resolution):
                raise Error("Cannot verify the interrupted cherry-pick. Abort this operation.")
    else:
        raise Error(f"Unknown operation action: {kind}")


def publish(git: Git, state: dict) -> None:
    if not git.clean():
        raise Error("The prepared worktree is not clean. Finish the current resolution before continuing.")
    if state.get("parents") is not None:
        from .model import patches
        tips = {ref[11:]: oid for ref, oid in state["expected"].items()
                if ref.startswith("refs/heads/custom/")}
        for update in state["updates"]:
            if update["ref"].startswith("refs/heads/custom/"):
                name = update["ref"][11:]
                if update["source"] is None:
                    tips.pop(name, None)
                else:
                    tips[name] = git.oid(update["source"])
        actual = patches(git, state["baseline"], tips=tips)
        if {n: p.parent for n, p in actual.items()} != state["parents"]:
            raise Error("The prepared patch ancestry does not match the planned dependencies. Abort and inspect the stack.")
        for patch in actual.values():
            if "forkman.yaml" in git.run("diff", "--name-only", patch.base, patch.tip).splitlines():
                raise Error(f"{patch.name} changes reserved forkman.yaml; configuration belongs to integration.")
    zeros = "0" * len(state["original_oid"])
    lines = ["start"]
    changed = {u["ref"] for u in state["updates"]}
    for ref, old in state["expected"].items():
        if ref not in changed:
            lines.append(f"verify {ref} {old}")
    for update in state["updates"]:
        old = update["old"] or zeros
        if update["source"] is None:
            lines.append(f"delete {update['ref']} {old}")
        else:
            new = git.oid(update["source"])
            update["new"] = new
            lines.append(f"update {update['ref']} {new} {old}")
    lines += ["prepare", "commit", ""]
    # Save the intended final OIDs so recovery can detect a crash after commit.
    state["publishing"] = True
    save(git, state)
    git.run("update-ref", "--stdin", input="\n".join(lines))
    state["published"] = True
    save(git, state)


def published(git: Git, state: dict) -> bool:
    if not state.get("publishing"):
        return False
    return all(
        (not git.exists(u["ref"])) if u["source"] is None else
        (git.exists(u["ref"]) and git.oid(u["ref"]) == u.get("new"))
        for u in state["updates"]
    )


def cleanup(git: Git, state: dict) -> None:
    if state["original"] and git.exists("refs/heads/" + state["original"]):
        git.run("switch", state["original"])
    else:
        git.run("switch", "--detach", state["original_oid"])
    for branch in state["temps"]:
        if git.exists("refs/heads/" + branch):
            git.run("branch", "-D", branch)
    state_path(git).unlink()


def resume(git: Git) -> None:
    state = read(git)
    if state.get("published") or published(git, state):
        cleanup(git, state)
        print("Operation completed.")
        return
    try:
        while state["index"] < len(state["actions"]):
            action = state["actions"][state["index"]]
            if action["kind"] != "checkout" and state.get("work_branch"):
                expected = state["work_branch"]
                if marker(git, "rebase-merge") or marker(git, "rebase-apply"):
                    directory = "rebase-merge" if marker(git, "rebase-merge") else "rebase-apply"
                    head_name = (git.git_dir / directory / "head-name").read_text().strip()
                    if head_name != "refs/heads/" + expected:
                        raise Error("The active Git rebase does not belong to this Forkman operation.")
                elif git.branch() != expected:
                    raise Error(f"Switch back to {expected} before continuing this operation.")
            active = state["active"]
            if not active:
                action["before_head"] = git.oid("HEAD")
            state["active"] = True
            save(git, state)
            perform(git, action, active)
            if action["kind"] == "checkout":
                state["work_branch"] = action["branch"]
            state["index"] += 1
            state["active"] = False
            save(git, state)
        publish(git, state)
        cleanup(git, state)
    except Error as error:
        remember_conflict(git, state)
        raise Error(f"{error}\nOperation paused. Resolve conflicts and git add the resolved files, then run "
                    "'forkman continue'; or run 'forkman abort'. Managed refs have not been published "
                    "unless the operation reached its final publication step.") from None
    print("Operation completed.")


def abort(git: Git) -> None:
    state = read(git)
    if state.get("published") or published(git, state):
        cleanup(git, state)
        print("The refs were already published; cleaned up the completed operation. Backup refs retain the originals.")
        return
    if marker(git, "rebase-merge") or marker(git, "rebase-apply"):
        directory = "rebase-merge" if marker(git, "rebase-merge") else "rebase-apply"
        head_name = (git.git_dir / directory / "head-name").read_text().strip()
        if head_name.removeprefix("refs/heads/") not in state["temps"]:
            raise Error("The active rebase does not belong to Forkman. Finish it before aborting Forkman.")
        git.run("rebase", "--abort")
    elif marker(git, "MERGE_HEAD"):
        if git.branch() not in state["temps"]:
            raise Error("The active merge does not belong to Forkman. Finish it before aborting Forkman.")
        git.run("merge", "--abort")
    elif marker(git, "CHERRY_PICK_HEAD"):
        if git.branch() not in state["temps"]:
            raise Error("The active cherry-pick does not belong to Forkman. Finish it before aborting Forkman.")
        git.run("cherry-pick", "--abort")
    # Discard only tracked changes in Forkman's temporary checkout.
    if git.branch() in state["temps"]:
        git.run("reset", "--hard", "HEAD")
    cleanup(git, state)
    print("Operation aborted. Managed branches are unchanged.")
