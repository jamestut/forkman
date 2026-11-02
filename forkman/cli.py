"""Command-line workflow and operation planning."""
from __future__ import annotations

import argparse
from dataclasses import replace
import sys

from .git import Error, Git
from .model import Config, integration_order, load_config, patches
from .engine import Plan, abort, check_idle, read, resume, state_path


def branch_name(git: Git, name: str, prefix: str | None = None) -> str:
    if prefix and not name.startswith(prefix):
        raise Error(f"Branch must start with {prefix}: {name}")
    if name.startswith("-") or git.run("check-ref-format", "--branch", name, check=False) != name:
        raise Error(f"Invalid branch name: {name}")
    return name


def descendants(items: dict, root: str) -> set[str]:
    found = {root}
    while True:
        more = {n for n, p in items.items() if p.parent in found}
        if more <= found:
            return found
        found |= more


def ordered(items: dict, previous: list[str], parents: dict | None = None) -> list[str]:
    parents = parents or {n: p.parent for n, p in items.items()}
    result, active = [], set()
    def visit(name):
        if name in active:
            raise Error("Patch dependencies contain a cycle.")
        if name in result:
            return
        if name not in items:
            raise Error(f"Integration references missing patch {name}.")
        active.add(name)
        if parents.get(name):
            visit(parents[name])
        active.remove(name)
        result.append(name)
    for name in previous + sorted(set(items) - set(previous)):
        visit(name)
    return result


def validate_order(order: list[str], items: dict, parents: dict | None = None) -> None:
    parents = parents or {n: p.parent for n, p in items.items()}
    seen = set()
    for name in order:
        if name not in items:
            raise Error(f"Integration references missing patch {name}.")
        parent = parents.get(name)
        if parent and parent not in seen:
            raise Error(f"Patch {name} must appear after its parent {parent}.")
        seen.add(name)


def context(git: Git, args):
    check_idle(git)
    config, text = load_config(git)
    if getattr(args, "fetch", False):
        if getattr(args, "dry_run", False):
            raise Error("--fetch changes remote refs; run fetch separately before using --dry-run.")
        git.run("fetch", config.upstream_remote)
    items = patches(git, config.upstream_ref)
    previous = integration_order(git, config)
    validate_order(previous, items)
    return config, text, items, previous


def plan_for(git: Git, label: str, config: Config, items: dict) -> Plan:
    plan = Plan(git, label)
    plan.parents = {n: p.parent for n, p in items.items()}
    plan.baseline = git.oid(config.upstream_ref)
    plan.expected = {"refs/heads/" + n: p.tip for n, p in items.items()}
    plan.expected[config.upstream_ref] = git.oid(config.upstream_ref)
    if git.exists("refs/heads/" + config.integration):
        plan.expected["refs/heads/" + config.integration] = git.oid("refs/heads/" + config.integration)
    return plan


def rebuild(plan: Plan, config: Config, text: str, order: list[str], refs: dict[str, str],
            destination: str | None = None, baseline: str | None = None) -> None:
    plan.temp(baseline or plan.git.oid(config.upstream_ref))
    plan.action("config", text=text)
    for name in order:
        plan.action("merge", name=name, ref=refs[name])
    plan.update(destination or config.integration, plan.temps[-1])


