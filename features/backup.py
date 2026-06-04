"""Off-box, per-guild database backups — guards against host filesystem wipes.

A normal crash, restart, or sleep never loses data: SQLite commits to disk on
every write, so the file is intact when the bot wakes up. The real risk is the
host (e.g. a free Replit/Render container) being rebuilt from scratch, which
wipes the file entirely. This feature periodically uploads a snapshot of the
database to an Eboard-only Discord channel, so the data survives even a full
container reset — you re-download the latest `.db` and drop it back in.

Each guild's backup contains ONLY that guild's rows (its verified members, XP,
warnings, settings, …) — never other servers' data — so one server's Eboard can
never see another's names/emails. Alongside the .db, each backup includes a CSV
roster of the guild's current members who hold the Verified role or are admins.
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
    """Create this guild's backup files. Returns (files, db_bytes, roster_count).

    `files` is a list of (temp_path, upload_filename); the caller sends them and
    then deletes the temp paths. It contains:
      • the filtered per-guild database (.db), and
      • a CSV roster of the guild's current verified + admin members.

    A current member is rostered if they hold the Verified role or are an
    administrator. (Members who verified on another server still appear, because
    joining here auto-grants them the Verified role.)
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    db_path = os.path.join(tempfile.gettempdir(), f"taigabot-{guild.id}-{ts}.db")
    await db.export_guild(guild.id, db_path)
    db_bytes = os.path.getsize(db_path)

    verified_role = config.VERIFIED_ROLE_NAME.lower()
    roster_path = os.path.join(tempfile.gettempdir(), f"roster-{guild.id}-{ts}.csv")
    count = 0
    with open(roster_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "display_name", "username", "user_id", "is_admin",
            "has_verified_role", "verified_in_db", "real_name", "email", "verified_at",
        ])
        async for m in guild.fetch_members(limit=None):
            if m.bot:
                continue
            is_admin = m.guild_permissions.administrator
            has_role = any(r.name.lower() == verified_role for r in m.roles)
            if not (is_admin or has_role):
                continue
            info = await db.get_verified_user(m.id)  # fill name/email if on record
            writer.writerow([
                m.display_name, str(m), m.id, is_admin, has_role, info is not None,
                info["real_name"] if info else "",
                info["email"] if info else "",
                info["verified_at"] if info else "",
            ])
            count += 1

    return (
        [(db_path, f"taigabot-{guild.id}-{ts}.db"),
         (roster_path, f"roster-{guild.id}-{ts}.csv")],
        db_bytes,
        count,
    )


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
        """Export this guild's data + member roster and upload to its backup
        channel. Returns the DB byte size, or None if there's no backup channel."""
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
        files_meta, db_bytes, count = await build_guild_backup(self.bot.db, guild)
        try:
            files = [discord.File(p, filename=n) for p, n in files_meta]
            ts = time.strftime("%Y%m%d-%H%M%S")
            await channel.send(
                content=(
                    f"🗄️ Backup for **{guild.name}** — {ts} "
                    f"({db_bytes / 1024:.0f} KB DB + roster of {count} verified/admin member(s))"
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
        return db_bytes

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
        description="Back up THIS server's data to its backup channel now (Eboard only).",
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
            size = await self._backup_guild(guild)
        except Exception:  # noqa: BLE001
            log.exception("Manual backup failed for guild %s.", guild.id)
            await interaction.followup.send(
                "⚠️ Backup failed — check the bot logs.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Backed up **{guild.name}**'s data to "
            f"{self._channel_for(guild).mention} ({size / 1024:.0f} KB).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
