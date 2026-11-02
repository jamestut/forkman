# Forkman

Forkman is a Python CLI for maintaining a fork as a set of small, ordered Git patch branches. It keeps patch ancestry in Git itself, rebuilds a predictable integration branch, and helps promote development work into the patch stack.

The intended remote layout is:

```text
upstream/main  — the source project
origin/main    — your fork's integration branch (configurable)
custom/*       — maintained patches, each based on upstream or another patch
dev/*          — temporary work that can be explicitly promoted
```

Forkman does not push automatically. Fetching is optional, and publishing branches is always an explicit `push` command.

## Install

From a checkout of this repository, install the CLI in editable mode:

```sh
python -m pip install -e .
forkman --help
```

Forkman requires Python 3.10 or later, Git 2.38 or later, and PyYAML (installed by the package). Git 2.38 is the minimum because Forkman rebases with `--empty=keep` and `--no-update-refs`.

To run the test suite:

```sh
python -m unittest discover -s tests -v
```

## Initialize a repository

Start in the fork's local clone, with an `upstream` remote and a local integration branch. The defaults are `upstream/main`, `origin`, and `main`:

```sh
forkman init
```

Remote names and branch names can be changed at initialization:

```sh
forkman init \
  --upstream-remote upstream --upstream-branch main \
  --fork-remote origin --integration main
```

Fetch the configured upstream before initialization. Forkman manages local `custom/*` branches; after cloning a fork, create local tracking branches for the remote patches you want to maintain before rebuilding integration. To change settings later, rerun `init` with the complete desired settings and the same integration branch name. Changing an existing integration branch's name requires retiring the old configuration-bearing branch to avoid ambiguous configuration discovery.

Initialization writes `forkman.yaml` and creates the initial integration history. It also accepts an existing integration branch when its history contains only identifiable no-ff merges of custom patch branches on the upstream baseline; direct commits in that history are rejected. The config file contains only repository settings:

```yaml
upstream:
  remote: upstream
  branch: main
fork:
  remote: origin
integration:
  branch: main
```

Forkman creates the single configuration commit directly on top of the chosen upstream revision as part of initialization or a rebuild. It is the first commit after upstream and precedes all patch merges. Forkman records the ordered patch stack in the merge parents and commit messages on the integration branch. It does not keep a second, permanent branch-order manifest.

## Patch branches

Create a `custom/*` branch for each maintained patch. A patch branch has one linear history and exactly one parent: either the upstream base or another `custom/*` patch. Sibling patches may share the same parent. When rebuilding integration, Forkman no-ff merges patches in stack order, preserving each patch's identity and recording the order in integration history.

For example:

```text
upstream/main
    ├── custom/feature-a
    ├── custom/feature-b
    └── custom/base-fix
            └── custom/follow-up
```

Use `status` to inspect the current stack and `validate` to check that the repository follows Forkman's history rules:

```sh
forkman status
forkman validate
```

`update` rebases the patch stack on the configured upstream and rebuilds the integration branch. `integrate` rebuilds integration from the current stack without rebasing patches. Both can fetch remotes first when requested:

```sh
forkman update --fetch
forkman integrate
forkman integrate --fetch
```

Forkman refuses unexpected commits on integration. Apart from the initial config commit, integration history is expected to consist of Forkman's no-ff patch merges. Review and resolve any divergence before rebuilding.

## Releases and development promotion

Build a named release branch at `release/NAME` from the selected patch merges, excluding branches as needed. Release commits can also be selected explicitly with `--commit`. Make release-specific source adjustments on a `dev/*` branch, then select those commits; keep the integration branch for the maintained patch stack.

```sh
forkman release 2.4
forkman release 2.4 --exclude custom/experimental
forkman release 2.4 --exclude custom/experimental --exclude-dependents
forkman release 2.4 --commit a1b2c3d --commit d4e5f6a
forkman release v1.104.2-m1 --base v1.104.2 --commit a1b2c3d
```

Use `--base TAG_OR_COMMIT` to select the upstream revision for a release. Forkman replays each selected patch's own commits onto that revision, or onto its replayed parent patch, then merges those temporary copies in integration order. The maintained `custom/*` branches and integration branch stay at their original tips. This lets patches maintained on a newer upstream also be used for an older release without importing unrelated upstream changes.

The first command creates `release/2.4`; the last creates `release/v1.104.2-m1` from the `v1.104.2` tag. `--commit` supplies additional release adjustments after patch integration; it does not select the base. Without `--base`, the default is the configured upstream tip. The source integration must reference the current custom patch tips; rebuild it first if those differ. When using the default base, update integration first if upstream has advanced.

Use `dev/*` branches for work that is not yet in the maintained stack. Promote it by naming the exact `custom/*` destination to create or update:

```sh
forkman promote dev/new-cache --new custom/cache
forkman promote dev/cache-refresh --update custom/cache
```

Promotion can specify the destination patch's parent and where it belongs in the stack. `--from` sets an exclusive lower boundary: commits at that revision and its ancestors are not promoted.

```sh
forkman promote dev/new-cache --new custom/cache \
  --onto custom/base-fix --after custom/base-fix

forkman promote dev/cache-refresh --update custom/cache \
  --from a1b2c3d
```

New patches append by default. Placement with `--before` or `--after` applies only when creating a new patch, and parents must always precede their children. To rebase development work directly onto a patch or upstream, use `rebase`; `--from` marks the exclusive fork-point boundary:

```sh
forkman rebase dev/new-cache --onto custom/base-fix --from a1b2c3d
```

Forkman may infer a development branch's fork point from its relationship to the current stack. That inference becomes ambiguous after its parent or the integration history is rewritten, or once needed reflog entries have expired. Use `--from COMMIT` to set the lower boundary explicitly in those cases.

## Reparent and remove patches

Rebase a patch and its descendants onto upstream or onto another custom patch. Forkman then rebuilds integration:

```sh
forkman rebase custom/follow-up --onto upstream
forkman rebase custom/follow-up --onto custom/base-fix
```

Delete a patch branch only when no other patch depends on it:

```sh
forkman delete custom/obsolete
```

Forkman refuses to delete a branch that still has patch children. Reparent or remove those children first.

## Conflicts, previews, and pushing

Modifying commands support `--dry-run` to preview the planned work. A dry run cannot use `--fetch`, since fetching changes remote refs; fetch separately, then run the dry run. If a rebase or merge stops for conflicts, resolve them in the working tree, stage the resolutions, and continue or roll back the in-progress operation with:

```sh
forkman continue
forkman abort
```

If a resolution leaves the tree unchanged, `continue` preserves an empty patch or adjustment commit so the operation can finish. You can also commit a cherry-pick resolution manually with your own message and then use `forkman continue`. An unresolved conflict keeps the operation paused; continuing through several conflicts resumes the same saved plan.

An integration merge resolution is not stored as a separate patch. A later rebuild replays the merges and may need the same conflict resolved again. Git's optional rerere support can reuse recorded resolutions when enabled:

```sh
git config rerere.enabled true
```

With no branch arguments, `push` publishes the integrated custom branches and the integration branch. To publish an explicit set instead, name each branch. Forkman uses explicit leases so a remote update made since your last fetch is not silently overwritten:

```sh
forkman push main custom/base-fix custom/follow-up
```

## Recovery data

During an operation Forkman writes transient progress to `forkman-state.json` in the Git directory for the current worktree (usually `.git`; linked worktrees have their own Git directory). Before rewriting refs, it saves their prior values under Git backup refs named `refs/forkman/backups/<id>/...`. These let you recover branch tips after an interrupted or unwanted rewrite. They are local Git refs, not a permanent patch manifest; inspect them with Git and remove them when no longer needed. Do not delete the state file while an operation is in progress. `forkman continue` and `forkman abort` manage that operation state.

## Working rules and limitations

- Keep every `custom/*` patch branch linear and based on upstream or one `custom/*` parent. Siblings are supported; merge commits inside patch branches are not.
- Do not merge integration into a custom patch. Integration is rebuilt from the stack and is not a patch parent.
- Reserve `forkman.yaml` for integration configuration; custom patches must not change it.
- Upstream history must advance without rewriting the recorded baseline. Forkman refuses an integration baseline that is no longer an ancestor of the configured upstream.
- Let Forkman perform rebases, integration rebuilds, and patch deletion. Manual rewrites or deleting a parent outside Forkman can make the stack ambiguous and can invalidate development fork-point inference.
- Keep the initial config commit immediately after upstream and ahead of all no-ff patch merges. Forkman refuses integration history containing other unexpected commits.
- Git backup refs and the operation state file are local recovery aids. They are not pushed as a substitute for reviewing and publishing the intended branches.
- Forkman does not fetch unless asked, and never pushes as a side effect of another command.
- Deleting a patch removes its local branch. Remove any remote counterpart explicitly with Git after reviewing it; Forkman does not delete remote refs as a side effect of local deletion.
- Run one Forkman command at a time in a worktree. Branches being rewritten cannot be checked out in another worktree.
