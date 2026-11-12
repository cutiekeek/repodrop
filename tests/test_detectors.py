import json
from datetime import UTC, datetime, timedelta

from repodrop.github.client import TAGS_PER_PAGE
from repodrop.github.schemas import Commit, Release, Tag
from repodrop.poller.detectors import (
    RepoRef,
    baseline_commits,
    baseline_releases,
    baseline_tags,
    detect_commits,
    detect_promotions,
    detect_releases,
    detect_tags,
)

REPO = RepoRef("octo/widget")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def release(id: int, minutes: int | None, *, draft=False, prerelease=False) -> Release:
    return Release.model_validate(
        {
            "id": id,
            "tag_name": f"v{id}",
            "name": None,
            "body": "notes",
            "draft": draft,
            "prerelease": prerelease,
            "html_url": f"https://github.com/octo/widget/releases/v{id}",
            "created_at": T0.isoformat(),
            "published_at": (T0 + timedelta(minutes=minutes)).isoformat()
            if minutes is not None
            else None,
            "author": None,
        }
    )


def tag(name: str) -> Tag:
    return Tag.model_validate({"name": name, "commit": {"sha": f"sha-{name}"}})


def commit(sha: str, message: str = "msg") -> Commit:
    return Commit.model_validate(
        {
            "sha": sha,
            "html_url": f"https://github.com/octo/widget/commit/{sha}",
            "commit": {"message": message, "author": {"name": "Octo"}},
            "author": None,
        }
    )


# --------------------------------------------------------------------------- releases


def test_baseline_releases_ignores_drafts():
    releases = [release(3, None, draft=True), release(2, 20), release(1, 10)]
    assert baseline_releases(releases) == (T0 + timedelta(minutes=20)).isoformat()


def test_baseline_releases_empty():
    assert baseline_releases([]) == ""


def test_detect_releases_returns_new_oldest_first():
    last = (T0 + timedelta(minutes=10)).isoformat()
    releases = [release(3, 30), release(2, 20), release(1, 10)]
    d = detect_releases(REPO, releases, last)
    assert [e.external_id for e in d.events] == ["2", "3"]
    assert d.watermark == (T0 + timedelta(minutes=30)).isoformat()


def test_detect_releases_nothing_new_keeps_watermark():
    last = (T0 + timedelta(minutes=10)).isoformat()
    d = detect_releases(REPO, [release(1, 10)], last)
    assert d.events == []
    assert d.watermark == last


def test_detect_releases_catches_draft_published_later():
    # Release 5 was created (as a draft) before release 6, but published after it.
    last = (T0 + timedelta(minutes=10)).isoformat()
    d = detect_releases(REPO, [release(6, 10), release(5, 15)], last)
    assert [e.external_id for e in d.events] == ["5"]


def test_detect_releases_flags_prereleases():
    d = detect_releases(REPO, [release(2, 20, prerelease=True)], "")
    assert d.events[0].prerelease is True
    assert d.events[0].payload["prerelease"] is True


def test_detect_promotions_are_stable_releases_announced_as_prereleases():
    releases = [release(3, 30, prerelease=True), release(2, 20), release(1, 10)]
    # 2 and 3 were announced as pre-releases; 2 has since been marked stable.
    events = detect_promotions(REPO, releases, {"2", "3"})
    assert [e.external_id for e in events] == ["2:stable"]
    assert events[0].promoted and events[0].payload["prerelease"] is False
    assert events[0].payload["previous_tag"] == "v1"


def test_detect_releases_from_empty_baseline():
    d = detect_releases(REPO, [release(2, 20), release(1, 10)], "")
    assert [e.external_id for e in d.events] == ["1", "2"]


# --------------------------------------------------------------------------- tags


def tags(*names: str) -> list[Tag]:
    return [tag(n) for n in names]


def seen(*names: str) -> str:
    return baseline_tags(tags(*names))


def test_baseline_tags():
    assert json.loads(baseline_tags(tags("v2", "v1"))) == ["v2", "v1"]
    assert json.loads(baseline_tags([])) == []


def test_detect_tags_new_at_the_top():
    d = detect_tags(REPO, tags("v3", "v2", "v1"), seen("v1"))
    assert [e.external_id for e in d.events] == ["v2", "v3"]
    assert d.watermark == seen("v3", "v2", "v1")


def test_detect_tags_new_below_the_top():
    # GitHub lists by version name: a v24 patch lands under the v26 line, not first.
    d = detect_tags(
        REPO, tags("v27.0.0", "v26.1.0", "v24.2.1", "v24.2.0"), seen("v27.0.0", "v24.2.0")
    )
    assert [e.external_id for e in d.events] == ["v24.2.1", "v26.1.0"]
    assert d.events[0].payload["previous_tag"] == "v24.2.0"


def test_detect_tags_unchanged():
    d = detect_tags(REPO, tags("v1"), seen("v1"))
    assert d.events == [] and d.watermark == seen("v1")


def test_detect_tags_first_tags_ever():
    for empty in ("", seen()):
        d = detect_tags(REPO, tags("v2", "v1"), empty)
        assert [e.external_id for e in d.events] == ["v1", "v2"]


def test_detect_tags_nothing_known_only_announces_first():
    d = detect_tags(REPO, tags("v9", "v8"), seen("v1"))
    assert [e.external_id for e in d.events] == ["v9"]


def test_detect_tags_old_single_name_watermark_baselines_again():
    d = detect_tags(REPO, tags("v3", "v2", "v1"), "v1")
    assert d.events == [] and d.watermark == seen("v3", "v2", "v1")


def test_detect_tags_full_page_ignores_old_tags_that_moved_up():
    page = [f"v{n}" for n in range(TAGS_PER_PAGE, 0, -1)]  # v100 .. v1
    # v50 and v40 were deleted, so the old v0 moved onto the page; v101 is really new.
    now = ["v101", *[n for n in page if n not in ("v50", "v40")], "v0"]
    assert len(now) == TAGS_PER_PAGE
    d = detect_tags(REPO, tags(*now), seen(*page))
    assert [e.external_id for e in d.events] == ["v101"]


def test_detect_tags_empty_list_keeps_watermark():
    d = detect_tags(REPO, [], seen("v1"))
    assert d.events == [] and d.watermark == seen("v1")


# --------------------------------------------------------------------------- commits


def test_baseline_commits():
    assert baseline_commits([commit("b"), commit("a")]) == "b"
    assert baseline_commits([]) == ""


def test_detect_commits_batches_into_one_event():
    d = detect_commits(REPO, "main", [commit("c", "third\n\nbody"), commit("b"), commit("a")], "a")
    assert len(d.events) == 1
    event = d.events[0]
    assert event.external_id == "main:a..c"
    assert [c["sha"] for c in event.payload["commits"]] == ["c", "b"]
    assert event.payload["commits"][0]["message"] == "third"
    assert event.payload["truncated"] is False
    assert d.watermark == "c"


def test_detect_commits_force_push_marks_truncated():
    d = detect_commits(REPO, "main", [commit("y"), commit("x")], "gone")
    assert d.events[0].payload["truncated"] is True
    assert len(d.events[0].payload["commits"]) == 2


def test_detect_commits_unchanged():
    d = detect_commits(REPO, "main", [commit("a")], "a")
    assert d.events == [] and d.watermark == "a"