def do_init(git: Git, args) -> None:
    check_idle(git)
    config = Config(upstream_remote=args.upstream_remote, upstream_branch=args.upstream_branch,
                    fork_remote=args.fork_remote, integration=args.integration)
    # Round-trip through the same validation used for loaded configuration.
    config = Config.parse(config.to_yaml())
    if not git.exists(config.upstream_ref):
        raise Error(f"Missing {config.upstream_ref}. Fetch the upstream remote first.")
    remotes = git.run("remote").splitlines()
    if config.fork_remote not in remotes or config.upstream_remote not in remotes:
        raise Error("Both configured remotes must exist. Use git remote add first.")
    branch_name(git, config.integration)
    if config.integration.startswith(("custom/", "dev/", "forkman-tmp/")):
        raise Error("Integration cannot use the custom/, dev/, or forkman-tmp/ prefixes.")
    items = patches(git, config.upstream_ref)
    previous = []
    if git.exists("refs/heads/" + config.integration):
        config_blob = git.run("show", f"refs/heads/{config.integration}:forkman.yaml", check=False)
        if config_blob:
            previous = integration_order(git, Config.parse(config_blob))
        else:
            # Adopt an existing no-ff-only integration history on the upstream baseline.
            base = git.run("merge-base", config.upstream_ref, "refs/heads/" + config.integration)
            commits = git.run("rev-list", "--first-parent", "--reverse", f"{base}..refs/heads/{config.integration}").splitlines()
            for commit in commits:
                parents = git.run("show", "-s", "--format=%P", commit).split()
                if len(parents) != 2:
                    raise Error("Existing integration contains a direct commit. Init accepts only upstream and custom patch merges.")
                matches = [n for n, p in items.items() if p.tip == parents[1]]
                if len(matches) != 1:
                    raise Error(f"Cannot identify the custom branch merged by {commit}.")
                if matches[0] in previous:
                    raise Error("Existing integration merges the same patch more than once.")
                previous.append(matches[0])
    order = ordered(items, previous)
    plan = plan_for(git, "Initialize integration", config, items)
    rebuild(plan, config, config.to_yaml(), order, {n: p.tip for n, p in items.items()})
    plan.execute(args.dry_run)


def do_integrate(git: Git, args) -> None:
    config, text, items, previous = context(git, args)
    plan = plan_for(git, "Update patches and integration" if args.command == "update" else "Rebuild integration", config, items)
    order = ordered(items, previous)
    refs = {n: p.tip for n, p in items.items()}
    if args.command == "update":
        for name in order:
            patch = items[name]
            onto = refs[patch.parent] if patch.parent else git.oid(config.upstream_ref)
            temp = plan.temp(patch.tip)
            plan.action("rebase", name=name, onto_name=patch.parent or "upstream", onto=onto, base=patch.base)
            refs[name] = temp
            plan.update(name, temp)
    rebuild(plan, config, text, order, refs)
    plan.execute(args.dry_run)


def do_delete(git: Git, args) -> None:
    config, text, items, previous = context(git, args)
    if args.branch not in items:
        raise Error(f"Unknown patch: {args.branch}")
    children = descendants(items, args.branch) - {args.branch}
    if children:
        raise Error("Cannot delete a parent patch. Reparent or delete its dependents first: " + ", ".join(sorted(children)))
    plan = plan_for(git, "Delete " + args.branch, config, items)
    remaining = {n: p for n, p in items.items() if n != args.branch}
    plan.parents = {n: p.parent for n, p in remaining.items()}
    order = ordered(remaining, [n for n in previous if n != args.branch])
    rebuild(plan, config, text, order, {n: p.tip for n, p in remaining.items()})
    plan.update(args.branch, None)
    plan.execute(args.dry_run)


def target_parent(target: str, items: dict) -> str | None:
    if target == "upstream":
        return None
    if target not in items:
        raise Error(f"Unknown parent patch: {target}; use upstream or an existing custom/* branch.")
    return target


def rebase_affected(plan: Plan, config: Config, items: dict, order: list[str], affected: set[str], parents: dict,
                    initial_refs: dict | None = None) -> dict:
    refs = {n: p.tip for n, p in items.items()}
    refs.update(initial_refs or {})
    for name in order:
        if name not in affected or name in (initial_refs or {}):
            continue
        patch = items[name]
        parent = parents[name]
        onto = refs[parent] if parent else plan.git.oid(config.upstream_ref)
        temp = plan.temp(patch.tip)
        plan.action("rebase", name=name, onto_name=parent or "upstream", onto=onto, base=patch.base)
        refs[name] = temp
        plan.update(name, temp)
    return refs


