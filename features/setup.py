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

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu
from utils.checks import is_eboard

# ── Interactive exclusion picker ─────────────────────────────────────────────

class _ExcludeSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Pick categories/channels to exclude from gating (optional)…",
            min_values=0,
            max_values=min(len(options), 25),
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _SetupView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.select = _ExcludeSelect(options)
        self.add_item(self.select)
        self.confirmed = False
        self.excluded_ids: set[int] = set()

    @discord.ui.button(label="Run setup", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.excluded_ids = {int(v) for v in self.select.values}
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="⚙️ Running setup…", view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Setup cancelled.", view=self)
        self.stop()


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

    async def _strip_roles(self, guild: discord.Guild, keep: set) -> int:
        """Remove every member's roles except those in `keep`. Skips @everyone,
        managed roles (bots/booster), and roles above TaigaBot (Discord won't let
        it touch those). Returns how many members were changed. DESTRUCTIVE."""
        bot_top = guild.me.top_role
        changed = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            to_remove = [
                r for r in member.roles
                if r != guild.default_role
                and not r.managed
                and r < bot_top
                and r not in keep
            ]
            if not to_remove:
                continue
            try:
                await member.remove_roles(
                    *to_remove, reason="TaigaBot: role reset before re-verification"
                )
                changed += 1
            except discord.Forbidden:
                pass
        return changed

    @app_commands.command(
        name="setup",
        description="(Server owner) Create TaigaBot's roles/channels and gate the server.",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        # /setup is powerful (and optionally destructive), so restrict it to the
        # server owner or any member with the Administrator permission — not all
        # Eboard members.
        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.administrator
        if member.id != guild.owner_id and not is_admin:
            await interaction.response.send_message(
                "⛔ Only the **server owner** or an **administrator** can run `/setup`.",
                ephemeral=True,
            )
            return

        # Build the exclusion picker from this guild's categories + channels.
        options: list[discord.SelectOption] = []
        for cat in guild.categories:
            options.append(discord.SelectOption(
                label=f"📁 {cat.name[:90]}",
                value=str(cat.id),
                description=f"Category — skips all {len(cat.channels)} channel(s) inside",
            ))
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            label = f"#{ch.name[:90]}" if isinstance(ch, discord.TextChannel) else f"🔊 {ch.name[:88]}"
            options.append(discord.SelectOption(
                label=label, value=str(ch.id), description="Single channel"
            ))

        # Pre-seed with any IDs already in the env so the user sees them selected.
        pre = [str(i) for i in config.GATING_IGNORE_IDS]

        if options:
            view = _SetupView(options)
            for opt in view.select.options:
                if opt.value in pre:
                    opt.default = True
            await interaction.response.send_message(
                "**TaigaBot Setup**\n"
                "Select any **categories or channels** you want to exclude from "
                "verification gating (e.g. a Projects category gated by interest roles). "
                "Leave blank to gate everything. Then click **Run setup**.",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.confirmed:
                return
            # Merge env-configured IDs with the user's interactive selection.
            ignore_ids = config.GATING_IGNORE_IDS | view.excluded_ids
        else:
            # No categories/channels yet — skip the picker.
            await interaction.response.defer(ephemeral=True, thinking=True)
            ignore_ids = config.GATING_IGNORE_IDS

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

        # 1.5 OPTIONAL, DESTRUCTIVE: wipe everyone's roles (except the functional
        #     ones) so old interest/self-assign roles no longer grant access until
        #     members re-verify and re-pick in #roles. Opt in via RESET_ROLES_ON_SETUP.
        if config.RESET_ROLES_ON_SETUP:
            keep = {unverified, verified, eboard}
            stripped = await self._strip_roles(guild, keep)
            steps.append(
                f"🧹 Role reset: removed non-essential roles from {stripped} member(s) "
                f"(kept {config.EBOARD_ROLE_NAME}/{config.VERIFIED_ROLE_NAME}/"
                f"{config.UNVERIFIED_ROLE_NAME} + bot-managed roles)."
            )

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
        # #roles: where verified members self-assign interest roles (set up with
        # /reactionrole). Visible + reactable to Verified, but read-only.
        roles_ch = await self._ensure_channel(guild, config.ROLES_CHANNEL_NAME)
        await self._set_perms(
            roles_ch, guild.default_role, view_channel=False, reason="TaigaBot setup"
        )
        await self._set_perms(
            roles_ch, verified, view_channel=True, send_messages=False,
            add_reactions=True, read_message_history=True, reason="TaigaBot setup",
        )
        steps.append(
            f"Channels ready: {unverified_ch.mention}, {welcome_ch.mention}, "
            f"{modlog_ch.mention}, {backups_ch.mention}, {roles_ch.mention}"
        )

        # 3. Gate every OTHER channel/category behind the Verified role
        #    (allowlist / default-deny): @everyone can't see it, Verified and
        #    Eboard can. A member with no roles therefore sees nothing until they
        #    verify — safe even if the bot was offline when they joined. Covers
        #    categories and voice channels, not just text.
        core_ids = {
            unverified_ch.id, welcome_ch.id, modlog_ch.id, backups_ch.id, roles_ch.id,
        }
        # ignore_ids was built above: env GATING_IGNORE merged with interactive picks
        gated = 0
        ignored = 0
        skipped_names: list[str] = []
        for ch in guild.channels:
            if ch.id in core_ids:
                continue
            # Leave alone anything in the ignore list, by its own id or its
            # category's id (so a category id skips all channels inside it).
            if ch.id in ignore_ids or getattr(ch, "category_id", None) in ignore_ids:
                ignored += 1
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
        if ignored:
            msg += f" Left {ignored} channel(s)/categor(ies) alone (excluded from gating)."
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

        # Count members who currently hold the Verified role in THIS guild, not
        # DB rows by guild_id — verification is global, so a member verified on
        # another server is promoted here (role granted) without a row for this
        # guild, and a DB count would wrongly read 0.
        vrole = gu.verified_role(guild)
        verified_count = len(vrole.members) if vrole else 0
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
