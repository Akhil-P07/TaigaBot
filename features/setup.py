"""Server setup & admin utilities.

`/setup` is the one-time command you run after inviting TaigaBot:
  • creates the Unverified / Verified / Eboard roles if missing
  • creates the #unverified, #welcome and #mod-log channels if missing
  • locks down channels so Unverified members can only see/talk in #unverified
  • assigns the Unverified role to EVERY existing member who isn't verified yet

This is the piece that handles "the server already has a bunch of members".
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu
from utils.checks import is_eboard


class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ensure_role(self, guild: discord.Guild, name: str, **kwargs) -> discord.Role:
        role = gu.get_role(guild, name)
        if role is None:
            role = await guild.create_role(name=name, reason="TaigaBot setup", **kwargs)
        return role

    async def _ensure_channel(
        self, guild: discord.Guild, name: str, overwrites=None
    ) -> discord.TextChannel:
        ch = gu.get_channel(guild, name)
        if ch is None:
            ch = await guild.create_text_channel(
                name, overwrites=overwrites or {}, reason="TaigaBot setup"
            )
        return ch

    @app_commands.command(
        name="setup",
        description="Create TaigaBot's roles/channels and assign Unverified to existing members.",
    )
    @app_commands.default_permissions(administrator=True)
    @is_eboard()
    async def setup_cmd(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        steps: list[str] = []

        # 1. Roles
        unverified = await self._ensure_role(
            guild, config.UNVERIFIED_ROLE_NAME, color=discord.Color.light_grey()
        )
        verified = await self._ensure_role(
            guild, config.VERIFIED_ROLE_NAME, color=discord.Color.green()
        )
        eboard = await self._ensure_role(
            guild, config.EBOARD_ROLE_NAME, color=discord.Color.gold(), hoist=True
        )
        steps.append(f"Roles ready: {unverified.mention}, {verified.mention}, {eboard.mention}")

        # 2. Channels with permission overwrites.
        # #unverified: ONLY unverified members can talk; verified members can't see it.
        unverified_ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            unverified: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        unverified_ch = await self._ensure_channel(
            guild, config.UNVERIFIED_CHANNEL_NAME, unverified_ow
        )
        welcome_ch = await self._ensure_channel(guild, config.WELCOME_CHANNEL_NAME)
        modlog_ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            eboard: discord.PermissionOverwrite(view_channel=True),
        }
        modlog_ch = await self._ensure_channel(guild, config.MODLOG_CHANNEL_NAME, modlog_ow)
        steps.append(
            f"Channels ready: {unverified_ch.mention}, {welcome_ch.mention}, {modlog_ch.mention}"
        )

        # 3. Lock every OTHER channel so Unverified members can't talk there.
        locked = 0
        for ch in guild.text_channels:
            if ch.id == unverified_ch.id:
                continue
            try:
                await ch.set_permissions(
                    unverified, send_messages=False, add_reactions=False,
                    reason="TaigaBot: restrict unverified members",
                )
                locked += 1
            except discord.Forbidden:
                pass
        steps.append(f"Restricted Unverified from talking in {locked} other channel(s).")

        # 4. Assign Unverified to all existing members who aren't verified.
        assigned = 0
        skipped_verified = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            if gu.verified_role(guild) in member.roles or await self.bot.db.user_is_verified(
                member.id
            ):
                skipped_verified += 1
                continue
            if unverified not in member.roles:
                try:
                    await member.add_roles(unverified, reason="TaigaBot setup backfill")
                    assigned += 1
                except discord.Forbidden:
                    pass
        steps.append(
            f"Assigned **{config.UNVERIFIED_ROLE_NAME}** to {assigned} member(s) "
            f"({skipped_verified} already verified/skipped)."
        )

        embed = discord.Embed(
            title="✅ TaigaBot setup complete",
            description="\n".join(f"• {s}" for s in steps),
            color=config.BOT_COLOR,
        )
        embed.set_footer(
            text="Make sure TaigaBot's role is ABOVE Unverified/Verified in Server Settings → Roles."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="health", description="Show TaigaBot's configuration & role/channel status."
    )
    @is_eboard()
    async def health(self, interaction: discord.Interaction):
        guild = interaction.guild

        def status(obj, label):
            return f"{'✅' if obj else '❌'} {label}"

        verified_count = await self.bot.db.count_verified(guild.id)
        lines = [
            status(gu.eboard_role(guild), f"Eboard role (`{config.EBOARD_ROLE_NAME}`)"),
            status(gu.unverified_role(guild), f"Unverified role (`{config.UNVERIFIED_ROLE_NAME}`)"),
            status(gu.verified_role(guild), f"Verified role (`{config.VERIFIED_ROLE_NAME}`)"),
            status(gu.get_channel(guild, config.UNVERIFIED_CHANNEL_NAME), "#unverified channel"),
            status(gu.welcome_channel(guild), "#welcome channel"),
            status(gu.modlog_channel(guild), "#mod-log channel"),
            status(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD, "Email (OTP) configured"),
        ]
        embed = discord.Embed(
            title="🐯 TaigaBot health",
            description="\n".join(lines),
            color=config.BOT_COLOR,
        )
        embed.add_field(name="Verified members", value=str(verified_count))
        embed.add_field(
            name="Allowed email domains",
            value=", ".join(f"@{d}" for d in config.ALLOWED_EMAIL_DOMAINS),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Setup(bot))
