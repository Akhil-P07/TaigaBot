"""Off-box, per-guild database backups — guards against host filesystem wipes.

A normal crash, restart, or sleep never loses data: SQLite commits to disk on
every write, so the file is intact when the bot wakes up. The real risk is the
host (e.g. a free Replit/Render container) being rebuilt from scratch, which
wipes the file entirely. This feature periodically uploads a snapshot of the
database to an Eboard-only Discord channel, so the data survives even a full
container reset — you re-download the latest `.db` and drop it back in.

Each guild's backup contains ONLY that guild's rows (its verified members, XP,
warnings, settings, …) — never other servers' data — so one server's Eboard can
never see another's names/emails. /setup creates the Eboard-only backup channel
(named BACKUP_CHANNEL_NAME) in each server.
"""
from __future__ import annotations

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
        """Export just this guild's data and upload it to its backup channel.
        Returns the byte size, or None if the guild has no backup channel."""
        channel = self._channel_for(guild)
        if channel is None:
            return None
        ts = time.strftime("%Y%m%d-%H%M%S")
        tmp = os.path.join(tempfile.gettempdir(), f"taigabot-{guild.id}-{ts}.db")
        await self.bot.db.export_guild(guild.id, tmp)
        size = os.path.getsize(tmp)
        try:
            await channel.send(
                content=f"🗄️ Backup for **{guild.name}** — {ts} ({size / 1024:.0f} KB)",
                file=discord.File(tmp, filename=f"taigabot-{guild.id}-{ts}.db"),
            )
        finally:
            os.remove(tmp)
        return size

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
