"""Error replies shared by every command cog."""

import discord
import logfire
from discord import app_commands

from repodrop.bot.checks import AccessDenied


class UserError(Exception):
    """Shown to the invoking user as-is."""


async def reply_with_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Explain expected refusals to the user and log them; log anything unexpected as an error."""
    original = getattr(error, "original", error)
    command = getattr(interaction.command, "qualified_name", None)
    if isinstance(original, UserError | AccessDenied | app_commands.CommandOnCooldown):
        if isinstance(original, app_commands.CommandOnCooldown):
            message = f"Slow down a little. Try again in {original.retry_after:.0f} seconds."
        else:
            message = str(original)
        # No access, cooldown, or bad input: useful context when someone asks for help.
        logfire.info(
            "/{command} refused for {user_id}: {reason}",
            command=command,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            refusal=type(original).__name__,
            reason=message,
        )
    else:
        logfire.exception(
            "/{command} failed for {user_id}",
            command=command,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            _exc_info=original,
        )
        message = "Something went wrong on my end. Please try again."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
