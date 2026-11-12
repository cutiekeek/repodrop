"""Release / tag / commit diffing.

Pure functions: given the latest items from GitHub and the stored watermark
(`repo_watches.last_seen_id`), decide which items are new and what the next watermark is.

Watermarks per kind:
- release: `published_at` (ISO 8601, UTC) of the newest published release. Using the publish
  time rather than the release ID means drafts that get published later are still caught.
- tag: a JSON list of the tag names on the first page GitHub returns.
- commit: the SHA of the branch head.

An empty-string watermark means "baselined, but there was nothing yet". `None` means the
watch has never been baselined, and callers must baseline instead of detecting.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from repodrop.github.client import TAGS_PER_PAGE
from repodrop.github.schemas import Commit, Release, Tag

MAX_BODY_CHARS = 6000
MAX_COMMIT_MESSAGE_CHARS = 200


@dataclass(frozen=True, slots=True)
class RepoRef:
    full_name: str

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def owner_avatar_url(self) -> str:
        return f"https://github.com/{self.full_name.split('/')[0]}.png"

    def payload(self) -> dict[str, str]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "owner_avatar_url": self.owner_avatar_url,
        }


@dataclass(slots=True)
class DetectedEvent:
    external_id: str
    payload: dict[str, Any]
    prerelease: bool = False
    promoted: bool = False  # a pre-release that was later marked stable


@dataclass(slots=True)
class Detection:
    watermark: str
    events: list[DetectedEvent] = field(default_factory=list)  # oldest first


# --------------------------------------------------------------------------- releases


def _published(releases: list[Release]) -> list[Release]:
    return [r for r in releases if not r.draft and r.published_at is not None]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def latest_release(releases: list[Release]) -> Release | None:
    """The most recently published release, if there is one."""
    return max(_published(releases), key=lambda r: r.published_at, default=None)  # type: ignore[arg-type, return-value]


def baseline_releases(releases: list[Release]) -> str:
    latest = latest_release(releases)
    return _iso(latest.published_at) if latest else ""  # type: ignore[arg-type]


def detect_releases(repo: RepoRef, releases: list[Release], last_seen: str) -> Detection:
    since = datetime.fromisoformat(last_seen) if last_seen else None
    new = [
        r
        for r in _published(releases)
        if since is None or r.published_at > since  # type: ignore[operator]
    ]
    new.sort(key=lambda r: r.published_at)  # type: ignore[arg-type, return-value]
    watermark = _iso(new[-1].published_at) if new else last_seen  # type: ignore[arg-type]
    return Detection(
        watermark=watermark,
        events=[
            DetectedEvent(
                external_id=str(r.id),
                payload=release_payload(repo, r, previous_tag=previous_release_tag(r, releases)),
                prerelease=r.prerelease,
            )
            for r in new
        ],
    )


def detect_promotions(
    repo: RepoRef, releases: list[Release], announced_as_prerelease: set[str]
) -> list[DetectedEvent]:
    """Stable releases that were announced as pre-releases (the flag was removed later).

    A promoted release keeps its ID, so `detect_releases` never reports it again and
    subscriptions without pre-releases would never hear about it. It gets a second event,
    `<id>:stable`, which is recorded once like any other.
    """
    return [
        DetectedEvent(
            external_id=f"{r.id}:stable",
            payload=release_payload(repo, r, previous_tag=previous_release_tag(r, releases)),
            promoted=True,
        )
        for r in _published(releases)
        if not r.prerelease and str(r.id) in announced_as_prerelease
    ]


def previous_release_tag(release: Release, releases: list[Release]) -> str | None:
    """The tag of the next-older published release in the same response, for Compare.

    A stable release compares against the previous stable one (v1.9 -> v2.0, not
    v2.0-rc1 -> v2.0); a pre-release compares against the previous release of any kind.
    """
    if release.published_at is None:
        return None
    older = [
        r
        for r in _published(releases)
        if r.published_at < release.published_at  # type: ignore[operator]
        and (release.prerelease or not r.prerelease)
    ]
    if not older:
        return None
    return max(older, key=lambda r: r.published_at).tag_name  # type: ignore[arg-type, return-value]


def release_payload(
    repo: RepoRef, release: Release, *, previous_tag: str | None = None
) -> dict[str, Any]:
    body = release.body or ""
    return {
        "repo": repo.payload(),
        "id": release.id,
        "tag_name": release.tag_name,
        "name": release.name or release.tag_name,
        "body": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "html_url": release.html_url,
        "prerelease": release.prerelease,
        "published_at": _iso(release.published_at) if release.published_at else None,
        "author": _user(release.author),
        "previous_tag": previous_tag,
    }


# --------------------------------------------------------------------------- tags


def baseline_tags(tags: list[Tag]) -> str:
    return json.dumps([t.name for t in tags])


def detect_tags(repo: RepoRef, tags: list[Tag], last_seen: str) -> Detection:
    """A tag is new when its name isn't in the stored set.

    GitHub lists tags by version name, not by date, so a new tag can appear anywhere on the
    page (a v24 patch below the v26 line), not only at the top.
    """
    # shortcut: only the first page (TAGS_PER_PAGE names, highest versions first) is watched,
    # so a new tag sorting below it is missed; page through all tags if that ever matters.
    if not tags:
        return Detection(watermark=last_seen)
    watermark = baseline_tags(tags)
    if last_seen and not last_seen.startswith("["):
        # Stored before name sets were kept (one tag name, which can't start with "["):
        # baseline again rather than announce the whole page.
        return Detection(watermark=watermark)

    seen = set(json.loads(last_seen)) if last_seen else set()
    known = [i for i, t in enumerate(tags) if t.name in seen]
    new = [i for i, t in enumerate(tags) if t.name not in seen]
    if seen and not known:
        # Nothing we knew is left on the page (tags rewritten, or a page of new ones): announce
        # only the first rather than flood the channel.
        new = new[:1]
    elif known and len(tags) >= TAGS_PER_PAGE:
        # On a full page, unknown names below the last known one are old tags that moved up
        # after a tag above them was deleted.
        new = [i for i in new if i < known[-1]]

    return Detection(
        watermark=watermark,
        events=[
            DetectedEvent(
                external_id=tags[i].name,
                payload=tag_payload(repo, tags[i], previous_tag=_next_name(tags, i)),
            )
            for i in reversed(new)
        ],
    )


def _next_name(tags: list[Tag], index: int) -> str | None:
    """The tag listed after `tags[index]` (GitHub lists newest first), for Compare."""
    return tags[index + 1].name if index + 1 < len(tags) else None


def tag_payload(repo: RepoRef, tag: Tag, *, previous_tag: str | None = None) -> dict[str, Any]:
    return {
        "repo": repo.payload(),
        "name": tag.name,
        "sha": tag.commit.sha,
        "html_url": f"{repo.html_url}/tree/{tag.name}",
        "previous_tag": previous_tag,
    }


# --------------------------------------------------------------------------- commits


def baseline_commits(commits: list[Commit]) -> str:
    return commits[0].sha if commits else ""


def detect_commits(repo: RepoRef, branch: str, commits: list[Commit], last_seen: str) -> Detection:
    """All new commits from one poll become a single batched event."""
    if not commits or commits[0].sha == last_seen:
        return Detection(watermark=last_seen)

    shas = [c.sha for c in commits]
    if last_seen in shas:
        new, truncated = commits[: shas.index(last_seen)], False
    else:
        # Force-push, or more new commits than fit in one page.
        new, truncated = commits, bool(last_seen)

    head = commits[0].sha
    compare_url = (
        f"{repo.html_url}/compare/{last_seen[:12]}...{head[:12]}"
        if last_seen
        else f"{repo.html_url}/commits/{branch}"
    )
    payload = {
        "repo": repo.payload(),
        "branch": branch,
        "before": last_seen or None,
        "after": head,
        "compare_url": compare_url,
        "truncated": truncated,
        "commits": [_commit(c) for c in new],  # newest first
    }
    return Detection(
        watermark=head,
        events=[DetectedEvent(external_id=f"{branch}:{last_seen}..{head}", payload=payload)],
    )


def _commit(c: Commit) -> dict[str, Any]:
    message = c.commit.message.strip().splitlines()[0] if c.commit.message.strip() else ""
    return {
        "sha": c.sha,
        "message": message[:MAX_COMMIT_MESSAGE_CHARS],
        "html_url": c.html_url,
        "author_name": c.commit.author.name if c.commit.author else None,
        "author": _user(c.author),
    }


def _user(user: Any) -> dict[str, str] | None:
    if user is None:
        return None
    return {"login": user.login, "html_url": user.html_url, "avatar_url": user.avatar_url}
