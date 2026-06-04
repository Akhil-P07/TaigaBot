"""Helpers for resolving (and creating) the roles/channels TaigaBot relies on.

Roles and channels are looked up by the names configured in `config`. This keeps
the bot working out-of-the-box while letting the club rename things via .env.
"""
from __future__ import annotations

import discord

import config


def get_role(guild: discord.Guild, name: str) -> discord.Role | None:
    n = name.lower()
    return discord.utils.find(lambda r: r.name.lower() == n, guild.roles)


def get_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    n = name.lower()
    return discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == n,
        guild.channels,
    )


def eboard_role(guild: discord.Guild) -> discord.Role | None:
    return get_role(guild, config.EBOARD_ROLE_NAME)


def unverified_role(guild: discord.Guild) -> discord.Role | None:
    return get_role(guild, config.UNVERIFIED_ROLE_NAME)


def verified_role(guild: discord.Guild) -> discord.Role | None:
    return get_role(guild, config.VERIFIED_ROLE_NAME)


def welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.WELCOME_CHANNEL_NAME)


def modlog_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.MODLOG_CHANNEL_NAME)


async def log_mod_action(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post an embed to the mod-log channel if it exists."""
    ch = modlog_channel(guild)
    if ch is not None:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass
