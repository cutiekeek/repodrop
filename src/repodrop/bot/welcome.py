"""The welcome message posted once when RepoDrop is added to a server."""

from typing import Protocol

import discord

from repodrop.announcer.embeds import link_view
from repodrop.guild_settings import EffectiveSettings

REQUIRED = ("view_channel", "send_messages", "embed_links")
WELCOME_COLOR = discord.Color.from_rgb(46, 160, 67)


class _Channel(Protocol):
    position: int

    def permissions_for(self, member: discord.Member, /) -> discord.Permissions: ...


def _can_post(channel: _Channel, me: discord.Member) -> bool:
    have = channel.permissions_for(me)
    return all(getattr(have, p) for p in REQUIRED)


def pick_welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """The server's system channel if the bot can post there, else the first text channel
    (by position) where it can. None if there's nowhere to post."""
    me = guild.me
    if guild.system_channel is not None and _can_post(guild.system_channel, me):
        return guild.system_channel
    for channel in sorted(guild.text_channels, key=lambda c: c.position):
        if _can_post(channel, me):
            return channel
    return None


def welcome_message(
    settings: EffectiveSettings, docs_url: str | None
) -> tuple[discord.Embed, discord.ui.View | None]:
    embed = discord.Embed(
        title="Thanks for adding RepoDrop!",
        description=(
            "I announce GitHub releases, tags, and commits in your channels. "
            "Here's how to get started:"
        ),
        color=WELCOME_COLOR,
    )
    embed.add_field(
        name="1. Subscribe a channel",
        value=(
            "Run this in the channel where announcements should go:\n"
            "`/repodrop subscribe repo:owner/name`\n"
            "You get releases by default; add `tags:true` or `commits:true` for more. "
            "Nothing old is re-announced, only what's new from now on."
        ),
        inline=False,
    )
    embed.add_field(
        name="2. Check your setup",
        value=(
            "`/repodrop list` shows your subscriptions, and `/repodrop test` previews an "
            "announcement."
        ),
        inline=False,
    )
    embed.add_field(
        name="3. Adjust settings (managers)",
        value=(
            "Everyone can subscribe by default. `/repodrop settings` lets members with "
            "Manage Server limit that to a role, pick a manager role and a default channel, "
            "and switch to a compact announcement style."
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"This server can follow up to {settings.max_repos} repos across "
            f"{settings.max_subscriptions} subscriptions. Announcements never ping anyone."
        )
    )
    view = link_view([("Getting started", docs_url)]) if docs_url else None
    return embed, view
