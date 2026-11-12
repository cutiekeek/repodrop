"""The real GitHubClient against a fake transport: how HTTP answers become results and errors."""

from datetime import UTC, datetime

import httpx
import pytest

from repodrop.github.client import GitHubClient, GitHubError, NotFoundError, RateLimitedError

RELEASE = {
    "id": 1,
    "tag_name": "v1",
    "draft": False,
    "prerelease": False,
    "html_url": "https://github.com/octo/widget/releases/tag/v1",
    "created_at": "2026-01-01T00:00:00Z",
    "published_at": "2026-01-01T00:00:00Z",
}


def client_for(handler) -> GitHubClient:
    return GitHubClient("secret-token", transport=httpx.MockTransport(handler))


def answering(status: int, *, headers: dict[str, str] | None = None, **kw) -> GitHubClient:
    return client_for(lambda request: httpx.Response(status, headers=headers, **kw))


def seconds_from_now(at: datetime) -> float:
    return (at - datetime.now(UTC)).total_seconds()


async def test_ok_parses_items_and_tracks_the_rate_limit():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[RELEASE],
            headers={"ETag": '"abc"', "X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "100"},
        )

    client = client_for(handler)
    result = await client.list_releases("octo/widget")
    assert [r.tag_name for r in result.items] == ["v1"]
    assert result.etag == '"abc"' and not result.not_modified and not result.redirected
    assert client.rate_limit.remaining == 4999
    assert client.rate_limit.reset_at == datetime.fromtimestamp(100, UTC)
    assert seen[0].headers["Authorization"] == "Bearer secret-token"
    assert seen[0].url.params["per_page"] == "10"


async def test_not_modified_keeps_the_etag_it_sent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    result = await client_for(handler).list_tags("octo/widget", etag='"abc"')
    assert result.not_modified and result.etag == '"abc"'
    assert seen[0].headers["If-None-Match"] == '"abc"'


async def test_renamed_repo_is_followed_and_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/old/releases":
            return httpx.Response(301, headers={"Location": "/repositories/42/releases"})
        return httpx.Response(200, json=[RELEASE])

    result = await client_for(handler).list_releases("octo/old")
    assert result.redirected and len(result.items) == 1


@pytest.mark.parametrize("status", [404, 410, 451])
async def test_missing_repo(status):
    with pytest.raises(NotFoundError):
        await answering(status).list_releases("octo/widget")


async def test_branch_exists():
    assert await answering(200, json={}).branch_exists("octo/widget", "main") is True
    assert await answering(404).branch_exists("octo/widget", "gone") is False


async def test_empty_repo_has_no_commits():
    client = answering(409, json={"message": "Git Repository is empty."})
    result = await client.list_commits("octo/widget", "main")
    assert result.items == [] and not result.not_modified


@pytest.mark.parametrize("status", [403, 429])
async def test_rate_limited_with_retry_after(status):
    with pytest.raises(RateLimitedError) as caught:
        await answering(status, headers={"Retry-After": "30"}).list_releases("octo/widget")
    assert 25 < seconds_from_now(caught.value.retry_at) <= 30


async def test_rate_limit_used_up_waits_for_the_reset():
    reset = int(datetime.now(UTC).timestamp()) + 600
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}
    with pytest.raises(RateLimitedError) as caught:
        await answering(403, headers=headers).list_releases("octo/widget")
    assert caught.value.retry_at == datetime.fromtimestamp(reset, UTC)


async def test_secondary_rate_limit_without_headers_waits_a_minute():
    client = answering(403, json={"message": "You have exceeded a secondary rate limit."})
    with pytest.raises(RateLimitedError) as caught:
        await client.list_releases("octo/widget")
    assert 55 < seconds_from_now(caught.value.retry_at) <= 60


@pytest.mark.parametrize("status", [401, 403, 409, 500, 502])
async def test_other_failures_are_plain_errors(status):
    # Not "missing" (which can disable subscriptions) and not "rate limited".
    with pytest.raises(GitHubError) as caught:
        await answering(status, json={"message": "nope"}).list_releases("octo/widget")
    assert type(caught.value) is GitHubError


async def test_network_failure_is_a_plain_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(GitHubError) as caught:
        await client_for(handler).get_repo("octo/widget")
    assert type(caught.value) is GitHubError
