"""Embed builders per event kind. Input is the `events.payload` JSON."""

from datetime import datetime
from typing import Any

import discord

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
AUTHOR_NAME_LIMIT = 256

RELEASE_COLOR = discord.Color.from_rgb(46, 160, 67)
PRERELEASE_COLOR = discord.Color.from_rgb(210, 153, 34)
TAG_COLOR = discord.Color.from_rgb(130, 80, 223)
COMMIT_COLOR = discord.Color.from_rgb(9, 105, 218)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)].rstrip() + suffix


def build_embed(kind: str, payload: dict[str, Any]) -> discord.Embed:
    match kind:
        case "release":
            return release_embed(payload)
        case "tag":
            return tag_embed(payload)
        case "commit":
            return commit_embed(payload)
    raise ValueError(f"unknown event kind {kind!r}")


def release_embed(p: dict[str, Any]) -> discord.Embed:
    repo = p["repo"]
    body = (p.get("body") or "").strip()
    more = f"\n\n[Read the full release notes]({p['html_url']})"
    if p.get("body_truncated") or len(body) > DESCRIPTION_LIMIT:
        body = truncate(body, DESCRIPTION_LIMIT - len(more)) + more
    embed = discord.Embed(
        title=truncate(p["name"], TITLE_LIMIT),
        url=p["html_url"],
        description=body or None,
        color=PRERELEASE_COLOR if p.get("prerelease") else RELEASE_COLOR,
        timestamp=_parse_ts(p.get("published_at")),
    )
    _set_repo_author(embed, repo)
    footer = "Pre-release" if p.get("prerelease") else "Release"
    if p["name"] != p["tag_name"]:
        footer += f" · {p['tag_name']}"
    if author := p.get("author"):
        embed.set_footer(text=f"{footer} · by {author['login']}", icon_url=author["avatar_url"])
    else:
        embed.set_footer(text=footer)
    return embed


def tag_embed(p: dict[str, Any]) -> discord.Embed:
    repo = p["repo"]
    embed = discord.Embed(
        title=truncate(f"New tag {p['name']}", TITLE_LIMIT),
        url=p["html_url"],
        description=f"`{p['sha'][:7]}`",
        color=TAG_COLOR,
    )
    _set_repo_author(embed, repo)
    return embed


def commit_embed(p: dict[str, Any]) -> discord.Embed:
    repo = p["repo"]
    commits = p["commits"]
    count = f"{len(commits)}+" if p.get("truncated") else str(len(commits))
    noun = "commit" if len(commits) == 1 and not p.get("truncated") else "commits"

    lines: list[str] = []
    used = 0
    for i, c in enumerate(commits):
        who = c["author"]["login"] if c.get("author") else (c.get("author_name") or "unknown")
        message = discord.utils.escape_markdown(c["message"]) or "*(no message)*"
        line = (
            f"[`{c['sha'][:7]}`]({c['html_url']}) {message} — {discord.utils.escape_markdown(who)}"
        )
        remaining = len(commits) - i
        tail = f"\n…and {remaining} more"
        if used + len(line) + 1 + len(tail) > DESCRIPTION_LIMIT:
            lines.append(tail.strip())
            break
        lines.append(line)
        used += len(line) + 1

    embed = discord.Embed(
        title=truncate(f"[{repo['full_name']}:{p['branch']}] {count} new {noun}", TITLE_LIMIT),
        url=p["compare_url"],
        description="\n".join(lines),
        color=COMMIT_COLOR,
    )
    _set_repo_author(embed, repo)
    return embed


def _set_repo_author(embed: discord.Embed, repo: dict[str, Any]) -> None:
    embed.set_author(
        name=truncate(repo["full_name"], AUTHOR_NAME_LIMIT),
        url=repo["html_url"],
        icon_url=repo.get("owner_avatar_url"),
    )


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