def do_rebase(git: Git, args) -> None:
    config, text, items, previous = context(git, args)
    if args.branch.startswith("dev/"):
        return rebase_dev(git, args, config, items)
    if args.from_commit:
        raise Error("--from applies to dev/* branches; custom patch boundaries come from ancestry.")
    if args.branch not in items:
        raise Error(f"Unknown custom patch: {args.branch}")
    parent = target_parent(args.onto, items)
    affected = descendants(items, args.branch)
    if parent in affected:
        raise Error("Cannot rebase a patch onto itself or one of its descendants.")
    parents = {n: p.parent for n, p in items.items()}
    parents[args.branch] = parent
    order = ordered(items, previous, parents)
    plan = plan_for(git, "Reparent " + args.branch, config, items)
    plan.parents = parents
    refs = rebase_affected(plan, config, items, order, affected, parents)
    rebuild(plan, config, text, order, refs)
    plan.execute(args.dry_run)


def development_boundary(git: Git, dev: str, config: Config, items: dict, explicit: str | None) -> str:
    if explicit:
        boundary = git.oid(explicit)
        if not git.ancestor(boundary, dev):
            raise Error("--from must be an ancestor of the development branch.")
    else:
        candidates = set()
        for ref in ["refs/heads/" + config.integration, config.upstream_ref] + ["refs/heads/" + n for n in items]:
            if not git.exists(ref):
                continue
            if git.ancestor(ref, dev):
                candidates.add(git.oid(ref))
            old = git.run("merge-base", "--fork-point", ref, dev, check=False)
            if old:
                candidates.add(old)
        maximal = [c for c in candidates if not any(c != other and git.ancestor(c, other) for other in candidates)]
        if len(maximal) != 1:
            raise Error("Cannot establish a unique development fork point. Supply --from COMMIT (exclusive).")
        boundary = maximal[0]
    if git.run("rev-list", "--merges", f"{boundary}..{dev}"):
        raise Error("Development work contains merge commits. Supply --from after the integration fork point; patch commits must be linear.")
    if boundary == git.oid(dev):
        raise Error("The development branch has no commits after its fork point.")
    return boundary


def rebase_dev(git: Git, args, config: Config, items: dict) -> None:
    branch_name(git, args.branch, "dev/")
    dev = "refs/heads/" + args.branch
    if not git.exists(dev):
        raise Error(f"Unknown development branch: {args.branch}")
    parent = target_parent(args.onto, items)
    base = development_boundary(git, dev, config, items, args.from_commit)
    plan = plan_for(git, "Rebase development branch " + args.branch, config, items)
    plan.expected[dev] = git.oid(dev)
    temp = plan.temp(git.oid(dev))
    plan.action("rebase", name=args.branch, onto_name=parent or "upstream",
                onto=items[parent].tip if parent else git.oid(config.upstream_ref), base=base)
    plan.update(args.branch, temp)
    plan.execute(args.dry_run)


def do_promote(git: Git, args) -> None:
    config, text, items, previous = context(git, args)
    branch_name(git, args.branch, "dev/")
    dev = "refs/heads/" + args.branch
    if not git.exists(dev):
        raise Error(f"Unknown development branch: {args.branch}")
    name = args.new or args.update
    branch_name(git, name, "custom/")
    if args.new and name in items:
        raise Error(f"Patch already exists: {name}")
    if args.update and name not in items:
        raise Error(f"Unknown patch to update: {name}")
    if args.update and (args.before or args.after):
        raise Error("--before and --after apply only to new patches.")
    if args.update and args.onto:
        raise Error("Use rebase to change an existing patch's parent; --onto applies to --new.")
    boundary = development_boundary(git, dev, config, items, args.from_commit)
    parent = target_parent(args.onto or "upstream", items) if args.new else items[name].parent
    plan = plan_for(git, "Promote " + args.branch + " to " + name, config, items)
    plan.expected[dev] = git.oid(dev)
    # Updating an existing patch keeps its commits and adds only the development range.
    onto = items[name].tip if args.update else (items[parent].tip if parent else git.oid(config.upstream_ref))
    temp = plan.temp(git.oid(dev))
    plan.action("rebase", name=args.branch, onto_name=name if args.update else (parent or "upstream"), onto=onto, base=boundary)
    plan.update(name, temp)
    if args.new:
        from .model import Patch
        items = dict(items)
        items[name] = Patch(name=name, tip=git.oid(dev), parent=parent,
                            base=items[parent].tip if parent else git.oid(config.upstream_ref))
    parents = {n: p.parent for n, p in items.items()}
    plan.parents = parents
    order = ordered(items, previous)
    if args.before or args.after:
        anchor = args.before or args.after
        if anchor not in items or anchor == name:
            raise Error("Ordering anchor must be another existing custom branch.")
        order.remove(name)
        index = order.index(anchor) + (1 if args.after else 0)
        order.insert(index, name)
        validate_order(order, items)
    affected = descendants(items, name) - {name}
    refs = rebase_affected(plan, config, items, order, affected, parents, {name: temp})
    rebuild(plan, config, text, order, refs)
    plan.execute(args.dry_run)


