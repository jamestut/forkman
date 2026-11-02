"""Configuration and Git-derived patch-stack model."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Mapping

import yaml

from .git import Error, Git


def _check_ref(ref: str) -> bool:
    """Use Git's own ref-name rules, independent of a repository cwd."""
    try:
        result = subprocess.run(
            ["git", "check-ref-format", ref],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise Error(f"could not validate Git ref {ref!r}: {exc}") from exc
    return result.returncode == 0


def _validate_config_ref(value: str, *, kind: str) -> None:
    if not value or value.strip() != value:
        raise Error(f"{kind} must be a non-empty Git ref name")
    if kind == "integration branch":
        candidate = f"refs/heads/{value}"
    elif kind == "remote":
        candidate = f"refs/remotes/{value}/forkman-validation"
    else:
        # Upstream branch names are checked below with the remote included by
        # Config.parse, so this path is not used directly.
        candidate = value
    if not _check_ref(candidate):
        raise Error(f"invalid {kind} {value!r} (Git rejected {candidate!r})")


@dataclass(frozen=True)
class Config:
    upstream_remote: str = "upstream"
    upstream_branch: str = "main"
    fork_remote: str = "origin"
    integration: str = "main"

    @property
    def upstream_ref(self) -> str:
        return f"refs/remotes/{self.upstream_remote}/{self.upstream_branch}"

    def to_yaml(self) -> str:
        """Serialize the repository settings in the documented YAML shape."""
        value = {
            "upstream": {
                "remote": self.upstream_remote,
                "branch": self.upstream_branch,
            },
            "fork": {"remote": self.fork_remote},
            "integration": {"branch": self.integration},
        }
        return yaml.safe_dump(value, sort_keys=False, default_flow_style=False)

    @classmethod
    def parse(cls, text: str) -> Config:
        """Parse and validate a ``forkman.yaml`` document."""
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise Error(f"invalid forkman.yaml: {exc}") from exc
        if not isinstance(raw, dict):
            raise Error("invalid forkman.yaml: expected a YAML mapping")

        allowed_top = {"upstream", "fork", "integration"}
        unknown_top = set(raw) - allowed_top
        if unknown_top:
            keys = ", ".join(sorted(map(str, unknown_top)))
            raise Error(f"invalid forkman.yaml: unknown top-level key(s): {keys}")

        def section(name: str, allowed: set[str]) -> dict[str, object]:
            value = raw.get(name, {})
            if not isinstance(value, dict):
                raise Error(f"invalid forkman.yaml: {name} must be a mapping")
            unknown = set(value) - allowed
            if unknown:
                keys = ", ".join(sorted(map(str, unknown)))
                raise Error(f"invalid forkman.yaml: unknown {name} key(s): {keys}")
            return value

        upstream = section("upstream", {"remote", "branch"})
        fork = section("fork", {"remote"})
        integration = section("integration", {"branch"})

        missing = object()

        def string(value: object, default: str, field: str) -> str:
            if value is missing:
                return default
            if not isinstance(value, str):
                raise Error(f"invalid forkman.yaml: {field} must be a string")
            if not value or value.strip() != value:
                raise Error(f"invalid forkman.yaml: {field} must be non-empty")
            return value

        config = cls(
            upstream_remote=string(upstream.get("remote", missing), "upstream", "upstream.remote"),
            upstream_branch=string(upstream.get("branch", missing), "main", "upstream.branch"),
            fork_remote=string(fork.get("remote", missing), "origin", "fork.remote"),
            integration=string(integration.get("branch", missing), "main", "integration.branch"),
        )
        _validate_config_ref(config.upstream_remote, kind="remote")
        _validate_config_ref(config.fork_remote, kind="remote")
        if not _check_ref(f"refs/remotes/{config.upstream_remote}/{config.upstream_branch}"):
            raise Error(f"invalid upstream branch {config.upstream_branch!r}")
        _validate_config_ref(config.integration, kind="integration branch")
        return config


def _eligible_integration_branch(name: str) -> bool:
    lowered = name.lower()
    return not any(
        lowered == prefix.rstrip("/") or lowered.startswith(prefix)
        for prefix in ("custom/", "dev/", "release/", "releases/")
    )


def _read_config(git: Git, ref: str) -> tuple[Config, str] | None:
    try:
        text = git.run("show", f"{ref}:forkman.yaml")
    except Error:
        return None
    return Config.parse(text), text


def _remote_branches(git: Git, remote: str) -> dict[str, str]:
    output = git.run(
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        f"refs/remotes/{remote}",
    )
    result: dict[str, str] = {}
    prefix = f"refs/remotes/{remote}/"
    for line in output.splitlines():
        name, separator, oid = line.partition("\t")
        if separator and name.startswith(prefix) and not name.endswith("/HEAD"):
            result[name[len(prefix) :]] = oid
    return result


def load_config(git: Git, integration: str | None = None) -> tuple[Config, str]:
    """Find the canonical integration config in local or tracking branches.

    Eligible local branches are inspected in a stable order. If they contain
    different repository settings, selection is ambiguous and fails. Custom,
    development, and release branches are skipped because their config copies
    can legitimately lag behind integration.
    """
    local = git.branches()
    current = git.branch()
    ordered: list[str] = []
    if integration and integration in local and _eligible_integration_branch(integration):
        ordered.append(integration)
    if current in local and _eligible_integration_branch(current) and current not in ordered:
        ordered.append(current)
    ordered.extend(
        name
        for name in sorted(local)
        if name not in ordered and _eligible_integration_branch(name)
    )

    discovered: list[tuple[str, Config, str]] = []
    for name in ordered:
        candidate = _read_config(git, f"refs/heads/{name}")
        if candidate is not None:
            config, text = candidate
            discovered.append((name, config, text))

    if discovered:
        first_name, config, text = discovered[0]
        mismatches = [
            (name, other)
            for name, other, _ in discovered[1:]
            if other != config
        ]
        if mismatches:
            names = ", ".join(
                [first_name, *(name for name, _ in mismatches)]
            )
            raise Error(
                "ambiguous forkman.yaml settings on local branches "
                f"({names}); inspect their upstream, fork, and integration values"
            )
        if integration is not None and config.integration != integration:
            raise Error(
                f"requested integration branch {integration!r}, but forkman.yaml "
                f"configures {config.integration!r}"
            )
        canonical = _read_config(git, f"refs/heads/{config.integration}")
        if config.integration in local and canonical is None:
            raise Error(
                f"integration branch {config.integration!r} has no forkman.yaml"
            )
        if canonical is None:
            canonical = _read_config(
                git,
                f"refs/remotes/{config.fork_remote}/{config.integration}",
            )
        if canonical is not None:
            canonical_config, canonical_text = canonical
            if canonical_config != config:
                raise Error(
                    f"forkman.yaml on integration branch {config.integration!r} "
                    "does not match the discovered repository settings"
                )
            return canonical_config, canonical_text
        return config, text

    # A fresh clone may have only remote-tracking refs. Try the explicit or
    # current integration name first, then all branches of the default fork
    # remote in sorted order.
    wanted = integration or (
        current if current and _eligible_integration_branch(current) else "main"
    )
    remote_names = _remote_branches(git, "origin")
    eligible_remote_names = {
        name for name in remote_names if _eligible_integration_branch(name)
    }
    remote_order = [wanted] if wanted in eligible_remote_names else []
    remote_order.extend(
        name for name in sorted(eligible_remote_names) if name not in remote_order
    )
    remote_configs: list[tuple[str, Config, str]] = []
    for name in remote_order:
        candidate = _read_config(git, f"refs/remotes/origin/{name}")
        if candidate is not None:
            config, text = candidate
            remote_configs.append((name, config, text))
    if not remote_configs:
        target = integration or "main"
        raise Error(
            f"could not find forkman.yaml on an eligible local branch or "
            f"origin/* (tried integration {target!r})"
        )
    _, config, text = remote_configs[0]
    if integration is not None and config.integration != integration:
        raise Error(
            f"requested integration branch {integration!r}, but forkman.yaml "
            f"configures {config.integration!r}"
        )
    mismatches = [
        (name, other)
        for name, other, _ in remote_configs[1:]
        if other != config
    ]
    if mismatches:
        names = ", ".join([remote_configs[0][0], *(n for n, _ in mismatches)])
        raise Error(
            "ambiguous forkman.yaml settings on origin tracking branches "
            f"({names}); inspect their upstream, fork, and integration values"
        )
    canonical = _read_config(git, f"refs/remotes/origin/{config.integration}")
    if canonical is not None:
        canonical_config, canonical_text = canonical
        if canonical_config != config:
            raise Error(
                f"forkman.yaml on origin/{config.integration} does not match "
                "the discovered repository settings"
            )
        return canonical_config, canonical_text
    return config, text


@dataclass(frozen=True)
class Patch:
    name: str
    tip: str
    parent: str | None
    base: str


def patches(
    git: Git,
    baseline: str,
    tips: Mapping[str, str] | None = None,
) -> dict[str, Patch]:
    """Infer each custom patch's direct parent and fork-point commit."""
    raw_tips = dict(tips if tips is not None else git.branches(prefix="custom/"))
    resolved: dict[str, str] = {}
    for name in sorted(raw_tips):
        try:
            resolved[name] = git.oid(raw_tips[name])
        except Error as exc:
            raise Error(f"custom patch {name!r} has an invalid tip: {exc}") from exc

    by_oid: dict[str, list[str]] = {}
    for name, tip in resolved.items():
        by_oid.setdefault(tip, []).append(name)
    duplicates = [names for names in by_oid.values() if len(names) > 1]
    if duplicates:
        names = "; ".join(", ".join(group) for group in duplicates)
        raise Error(f"custom branches have equal tips and are ambiguous: {names}")

    baseline_oid = git.oid(baseline)
    candidates: dict[str, list[str]] = {name: [] for name in resolved}
    for child, child_tip in resolved.items():
        for parent, parent_tip in resolved.items():
            if parent != child and git.ancestor(parent_tip, child_tip):
                candidates[child].append(parent)

    result: dict[str, Patch] = {}
    for name in sorted(resolved):
        tip = resolved[name]
        ancestors = candidates[name]
        maximal = [
            candidate
            for candidate in ancestors
            if not any(
                candidate != other
                and git.ancestor(resolved[candidate], resolved[other])
                for other in ancestors
            )
        ]
        if len(maximal) > 1:
            raise Error(
                f"custom patch {name!r} has multiple nearest custom parents: "
                + ", ".join(sorted(maximal))
            )

        if maximal:
            parent_name = maximal[0]
            base = resolved[parent_name]
        else:
            parent_name = None
            try:
                merge_bases = git.run("merge-base", "--all", baseline_oid, tip).splitlines()
            except Error as exc:
                raise Error(
                    f"custom patch {name!r} has no merge base with baseline "
                    f"{baseline!r}: {exc}"
                ) from exc
            if len(merge_bases) != 1:
                if not merge_bases:
                    raise Error(
                        f"custom patch {name!r} has no valid root history from "
                        f"baseline {baseline!r}"
                    )
                raise Error(
                    f"custom patch {name!r} has multiple merge bases with "
                    f"baseline {baseline!r}"
                )
            base = git.oid(merge_bases[0])

        try:
            commit_count = int(git.run("rev-list", "--count", f"{base}..{tip}"))
        except (Error, ValueError) as exc:
            raise Error(f"could not inspect commit range for custom patch {name!r}: {exc}") from exc
        if commit_count == 0:
            raise Error(f"custom patch {name!r} contains no commits after its base")

        merge_commits = git.run("rev-list", "--merges", f"{base}..{tip}")
        if merge_commits:
            first = merge_commits.splitlines()[0]
            raise Error(
                f"custom patch {name!r} is not linear: merge commit {first} "
                "appears after its base"
            )
        if git.run("diff", "--name-only", base, tip, "--", "forkman.yaml"):
            raise Error(
                f"custom patch {name!r} changes reserved forkman.yaml; "
                "configuration belongs to integration"
            )
        result[name] = Patch(name=name, tip=tip, parent=parent_name, base=base)
    return result


def _merge_identity(subject: str) -> str | None:
    match = re.fullmatch(r"forkman: merge (custom/[^\s]+)", subject)
    name = match.group(1) if match else None
    if name is None:
        match = re.fullmatch(
            r"Merge branch ['\"](custom/[^'\"]+)['\"](?: into [^\r\n]+)?",
            subject,
        )
        name = match.group(1) if match else None
    if name is not None and _check_ref(f"refs/heads/{name}"):
        return name
    return None


def _recorded_patch_tips(git: Git, name: str) -> set[str]:
    """Return tips Git can prove were held by a named patch branch."""
    result: set[str] = set()
    ref = f"refs/heads/{name}"
    if git.exists(ref):
        result.add(git.oid(ref))
    # A branch reflog records the old tips when a manual rebase or ref update
    # rewrites a patch after it has been merged into integration.
    reflog = git.run("reflog", "show", "--format=%H%x09%gs", ref, check=False)
    for line in reflog.splitlines():
        oid, separator, action = line.partition("\t")
        if separator and not action.startswith("branch: Created from"):
            result.add(oid)
    # Forkman's own rewrites preserve prior branch tips under backup refs.
    backups = git.run(
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        "refs/forkman/backups",
    )
    suffix = "/" + name
    for line in backups.splitlines():
        refname, separator, oid = line.partition("\t")
        if separator and refname.endswith(suffix):
            result.add(oid)
    return result


def _merge_patch_name(git: Git, merge: str, second_parent: str) -> str:
    subject = git.run("log", "-1", "--format=%s", merge)
    message_name = _merge_identity(subject)
    custom_tips = git.branches(prefix="custom/")
    exact_current = sorted(
        name for name, tip in custom_tips.items() if tip == second_parent
    )
    if len(exact_current) > 1:
        raise Error(
            f"integration merge {merge} has a second parent shared by custom "
            f"branches: {', '.join(exact_current)}"
        )
    if exact_current:
        exact_name = exact_current[0]
        if message_name and message_name != exact_name:
            raise Error(
                f"integration merge {merge} identifies {message_name!r}, but "
                f"its second parent is exactly the tip of {exact_name!r}"
            )
        return exact_name

    known_tips = {
        name: _recorded_patch_tips(git, name)
        for name in sorted(set(custom_tips) | ({message_name} if message_name else set()))
    }
    matches = sorted(name for name, tips in known_tips.items() if second_parent in tips)
    if message_name:
        if message_name not in known_tips or second_parent not in known_tips[message_name]:
            raise Error(
                f"integration merge {merge} claims to merge {message_name!r}, "
                "but its second parent was never recorded as a tip of that "
                "patch branch; the merge message may be spoofed"
            )
        # A parent's old tip can also appear in a child's recorded history.
        # The message disambiguates that historical ancestry once the named
        # branch itself has a ref-log or backup record for the exact tip.
        return message_name
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise Error(
            f"integration merge {merge} has a second parent recorded for "
            f"multiple custom branches: {', '.join(matches)}"
        )

    raise Error(
        f"cannot identify patch for integration merge {merge}; its second "
        "parent is not a recorded custom branch tip and its message has no "
        "Forkman identity"
    )


def integration_order(
    git: Git,
    config: Config,
    config_text: str | None = None,
) -> list[str]:
    """Validate integration history and return its patch merges in order."""
    integration_ref = f"refs/heads/{config.integration}"
    if config.integration not in git.branches():
        raise Error(f"integration branch {config.integration!r} does not exist locally")
    tip = git.oid(integration_ref)
    try:
        upstream = git.oid(config.upstream_ref)
    except Error as exc:
        raise Error(
            f"configured upstream {config.upstream_ref!r} is unavailable: {exc}"
        ) from exc

    commits = git.run("rev-list", "--first-parent", tip).splitlines()
    reverse_order: list[str] = []
    seen_names: set[str] = set()
    config_commit: str | None = None
    for commit in commits:
        fields = git.run("rev-list", "--parents", "-n", "1", commit).split()
        parents = fields[1:]
        if len(parents) == 2:
            name = _merge_patch_name(git, commit, parents[1])
            if name in seen_names:
                raise Error(
                    f"integration history merges custom patch {name!r} more than once"
                )
            seen_names.add(name)
            reverse_order.append(name)
            continue
        if len(parents) > 2:
            raise Error(
                f"integration commit {commit} is an unsupported octopus merge"
            )
        if len(parents) == 0:
            raise Error(
                "integration history reached a root commit before finding its "
                "single-file forkman.yaml config commit"
            )

        changed = git.run(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            f"{commit}^",
            commit,
        ).splitlines()
        if changed != ["forkman.yaml"]:
            raise Error(
                f"unexpected single-parent integration commit {commit}; after "
                "the config commit, integration may contain only Forkman patch "
                "merges"
            )
        config_commit = commit
        parent = parents[0]
        if not git.ancestor(parent, upstream):
            raise Error(
                f"forkman.yaml config commit {commit} is not based on the "
                f"configured upstream {config.upstream_ref}"
            )
        try:
            committed_text = git.run("show", f"{commit}:forkman.yaml")
            committed_config = Config.parse(committed_text)
        except Error as exc:
            raise Error(f"invalid forkman.yaml in config commit {commit}: {exc}") from exc
        if committed_config != config:
            raise Error(
                f"forkman.yaml in config commit {commit} does not match the "
                "active repository configuration"
            )
        if config_text is not None:
            supplied = Config.parse(config_text)
            if supplied != committed_config:
                raise Error(
                    f"forkman.yaml in config commit {commit} does not match "
                    "the loaded configuration text"
                )
        break

    if config_commit is None:
        raise Error(
            f"integration history on {config.integration!r} has no single-file "
            "forkman.yaml config commit"
        )
    return list(reversed(reverse_order))
