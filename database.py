"""SQLite (async) data layer for TaigaBot.

A single `Database` instance is created in bot.py and attached as `bot.db`, so
every feature can call e.g. `await self.bot.db.add_verified_user(...)`.

Tables
------
verified_users  : one row per verified member (discord id, name, email)
guild_settings  : per-guild automod toggles
banned_words    : per-guild banned word list (automod)
automod_exempt  : per-guild channel/category exemptions for automod filters
levels          : per-user XP / level (GLOBAL — shared across all guilds)
warnings        : moderation warnings issued by Eboard
reaction_roles  : emoji -> role bindings on specific messages
"""
from __future__ import annotations

import contextlib
import time
import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS verified_users (
    discord_id       INTEGER PRIMARY KEY,
    discord_username TEXT    NOT NULL,
    real_name        TEXT    NOT NULL,
    email            TEXT    NOT NULL UNIQUE,
    guild_id         INTEGER NOT NULL,
    verified_at      INTEGER NOT NULL,
    last_recovery_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id         INTEGER PRIMARY KEY,
    automod_enabled  INTEGER NOT NULL DEFAULT 1,
    filter_words     INTEGER NOT NULL DEFAULT 1,
    filter_invites   INTEGER NOT NULL DEFAULT 1,
    filter_spam      INTEGER NOT NULL DEFAULT 1,
    filter_mentions  INTEGER NOT NULL DEFAULT 1,
    filter_caps      INTEGER NOT NULL DEFAULT 0,
    filter_phishing  INTEGER NOT NULL DEFAULT 1,
    filter_contact   INTEGER NOT NULL DEFAULT 1,
    levels_enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS banned_words (
    guild_id INTEGER NOT NULL,
    word     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, word)
);

-- Channel/category gating for automod: a filter can be exempted in specific
-- channels or categories (e.g. let #memes bypass caps/spam). `filter` holds the
-- setting column it exempts (e.g. 'filter_caps') or the sentinel 'all' to skip
-- every filter there. `target_id` is a channel OR category id; `target_type`
-- ('channel'/'category') is kept only so status output reads well after the
-- channel is deleted.
CREATE TABLE IF NOT EXISTS automod_exempt (
    guild_id    INTEGER NOT NULL,
    filter      TEXT    NOT NULL,
    target_id   INTEGER NOT NULL,
    target_type TEXT    NOT NULL DEFAULT 'channel',
    PRIMARY KEY (guild_id, filter, target_id)
);

CREATE TABLE IF NOT EXISTS levels (
    user_id      INTEGER PRIMARY KEY,
    xp           INTEGER NOT NULL DEFAULT 0,
    level        INTEGER NOT NULL DEFAULT 0,
    last_msg_ts  REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    -- The warned member's RIT student id (email local part) at the time of the
    -- warning. Global/cross-server counts key off this so warnings follow the
    -- person rather than the Discord account. Blank for unverified members.
    identity_key TEXT    NOT NULL DEFAULT ''
);
-- NB: the index on identity_key is created in _migrate(), not here. This script
-- runs before migrations, and on a pre-existing database the CREATE TABLE above
-- is a no-op — so indexing identity_key here would reference a column that the
-- ALTER TABLE hasn't added yet and blow up on startup.

CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    role_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS projects (
    channel_id       INTEGER PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    role_id          INTEGER NOT NULL,
    lead_id          INTEGER NOT NULL,
    lead_ids         TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '',
    intro_message_id INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL
);

-- News watcher. Deliberately split into "what to poll" (news_feeds, keyed by
-- URL) and "who wants it" (news_subs), so twenty guilds watching the same feed
-- cost exactly one HTTP request per cycle instead of twenty.
CREATE TABLE IF NOT EXISTS news_feeds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL UNIQUE,
    kind          TEXT    NOT NULL,              -- 'rss' | 'sitemap'
    path_prefix   TEXT    NOT NULL DEFAULT '',   -- sitemap kind: only these paths
    etag          TEXT    NOT NULL DEFAULT '',
    last_modified TEXT    NOT NULL DEFAULT '',
    -- Neither openai.com nor anthropic.com sends ETag/Last-Modified, so the 304
    -- path rarely fires on the built-in sources. Hashing the body is the
    -- fallback: identical bytes mean we can skip parsing entirely.
    content_hash  TEXT    NOT NULL DEFAULT '',
    last_polled   INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS news_subs (
    guild_id   INTEGER NOT NULL,
    feed_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    -- 'custom' or a built-in source key. This is the *type* marker (the custom
    -- feed cap counts it), never a display string.
    label      TEXT    NOT NULL DEFAULT '',
    -- Optional per-server display name. It lives on the subscription, not the
    -- feed, so two guilds watching the same URL can each call it what they like.
    -- Blank means "fall back to the built-in label or the feed's hostname".
    display_name TEXT  NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, feed_id)
);

-- Seen items are global per feed, not per guild: an article is recorded once
-- and fanned out to every subscriber.
CREATE TABLE IF NOT EXISTS news_seen (
    feed_id INTEGER NOT NULL,
    guid    TEXT    NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (feed_id, guid)
);

-- Premium servers. Granted from the dashboard by the bot owner after an offline
-- payment; there is deliberately no payment integration here. expires_at = 0
-- means the grant never lapses.
CREATE TABLE IF NOT EXISTS premium_guilds (
    guild_id   INTEGER PRIMARY KEY,
    granted_by INTEGER NOT NULL DEFAULT 0,
    granted_at INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL DEFAULT 0,
    note       TEXT    NOT NULL DEFAULT ''
);

-- Dashboard login sessions. The token here is the *hash* of the cookie value,
-- never the value itself: a stolen database then can't be replayed as a login.
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL DEFAULT '',
    avatar     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions (expires_at);

-- Support tickets raised from the dashboard.
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL DEFAULT 0,   -- 0 = not about a specific server
    user_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL DEFAULT '',
    subject    TEXT    NOT NULL,
    category   TEXT    NOT NULL DEFAULT 'general',
    status     TEXT    NOT NULL DEFAULT 'open',  -- open | answered | closed
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status, updated_at);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL DEFAULT '',
    is_staff    INTEGER NOT NULL DEFAULT 0,
    body        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ticket_messages ON ticket_messages (ticket_id, created_at);
