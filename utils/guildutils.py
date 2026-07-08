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


def project_lead_role(guild: discord.Guild) -> discord.Role | None:
    return get_role(guild, config.PROJECT_LEAD_ROLE_NAME)


async def ensure_project_lead_role(guild: discord.Guild) -> discord.Role | None:
    """Resolve the shared Project Lead role, creating it if the server doesn't
    have one yet. Returns None if the bot lacks Manage Roles."""
    role = project_lead_role(guild)
    if role is None:
        try:
            role = await guild.create_role(
                name=config.PROJECT_LEAD_ROLE_NAME,
                reason="TaigaBot: shared role for all project leads",
            )
        except discord.Forbidden:
            return None
    return role


def welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.WELCOME_CHANNEL_NAME)


def modlog_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.MODLOG_CHANNEL_NAME)


def backups_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.BACKUP_CHANNEL_NAME)


async def promote_to_verified(member: discord.Member) -> bool:
    """Give the member the Verified role and strip Unverified, in their guild.

    Returns False if the bot lacks permission (its role is too low). Safe to call
    when the member is already verified — it just ensures the roles are right.
    """
    verified = verified_role(member.guild)
    unverified = unverified_role(member.guild)
    try:
        if verified and verified not in member.roles:
            await member.add_roles(verified, reason="TaigaBot: verified")
        if unverified and unverified in member.roles:
            await member.remove_roles(unverified, reason="TaigaBot: verified")
        return True
    except discord.Forbidden:
        return False


async def demote_to_unverified(member: discord.Member, reason: str = "TaigaBot: unverified") -> bool:
    """Inverse of promote_to_verified: strip the Verified role and (re)apply
    Unverified. Used when a member's verification is removed or transferred away.
    Returns False if the bot lacks permission (its role is too low)."""
    verified = verified_role(member.guild)
    unverified = unverified_role(member.guild)
    try:
        if verified and verified in member.roles:
            await member.remove_roles(verified, reason=reason)
        if unverified and unverified not in member.roles:
            await member.add_roles(unverified, reason=reason)
        return True
    except discord.Forbidden:
        return False


async def log_mod_action(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post an embed to the mod-log channel if it exists."""
    ch = modlog_channel(guild)
    if ch is not None:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass
