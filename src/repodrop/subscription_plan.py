"""What a `/github subscribe` call turns a subscription into, and what changed.

Pure logic, no Discord or database: `/github subscribe` both creates and updates, and on an
update only the options actually passed change.
"""

import re
from dataclasses import dataclass

from repodrop.db.models import Kind
from repodrop.db.queries import WatchKey

KIND_ORDER = (Kind.RELEASE, Kind.TAG, Kind.COMMIT)
KIND_LABELS = {Kind.RELEASE: "releases", Kind.TAG: "tags", Kind.COMMIT: "commits"}

# Conservative subset of git's ref-name rules; GitHub validates the rest when we check the branch.
_BRANCH_RE = re.compile(r"^[^\s~^:?*\[\\]+$")


class PlanError(Exception):
    """The requested change isn't allowed; the message is shown to the member."""


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    kinds: frozenset[str]
    branches: tuple[str, ...] = ()  # commit branches; empty = follow the default branch
    include_prereleases: bool = False

    @property
    def sorted_kinds(self) -> list[str]:
        return [k for k in KIND_ORDER if k in self.kinds]


DEFAULT_STATE = SubscriptionState(kinds=frozenset({Kind.RELEASE}))


@dataclass(frozen=True, slots=True)
class SubscribeOptions:
    """Options as passed to the command; None means "not given"."""

    releases: bool | None = None
    tags: bool | None = None
    commits: bool | None = None
    branches: str | None = None  # comma-separated names, or "default"
    prereleases: bool | None = None


def parse_branches(raw: str) -> tuple[str, ...]:
    """`"main, dev"` -> `("main", "dev")`; `"default"` -> `()` (follow the default branch)."""
    raw = raw.strip()
    if raw.lower() == "default":
        return ()
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not _BRANCH_RE.match(name) or ".." in name or name.startswith("-"):
            raise PlanError(f"`{name}` isn't a valid branch name.")
        if name not in names:
            names.append(name)
    if not names:
        raise PlanError("List branch names separated by commas, or use `default`.")
    return tuple(names)


def plan_subscription(
    existing: SubscriptionState | None, opts: SubscribeOptions, *, max_branches: int
) -> SubscriptionState:
    """The subscription after applying the options that were passed.

    Branch names from `opts.branches` still need checking against GitHub by the caller.
    """
    base = existing or DEFAULT_STATE
    kinds = set(base.kinds)
    for kind, value in (
        (Kind.RELEASE, opts.releases),
        (Kind.TAG, opts.tags),
        (Kind.COMMIT, opts.commits),
    ):
        if value is True:
            kinds.add(kind)
        elif value is False:
            kinds.discard(kind)

    if not kinds:
        if existing is None:
            raise PlanError("Turn on at least one of `releases`, `tags` or `commits`.")
        raise PlanError(
            "That would turn off every event type. To stop announcing this repo here, use "
            "`/github unsubscribe`."
        )

    branches = base.branches
    if opts.branches is not None:
        if Kind.COMMIT not in kinds:
            raise PlanError("`branches` only applies to commits. Add `commits:true` too.")
        branches = parse_branches(opts.branches)
    if len(branches) > max_branches:
        raise PlanError(f"A subscription can follow at most {max_branches} branches.")

    prereleases = base.include_prereleases if opts.prereleases is None else opts.prereleases
    return SubscriptionState(
        kinds=frozenset(kinds), branches=branches, include_prereleases=prereleases
    )


def needed_watches(repo_id: int, state: SubscriptionState, default_branch: str) -> set[WatchKey]:
    """The poll watches a subscription in this state relies on."""
    keys: set[WatchKey] = set()
    for kind in state.kinds:
        if kind == Kind.COMMIT:
            for branch in state.branches or (default_branch,):
                keys.add(WatchKey(repo_id, kind, branch))
        else:
            keys.add(WatchKey(repo_id, kind, ""))
    return keys


@dataclass(frozen=True, slots=True)
class Removed:
    """What an update stopped announcing, so its pending deliveries can be skipped."""

    kinds: tuple[str, ...] = ()
    commit_branches: tuple[str, ...] = ()
    prereleases: bool = False

    def __bool__(self) -> bool:
        return bool(self.kinds or self.commit_branches or self.prereleases)


def removed_by_update(
    old: SubscriptionState, new: SubscriptionState, default_branch: str
) -> Removed:
    kinds = tuple(k for k in old.sorted_kinds if k not in new.kinds)
    branches: tuple[str, ...] = ()
    if Kind.COMMIT in old.kinds and Kind.COMMIT in new.kinds:
        old_b = old.branches or (default_branch,)
        new_b = new.branches or (default_branch,)
        branches = tuple(b for b in old_b if b not in new_b)
    prereleases = (
        old.include_prereleases and not new.include_prereleases and Kind.RELEASE in new.kinds
    )
    return Removed(kinds=kinds, commit_branches=branches, prereleases=prereleases)


def describe_state(state: SubscriptionState) -> str:
    """`releases, commits on `main`, `dev``"""
    parts = []
    for kind in state.sorted_kinds:
        label = KIND_LABELS[kind]
        if kind == Kind.COMMIT and state.branches:
            label += " on " + ", ".join(f"`{b}`" for b in state.branches)
        parts.append(label)
    return ", ".join(parts)


def describe_changes(old: SubscriptionState, new: SubscriptionState) -> list[str]:
    """Human summary of an update, e.g. `["Added commits on `main`", "Removed tags"]`."""
    changes = []
    for kind in new.sorted_kinds:
        if kind not in old.kinds:
            label = KIND_LABELS[kind]
            if kind == Kind.COMMIT:
                label += (
                    " on " + ", ".join(f"`{b}`" for b in new.branches)
                    if new.branches
                    else " on the default branch"
                )
            changes.append(f"Added {label}")
    for kind in old.sorted_kinds:
        if kind not in new.kinds:
            changes.append(f"Removed {KIND_LABELS[kind]}")
    if Kind.COMMIT in old.kinds and Kind.COMMIT in new.kinds and old.branches != new.branches:
        if new.branches:
            changes.append("Commits now on " + ", ".join(f"`{b}`" for b in new.branches))
        else:
            changes.append("Commits now follow the default branch")
    if old.include_prereleases != new.include_prereleases:
        changes.append("Pre-releases " + ("on" if new.include_prereleases else "off"))
    return changes