"""

# Default automod toggle values, used when a guild has no row yet.
DEFAULT_SETTINGS = {
    "automod_enabled": 1,
    "filter_words": 1,
    "filter_invites": 1,
    "filter_spam": 1,
    "filter_mentions": 1,
    "filter_caps": 0,
    "filter_phishing": 1,
    "filter_contact": 1,
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
        await self._migrate()

    async def _migrate(self) -> None:
        """Lightweight schema migrations for DBs created by older versions."""
        cur = await self.conn.execute("PRAGMA table_info(projects)")
        cols = {r[1] for r in await cur.fetchall()}
        if cols and "lead_ids" not in cols:
            await self.conn.execute(
                "ALTER TABLE projects ADD COLUMN lead_ids TEXT NOT NULL DEFAULT ''"
            )
            # Backfill from the single legacy lead_id.
            await self.conn.execute(
                "UPDATE projects SET lead_ids = CAST(lead_id AS TEXT) WHERE lead_ids = ''"
            )
            await self.conn.commit()

        if cols and "intro_message_id" not in cols:
            await self.conn.execute(
                "ALTER TABLE projects ADD COLUMN intro_message_id INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()

        # Phishing/scam automod filter toggle (added later).
        cur = await self.conn.execute("PRAGMA table_info(guild_settings)")
        gcols = {r[1] for r in await cur.fetchall()}
        if gcols and "filter_phishing" not in gcols:
            await self.conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN filter_phishing INTEGER NOT NULL DEFAULT 1"
            )
            await self.conn.commit()

        # Personal-contact / solicitation filter toggle (added later).
        if gcols and "filter_contact" not in gcols:
            await self.conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN filter_contact INTEGER NOT NULL DEFAULT 1"
            )
            await self.conn.commit()

        # Account-recovery rate limit marker (added later).
        cur = await self.conn.execute("PRAGMA table_info(verified_users)")
        vcols = {r[1] for r in await cur.fetchall()}
        if vcols and "last_recovery_at" not in vcols:
            await self.conn.execute(
                "ALTER TABLE verified_users ADD COLUMN last_recovery_at INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()

        # Warnings used to be keyed only by Discord account, so switching accounts
        # reset your global history. Stamp each warning with the RIT identity and
        # backfill what we can from current verification records.
        cur = await self.conn.execute("PRAGMA table_info(warnings)")
        wcols = {r[1] for r in await cur.fetchall()}
        if wcols and "identity_key" not in wcols:
            await self.conn.execute(
                "ALTER TABLE warnings ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''"
            )
            # Backfill: any warning whose account is still verified inherits that
            # account's student id. Warnings for accounts that have since been
            # recovered away or deleted stay blank and keep matching on user_id.
            await self.conn.execute(
                """UPDATE warnings SET identity_key = COALESCE((
                       SELECT lower(substr(v.email, 1, instr(v.email, '@') - 1))
                       FROM verified_users v WHERE v.discord_id = warnings.user_id
                   ), '')"""
            )
            await self.conn.commit()

        # Safe to run every start: by here the column is guaranteed to exist,
        # whether from SCHEMA (fresh DB) or the ALTER above (existing DB).
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_warnings_identity ON warnings (identity_key)"
        )
        await self.conn.commit()

        # Per-server display names for news subscriptions (added later).
        cur = await self.conn.execute("PRAGMA table_info(news_subs)")
        ncols = {r[1] for r in await cur.fetchall()}
        if ncols and "display_name" not in ncols:
            await self.conn.execute(
                "ALTER TABLE news_subs ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )
            await self.conn.commit()

        # Levels used to be per-guild (PRIMARY KEY guild_id, user_id). Collapse
        # them into a single global row per user so XP follows a member across
        # every server: sum their XP, keep their most recent message timestamp.
        cur = await self.conn.execute("PRAGMA table_info(levels)")
        lcols = {r[1] for r in await cur.fetchall()}
        if "guild_id" in lcols:
            await self.conn.executescript(
                """
                CREATE TABLE levels_global (
                    user_id      INTEGER PRIMARY KEY,
                    xp           INTEGER NOT NULL DEFAULT 0,
                    level        INTEGER NOT NULL DEFAULT 0,
                    last_msg_ts  REAL    NOT NULL DEFAULT 0
                );
                INSERT INTO levels_global (user_id, xp, last_msg_ts)
                    SELECT user_id, SUM(xp), MAX(last_msg_ts)
                    FROM levels GROUP BY user_id;
                DROP TABLE levels;
                ALTER TABLE levels_global RENAME TO levels;
                """
            )
            # Recompute level from the summed XP (same gentle curve as leveling).
            def _level_from_xp(xp: int) -> int:
                lvl = 0
                while xp >= 5 * lvl * lvl + 50 * lvl + 100:
                    xp -= 5 * lvl * lvl + 50 * lvl + 100
                    lvl += 1
                return lvl

            cur = await self.conn.execute("SELECT user_id, xp FROM levels")
            for r in await cur.fetchall():
                await self.conn.execute(
                    "UPDATE levels SET level = ? WHERE user_id = ?",
                    (_level_from_xp(r["xp"]), r["user_id"]),
                )
            await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @contextlib.asynccontextmanager
    async def _tx(self):
        """Wrap writes so they commit on success and ROLL BACK on any error.

        Every feature shares this one connection. Without the rollback, a failed
        statement (e.g. a constraint violation) would leave an open, half-finished
        transaction on the connection — which can break or stall later queries
        from completely unrelated features. Rolling back keeps the shared
        connection clean so one feature's DB error can never cascade to others."""
        try:
            yield
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

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
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO verified_users
                   (discord_id, discord_username, real_name, email, guild_id, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (discord_id, discord_username, real_name, email.lower(), guild_id, int(time.time())),
            )

    async def get_verified_user(self, discord_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        return await cur.fetchone()

    async def remove_verified_user(self, discord_id: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM verified_users WHERE discord_id = ?", (discord_id,)
            )

    async def verified_discord_id_for(self, student_id: str) -> int | None:
        """The Discord ID currently linked to this RIT student id (local part of the
        email), or None. Used to find the OLD account during recovery."""
        cur = await self.conn.execute(
            "SELECT discord_id FROM verified_users "
            "WHERE lower(substr(email, 1, instr(email, '@') - 1)) = ?",
            (student_id.lower(),),
        )
        row = await cur.fetchone()
        return row["discord_id"] if row else None

    async def last_recovery_at_for(self, student_id: str) -> int:
        """When this RIT identity was last transferred to a new account (0 if never).
        Drives the recovery rate limit so accounts can't be shuffled rapidly."""
        cur = await self.conn.execute(
            "SELECT last_recovery_at FROM verified_users "
            "WHERE lower(substr(email, 1, instr(email, '@') - 1)) = ?",
            (student_id.lower(),),
        )
        row = await cur.fetchone()
        return (row["last_recovery_at"] or 0) if row else 0

    async def transfer_verification(
        self, student_id: str, new_discord_id: int, new_username: str, guild_id: int
    ) -> bool:
        """Re-point an existing verified record (matched by RIT student id, i.e. the
        local part before '@', so both domains count) to a NEW Discord account.

        Used for account recovery when someone loses their old Discord. A single
        UPDATE — the old discord_id row becomes the new account's; stamps
        last_recovery_at for the rate limit. Returns True if a record was moved."""
        now = int(time.time())
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE verified_users SET discord_id = ?, discord_username = ?, "
                "guild_id = ?, verified_at = ?, last_recovery_at = ? "
                "WHERE lower(substr(email, 1, instr(email, '@') - 1)) = ?",
                (new_discord_id, new_username, guild_id, now, now, student_id.lower()),
            )
        return cur.rowcount > 0

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
            async with self._tx():
                await self.conn.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
                )
            return {"guild_id": guild_id, **DEFAULT_SETTINGS}
        return dict(row)

    async def set_setting(self, guild_id: int, key: str, value: int) -> None:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting: {key}")
        await self.get_settings(guild_id)  # ensure row exists
        async with self._tx():
            await self.conn.execute(
                f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id)
            )

    # ── banned words ──────────────────────────────────────────────────────
    async def add_banned_word(self, guild_id: int, word: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
                (guild_id, word.lower()),
            )

    async def remove_banned_word(self, guild_id: int, word: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM banned_words WHERE guild_id = ? AND word = ?",
                (guild_id, word.lower()),
            )

    async def get_banned_words(self, guild_id: int) -> list[str]:
        cur = await self.conn.execute(
            "SELECT word FROM banned_words WHERE guild_id = ?", (guild_id,)
        )
        return [r["word"] for r in await cur.fetchall()]

    # ── automod channel/category gating ───────────────────────────────────
    async def add_automod_exemption(
        self, guild_id: int, filter_key: str, target_id: int, target_type: str
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO automod_exempt
                   (guild_id, filter, target_id, target_type) VALUES (?, ?, ?, ?)""",
                (guild_id, filter_key, target_id, target_type),
            )

    async def remove_automod_exemption(
        self, guild_id: int, filter_key: str, target_id: int
    ) -> int:
        """Delete one exemption. Returns the number of rows removed (0 if it
        wasn't exempt)."""
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM automod_exempt "
                "WHERE guild_id = ? AND filter = ? AND target_id = ?",
                (guild_id, filter_key, target_id),
            )
        return cur.rowcount

    async def get_automod_exemptions(self, guild_id: int) -> dict[str, set[int]]:
        """Runtime lookup: {filter_key: {exempt channel/category ids}}. Read once
        per message, so it stays small and cheap."""
        cur = await self.conn.execute(
            "SELECT filter, target_id FROM automod_exempt WHERE guild_id = ?",
            (guild_id,),
        )
        out: dict[str, set[int]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["filter"], set()).add(r["target_id"])
        return out

    async def list_automod_exemptions(self, guild_id: int) -> list[aiosqlite.Row]:
        """Full rows (filter, target_id, target_type) for the status display."""
        cur = await self.conn.execute(
            "SELECT filter, target_id, target_type FROM automod_exempt "
            "WHERE guild_id = ? ORDER BY filter",
            (guild_id,),
        )
        return await cur.fetchall()

    # ── levels / XP (global — shared across all guilds) ─────────────────────
    async def get_level_row(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM levels WHERE user_id = ?", (user_id,)
        )
        return await cur.fetchone()

    async def upsert_level(
        self, user_id: int, xp: int, level: int, last_msg_ts: float
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                """INSERT INTO levels (user_id, xp, level, last_msg_ts)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id)
                   DO UPDATE SET xp=excluded.xp, level=excluded.level,
                                 last_msg_ts=excluded.last_msg_ts""",
                (user_id, xp, level, last_msg_ts),
            )

    async def leaderboard(self, limit: int | None = 10) -> list[aiosqlite.Row]:
        sql = "SELECT user_id, xp, level FROM levels ORDER BY xp DESC"
        if limit is None:
            cur = await self.conn.execute(sql)
        else:
            cur = await self.conn.execute(sql + " LIMIT ?", (limit,))
        return await cur.fetchall()

    async def rank(self, user_id: int) -> int | None:
        cur = await self.conn.execute(
            """SELECT COUNT(*) + 1 AS rnk FROM levels
               WHERE xp > (SELECT xp FROM levels WHERE user_id = ?)""",
            (user_id,),
        )
        row = await cur.fetchone()
        return row["rnk"] if row else None

    # ── warnings ──────────────────────────────────────────────────────────
    async def student_id_for(self, discord_id: int) -> str:
        """This account's RIT student id (the local part of their verified email),
        or '' if they aren't verified. Both @rit.edu and @g.rit.edu collapse to
        the same id, which is the point."""
        cur = await self.conn.execute(
            "SELECT lower(substr(email, 1, instr(email, '@') - 1)) AS sid "
            "FROM verified_users WHERE discord_id = ?",
            (discord_id,),
        )
        row = await cur.fetchone()
        return (row["sid"] or "") if row else ""

    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        """Record a warning, stamping the warned member's RIT identity onto it.

        The identity is captured *now* rather than joined at read time, because
        `/recover` moves a verification record to a new Discord account — a join
        would silently drop the old account's history the moment someone
        recovered. Stamping means warnings follow the person, not the account,
        which is the whole point of tying them to the RIT email.
        """
        identity = await self.student_id_for(user_id)
        async with self._tx():
            cur = await self.conn.execute(
                """INSERT INTO warnings
                   (guild_id, user_id, moderator_id, reason, created_at, identity_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, moderator_id, reason, int(time.time()), identity),
            )
        return cur.lastrowid

    async def get_warnings(self, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """SELECT * FROM warnings WHERE guild_id = ? AND user_id = ?
               ORDER BY created_at DESC""",
            (guild_id, user_id),
        )
        return await cur.fetchall()

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )
        return cur.rowcount

    async def _warning_identity_clause(self, user_id: int) -> tuple[str, tuple]:
        """SQL fragment matching every warning belonging to this *person*.

        Verified members match on their RIT identity, so warnings collected on a
        previous or alternate Discord account still count. Unverified members
        have no identity to match on, so they fall back to the account id.
        """
        identity = await self.student_id_for(user_id)
        if identity:
            return "(identity_key = ? OR user_id = ?)", (identity, user_id)
        return "user_id = ?", (user_id,)

    async def cross_server_warnings(self, user_id: int, exclude_guild_id: int) -> tuple[int, int]:
        """Cross-server repeat-offender summary: (other_servers, other_warnings) —
        how many OTHER guilds this bot is in have warned the person, and the total
        warnings there. Counts only, no details/server names, so it's a privacy-
        preserving marker rather than exposing another club's mod history.

        Scoped by RIT identity, so switching Discord accounts doesn't reset it."""
        clause, params = await self._warning_identity_clause(user_id)
        cur = await self.conn.execute(
            f"SELECT COUNT(DISTINCT guild_id) AS servers, COUNT(*) AS warns "
            f"FROM warnings WHERE {clause} AND guild_id != ?",
            (*params, exclude_guild_id),
        )
        row = await cur.fetchone()
        if not row:
            return (0, 0)
        return (row["servers"] or 0, row["warns"] or 0)

    async def global_warnings(self, user_id: int) -> tuple[int, int]:
        """(servers, warnings) across EVERY server, tied to the person's RIT
        identity. Used by /whois so an Eboard can see total history at a glance."""
        clause, params = await self._warning_identity_clause(user_id)
        cur = await self.conn.execute(
            f"SELECT COUNT(DISTINCT guild_id) AS servers, COUNT(*) AS warns "
            f"FROM warnings WHERE {clause}",
            params,
        )
        row = await cur.fetchone()
        if not row:
            return (0, 0)
        return (row["servers"] or 0, row["warns"] or 0)

    # ── reaction roles ────────────────────────────────────────────────────
    async def add_reaction_role(
        self, guild_id: int, message_id: int, emoji: str, role_id: int
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO reaction_roles (guild_id, message_id, emoji, role_id)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, message_id, emoji, role_id),
            )

    async def remove_reaction_role(self, message_id: int, emoji: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?",
                (message_id, emoji),
            )

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
        role_id: int, lead_ids: list[int], description: str, tags: str,
    ) -> None:
        leads_csv = ",".join(str(i) for i in lead_ids)
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO projects
                   (channel_id, guild_id, name, role_id, lead_id, lead_ids,
                    description, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel_id, guild_id, name, role_id, lead_ids[0], leads_csv,
                 description, tags, int(time.time())),
            )

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
        async with self._tx():
            await self.conn.execute("DELETE FROM projects WHERE channel_id = ?", (channel_id,))
            await self.conn.execute(
                "DELETE FROM project_requests WHERE channel_id = ?", (channel_id,)
            )

    async def update_project_details(
        self, channel_id: int, name: str, description: str, tags: str
    ) -> None:
        """Edit a project's editable fields in place (keeps role/leads/created_at)."""
        async with self._tx():
            await self.conn.execute(
                "UPDATE projects SET name = ?, description = ?, tags = ? WHERE channel_id = ?",
                (name, description, tags, channel_id),
            )

    async def set_intro_message(self, channel_id: int, message_id: int) -> None:
        """Remember the id of the project channel's intro embed, so it can be
        deleted and reposted when the project is edited."""
        async with self._tx():
            await self.conn.execute(
                "UPDATE projects SET intro_message_id = ? WHERE channel_id = ?",
                (message_id, channel_id),
            )

    # ── project requests ──────────────────────────────────────────────────────

    async def add_project_request(self, guild_id: int, channel_id: int, user_id: int) -> int:
        async with self._tx():
            cur = await self.conn.execute(
                """INSERT INTO project_requests (guild_id, channel_id, user_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, channel_id, user_id, int(time.time())),
            )
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
        async with self._tx():
            await self.conn.execute(
                "UPDATE project_requests SET status = ? WHERE id = ?", (status, request_id)
            )

    # ── news feeds ────────────────────────────────────────────────────────────

    async def upsert_feed(self, url: str, kind: str, path_prefix: str = "") -> int:
        """Get the id of the feed row for this URL, creating it if needed.

        Feeds are keyed by URL and shared across guilds, so subscribing a second
        guild to an already-watched feed reuses the same row (and therefore the
        same single poll)."""
        async with self._tx():
            await self.conn.execute(
                "INSERT OR IGNORE INTO news_feeds (url, kind, path_prefix) VALUES (?, ?, ?)",
                (url, kind, path_prefix),
            )
        cur = await self.conn.execute("SELECT id FROM news_feeds WHERE url = ?", (url,))
        row = await cur.fetchone()
        return row["id"]

    async def get_feed(self, feed_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM news_feeds WHERE id = ?", (feed_id,))
        return await cur.fetchone()

    async def get_active_feeds(self) -> list[aiosqlite.Row]:
        """Every distinct feed that at least one guild subscribes to."""
        cur = await self.conn.execute(
            "SELECT * FROM news_feeds WHERE id IN (SELECT feed_id FROM news_subs) "
            "ORDER BY id"
        )
        return list(await cur.fetchall())

    async def record_feed_poll(
        self,
        feed_id: int,
        etag: str,
        last_modified: str,
        content_hash: str = "",
        error: str = "",
    ) -> None:
        """Stamp a poll result. A successful poll clears the failure counter; a
        failed one increments it so the cog can back off a persistently broken
        feed instead of retrying it every cycle forever."""
        async with self._tx():
            if error:
                await self.conn.execute(
                    "UPDATE news_feeds SET last_polled = ?, fail_count = fail_count + 1, "
                    "last_error = ? WHERE id = ?",
                    (int(time.time()), error[:300], feed_id),
                )
            else:
                await self.conn.execute(
                    "UPDATE news_feeds SET last_polled = ?, etag = ?, last_modified = ?, "
                    "content_hash = ?, fail_count = 0, last_error = '' WHERE id = ?",
                    (int(time.time()), etag, last_modified, content_hash, feed_id),
                )

    async def add_news_sub(
        self,
        guild_id: int,
        feed_id: int,
        channel_id: int,
        label: str,
        display_name: str = "",
    ) -> None:
        """Subscribe a guild to a feed. Re-adding an existing feed updates the
        channel, and only overwrites the display name when a new one was given —
        so `/news add` without a name doesn't silently wipe a rename."""
        async with self._tx():
            await self.conn.execute(
                """INSERT INTO news_subs
                       (guild_id, feed_id, channel_id, label, display_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, feed_id)
                   DO UPDATE SET channel_id = excluded.channel_id,
                                 label = excluded.label,
                                 display_name = CASE
                                     WHEN excluded.display_name != '' THEN excluded.display_name
                                     ELSE news_subs.display_name
                                 END""",
                (guild_id, feed_id, channel_id, label, display_name, int(time.time())),
            )

    async def rename_news_sub(self, guild_id: int, feed_id: int, display_name: str) -> bool:
        """Set (or, with an empty string, clear) this guild's name for a feed.

        Scoped to the guild's own subscription row, so renaming never touches
        what another server calls the same URL."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE news_subs SET display_name = ? WHERE guild_id = ? AND feed_id = ?",
                (display_name, guild_id, feed_id),
            )
            return cur.rowcount > 0

    async def remove_news_sub(self, guild_id: int, feed_id: int) -> bool:
        """Drop a subscription. Also deletes the feed (and its seen history) if no
        other guild still wants it, so abandoned URLs stop being polled."""
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM news_subs WHERE guild_id = ? AND feed_id = ?",
                (guild_id, feed_id),
            )
            removed = cur.rowcount > 0
            if removed:
                orphan = await self.conn.execute(
                    "SELECT 1 FROM news_subs WHERE feed_id = ?", (feed_id,)
                )
                if await orphan.fetchone() is None:
                    await self.conn.execute(
                        "DELETE FROM news_seen WHERE feed_id = ?", (feed_id,)
                    )
                    await self.conn.execute(
                        "DELETE FROM news_feeds WHERE id = ?", (feed_id,)
                    )
        return removed

    async def get_guild_news_subs(self, guild_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT s.*, f.url, f.kind, f.last_polled, f.fail_count, f.last_error "
            "FROM news_subs s JOIN news_feeds f ON f.id = s.feed_id "
            "WHERE s.guild_id = ? "
            "ORDER BY LOWER(CASE WHEN s.display_name != '' THEN s.display_name "
            "ELSE s.label END), s.feed_id",
            (guild_id,),
        )
        return list(await cur.fetchall())

    async def get_subs_for_feed(self, feed_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM news_subs WHERE feed_id = ?", (feed_id,)
        )
        return list(await cur.fetchall())

    async def count_custom_feeds(self, guild_id: int) -> int:
        """How many user-supplied (non-built-in) feeds this guild watches, for the
        NEWS_MAX_CUSTOM_FEEDS cap."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM news_subs WHERE guild_id = ? AND label = 'custom'",
            (guild_id,),
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def filter_unseen(self, feed_id: int, guids: list[str]) -> list[str]:
        """Return the subset of guids we haven't posted for this feed yet,
        preserving the caller's ordering."""
        if not guids:
            return []
        placeholders = ",".join("?" for _ in guids)
        cur = await self.conn.execute(
            f"SELECT guid FROM news_seen WHERE feed_id = ? AND guid IN ({placeholders})",
            (feed_id, *guids),
        )
        seen = {r["guid"] for r in await cur.fetchall()}
        return [g for g in guids if g not in seen]

    async def mark_seen(self, feed_id: int, guids: list[str]) -> None:
        if not guids:
            return
        now = int(time.time())
        async with self._tx():
            await self.conn.executemany(
                "INSERT OR IGNORE INTO news_seen (feed_id, guid, seen_at) VALUES (?, ?, ?)",
                [(feed_id, g, now) for g in guids],
            )

    # ── premium servers ───────────────────────────────────────────────────────

    async def is_premium(self, guild_id: int) -> bool:
        """True if this guild currently has premium. Generic on purpose — any
        feature can gate on it without knowing how the grant was made."""
        cur = await self.conn.execute(
            "SELECT expires_at FROM premium_guilds WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return False
        return row["expires_at"] == 0 or row["expires_at"] > int(time.time())

    async def grant_premium(
        self, guild_id: int, granted_by: int, expires_at: int = 0, note: str = ""
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                """INSERT INTO premium_guilds (guild_id, granted_by, granted_at, expires_at, note)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       granted_by = excluded.granted_by,
                       granted_at = excluded.granted_at,
                       expires_at = excluded.expires_at,
                       note       = excluded.note""",
                (guild_id, granted_by, int(time.time()), expires_at, note[:300]),
            )

    async def revoke_premium(self, guild_id: int) -> bool:
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM premium_guilds WHERE guild_id = ?", (guild_id,)
            )
        return cur.rowcount > 0

    async def get_premium(self, guild_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM premium_guilds WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchone()

    async def list_premium(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM premium_guilds ORDER BY granted_at DESC"
        )
        return list(await cur.fetchall())

    # ── dashboard sessions ────────────────────────────────────────────────────

    async def create_session(
        self, token_hash: str, user_id: int, username: str, avatar: str, ttl_seconds: int
    ) -> None:
        now = int(time.time())
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO web_sessions
                   (token_hash, user_id, username, avatar, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (token_hash, user_id, username, avatar, now, now + ttl_seconds),
            )

    async def get_session(self, token_hash: str) -> aiosqlite.Row | None:
        """Look up a live session. Expired rows are treated as absent."""
        cur = await self.conn.execute(
            "SELECT * FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, int(time.time())),
        )
        return await cur.fetchone()

    async def delete_session(self, token_hash: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,)
            )

    async def prune_sessions(self) -> int:
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?", (int(time.time()),)
            )
        return cur.rowcount

    # ── support tickets ───────────────────────────────────────────────────────

    async def create_ticket(
        self,
        user_id: int,
        username: str,
        subject: str,
        body: str,
        guild_id: int = 0,
        category: str = "general",
    ) -> int:
        now = int(time.time())
        async with self._tx():
            cur = await self.conn.execute(
                """INSERT INTO tickets
                   (guild_id, user_id, username, subject, category, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                (guild_id, user_id, username, subject, category, now, now),
            )
            ticket_id = cur.lastrowid
            await self.conn.execute(
                """INSERT INTO ticket_messages
                   (ticket_id, author_id, author_name, is_staff, body, created_at)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (ticket_id, user_id, username, body, now),
            )
        return ticket_id

    async def get_ticket(self, ticket_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return await cur.fetchone()

    async def get_ticket_messages(self, ticket_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at, id",
            (ticket_id,),
        )
        return list(await cur.fetchall())

    async def list_tickets_for_user(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
        )
        return list(await cur.fetchall())

    async def list_all_tickets(self, status: str = "") -> list[aiosqlite.Row]:
        """Staff view. `status` filters; blank returns everything, open first."""
        if status:
            cur = await self.conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY updated_at DESC", (status,)
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM tickets ORDER BY "
                "CASE status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END, "
                "updated_at DESC"
            )
        return list(await cur.fetchall())

    async def add_ticket_message(
        self, ticket_id: int, author_id: int, author_name: str, body: str, is_staff: bool
    ) -> None:
        """Append a reply. A staff reply marks the ticket 'answered'; the
        requester replying reopens it, so nothing gets silently dropped."""
        now = int(time.time())
        async with self._tx():
            await self.conn.execute(
                """INSERT INTO ticket_messages
                   (ticket_id, author_id, author_name, is_staff, body, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ticket_id, author_id, author_name, int(is_staff), body, now),
            )
            await self.conn.execute(
                "UPDATE tickets SET updated_at = ?, status = ? WHERE id = ?",
                (now, "answered" if is_staff else "open", ticket_id),
            )

    async def set_ticket_status(self, ticket_id: int, status: str) -> None:
        if status not in ("open", "answered", "closed"):
            raise ValueError(f"Unknown ticket status: {status}")
        async with self._tx():
            await self.conn.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                (status, int(time.time()), ticket_id),
            )

    async def count_open_tickets(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE status != 'closed'"
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def count_recent_tickets(self, user_id: int, within_seconds: int) -> int:
        """For rate-limiting ticket creation — anyone with a Discord account can
        open one, so this is the guard against a flood."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE user_id = ? AND created_at > ?",
            (user_id, int(time.time()) - within_seconds),
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def prune_news_seen(self, max_age_days: int = 90) -> int:
        """Drop seen-item rows older than max_age_days. Items that old have long
        since fallen out of the feed window, so they can't be re-posted."""
        cutoff = int(time.time()) - max_age_days * 86400
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM news_seen WHERE seen_at < ?", (cutoff,)
            )
        return cur.rowcount
