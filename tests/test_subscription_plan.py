import pytest

from repodrop.db.queries import WatchKey
from repodrop.subscription_plan import (
    DEFAULT_STATE,
    PlanError,
    SubscribeOptions,
    SubscriptionState,
    describe_changes,
    describe_state,
    needed_watches,
    parse_branches,
    plan_subscription,
    removed_by_update,
)


def state(*kinds, branches=(), pre=False):
    return SubscriptionState(kinds=frozenset(kinds), branches=branches, include_prereleases=pre)


def plan(existing, **opts):
    return plan_subscription(existing, SubscribeOptions(**opts), max_branches=5)


# --------------------------------------------------------------------------- creating


def test_new_subscription_defaults_to_releases():
    assert plan(None) == DEFAULT_STATE == state("release")


def test_new_subscription_options():
    assert plan(None, tags=True, commits=True) == state("release", "tag", "commit")
    assert plan(None, releases=False, tags=True) == state("tag")
    assert plan(None, commits=True, branches="main, dev") == state(
        "release", "commit", branches=("main", "dev")
    )


def test_new_subscription_needs_a_kind():
    with pytest.raises(PlanError, match="at least one"):
        plan(None, releases=False)


# --------------------------------------------------------------------------- updating


def test_update_only_changes_passed_options():
    old = state("release", "commit", branches=("dev",), pre=True)
    assert plan(old, tags=True) == state("release", "tag", "commit", branches=("dev",), pre=True)
    assert plan(old, prereleases=False) == state("release", "commit", branches=("dev",))
    assert plan(old, releases=False) == state("commit", branches=("dev",), pre=True)


def test_update_branches_replace_and_default():
    old = state("commit", branches=("dev",))
    assert plan(old, branches="main,dev").branches == ("main", "dev")
    assert plan(old, branches="default").branches == ()


def test_update_that_turns_everything_off_points_to_unsubscribe():
    with pytest.raises(PlanError, match="unsubscribe"):
        plan(state("tag"), tags=False)


def test_branches_require_commits():
    with pytest.raises(PlanError, match="commits:true"):
        plan(state("release"), branches="dev")
    # Turning commits on in the same call is fine.
    assert plan(state("release"), commits=True, branches="dev").branches == ("dev",)


def test_turning_commits_back_on_keeps_stored_branches():
    old = state("release", branches=("dev",))  # commits were turned off earlier
    assert plan(old, commits=True).branches == ("dev",)


def test_branch_cap():
    with pytest.raises(PlanError, match="at most 2"):
        plan_subscription(None, SubscribeOptions(commits=True, branches="a,b,c"), max_branches=2)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("main", ("main",)),
        (" main , dev ,, main ", ("main", "dev")),
        ("DEFAULT", ()),
        ("release/1.x", ("release/1.x",)),
    ],
)
def test_parse_branches(raw, expected):
    assert parse_branches(raw) == expected


@pytest.mark.parametrize("raw", ["", " , ", "has space", "a..b", "-flag", "wild*"])
def test_parse_branches_rejects(raw):
    with pytest.raises(PlanError):
        parse_branches(raw)


# --------------------------------------------------------------------------- watches


def test_needed_watches():
    assert needed_watches(1, state("release", "commit"), "main") == {
        WatchKey(1, "release", ""),
        WatchKey(1, "commit", "main"),
    }
    assert needed_watches(1, state("commit", branches=("dev", "main")), "main") == {
        WatchKey(1, "commit", "dev"),
        WatchKey(1, "commit", "main"),
    }


# --------------------------------------------------------------------------- removals


def test_removed_by_update():
    old = state("release", "tag", "commit", branches=("dev", "main"), pre=True)
    new = state("release", "commit", branches=("main",))
    removed = removed_by_update(old, new, default_branch="main")
    assert removed.kinds == ("tag",)
    assert removed.commit_branches == ("dev",)
    assert removed.prereleases is True


def test_switching_to_default_that_is_the_same_branch_removes_nothing():
    old = state("commit", branches=("main",))
    new = state("commit")
    assert not removed_by_update(old, new, default_branch="main")


def test_dropping_commits_removes_the_kind_not_branches():
    removed = removed_by_update(state("release", "commit"), state("release"), "main")
    assert removed.kinds == ("commit",) and removed.commit_branches == ()


# --------------------------------------------------------------------------- summaries


def test_describe_state():
    assert describe_state(state("commit", "release")) == "releases, commits"
    assert describe_state(state("commit", branches=("main", "dev"))) == "commits on `main`, `dev`"


def test_describe_changes():
    old = state("release", "tag")
    assert describe_changes(old, state("release", "commit", branches=("main", "dev"))) == [
        "Added commits on `main`, `dev`",
        "Removed tags",
    ]
    assert describe_changes(state("release"), state("release", "commit")) == [
        "Added commits on the default branch"
    ]
    assert describe_changes(state("commit", branches=("dev",)), state("commit")) == [
        "Commits now follow the default branch"
    ]
    assert describe_changes(state("release"), state("release", pre=True)) == ["Pre-releases on"]
    assert describe_changes(old, old) == []