def do_release(git: Git, args) -> None:
    config, text, items, previous = context(git, args)
    # Releases reproduce exactly the integrated set and order, not unregistered branches.
    name = args.name if args.name.startswith("release/") else "release/" + args.name
    branch_name(git, name, "release/")
    if git.exists("refs/heads/" + name):
        raise Error(f"Release branch already exists: {name}")
    excluded = set(args.exclude)
    unknown = excluded - set(previous)
    if unknown:
        raise Error("Excluded patches are not integrated: " + ", ".join(sorted(unknown)))
    closure = set(excluded)
    for patch in excluded:
        closure |= descendants(items, patch) & set(previous)
    if closure != excluded and not args.exclude_dependents:
        raise Error("Exclusion leaves dependent patches selected: " + ", ".join(sorted(closure - excluded)) + ". Use --exclude-dependents.")
    order = [n for n in previous if n not in closure]
    # Ensure the recorded merge tips are still the current custom tips before replay.
    # Otherwise rebuilding would silently release patches that were never integrated.
    current = "refs/heads/" + config.integration
    for patch in reversed(previous):
        parents = git.run("show", "-s", "--format=%P", current).split()
        if len(parents) != 2 or parents[1] != items[patch].tip:
            raise Error("Integration is stale relative to custom branches. Run integrate before creating a release.")
        current = parents[0]
    upstream_oid = git.oid(config.upstream_ref)
    config_parent = git.oid(current + "^")
    if not args.base and config_parent != upstream_oid:
        raise Error("Integration uses an older upstream baseline. Run update before creating a release.")
    baseline = git.oid(args.base) if args.base else upstream_oid
    commits = []
    for rev in args.commit:
        oid = git.oid(rev)
        if len(git.run("show", "-s", "--format=%P", oid).split()) != 1:
            raise Error("Release adjustments must be ordinary single-parent commits.")
        commits.append(oid)
    plan = plan_for(git, "Create " + name, config, items)
    # A maintained patch may be based on much newer upstream history than the
    # release. Merge only a replay of its own range, never that newer history.
    refs: dict[str, str] = {}
    for patch_name in order:
        patch = items[patch_name]
        temp = plan.temp(patch.tip)
        plan.action("rebase", name=patch_name, onto_name=patch.parent or args.base or "upstream",
                    onto=refs[patch.parent] if patch.parent else baseline, base=patch.base)
        refs[patch_name] = temp
    rebuild(plan, config, text, order, refs, name, baseline=baseline)
    for oid in commits:
        plan.action("cherry-pick", commit=oid)
    plan.execute(args.dry_run)


