"""Permission checks shared across features.

The headline requirement: moderator commands may only be run by members who
hold the **Eboard** role. `is_eboard()` is an app-command check that inspects
the *invoking member's* roles, so it works no matter which command uses it.
"""
from __future__ import annotations

import discord
from discord import app_commands

import config


def member_has_role(member: discord.Member, role_name: str) -> bool:
    """True if the member has a role with the given (case-insensitive) name."""
    name = role_name.lower()
    return any(r.name.lower() == name for r in member.roles)


class NotEboard(app_commands.CheckFailure):
    """Raised when a non-Eboard member tries to use a mod command."""


class NotBotOwner(app_commands.CheckFailure):
    """Raised when someone other than the bot owner tries to use an owner command."""


def is_bot_owner():
    """App-command check: caller must own this bot instance.

    This is deliberately stricter than `is_eboard()` — server administrators do
    NOT pass. Eboard is per-server authority; this is authority over the bot
    itself, used for things like granting premium, which no server should be
    able to give itself.

    Identity comes from BOT_OWNER_IDS when set, otherwise from the Discord
    application's owner (or team members) as reported by `bot.is_owner`.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        if config.BOT_OWNER_IDS:
            if interaction.user.id in config.BOT_OWNER_IDS:
                return True
            raise NotBotOwner("This command is restricted to the bot owner.")
        # No explicit allowlist — fall back to the application owner/team.
        if await interaction.client.is_owner(interaction.user):
            return True
        raise NotBotOwner("This command is restricted to the bot owner.")

    return app_commands.check(predicate)


def is_eboard():
    """App-command check: caller must have the configured Eboard role.

    Server administrators always pass, so the owner isn't locked out before
    the Eboard role exists.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise NotEboard("This command can only be used in a server.")
        if member.guild_permissions.administrator:
            return True
        if member_has_role(member, config.EBOARD_ROLE_NAME):
            return True
        raise NotEboard(
            f"You need the **{config.EBOARD_ROLE_NAME}** role to use this command."
        )

    return app_commands.check(predicate)
