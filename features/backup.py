"""Off-box database backups — guards against host filesystem wipes.

A normal crash, restart, or sleep never loses data: SQLite commits to disk on
every write, so the file is intact when the bot wakes up. The real risk is the
host (e.g. a free Replit/Render container) being rebuilt from scratch, which
wipes the file entirely. This feature periodically uploads a *consistent*
snapshot of the database to a Discord channel, so the data survives even a full
container reset — you just re-download the latest `.db` and drop it back in.

⚠️ PRIVACY: the database stores verified members' real names and emails. The
backup channel MUST be private (Eboard-only). Set `BACKUP_CHANNEL_ID` in the
config to that channel's ID; leave it blank to disable backups entirely.
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
from utils.checks import is_eboard

log = logging.getLogger("taigabot.backup")


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if config.BACKUP_CHANNEL_ID:
            self.auto_backup.change_interval(hours=config.BACKUP_INTERVAL_HOURS)
            self.auto_backup.start()
        else:
            log.info("BACKUP_CHANNEL_ID not set — automatic backups disabled.")

    def cog_unload(self) -> None:
        self.auto_backup.cancel()

    async def _make_and_send(self, channel: discord.abc.Messageable) -> int:
        """Snapshot the DB to a temp file, upload it, clean up. Returns bytes."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        tmp = os.path.join(tempfile.gettempdir(), f"taigabot-{ts}.db")
        await self.bot.db.snapshot(tmp)
        size = os.path.getsize(tmp)
        try:
            await channel.send(
                content=f"🗄️ Database backup — {ts} ({size / 1024:.0f} KB)",
                file=discord.File(tmp, filename=f"taigabot-{ts}.db"),
            )
        finally:
            os.remove(tmp)
        return size

    @tasks.loop(hours=12)  # real interval set from config in __init__
    async def auto_backup(self) -> None:
        channel = self.bot.get_channel(config.BACKUP_CHANNEL_ID)
        if channel is None:
            log.warning(
                "Backup channel %s not found (is the bot in that guild?); skipping.",
                config.BACKUP_CHANNEL_ID,
            )
            return
        try:
            size = await self._make_and_send(channel)
            log.info("Database backup uploaded (%d bytes).", size)
        except Exception:  # noqa: BLE001
            log.exception("Automatic database backup failed.")

    @auto_backup.before_loop
    async def _before_auto_backup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="backup", description="Back up the database to the backup channel now (Eboard only)."
    )
    @is_eboard()
    async def backup_now(self, interaction: discord.Interaction) -> None:
        if not config.BACKUP_CHANNEL_ID:
            await interaction.response.send_message(
                "⚠️ Backups are disabled — set `BACKUP_CHANNEL_ID` in the bot's config first.",
                ephemeral=True,
            )
            return
        channel = self.bot.get_channel(config.BACKUP_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                f"⚠️ Backup channel (`{config.BACKUP_CHANNEL_ID}`) not found.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            size = await self._make_and_send(channel)
        except Exception:  # noqa: BLE001
            log.exception("Manual database backup failed.")
            await interaction.followup.send(
                "⚠️ Backup failed — check the bot logs.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Backup uploaded to <#{config.BACKUP_CHANNEL_ID}> ({size / 1024:.0f} KB).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
