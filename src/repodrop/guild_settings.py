"""Effective per-server settings (server override, else global default) with an in-memory cache."""

import time
from dataclasses import dataclass
from typing import Any

from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.models import EmbedStyle, GuildSettings, LatestAccess
from repodrop.db.queries import GuildUsage
from repodrop.db.session import SessionFactory


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    guild_id: int

    # Operator-controlled
    max_repos: int
    max_subscriptions: int
    commits_allowed: bool = True
    blocked: bool = False
    blocked_reason: str | None = None

    # Admin-controlled
    manager_role_id: int | None = None
    subscriber_role_id: int | None = None
    default_channel_id: int | None = None
    embed_style: str = EmbedStyle.FULL
    latest_access: str = LatestAccess.EVERYONE
    latest_allow_public: bool = True

    @classmethod
    def resolve(
        cls, guild_id: int, row: GuildSettings | None, config: Settings
    ) -> "EffectiveSettings":
        if row is None:
            return cls(
                guild_id=guild_id,
                max_repos=config.max_repos_per_guild,
                max_subscriptions=config.max_subs_per_guild,
            )
        return cls(
            guild_id=guild_id,
            max_repos=row.max_repos if row.max_repos is not None else config.max_repos_per_guild,
            max_subscriptions=(
                row.max_subscriptions
                if row.max_subscriptions is not None
                else config.max_subs_per_guild
            ),
            commits_allowed=row.commits_allowed,
            blocked=row.blocked,
            blocked_reason=row.blocked_reason,
            manager_role_id=row.manager_role_id,
            subscriber_role_id=row.subscriber_role_id,
            default_channel_id=row.default_channel_id,
            embed_style=row.embed_style,
            latest_access=row.latest_access,
            latest_allow_public=row.latest_allow_public,
        )


class GuildSettingsCache:
    """Reads are constant (every command, later every delivery), writes are rare.

    Writes through `update` invalidate immediately. The TTL only bounds how long a direct
    database edit takes to be noticed. If the bot is ever split into several processes,
    invalidate through Postgres LISTEN/NOTIFY instead.
    """

    def __init__(self, config: Settings, sessions: SessionFactory) -> None:
        self._config = config
        self._sessions = sessions
        self._ttl = config.guild_settings_cache_ttl.total_seconds()
        self._entries: dict[int, tuple[float, EffectiveSettings]] = {}

    async def get(self, guild_id: int) -> EffectiveSettings:
        cached = self._entries.get(guild_id)
        if cached is not None and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        async with self._sessions() as session:
            row = await queries.get_guild_settings(session, guild_id)
        effective = EffectiveSettings.resolve(guild_id, row, self._config)
        self._entries[guild_id] = (time.monotonic(), effective)
        return effective

    async def update(self, guild_id: int, **values: Any) -> EffectiveSettings:
        async with self._sessions.begin() as session:
            await queries.upsert_guild_settings(session, guild_id, **values)
        self.invalidate(guild_id)
        return await self.get(guild_id)

    def invalidate(self, guild_id: int) -> None:
        self._entries.pop(guild_id, None)


def cap_violation(
    usage: GuildUsage, settings: EffectiveSettings, *, repo_already_followed: bool
) -> str | None:
    """Why a new subscription would exceed the server's caps, or None if it fits.

    A repo the server already follows (in another channel) doesn't count against the repo cap
    again, since it's polled once regardless.
    """
    if usage.subscriptions >= settings.max_subscriptions:
        return (
            f"This server has reached its limit of {settings.max_subscriptions} subscriptions. "
            "Remove one with `/github unsubscribe` first."
        )
    if not repo_already_followed and usage.repos >= settings.max_repos:
        return (
            f"This server already follows {usage.repos} repos (limit {settings.max_repos}). "
            "Unsubscribe from one with `/github unsubscribe` first."
        )
    return None


def usage_line(usage: GuildUsage, settings: EffectiveSettings) -> str:
    return (
        f"Repos {usage.repos}/{settings.max_repos} · "
        f"Subscriptions {usage.subscriptions}/{settings.max_subscriptions}"
    )
