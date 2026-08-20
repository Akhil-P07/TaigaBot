"""Off-box, per-guild roster backups — guards against host filesystem wipes.

A normal crash, restart, or sleep never loses data: SQLite commits to disk on
every write, so the file is intact when the bot wakes up. The real risk is the
host (a free Railway/Render container) being rebuilt from scratch, which wipes
the file entirely. This feature periodically uploads each guild's verified_users
rows to an Eboard-only Discord channel, so the membership record survives a full
container reset. restore_roster.py is the way back in.

What goes in the file
---------------------
CIPHERTEXT. The name/email/username columns are uploaded exactly as they sit in
the database — AES-256-GCM envelopes that are worthless without ENCRYPTION_KEY.
That is the whole reason this feature could come back: an earlier version wrote
cleartext rosters to Discord and was deleted for it.

What does NOT go in the file: anything outside verified_users. `levels`,
`warnings`, `tickets`, `premium_guilds` and `banned_guilds` are global or owner
tables, so putting a raw .db snapshot in a guild's channel would hand every
server a copy of every other server's data. That mistake has been made here once
already — don't reintroduce a raw DB export without auditing every table it
copies.

Two consequences worth knowing:
  * The backup is only as good as the key. Lose ENCRYPTION_KEY and these files
    are permanently undecryptable — they protect against a wiped disk, not
    against a lost key.
  * Rows are scoped by guild_id (where the member ran /verify), so the union of
    every guild's file is the whole table exactly once. Rows belonging to a
    guild the bot has left are in nobody's backup; the loop logs a warning when
    it notices any.

/setup creates the Eboard-only backup channel (named BACKUP_CHANNEL_NAME).

Set BACKUP_INTERVAL_HOURS=-1 to switch backups off across every server: this cog
is then never loaded, so neither the periodic upload nor /backup exists.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import crypto
from database import ENCRYPTED_EXPORT_COLUMNS
from utils import guildutils as gu
from utils.checks import is_eboard

log = logging.getLogger("taigabot.backup")

# Bump when the columns or the header change, so a future restore_roster.py can
# tell old files apart instead of guessing at them.
BACKUP_FORMAT = "taigabot-roster v1"

# Restore reads these back off the `#` header lines.
META_PREFIX = "#"

# Rate limit for the manual /backup command. utils.cooldowns.spam_cooldown is no
# use here: it exempts Eboard and admins, and /backup is Eboard-only, so every
# caller would be exempt. Keyed per-guild rather than per-member because the
# resource being protected is the guild's backup channel — two Eboard members
# taking turns shouldn't double the upload rate. The periodic upload
# (BACKUP_INTERVAL_HOURS) is a separate schedule and is unaffected.
BACKUP_COOLDOWN_SEC = 300.0


def render_backup_csv(rows: list[dict], guild_id: int, fingerprint: str, ts: str) -> str:
    """The backup file's exact text. Split out so tests can assert on it without
    a Discord guild or the filesystem."""
    buf = io.StringIO()
    buf.write(f"{META_PREFIX} {BACKUP_FORMAT}  guild={guild_id}  generated={ts}\n")
    buf.write(f"{META_PREFIX} key_fingerprint={fingerprint}  rows={len(rows)}\n")
    buf.write(
        f"{META_PREFIX} Encrypted. Restore with the SAME ENCRYPTION_KEY:"
        " python restore_roster.py <file>\n"
    )
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(ENCRYPTED_EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([r[c] for c in ENCRYPTED_EXPORT_COLUMNS])
    return buf.getvalue()


async def build_guild_backup(db, guild: discord.Guild):
    """Create this guild's backup file. Returns (files, row_count).

    `files` is a list of (temp_path, upload_filename); the caller sends them and
    then deletes the temp paths.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    rows = await db.export_encrypted_rows(guild.id)
    text = render_backup_csv(rows, guild.id, crypto.load().fingerprint, ts)

    name = f"roster-{guild.id}-{ts}.csv"
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    return [(path, name)], len(rows)


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
        """Upload this guild's encrypted roster. Returns the row count, or None
        if there's no usable backup channel."""
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
            await channel.send(
                content=(
                    f"🗄️ Encrypted roster backup for **{guild.name}** — "
                    f"{count} verified member(s).\n"
                    "🔒 Names and emails in this file are **encrypted**; it is unreadable "
                    "without the bot's `ENCRYPTION_KEY`. Keep it here — if the bot's "
                    "database is ever lost, an admin restores the roster from this file."
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
        covered = 0
        for guild in self.bot.guilds:
            try:
                count = await self._backup_guild(guild)
            except Exception:  # noqa: BLE001
                log.exception("Automatic backup failed for guild %s.", guild.id)
                continue
            if count is not None:
                backed_up += 1
                covered += count

        # Rows scoped to a guild the bot is no longer in land in no backup at
        # all. Nothing here can fix that (there's no channel left to post to),
        # but silently under-backing-up the roster is the kind of thing you only
        # discover during a restore, so say it out loud on every pass.
        total = await self.bot.db.count_all_verified()
        if covered < total:
            log.warning(
                "%d verified row(s) are not covered by any backup — they belong to "
                "guild(s) TaigaBot has left. Only %d of %d rows were uploaded.",
                total - covered, covered, total,
            )

        if backed_up:
            log.info("Uploaded encrypted roster backups for %d guild(s).", backed_up)
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
        description="Back up THIS server's encrypted roster to its backup channel now (Eboard only).",
    )
    # Order matters: checks run in the order they were registered, and decorators
    # apply bottom-up, so is_eboard() must stay nearest the function. Otherwise a
    # non-Eboard member's rejected call would still consume the guild's bucket and
    # lock out the whole Eboard for five minutes.
    @app_commands.checks.cooldown(1, BACKUP_COOLDOWN_SEC, key=lambda i: i.guild_id)
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
            f"✅ Backed up **{guild.name}**'s encrypted roster to "
            f"{self._channel_for(guild).mention} ({count} member(s)).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    # BACKUP_INTERVAL_HOURS=-1 is the global off switch. Skipping add_cog is the
    # honest way to honour it: no periodic upload, and no /backup command either,
    # rather than a command that exists and then refuses. Nothing else in the bot
    # imports this cog, so nothing breaks by its absence.
    if not config.BACKUPS_ENABLED:
        log.warning(
            "Roster backups are DISABLED (BACKUP_INTERVAL_HOURS=%s). Nothing is "
            "being uploaded to #%s in any server — if this host's disk is wiped, "
            "the verified-member roster is gone. Set a positive number of hours "
            "to turn them back on.",
            config.BACKUP_INTERVAL_HOURS, config.BACKUP_CHANNEL_NAME,
        )
        return
    await bot.add_cog(Backup(bot))
