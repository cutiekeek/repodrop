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
    """Explain expected failures to the user; log anything unexpected."""
    original = getattr(error, "original", error)
    if isinstance(original, UserError | AccessDenied):
        message = str(original)
    elif isinstance(original, app_commands.CommandOnCooldown):
        message = f"Slow down a little. Try again in {original.retry_after:.0f} seconds."
    else:
        logfire.exception(
            "command {command} failed",
            command=getattr(interaction.command, "qualified_name", None),
            _exc_info=original,
        )
        message = "Something went wrong on my end. Please try again."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
