"""Embed and link-button builders per event kind. Input is the `events.payload` JSON.

Shared by announcements, `/repodrop test` and `/repodrop latest`, so they all look the same.
Payloads stored before a field existed (e.g. `previous_tag`) still render; the matching
button is just left out.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import discord

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
AUTHOR_NAME_LIMIT = 256
FOOTER_LIMIT = 2048
BUTTON_LABEL_LIMIT = 80
BUTTON_URL_LIMIT = 512

RELEASE_COLOR = discord.Color.from_rgb(46, 160, 67)
PRERELEASE_COLOR = discord.Color.from_rgb(210, 153, 34)
TAG_COLOR = discord.Color.from_rgb(130, 80, 223)
COMMIT_COLOR = discord.Color.from_rgb(9, 105, 218)


@dataclass(frozen=True, slots=True)
class Presentation:
    """Per-server display settings (see guild_settings)."""

    embed_style: str = "full"  # "full" | "compact"


DEFAULT_PRESENTATION = Presentation()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)].rstrip() + suffix


def build_message(
    kind: str, payload: dict[str, Any], presentation: Presentation = DEFAULT_PRESENTATION
) -> tuple[discord.Embed, discord.ui.View | None]:
    """The embed plus its link buttons (None when there are none)."""
    match kind:
        case "release":
            embed = release_embed(payload, compact=presentation.embed_style == "compact")
            buttons = release_buttons(payload)
        case "tag":
            embed, buttons = tag_embed(payload), tag_buttons(payload)
        case "commit":
            embed, buttons = commit_embed(payload), commit_buttons(payload)
        case _:
            raise ValueError(f"unknown event kind {kind!r}")
    return embed, link_view(buttons)


def build_embed(kind: str, payload: dict[str, Any]) -> discord.Embed:
    return build_message(kind, payload)[0]


# --------------------------------------------------------------------------- releases


def release_embed(p: dict[str, Any], *, compact: bool = False) -> discord.Embed:
    """Full: notes included (truncated with a link). Compact: version, title and time only."""
    repo = p["repo"]
    body = "" if compact else (p.get("body") or "").strip()
    more = f"\n\n[Read the full release notes]({p['html_url']})"
    if body and (p.get("body_truncated") or len(body) > DESCRIPTION_LIMIT):
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
        footer += f" · by {author['login']}"
    embed.set_footer(
        text=truncate(footer, FOOTER_LIMIT), icon_url=author["avatar_url"] if author else None
    )
    return embed


def release_buttons(p: dict[str, Any]) -> list[tuple[str, str]]:
    """View release, and Compare when there's a previous tag."""
    buttons = [("View release", p["html_url"])]
    if compare := _compare_url(p["repo"], p.get("previous_tag"), p["tag_name"]):
        buttons.append(("Compare", compare))
    return buttons


# --------------------------------------------------------------------------- tags


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


def tag_buttons(p: dict[str, Any]) -> list[tuple[str, str]]:
    buttons = [("View tag", p["html_url"])]
    if compare := _compare_url(p["repo"], p.get("previous_tag"), p["name"]):
        buttons.append(("Compare", compare))
    return buttons


# --------------------------------------------------------------------------- commits


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


def commit_buttons(p: dict[str, Any]) -> list[tuple[str, str]]:
    return [("View commits", p["compare_url"])]


# --------------------------------------------------------------------------- helpers


def link_view(buttons: list[tuple[str, str]]) -> discord.ui.View | None:
    """Link buttons open URLs directly: no handlers, and they keep working across restarts.

    Every message has at most 2 buttons; the cap of 5 is Discord's per-row limit.
    """
    usable = [
        (truncate(label, BUTTON_LABEL_LIMIT), url)
        for label, url in buttons
        if url.startswith(("https://", "http://")) and len(url) <= BUTTON_URL_LIMIT
    ][:5]
    if not usable:
        return None
    view = discord.ui.View(timeout=None)
    for label, url in usable:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url))
    return view


def _compare_url(repo: dict[str, Any], base: str | None, head: str) -> str | None:
    if not base:
        return None
    return f"{repo['html_url']}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}"


def _set_repo_author(embed: discord.Embed, repo: dict[str, Any]) -> None:
    embed.set_author(
        name=truncate(repo["full_name"], AUTHOR_NAME_LIMIT),
        url=repo["html_url"],
        icon_url=repo.get("owner_avatar_url"),
    )


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
