"""`/repodrop settings`: an ephemeral panel whose controls save immediately.

Discord allows 5 component rows per message, so the design's "who can use /repodrop latest" and
"allow public /repodrop latest" controls share one select with four combined choices.
"""

from typing import Protocol

import discord

from repodrop.bot.checks import is_manager
from repodrop.db.models import EmbedStyle, LatestAccess
from repodrop.guild_settings import EffectiveSettings
from repodrop.observability import audit

PANEL_TIMEOUT = 300
REQUIRED_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links")

# value -> (label, latest_access, latest_allow_public)
LATEST_CHOICES: dict[str, tuple[str, str, bool]] = {
    "everyone:public": ("Anyone · can post publicly", LatestAccess.EVERYONE, True),
    "everyone:private": ("Anyone · private replies only", LatestAccess.EVERYONE, False),
    "managers:public": ("Managers only · can post publicly", LatestAccess.MANAGERS, True),
    "managers:private": ("Managers only · private replies only", LatestAccess.MANAGERS, False),
}
STYLE_CHOICES = {
    EmbedStyle.FULL: "Full: include release notes",
    EmbedStyle.COMPACT: "Compact: version and title only",
}


class SettingsStore(Protocol):
    async def get(self, guild_id: int) -> EffectiveSettings: ...
    async def update(self, guild_id: int, **values: object) -> EffectiveSettings: ...


# --------------------------------------------------------------------------- validation


def role_problem(role: discord.Role | None, *, purpose: str) -> str | None:
    """Why a role can't be used as the manager/subscriber role, or None if it can."""
    if role is None:
        return None
    if role.is_default():
        return f"@everyone can't be the {purpose} role. Clear the selection instead."
    if role.managed:
        return f"{role.name} is managed by an integration, so it can't be the {purpose} role."
    return None


def channel_problem(channel: object, bot_member: discord.Member) -> str | None:
    """Why a channel can't be the default channel, or None if it can."""
    if channel is None:
        return None
    if not isinstance(channel, discord.TextChannel):
        return "Pick a text or announcement channel."
    have = channel.permissions_for(bot_member)
    missing = [
        p.replace("_", " ").title() for p in REQUIRED_CHANNEL_PERMISSIONS if not getattr(have, p)
    ]
    if missing:
        return f"I'm missing {', '.join(missing)} in {channel.mention}."
    return None


def latest_choice(settings: EffectiveSettings) -> str:
    who = "managers" if settings.latest_access == LatestAccess.MANAGERS else "everyone"
    return f"{who}:{'public' if settings.latest_allow_public else 'private'}"


# --------------------------------------------------------------------------- rendering


def panel_embed(
    settings: EffectiveSettings, usage_text: str, notice: str | None = None
) -> discord.Embed:
    def role(role_id: int | None, default: str) -> str:
        return f"<@&{role_id}>" if role_id else default

    embed = discord.Embed(
        title="RepoDrop settings",
        description=notice,
        color=discord.Color.from_rgb(46, 160, 67) if notice is None else discord.Color.orange(),
    )
    embed.add_field(
        name="Manager role",
        value=role(settings.manager_role_id, "None: only Manage Server"),
        inline=True,
    )
    embed.add_field(
        name="Subscriber role",
        value=role(settings.subscriber_role_id, "None: everyone can subscribe"),
        inline=True,
    )
    embed.add_field(
        name="Default channel",
        value=f"<#{settings.default_channel_id}>"
        if settings.default_channel_id
        else "None: the channel the command is run in",
        inline=True,
    )
    embed.add_field(name="Embed style", value=STYLE_CHOICES[EmbedStyle(settings.embed_style)])
    embed.add_field(name="/repodrop latest", value=LATEST_CHOICES[latest_choice(settings)][0])
    embed.add_field(
        name="Set by the bot operator",
        value=f"{usage_text}\nCommit announcements "
        f"{'allowed' if settings.commits_allowed else 'not allowed'}",
        inline=False,
    )
    embed.set_footer(text="Changes save immediately.")
    return embed


class SettingsPanel(discord.ui.View):
    def __init__(
        self,
        store: SettingsStore,
        settings: EffectiveSettings,
        *,
        owner_id: int,
        can_change_manager_role: bool,
        usage_text: str,
    ) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.store = store
        self.settings = settings
        self.owner_id = owner_id
        self.can_change_manager_role = can_change_manager_role
        self.usage_text = usage_text
        self.origin: discord.Interaction | None = None  # set after sending, for on_timeout
        self._build()

    def embed(self, notice: str | None = None) -> discord.Embed:
        return panel_embed(self.settings, self.usage_text, notice)

    # ------------------------------------------------------------------ components

    def _build(self) -> None:
        self.clear_items()
        s = self.settings

        manager = discord.ui.RoleSelect(
            placeholder="Manager role"
            + ("" if self.can_change_manager_role else " (needs Manage Server to change)"),
            min_values=0,
            max_values=1,
            default_values=[discord.Object(s.manager_role_id)] if s.manager_role_id else [],
            disabled=not self.can_change_manager_role,
            row=0,
        )
        manager.callback = lambda i: self._on_role(i, manager, "manager_role_id", "manager")
        self.add_item(manager)

        subscriber = discord.ui.RoleSelect(
            placeholder="Subscriber role (empty = everyone can subscribe)",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(s.subscriber_role_id)] if s.subscriber_role_id else [],
            row=1,
        )
        subscriber.callback = lambda i: self._on_role(
            i, subscriber, "subscriber_role_id", "subscriber"
        )
        self.add_item(subscriber)

        channel = discord.ui.ChannelSelect(
            placeholder="Default channel (empty = the current channel)",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(s.default_channel_id)] if s.default_channel_id else [],
            row=2,
        )
        channel.callback = lambda i: self._on_channel(i, channel)
        self.add_item(channel)

        style = discord.ui.Select(
            options=[
                discord.SelectOption(label=label, value=value, default=value == s.embed_style)
                for value, label in STYLE_CHOICES.items()
            ],
            row=3,
        )
        style.callback = lambda i: self._save(i, embed_style=style.values[0])
        self.add_item(style)

        current = latest_choice(s)
        latest = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=f"/repodrop latest: {label}", value=value, default=value == current
                )
                for value, (label, _, _) in LATEST_CHOICES.items()
            ],
            row=4,
        )

        async def on_latest(interaction: discord.Interaction) -> None:
            _, access, public = LATEST_CHOICES[latest.values[0]]
            await self._save(interaction, latest_access=access, latest_allow_public=public)

        latest.callback = on_latest
        self.add_item(latest)

    # ------------------------------------------------------------------ callbacks

    async def _on_role(
        self,
        interaction: discord.Interaction,
        select: discord.ui.RoleSelect,
        column: str,
        purpose: str,
    ) -> None:
        role = select.values[0] if select.values else None
        if problem := role_problem(role, purpose=purpose):  # type: ignore[arg-type]
            await self._render(interaction, problem)
            return
        await self._save(interaction, **{column: role.id if role else None})

    async def _on_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        picked = select.values[0] if select.values else None
        guild = interaction.guild
        assert guild is not None
        channel = guild.get_channel(picked.id) if picked else None
        if problem := channel_problem(channel, guild.me):
            await self._render(interaction, problem)
            return
        await self._save(interaction, default_channel_id=channel.id if channel else None)

    async def _save(self, interaction: discord.Interaction, **values: object) -> None:
        # Re-check on every change: roles may have changed since the panel was opened.
        current = await self.store.get(self.settings.guild_id)
        member = interaction.user
        if not is_manager(member, current):  # type: ignore[arg-type]
            await self._render(interaction, "You're no longer a manager in this server.")
            return
        if "manager_role_id" in values and not member.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await self._render(
                interaction, "Only members with Manage Server can change the manager role."
            )
            return
        self.settings = await self.store.update(self.settings.guild_id, **values)
        audit(
            "changed server settings in {guild_id}",
            guild_id=self.settings.guild_id,
            user_id=member.id,
            changes={k: str(v) if v is not None else None for k, v in values.items()},
        )
        await self._render(interaction)

    async def _render(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(notice), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass
