"""SQLite (async) data layer for TaigaBot.

A single `Database` instance is created in bot.py and attached as `bot.db`, so
every feature can call e.g. `await self.bot.db.add_verified_user(...)`.

Tables
------
verified_users  : one row per verified member (discord id, name, email)
guild_settings  : per-guild automod toggles
banned_words    : per-guild banned word list (automod)
levels          : per-guild per-user XP / level
warnings        : moderation warnings issued by Eboard
reaction_roles  : emoji -> role bindings on specific messages
"""
from __future__ import annotations

import time
import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS verified_users (
    discord_id       INTEGER PRIMARY KEY,
    discord_username TEXT    NOT NULL,
    real_name        TEXT    NOT NULL,
    email            TEXT    NOT NULL UNIQUE,
    guild_id         INTEGER NOT NULL,
    verified_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id         INTEGER PRIMARY KEY,
    automod_enabled  INTEGER NOT NULL DEFAULT 1,
    filter_words     INTEGER NOT NULL DEFAULT 1,
    filter_invites   INTEGER NOT NULL DEFAULT 1,
    filter_spam      INTEGER NOT NULL DEFAULT 1,
    filter_mentions  INTEGER NOT NULL DEFAULT 1,
    filter_caps      INTEGER NOT NULL DEFAULT 0,
    levels_enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS banned_words (
    guild_id INTEGER NOT NULL,
    word     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, word)
);

