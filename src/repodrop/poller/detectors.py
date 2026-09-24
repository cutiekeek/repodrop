"""Release / tag / commit diffing.

Pure functions: given the latest items from GitHub and the stored watermark
(`repo_watches.last_seen_id`), decide which items are new and what the next watermark is.

Watermarks per kind:
- release: `published_at` (ISO 8601, UTC) of the newest published release. Using the publish
  time rather than the release ID means drafts that get published later are still caught.
- tag: the name of the first tag GitHub returns.
- commit: the SHA of the branch head.

An empty-string watermark means "baselined, but there was nothing yet". `None` means the
watch has never been baselined, and callers must baseline instead of detecting.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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


@dataclass(slots=True)
class Detection:
    watermark: str
    events: list[DetectedEvent] = field(default_factory=list)  # oldest first


# --------------------------------------------------------------------------- releases


def _published(releases: list[Release]) -> list[Release]:
    return [r for r in releases if not r.draft and r.published_at is not None]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def baseline_releases(releases: list[Release]) -> str:
    published = _published(releases)
    if not published:
        return ""
    return _iso(max(r.published_at for r in published))  # type: ignore[type-var]


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
    return tags[0].name if tags else ""


def detect_tags(repo: RepoRef, tags: list[Tag], last_seen: str) -> Detection:
    if not tags or tags[0].name == last_seen:
        return Detection(watermark=last_seen)

    names = [t.name for t in tags]
    if not last_seen:
        new = tags  # first tags ever pushed
    elif last_seen in names:
        new = tags[: names.index(last_seen)]
    else:
        # The watermark tag was deleted or pushed off the page; only announce the newest one
        # rather than risk re-announcing old tags. Events are deduped by tag name regardless.
        new = tags[:1]

    return Detection(
        watermark=tags[0].name,
        events=[
            DetectedEvent(
                external_id=t.name,
                payload=tag_payload(repo, t, previous_tag=_next_name(tags, i)),
            )
            for i, t in reversed(list(enumerate(new)))
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
