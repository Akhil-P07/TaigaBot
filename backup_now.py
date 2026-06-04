"""One-shot backup trigger you can run from a shell.

Logs in with the bot token, uploads a per-guild database backup to each server's
#taiga-backups channel (same as the scheduled job / the /backup command), then
exits. Useful for an immediate backup without waiting for the timer.

    python backup_now.py            # back up every guild
    GID=123456789012345678 python backup_now.py   # back up one guild only

Safe to run while the main bot is also running — it's a short-lived second
session that disconnects as soon as it's done.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import discord

import config
import database
from utils import guildutils as gu


async def _backup(client: discord.Client, db: database.Database, guild: discord.Guild) -> bool:
    channel = None
    if config.BACKUP_CHANNEL_ID:
        c = client.get_channel(config.BACKUP_CHANNEL_ID)
        if c is not None and getattr(c, "guild", None) == guild:
            channel = c
    if channel is None:
        channel = gu.backups_channel(guild)
    if channel is None:
        print(f"- {guild.name}: no #{config.BACKUP_CHANNEL_NAME} channel, skipping")
        return False

    ts = time.strftime("%Y%m%d-%H%M%S")
    tmp = os.path.join(tempfile.gettempdir(), f"taigabot-{guild.id}-{ts}.db")
    await db.export_guild(guild.id, tmp)
    size = os.path.getsize(tmp)
    try:
        await channel.send(
            content=f"🗄️ Manual backup for **{guild.name}** — {ts} ({size / 1024:.0f} KB)",
            file=discord.File(tmp, filename=f"taigabot-{guild.id}-{ts}.db"),
        )
    finally:
        os.remove(tmp)
    print(f"- {guild.name}: uploaded {size} bytes to #{channel.name}")
    return True


async def main() -> None:
    if not config.DISCORD_TOKEN:
        print("No DISCORD_TOKEN set."); return

    intents = discord.Intents.default()  # guilds enabled; that's all we need
    client = discord.Client(intents=intents)
    db = database.Database(config.DB_PATH)
    only = os.getenv("GID")

    @client.event
    async def on_ready() -> None:
        try:
            await db.connect()
            guilds = client.guilds
            if only:
                guilds = [g for g in guilds if str(g.id) == only]
                if not guilds:
                    print(f"Bot is not in guild {only}.")
            done = 0
            for guild in guilds:
                try:
                    if await _backup(client, db, guild):
                        done += 1
                except Exception as e:  # noqa: BLE001
                    print(f"- {guild.name}: FAILED ({e})")
            print(f"Done. {done} guild(s) backed up.")
        finally:
            await db.close()
            await client.close()

    await client.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