CREATE TABLE IF NOT EXISTS levels (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    xp           INTEGER NOT NULL DEFAULT 0,
    level        INTEGER NOT NULL DEFAULT 0,
    last_msg_ts  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    role_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS projects (
    channel_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    role_id     INTEGER NOT NULL,
    lead_id     INTEGER NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL
);
"""

# Default automod toggle values, used when a guild has no row yet.
DEFAULT_SETTINGS = {
    "automod_enabled": 1,
    "filter_words": 1,
    "filter_invites": 1,
    "filter_spam": 1,
    "filter_mentions": 1,
    "filter_caps": 0,
    "levels_enabled": 1,
}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def snapshot(self, dest_path: str) -> None:
        """Write a consistent copy of the WHOLE DB to dest_path.

        Uses SQLite's online backup API, so it's safe to call while the bot is
        running and writing — unlike a plain file copy, which can capture a
        half-written database.
        """
        dest = await aiosqlite.connect(dest_path)
        try:
            await self.conn.backup(dest)
        finally:
            await dest.close()

    # Every table is scoped by this column, so a per-guild export is just a
    # filtered copy of each one.
    _GUILD_TABLES = (
        "verified_users", "guild_settings", "banned_words",
        "levels", "warnings", "reaction_roles",
        "projects", "project_requests",
    )

    async def export_guild(self, guild_id: int, dest_path: str) -> None:
        """Write a new SQLite DB at dest_path containing ONLY this guild's rows.

        Used for backups so each server's snapshot holds just its own members'
        data (names/emails/XP/etc.), never other guilds'.
        """
        # Build an empty DB with the same schema.
        dest = await aiosqlite.connect(dest_path)
        try:
            await dest.executescript(SCHEMA)
            await dest.commit()
        finally:
            await dest.close()

        # Copy only this guild's rows via ATTACH (commit first so we're not
        # inside a transaction, which ATTACH disallows).
        await self.conn.commit()
        await self.conn.execute("ATTACH DATABASE ? AS bak", (dest_path,))
        try:
            for table in self._GUILD_TABLES:
                await self.conn.execute(
                    f"INSERT INTO bak.{table} SELECT * FROM main.{table} "
                    "WHERE guild_id = ?",
                    (guild_id,),
                )
            await self.conn.commit()
        finally:
            await self.conn.execute("DETACH DATABASE bak")

    # ── verified users ────────────────────────────────────────────────────
    async def email_is_registered(self, email: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM verified_users WHERE email = ?", (email.lower(),)
        )
        return await cur.fetchone() is not None

    async def student_id_is_registered(self, student_id: str) -> bool:
        """True if any verified email shares this local part (the student ID),
        regardless of which RIT domain it used (@rit.edu vs @g.rit.edu)."""
        cur = await self.conn.execute(
            "SELECT 1 FROM verified_users "
            "WHERE lower(substr(email, 1, instr(email, '@') - 1)) = ?",
            (student_id.lower(),),
        )
        return await cur.fetchone() is not None

    async def user_is_verified(self, discord_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        return await cur.fetchone() is not None

    async def add_verified_user(
        self, discord_id: int, discord_username: str, real_name: str, email: str, guild_id: int
    ) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO verified_users
               (discord_id, discord_username, real_name, email, guild_id, verified_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (discord_id, discord_username, real_name, email.lower(), guild_id, int(time.time())),
        )
        await self.conn.commit()

    async def get_verified_user(self, discord_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        return await cur.fetchone()

    async def remove_verified_user(self, discord_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        await self.conn.commit()

    async def count_verified(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM verified_users WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    # ── guild settings (automod) ──────────────────────────────────────────
    async def get_settings(self, guild_id: int) -> dict:
        cur = await self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
            )
            await self.conn.commit()
            return {"guild_id": guild_id, **DEFAULT_SETTINGS}
        return dict(row)

    async def set_setting(self, guild_id: int, key: str, value: int) -> None:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting: {key}")
        await self.get_settings(guild_id)  # ensure row exists
        await self.conn.execute(
            f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id)
        )
        await self.conn.commit()

    # ── banned words ──────────────────────────────────────────────────────
    async def add_banned_word(self, guild_id: int, word: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
            (guild_id, word.lower()),
        )
        await self.conn.commit()

    async def remove_banned_word(self, guild_id: int, word: str) -> None:
        await self.conn.execute(
            "DELETE FROM banned_words WHERE guild_id = ? AND word = ?",
            (guild_id, word.lower()),
        )
        await self.conn.commit()

    async def get_banned_words(self, guild_id: int) -> list[str]:
        cur = await self.conn.execute(
            "SELECT word FROM banned_words WHERE guild_id = ?", (guild_id,)
        )
        return [r["word"] for r in await cur.fetchall()]

    # ── levels / XP ───────────────────────────────────────────────────────
    async def get_level_row(self, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return await cur.fetchone()

    async def upsert_level(
        self, guild_id: int, user_id: int, xp: int, level: int, last_msg_ts: float
    ) -> None:
        await self.conn.execute(
            """INSERT INTO levels (guild_id, user_id, xp, level, last_msg_ts)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id)
               DO UPDATE SET xp=excluded.xp, level=excluded.level,
                             last_msg_ts=excluded.last_msg_ts""",
            (guild_id, user_id, xp, level, last_msg_ts),
        )
        await self.conn.commit()

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """SELECT user_id, xp, level FROM levels
               WHERE guild_id = ? ORDER BY xp DESC LIMIT ?""",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def rank(self, guild_id: int, user_id: int) -> int | None:
        cur = await self.conn.execute(
            """SELECT COUNT(*) + 1 AS rnk FROM levels
               WHERE guild_id = ? AND xp > (
                   SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?
               )""",
            (guild_id, guild_id, user_id),
        )
        row = await cur.fetchone()
        return row["rnk"] if row else None

    # ── warnings ──────────────────────────────────────────────────────────
    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, user_id, moderator_id, reason, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_warnings(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """SELECT * FROM warnings WHERE guild_id = ? AND user_id = ?
               ORDER BY created_at DESC""",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        await self.conn.commit()
        return cur.rowcount

    # ── reaction roles ────────────────────────────────────────────────────
    async def add_reaction_role(
        self, guild_id: int, message_id: int, emoji: str, role_id: int
    ) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO reaction_roles (guild_id, message_id, emoji, role_id)
               VALUES (?, ?, ?, ?)""",
            (guild_id, message_id, emoji, role_id),
        )
        await self.conn.commit()

    async def remove_reaction_role(self, message_id: int, emoji: str) -> None:
        await self.conn.execute(
            "DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (message_id, emoji),
        )
        await self.conn.commit()

    async def get_reaction_role(self, message_id: int, emoji: str) -> int | None:
        cur = await self.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (message_id, emoji),
        )
        row = await cur.fetchone()
        return row["role_id"] if row else None

    async def list_reaction_roles(self, guild_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM reaction_roles WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchall()

    # ── projects ──────────────────────────────────────────────────────────────

    async def add_project(
        self, channel_id: int, guild_id: int, name: str,
        role_id: int, lead_id: int, description: str, tags: str,
    ) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO projects
               (channel_id, guild_id, name, role_id, lead_id, description, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, guild_id, name, role_id, lead_id, description, tags, int(time.time())),
        )
        await self.conn.commit()

    async def get_project(self, channel_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM projects WHERE channel_id = ?", (channel_id,)
        )
        return await cur.fetchone()

    async def list_projects(self, guild_id: int, tag: str | None = None) -> list[aiosqlite.Row]:
        if tag:
            cur = await self.conn.execute(
                "SELECT * FROM projects WHERE guild_id = ? AND (',' || lower(tags) || ',') LIKE ? ORDER BY name",
                (guild_id, f"%,{tag.lower().strip()},%"),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM projects WHERE guild_id = ? ORDER BY name", (guild_id,)
            )
        return await cur.fetchall()

    async def delete_project(self, channel_id: int) -> None:
        await self.conn.execute("DELETE FROM projects WHERE channel_id = ?", (channel_id,))
        await self.conn.execute(
            "DELETE FROM project_requests WHERE channel_id = ?", (channel_id,)
        )
        await self.conn.commit()

    # ── project requests ──────────────────────────────────────────────────────

    async def add_project_request(self, guild_id: int, channel_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            """INSERT INTO project_requests (guild_id, channel_id, user_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (guild_id, channel_id, user_id, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_project_request(self, request_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM project_requests WHERE id = ?", (request_id,)
        )
        return await cur.fetchone()

    async def has_pending_request(self, channel_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM project_requests WHERE channel_id = ? AND user_id = ? AND status = 'pending'",
            (channel_id, user_id),
        )
        return await cur.fetchone() is not None

    async def update_request_status(self, request_id: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE project_requests SET status = ? WHERE id = ?", (status, request_id)
        )
        await self.conn.commit()
