"""Off-box, per-guild member-roster backups — guards against host filesystem wipes.

A normal crash, restart, or sleep never loses data: SQLite commits to disk on
every write, so the file is intact when the bot wakes up. The real risk is the
host (e.g. a free Replit/Render container) being rebuilt from scratch, which
wipes the file entirely. This feature periodically uploads a CSV roster of the
guild's current verified members to an Eboard-only Discord channel, so the
membership record survives even a full container reset.

Each backup is a CSV of the guild's current members who hold the Verified role
(admins are omitted — they're already visible in Discord), with each member's
verified real name and email regardless of which server they verified in —
the roster's job is to tell this server's Eboard who its members really are.

An earlier version also attached a per-guild `.db` snapshot for full restores,
but it leaked cross-server data (the levels table is global, so every server's
backup carried every user's XP and ids) — removed; don't reintroduce a raw DB
export without auditing every table it copies.

/setup creates the Eboard-only backup channel (named BACKUP_CHANNEL_NAME) in
each server.
"""
from __future__ import annotations

import csv
import logging
import os
import tempfile
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import guildutils as gu
from utils.checks import is_eboard

log = logging.getLogger("taigabot.backup")


async def build_guild_backup(db, guild: discord.Guild):
    """Create this guild's backup files. Returns (files, roster_count).

    `files` is a list of (temp_path, upload_filename); the caller sends them and
    then deletes the temp paths. It contains a CSV roster of the guild's current
    verified members.

    A current member is rostered if they hold the Verified role. Admins are
    omitted (they're already visible in Discord). Members who verified on another
    server still appear WITH their real name/email (joining here auto-grants
    them the Verified role) — intentional: any server's Eboard may see who its
    verified members are.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    verified_role = config.VERIFIED_ROLE_NAME.lower()
    roster_path = os.path.join(tempfile.gettempdir(), f"roster-{guild.id}-{ts}.csv")
    count = 0
    with open(roster_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "display_name", "username", "user_id",
            "verified_in_db", "real_name", "email",
        ])
        async for m in guild.fetch_members(limit=None):
            if m.bot:
                continue
            has_role = any(r.name.lower() == verified_role for r in m.roles)
            if not has_role:  # verified members only — admins are already visible in Discord
                continue
            info = await db.get_verified_user(m.id)  # fill name/email if on record
            writer.writerow([
                m.display_name, str(m), m.id, info is not None,
                info["real_name"] if info else "",
                info["email"] if info else "",
            ])
            count += 1

    return [(roster_path, f"roster-{guild.id}-{ts}.csv")], count


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_backup.change_interval(hours=config.BACKUP_INTERVAL_HOURS)
        self.auto_backup.start()

    def cog_unload(self) -> None:
        self.auto_backup.cancel()

    def _channel_for(self, guild: discord.Guild):
        """The Eboard-only backup channel for this guild, or None. An explicit
        BACKUP_CHANNEL_ID is honoured only if it lives in this same guild;
        otherwise the channel named BACKUP_CHANNEL_NAME that /setup creates."""
        if config.BACKUP_CHANNEL_ID:
            ch = self.bot.get_channel(config.BACKUP_CHANNEL_ID)
            if ch is not None and getattr(ch, "guild", None) == guild:
                return ch
        return gu.backups_channel(guild)

    async def _backup_guild(self, guild: discord.Guild) -> int | None:
        """Build this guild's verified-member roster and upload it to its backup
        channel. Returns the roster count, or None if there's no backup channel."""
        channel = self._channel_for(guild)
        if channel is None:
            return None
        # Make sure we can actually post here before doing the work — otherwise
        # we'd 403 mid-upload. (e.g. a #taiga-backups the bot can't access.)
        me_perms = channel.permissions_for(guild.me)
        if not (me_perms.view_channel and me_perms.send_messages and me_perms.attach_files):
            log.warning(
                "Backup skipped for '%s' (id=%s) — I can't post in #%s. Run /setup in that server.",
                guild.name, guild.id, channel.name,
            )
            return None
        files_meta, count = await build_guild_backup(self.bot.db, guild)
        try:
            files = [discord.File(p, filename=n) for p, n in files_meta]
            ts = time.strftime("%Y%m%d-%H%M%S")
            await channel.send(
                content=(
                    f"🗄️ Backup for **{guild.name}** — {ts} "
                    f"(roster of {count} verified member(s))"
                ),
                files=files,
            )
        except discord.Forbidden:
            log.warning(
                "Backup skipped for '%s' (id=%s) — I can't post in #%s. Run /setup in that server.",
                guild.name, guild.id, channel.name,
            )
            return None
        finally:
            for path, _ in files_meta:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return count

    @tasks.loop(hours=12)  # real interval set from config in __init__
    async def auto_backup(self) -> None:
        backed_up = 0
        for guild in self.bot.guilds:
            try:
                if await self._backup_guild(guild) is not None:
                    backed_up += 1
            except Exception:  # noqa: BLE001
                log.exception("Automatic backup failed for guild %s.", guild.id)
        if backed_up:
            log.info("Uploaded per-guild backups for %d guild(s).", backed_up)
        else:
            log.info(
                "No backup channels yet (run /setup to create #%s); skipping.",
                config.BACKUP_CHANNEL_NAME,
            )

    @auto_backup.before_loop
    async def _before_auto_backup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="backup",
        description="Back up THIS server's member roster to its backup channel now (Eboard only).",
    )
    @is_eboard()
    async def backup_now(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return
        if self._channel_for(guild) is None:
            await interaction.response.send_message(
                f"⚠️ No backup channel here. Run `/setup` to create the "
                f"`#{config.BACKUP_CHANNEL_NAME}` channel first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await self._backup_guild(guild)
        except Exception:  # noqa: BLE001
            log.exception("Manual backup failed for guild %s.", guild.id)
            await interaction.followup.send(
                "⚠️ Backup failed — check the bot logs.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Backed up **{guild.name}**'s verified-member roster to "
            f"{self._channel_for(guild).mention} ({count} member(s)).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
