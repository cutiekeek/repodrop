"""Async GitHub REST client with ETag handling and rate-limit tracking."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from repodrop import observability
from repodrop.github.schemas import (
    Commit,
    Release,
    Repository,
    Tag,
    commits_adapter,
    releases_adapter,
    tags_adapter,
)

API_URL = "https://api.github.com"


class GitHubError(Exception):
    """Unexpected or transient GitHub failure."""


class NotFoundError(GitHubError):
    """The repo (or branch) doesn't exist, or is private."""


class RateLimitedError(GitHubError):
    def __init__(self, retry_at: datetime) -> None:
        super().__init__(f"rate limited until {retry_at.isoformat()}")
        self.retry_at = retry_at


@dataclass(slots=True)
class RateLimit:
    remaining: int | None = None
    reset_at: datetime | None = None

    def is_low(self, floor: int) -> bool:
        return self.remaining is not None and self.remaining < floor


@dataclass(slots=True)
class Conditional[T]:
    """Result of a conditional GET. `items` is None when the server answered 304."""

    items: T | None
    etag: str | None
    redirected: bool = False

    @property
    def not_modified(self) -> bool:
        return self.items is None


class GitHubClient:
    def __init__(self, token: str, *, base_url: str = API_URL, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repodrop",
            },
            timeout=timeout,
            # Renamed/transferred repos answer with a 301 to the new location.
            follow_redirects=True,
        )
        self.rate_limit = RateLimit()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ repo metadata

    async def get_repo(self, full_name: str) -> Repository:
        resp = await self._get(f"/repos/{full_name}")
        return Repository.model_validate_json(resp.content)

    async def get_repo_by_id(self, github_id: int) -> Repository:
        resp = await self._get(f"/repositories/{github_id}")
        return Repository.model_validate_json(resp.content)

    async def branch_exists(self, full_name: str, branch: str) -> bool:
        try:
            await self._get(f"/repos/{full_name}/branches/{quote(branch, safe='')}")
        except NotFoundError:
            return False
        return True

    # ------------------------------------------------------------------ polled lists

    async def list_releases(
        self, full_name: str, *, etag: str | None = None, per_page: int = 10
    ) -> Conditional[list[Release]]:
        return await self._conditional(
            f"/repos/{full_name}/releases", releases_adapter, etag, {"per_page": per_page}
        )

    async def list_tags(
        self, full_name: str, *, etag: str | None = None, per_page: int = 10
    ) -> Conditional[list[Tag]]:
        return await self._conditional(
            f"/repos/{full_name}/tags", tags_adapter, etag, {"per_page": per_page}
        )

    async def list_commits(
        self, full_name: str, branch: str, *, etag: str | None = None, per_page: int = 20
    ) -> Conditional[list[Commit]]:
        try:
            return await self._conditional(
                f"/repos/{full_name}/commits",
                commits_adapter,
                etag,
                {"sha": branch, "per_page": per_page},
            )
        except _EmptyRepositoryError:
            return Conditional(items=[], etag=None)

    # ------------------------------------------------------------------ internals

    async def _conditional[T](
        self,
        path: str,
        adapter: TypeAdapter[T],
        etag: str | None,
        params: dict[str, str | int],
    ) -> Conditional[T]:
        headers = {"If-None-Match": etag} if etag else {}
        resp = await self._get(path, params=params, headers=headers)
        if resp.status_code == 304:
            return Conditional(items=None, etag=etag)
        return Conditional(
            items=adapter.validate_json(resp.content),
            etag=resp.headers.get("ETag"),
            redirected=bool(resp.history),
        )

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            resp = await self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"{type(exc).__name__} requesting {path}") from exc

        self._track_rate_limit(resp)

        if resp.status_code in (200, 304):
            return resp
        if resp.status_code in (404, 410, 451):
            raise NotFoundError(f"{path}: {resp.status_code}")
        if resp.status_code == 409 and "empty" in resp.text.lower():
            raise _EmptyRepositoryError(path)
        if resp.status_code in (403, 429):
            retry_at = self._rate_limited_until(resp)
            if retry_at is not None:
                raise RateLimitedError(retry_at)
        raise GitHubError(f"{path}: HTTP {resp.status_code}")

    def _track_rate_limit(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self.rate_limit.remaining = int(remaining)
            observability.rate_limit_remaining.set(int(remaining))
        if reset is not None:
            self.rate_limit.reset_at = datetime.fromtimestamp(int(reset), UTC)

    def _rate_limited_until(self, resp: httpx.Response) -> datetime | None:
        now = datetime.now(UTC)
        # Secondary rate limits send Retry-After (seconds).
        if (retry_after := resp.headers.get("Retry-After")) is not None:
            return now + timedelta(seconds=int(retry_after))
        # Primary rate limit exhausted.
        if resp.headers.get("X-RateLimit-Remaining") == "0" and self.rate_limit.reset_at:
            return self.rate_limit.reset_at
        # Secondary limit without Retry-After: GitHub recommends waiting at least a minute.
        if "secondary rate limit" in resp.text.lower():
            return now + timedelta(minutes=1)
        return None


class _EmptyRepositoryError(GitHubError):
    """GitHub answers 409 when listing commits of a repo with no commits."""