def do_push(git: Git, args) -> None:
    config, _, items, previous = context(git, args)
    names = args.branches or previous + [config.integration]
    if len(names) != len(set(names)):
        raise Error("Push branch list contains duplicates.")
    commands = ["push", "--atomic"]
    specs = []
    for name in names:
        branch_name(git, name)
        ref = "refs/heads/" + name
        if not git.exists(ref):
            raise Error(f"Unknown local branch: {name}")
        tracked = f"refs/remotes/{config.fork_remote}/{name}"
        expected = git.oid(tracked) if git.exists(tracked) else ""
        commands.append(f"--force-with-lease={ref}:{expected}")
        specs.append(f"{git.oid(ref)}:{ref}")
    if args.dry_run:
        commands.append("--dry-run")
    print(git.run(*commands, config.fork_remote, *specs))
    print("Push completed." if not args.dry_run else "Push preview completed.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="forkman", description="Maintain ancestry-based Git patch branches.")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Add configuration and initialize integration")
    init.add_argument("--upstream-remote", default="upstream")
    init.add_argument("--upstream-branch", default="main")
    init.add_argument("--fork-remote", default="origin")
    init.add_argument("--integration", default="main")
    commands = {"init": init}
    for name, help_text in (("update", "Rebase all patches onto upstream and rebuild integration"),
                            ("integrate", "Rebuild integration from the current patch branches")):
        commands[name] = sub.add_parser(name, help=help_text)
        commands[name].add_argument("--fetch", action="store_true", help="Fetch upstream before planning")
    delete = commands["delete"] = sub.add_parser("delete", help="Delete a leaf patch and rebuild integration")
    delete.add_argument("branch")
    rebase = commands["rebase"] = sub.add_parser("rebase", help="Reparent a custom patch or rebase a development branch")
    rebase.add_argument("branch")
    rebase.add_argument("--onto", required=True)
    rebase.add_argument("--from", dest="from_commit")
    promote = commands["promote"] = sub.add_parser("promote", help="Create or update a custom patch from development work")
    promote.add_argument("branch")
    mode = promote.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new")
    mode.add_argument("--update")
    promote.add_argument("--onto")
    promote.add_argument("--from", dest="from_commit")
    placement = promote.add_mutually_exclusive_group()
    placement.add_argument("--before")
    placement.add_argument("--after")
    release = commands["release"] = sub.add_parser("release", help="Replay integration into release/NAME with optional exclusions")
    release.add_argument("name")
    release.add_argument("--base", metavar="REV", help="Replay patches onto this tag or commit (default: upstream)")
    release.add_argument("--exclude", action="append", default=[])
    release.add_argument("--exclude-dependents", action="store_true")
    release.add_argument("--commit", action="append", default=[])
    push = commands["push"] = sub.add_parser("push", help="Publish selected branches atomically with explicit leases")
    push.add_argument("branches", nargs="*")
    for command in commands.values():
        command.add_argument("--dry-run", action="store_true")
    for name, help_text in (("status", "Show patch dependencies or an interrupted operation"),
                            ("validate", "Validate patch ancestry and integration history"),
                            ("continue", "Resume an interrupted operation after resolving conflicts"),
                            ("abort", "Cancel an operation and restore the original checkout")):
        sub.add_parser(name, help=help_text)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        git = Git()
        if args.command == "continue":
            resume(git)
        elif args.command == "abort":
            abort(git)
        elif args.command == "status" and state_path(git).exists():
            state = read(git)
            print(f"Paused: {state['label']} ({state['index']}/{len(state['actions'])} steps completed)")
            if state['index'] < len(state['actions']):
                print("Current step: " + state['actions'][state['index']]['kind'])
            print("Resolve conflicts and git add, then forkman continue; or forkman abort.")
        elif args.command in ("status", "validate"):
            config, _, items, previous = context(git, args)
            ordered(items, previous)
            print(f"Integration: {config.integration}; upstream: {config.upstream_remote}/{config.upstream_branch}")
            for name in ordered(items, previous):
                suffix = "" if name in previous else " (not integrated)"
                print(f"  {name} <- {items[name].parent or 'upstream'}{suffix}")
            print("History is valid.")
        else:
            handlers = {"init": do_init, "update": do_integrate, "integrate": do_integrate,
                        "delete": do_delete, "rebase": do_rebase, "promote": do_promote,
                        "release": do_release, "push": do_push}
            handlers[args.command](git, args)
        return 0
    except (Error, OSError, ValueError) as error:
        print(f"forkman: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nforkman: Interrupted. Run status to inspect any operation in progress.", file=sys.stderr)
        return 130
