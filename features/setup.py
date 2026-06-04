"""Server setup & admin utilities.

`/setup` is the one-time command you run after inviting TaigaBot:
  • creates the Unverified / Verified / Eboard roles if missing
  • creates the #unverified, #welcome and #mod-log channels if missing
  • gates every channel behind the Verified role (default-deny): @everyone can't
    see them, only Verified/Eboard can. #unverified is the verification landing;
    #welcome is a public, read-only entry point anyone can verify from.
  • assigns the Unverified role to EVERY existing member who isn't verified yet

Default-deny means a member with no roles sees nothing until they verify — safe
even if the bot was offline when they joined. Re-run any time; it's idempotent
and migrates an existing server. This handles "the server already has members".
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

    @staticmethod
    async def _set_perms(channel, target, **perms) -> bool:
        """set_permissions that returns False instead of raising when the bot
        can't edit this channel (e.g. a private channel it has no access to)."""
        try:
            await channel.set_permissions(target, **perms)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

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

        # 0. Bail early with a clear message if the bot lacks the permissions it
        #    needs — otherwise the first API call fails with a cryptic 403.
        # Note: to edit a channel's view_channel/use_application_commands
        # overwrites, the bot must HOLD those permissions itself (Discord blocks
        # granting perms you don't have, unless you're an Administrator).
        perms = guild.me.guild_permissions
        missing = [
            label for label, ok in (
                ("Manage Roles", perms.manage_roles),
                ("Manage Channels", perms.manage_channels),
                ("View Channels", perms.view_channel),
                ("Use Application Commands", perms.use_application_commands),
            ) if not ok
        ]
        if missing:
            await interaction.followup.send(
                f"⛔ I'm missing the **{', '.join(missing)}** permission(s), so I "
                "can't set up the server.\n\n"
                "Fix: in **Server Settings → Roles**, give TaigaBot's role those "
                "permissions and drag it **above** the Unverified/Verified roles, "
                "then re-run `/setup`. (Re-inviting with the correct OAuth link also "
                "works.)",
                ephemeral=True,
            )
            return

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

        # 2. Core channels. Overwrites are applied explicitly (not just on
        #    creation) so re-running /setup migrates an existing server too.
        # #unverified: the verification landing — only Unverified members see/talk.
        unverified_ch = await self._ensure_channel(guild, config.UNVERIFIED_CHANNEL_NAME)
        await self._set_perms(
            unverified_ch, guild.default_role, view_channel=False, reason="TaigaBot setup"
        )
        await self._set_perms(
            unverified_ch, unverified, view_channel=True, send_messages=True,
            read_message_history=True, use_application_commands=True,
            reason="TaigaBot setup",
        )
        # #welcome: visible to EVERYONE (even role-less members who joined while
        # the bot was offline) but read-only — commands stay enabled so anyone can
        # run /verify and /verifyhelp here. This is the universal entry point.
        welcome_ch = await self._ensure_channel(guild, config.WELCOME_CHANNEL_NAME)
        await self._set_perms(
            welcome_ch, guild.default_role, view_channel=True, send_messages=False,
            add_reactions=False, use_application_commands=True,
            reason="TaigaBot: public verification entry point",
        )
        # #mod-log: Eboard only.
        modlog_ch = await self._ensure_channel(guild, config.MODLOG_CHANNEL_NAME)
        await self._set_perms(
            modlog_ch, guild.default_role, view_channel=False, reason="TaigaBot setup"
        )
        await self._set_perms(modlog_ch, eboard, view_channel=True, reason="TaigaBot setup")
        # #taiga-backups: Eboard only — holds DB snapshots (names + emails).
        backups_ch = await self._ensure_channel(guild, config.BACKUP_CHANNEL_NAME)
        await self._set_perms(
            backups_ch, guild.default_role, view_channel=False, reason="TaigaBot setup"
        )
        await self._set_perms(backups_ch, eboard, view_channel=True, reason="TaigaBot setup")
        steps.append(
            f"Channels ready: {unverified_ch.mention}, {welcome_ch.mention}, "
            f"{modlog_ch.mention}, {backups_ch.mention}"
        )

        # 3. Gate every OTHER channel/category behind the Verified role
        #    (allowlist / default-deny): @everyone can't see it, Verified and
        #    Eboard can. A member with no roles therefore sees nothing until they
        #    verify — safe even if the bot was offline when they joined. Covers
        #    categories and voice channels, not just text.
        core_ids = {unverified_ch.id, welcome_ch.id, modlog_ch.id, backups_ch.id}
        gated = 0
        skipped_names: list[str] = []
        for ch in guild.channels:
            if ch.id in core_ids:
                continue
            ok = await self._set_perms(
                ch, guild.default_role, view_channel=False,
                reason="TaigaBot: gate behind verification",
            )
            ok &= await self._set_perms(
                ch, verified, view_channel=True,
                reason="TaigaBot: verified members can view",
            )
            ok &= await self._set_perms(
                ch, eboard, view_channel=True, reason="TaigaBot: eboard can view"
            )
            if ok:
                gated += 1
            else:
                skipped_names.append(ch.name)
        msg = f"Gated {gated} channel(s) behind the **{config.VERIFIED_ROLE_NAME}** role."
        if skipped_names:
            shown = ", ".join(f"`{n}`" for n in skipped_names[:10])
            extra = f" (+{len(skipped_names) - 10} more)" if len(skipped_names) > 10 else ""
            msg += (
                f"\n⚠️ Couldn't edit {len(skipped_names)} channel(s): {shown}{extra}. "
                "These are private channels I can't access — grant TaigaBot **View "
                "Channel** on them (or run /setup once with Administrator), then re-run."
            )
        steps.append(msg)

        # 4. Reconcile existing members:
        #    • already verified (in the DB) → give them the Verified role here
        #      (covers people who verified before, or on another server this bot
        #      runs, since on_member_join doesn't fire for members already present)
        #    • everyone else → give them Unverified
        assigned = 0   # received Unverified
        promoted = 0   # received Verified (were verified already)
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            if await self.bot.db.user_is_verified(member.id):
                had_role = verified in member.roles
                await gu.promote_to_verified(member)  # idempotent; also strips Unverified
                if not had_role:
                    promoted += 1
                continue
            if unverified not in member.roles:
                try:
                    await member.add_roles(unverified, reason="TaigaBot setup backfill")
                    assigned += 1
                except discord.Forbidden:
                    pass
        steps.append(
            f"Assigned **{config.UNVERIFIED_ROLE_NAME}** to {assigned} member(s); "
            f"granted **{config.VERIFIED_ROLE_NAME}** to {promoted} already-verified member(s)."
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
