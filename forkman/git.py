"""Small, non-interactive helpers for inspecting a Git repository."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Mapping


class Error(Exception):
    """An error returned by Git or by this repository helper."""


class Git:
    """Run Git commands in a repository and expose common read operations."""

    def __init__(self, cwd: str | os.PathLike[str] | None = None) -> None:
        self._resolved_oids: set[str] = set()
        self._ancestor_cache: dict[tuple[str, str], bool] = {}
        start = Path(cwd).expanduser() if cwd is not None else None
        self._cwd = start
        self.root = Path(self.run("rev-parse", "--show-toplevel")).resolve()
        self.git_dir = Path(self.run("rev-parse", "--absolute-git-dir")).resolve()
        # Resolve all later relative commands from the repository root, even
        # when construction began in a subdirectory.
        self._cwd = self.root

    def _execute(
        self,
        args: tuple[object, ...],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", *(os.fspath(arg) for arg in args)]
        command_env = os.environ.copy()
        command_env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_EDITOR": "true",
                "GIT_SEQUENCE_EDITOR": "true",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            }
        )
        if env:
            command_env.update(env)
        kwargs: dict[str, object] = {
            "cwd": self._cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": command_env,
        }
        if input is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input
        try:
            return subprocess.run(command, **kwargs)  # type: ignore[arg-type]
        except OSError as exc:
            raise Error(f"could not run {shlex.join(command)}: {exc}") from exc

    @staticmethod
    def _failure(args: tuple[object, ...], result: subprocess.CompletedProcess[str]) -> Error:
        command = shlex.join(["git", *(os.fspath(arg) for arg in args)])
        detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if detail:
            return Error(f"{command} failed: {detail}")
        return Error(f"{command} failed with exit status {result.returncode}")

    def run(
        self,
        *args: object,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Run Git and return stripped stdout.

        Git never inherits an interactive stdin, and editor or prompt requests
        are disabled by default. Callers can pass input and override the
        command environment when a particular operation needs it.
        """
        if not args:
            raise ValueError("run() requires a Git subcommand")
        result = self._execute(args, input=input, env=env)
        if check and result.returncode:
            raise self._failure(args, result)
        return result.stdout.strip()

    def oid(self, ref: str) -> str:
        """Resolve a revision to the object id of its commit."""
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
            normalized = ref.lower()
            if normalized in self._resolved_oids:
                return normalized
        resolved = self.run(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        )
        self._resolved_oids.add(resolved)
        return resolved

    def exists(self, ref: str) -> bool:
        """Return whether *ref* resolves to a commit."""
        try:
            self.oid(ref)
        except Error:
            return False
        return True

    def branch(self) -> str:
        """Return the current local branch, or an empty string when detached."""
        result = self._execute(("symbolic-ref", "--quiet", "--short", "HEAD"))
        if result.returncode:
            return ""
        return result.stdout.strip()

    def branches(self, prefix: str = "") -> dict[str, str]:
        """Return local branch names and their commit ids."""
        output = self.run(
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            "refs/heads",
        )
        found: dict[str, str] = {}
        for line in output.splitlines():
            ref, separator, oid = line.partition("\t")
            name = ref.removeprefix("refs/heads/")
            if separator and name.startswith(prefix):
                found[name] = oid
        return found

    def clean(self) -> bool:
        """Return whether the worktree and index have no visible changes."""
        return not self.run("status", "--porcelain", "--untracked-files=all")

    def ancestor(self, a: str, b: str) -> bool:
        """Return whether commit *a* is an ancestor of commit *b*."""
        a_oid = self.oid(a)
        b_oid = self.oid(b)
        key = (a_oid, b_oid)
        if key in self._ancestor_cache:
            return self._ancestor_cache[key]
        if a_oid == b_oid:
            self._ancestor_cache[key] = True
            return True
        args: tuple[object, ...] = ("merge-base", "--is-ancestor", a_oid, b_oid)
        result = self._execute(args)
        if result.returncode == 0:
            self._ancestor_cache[key] = True
            return True
        if result.returncode == 1:
            self._ancestor_cache[key] = False
            return False
        raise self._failure(args, result)

    def worktrees(self) -> list[dict[str, object]]:
        """Return the worktrees listed by ``git worktree list --porcelain``."""
        output = self.run("worktree", "list", "--porcelain")
        entries: list[dict[str, object]] = []
        entry: dict[str, object] = {}

        def finish() -> None:
            if entry:
                entries.append(dict(entry))
                entry.clear()

        for line in output.splitlines():
            if not line:
                finish()
                continue
            key, separator, value = line.partition(" ")
            if key == "worktree" and separator:
                entry["path"] = Path(value)
            elif key == "HEAD" and separator:
                entry["head"] = value
            elif key == "branch" and separator:
                entry["branch"] = value.removeprefix("refs/heads/")
            elif key in {"detached", "bare"}:
                entry[key] = True
            elif key in {"locked", "prunable"}:
                entry[key] = value if separator else True
        finish()
        return entries
