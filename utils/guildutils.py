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


def general_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return get_channel(guild, config.GENERAL_CHANNEL_NAME)


def _can_post(guild: discord.Guild, channel: discord.TextChannel | None) -> bool:
    if channel is None:
        return False
    perms = channel.permissions_for(guild.me)
    return perms.view_channel and perms.send_messages


def first_sendable_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """The best channel for an announcement that MUST land somewhere: mod-log,
    then general, then the server's system channel, then any text channel the bot
    can actually post in. Returns None if the bot can post nowhere.

    Unlike `modlog_channel`, this never gives up just because the configured
    channel is missing — it's for messages (e.g. "the bot is leaving") that have
    no second chance to be delivered.
    """
    preferred = (modlog_channel(guild), general_channel(guild), guild.system_channel)
    for channel in preferred:
        if isinstance(channel, discord.TextChannel) and _can_post(guild, channel):
            return channel
    for channel in guild.text_channels:
        if _can_post(guild, channel):
            return channel
    return None


async def announce_to_guild(
    guild: discord.Guild, embed: discord.Embed, plain: str
) -> discord.TextChannel | None:
    """Post `embed` to `first_sendable_channel`, falling back to `plain` text if
    the channel forbids embeds. Returns the channel it landed in, or None.

    Best-effort by design: callers use this immediately before something
    irreversible (leaving the server), so a delivery failure must never raise.
    """
    channel = first_sendable_channel(guild)
    if channel is None:
        return None
    try:
        await channel.send(embed=embed)
        return channel
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await channel.send(plain)
        return channel
    except (discord.Forbidden, discord.HTTPException):
        return None


def _site_line(suffix: str = "") -> str:
    """'…from https://taigabot.example' — or nothing when PUBLIC_BASE_URL is unset,
    the same way web.server._invite_url degrades."""
    return f"{config.PUBLIC_BASE_URL}{suffix}" if config.PUBLIC_BASE_URL else ""


def leaving_embed(reason: str) -> tuple[discord.Embed, str]:
    """The notice posted in a server just before the bot is ejected from it.
    Returns (embed, plain-text equivalent). Deliberately does not name the
    maintainer who ejected the server."""
    site = _site_line()
    tail = (
        f"A server admin can re-invite the bot at any time from {site}"
        if site else "A server admin can re-invite the bot at any time."
    )
    embed = discord.Embed(
        title="🚪 TaigaBot is leaving this server",
        description=tail,
        colour=discord.Colour.orange(),
    )
    # Discord rejects an empty field value; bans made without a reason are possible.
    embed.add_field(name="Reason", value=reason or "No reason given.", inline=False)
    return embed, f"**TaigaBot is leaving this server.**\nReason: {reason}\n{tail}"


def blocked_embed(reason: str) -> tuple[discord.Embed, str]:
    """The notice posted when a banned server invites the bot back."""
    site = _site_line()
    tail = (
        f"Think this is a mistake? Open a ticket at {site}"
        if site else "Think this is a mistake? Contact the bot maintainer."
    )
    embed = discord.Embed(
        title="🚫 TaigaBot can't be added to this server",
        description=f"This server has been blocked from using TaigaBot.\n\n{tail}",
        colour=discord.Colour.red(),
    )
    # Discord rejects an empty field value; bans made without a reason are possible.
    embed.add_field(name="Reason", value=reason or "No reason given.", inline=False)
    return embed, (
        f"**This server has been blocked from using TaigaBot.**\n"
        f"Reason: {reason}\n{tail}"
    )


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
