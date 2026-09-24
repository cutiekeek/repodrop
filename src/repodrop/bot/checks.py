"""Access rules for /github commands.

`/github` is visible to everyone (Discord can't gate subcommands individually), so access is
enforced here:

- A manager has Manage Server, or the server's manager role if one is set.
- Subscribe access belongs to everyone, unless the server sets a subscriber role. Managers
  always have it.
- A subscription can be changed or removed by its creator or a manager.
"""

from typing import TYPE_CHECKING, Protocol

import discord
from discord import app_commands

from repodrop.guild_settings import EffectiveSettings

if TYPE_CHECKING:
    from repodrop.bot.client import RepoDropBot


class AccessDenied(app_commands.CheckFailure):
    """Shown to the invoking member as-is."""


class _MemberLike(Protocol):
    id: int

    @property
    def guild_permissions(self) -> discord.Permissions: ...

    @property
    def roles(self) -> list[discord.Role]: ...


def _has_role(member: _MemberLike, role_id: int | None) -> bool:
    return role_id is not None and any(role.id == role_id for role in member.roles)


def is_manager(member: _MemberLike, settings: EffectiveSettings) -> bool:
    return member.guild_permissions.manage_guild or _has_role(member, settings.manager_role_id)


def has_subscribe_access(member: _MemberLike, settings: EffectiveSettings) -> bool:
    return (
        settings.subscriber_role_id is None
        or _has_role(member, settings.subscriber_role_id)
        or is_manager(member, settings)
    )


def can_modify(created_by: int, member: _MemberLike, settings: EffectiveSettings) -> bool:
    return created_by == member.id or is_manager(member, settings)


def blocked_notice(settings: EffectiveSettings, contact_url: str | None) -> str:
    notice = "RepoDrop has been disabled for this server"
    notice += f": {settings.blocked_reason}" if settings.blocked_reason else "."
    if contact_url:
        notice += f"\nQuestions? {contact_url}"
    return notice


async def settings_for(interaction: discord.Interaction) -> EffectiveSettings:
    bot: RepoDropBot = interaction.client  # type: ignore[assignment]
    assert interaction.guild_id is not None
    return await bot.guild_settings.get(interaction.guild_id)


def manager_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        settings = await settings_for(interaction)
        if not is_manager(interaction.user, settings):  # type: ignore[arg-type]
            raise AccessDenied(_manager_message(settings))
        return True

    return app_commands.check(predicate)


def subscriber_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        settings = await settings_for(interaction)
        if not has_subscribe_access(interaction.user, settings):  # type: ignore[arg-type]
            raise AccessDenied(
                f"Only members with the <@&{settings.subscriber_role_id}> role can subscribe "
                "in this server."
            )
        return True

    return app_commands.check(predicate)


def _manager_message(settings: EffectiveSettings) -> str:
    if settings.manager_role_id:
        return (
            f"Only members with Manage Server or the <@&{settings.manager_role_id}> role can "
            "use this."
        )
    return "Only members with Manage Server can use this."


def is_owner(user_id: int, owner_ids: list[int]) -> bool:
    """Operator commands: the invoker must be listed in OWNER_IDS (empty = nobody)."""
    return user_id in owner_ids
