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
crypto_meta     : which ENCRYPTION_KEY this database belongs to
roster_exports  : per-guild cooldown + audit trail for decrypted roster downloads

Encryption at rest
------------------
verified_users.real_name / .email / .discord_username are encrypted with
AES-256-GCM (see crypto.py) and are cleartext ONLY inside this module — every
caller hands over and receives plaintext, so no feature needs to know. discord_id
stays plaintext: it is the primary key, it joins to eight other tables, and
Discord hands it out publicly anyway. What's worth hiding is the *link* from that
id to a real person, and encrypting the other three columns severs it.

Because email is encrypted with a random nonce, SQL can neither match nor
uniquely constrain it. Lookups that used to substring the email instead go
through `student_id_hash`, a deterministic HMAC of the RIT student id, which also
carries the UNIQUE constraint that `email` used to hold. warnings.identity_key
stores that same hash, so its equality queries and index work unchanged.
"""
from __future__ import annotations

import contextlib
import os
import time
import aiosqlite

import crypto


class EncryptionKeyMismatch(RuntimeError):
    """ENCRYPTION_KEY is not the key this database was encrypted with."""


class DuplicateStudentIdError(RuntimeError):
    """Two rows share one RIT student id, which UNIQUE(student_id_hash) forbids."""


def _student_id_from_email(email: str) -> str:
    """The RIT student id: the email's local part, lowercased.

    Deliberately mirrors SQLite's `lower(substr(email, 1, instr(email,'@') - 1))`,
    the expression this replaces, INCLUDING its edge case: an address with no '@'
    yields '' there (substr with a negative length is empty), not the whole
    string. features/verification._student_id uses split('@')[0] and would
    disagree — but that only ever sees domain-validated input, whereas this runs
    over whatever is already sitting in the table. If the two disagreed, a
    malformed row would get one identity before the migration and another after,
    orphaning its warnings.
    """
    e = (email or "").strip().lower()
    i = e.find("@")
    return e[:i] if i > 0 else ""


# Column order for the encrypted roster backup. Shared by Database's export/import
# pair, features/backup.py and restore_roster.py so the CSV header, the SELECT and
# the INSERT can never drift apart. Appending here changes the on-disk backup
# format — bump the `taigabot-roster` version in features/backup.py if you do.
ENCRYPTED_EXPORT_COLUMNS = (
    "discord_id",
    "discord_username",
    "real_name",
    "email",
    "student_id_hash",
    "guild_id",
    "verified_at",
    "last_recovery_at",
)


SCHEMA = """
-- discord_username / real_name / email hold AES-GCM envelopes, not cleartext.
-- `email` is deliberately NOT UNIQUE any more: randomized ciphertext differs on
-- every write, so the constraint could not be enforced. student_id_hash carries
-- it instead, which is also the constraint the code always meant — every read
-- collapses @rit.edu and @g.rit.edu to the same person.
-- student_id_hash is NOT NULL with no default on purpose: SQLite treats each NULL
-- as distinct in a UNIQUE index, so a nullable column would silently permit the
-- exact duplicates the constraint exists to prevent.
CREATE TABLE IF NOT EXISTS verified_users (
    discord_id       INTEGER PRIMARY KEY,
    discord_username TEXT    NOT NULL,
    real_name        TEXT    NOT NULL,
    email            TEXT    NOT NULL,
    student_id_hash  TEXT    NOT NULL,
    guild_id         INTEGER NOT NULL,
    verified_at      INTEGER NOT NULL,
    last_recovery_at INTEGER NOT NULL DEFAULT 0
);
-- NB: the unique index on student_id_hash is created in _migrate(), not here —
-- same reason as idx_warnings_identity below.

-- Pins this database to one ENCRYPTION_KEY. Starting with a different key must
-- fail loudly rather than write a second key's ciphertext alongside the first's.
CREATE TABLE IF NOT EXISTS crypto_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Dashboard roster exports. That download is DECRYPTED, so unlike the encrypted
-- #taiga-backups upload it is rate-limited and recorded. Per-guild, and in SQLite
-- rather than a process dict for two reasons: the cooldown must survive a restart
-- (a redeploy would otherwise reset everyone's limit), and `exported_by` is the
-- only record of who pulled a server's real names and emails.
CREATE TABLE IF NOT EXISTS roster_exports (
    guild_id       INTEGER PRIMARY KEY,
    last_export_at INTEGER NOT NULL,
    exported_by    INTEGER NOT NULL DEFAULT 0,
    row_count      INTEGER NOT NULL DEFAULT 0
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
    -- Blind index (HMAC) of the warned member's RIT student id at the time of
    -- the warning. Global/cross-server counts key off this so warnings follow
    -- the person rather than the Discord account. Blank for unverified members —
    -- and blank must stay blank, never HMAC(''), or every unverified member in
    -- every server would merge into one identity. Deterministic, so equality
    -- matching and the index below work exactly as they did when this held the
    -- student id in cleartext.
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

-- Servers blocked from using TaigaBot. Checked on join, so a banned server that
-- re-invites the bot is told why and dropped again. `name` is the guild's name at
-- ban time, kept so the dashboard list still reads well for servers the bot has
-- never been in or can no longer see.
CREATE TABLE IF NOT EXISTS banned_guilds (
    guild_id  INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL DEFAULT '',
    banned_by INTEGER NOT NULL DEFAULT 0,
    banned_at INTEGER NOT NULL DEFAULT 0,
    reason    TEXT    NOT NULL DEFAULT ''
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
        try:
            self.conn.row_factory = aiosqlite.Row
            await self.conn.executescript(SCHEMA)
            await self.conn.commit()
            await self._check_encryption_key()
            await self._migrate()
        except BaseException:
            # Close before re-raising. aiosqlite drives each connection from a
            # non-daemon thread, so a leaked one keeps the interpreter alive
            # forever — a startup refusal (wrong ENCRYPTION_KEY, duplicate
            # student id) would hang instead of exiting with its error message.
            with contextlib.suppress(Exception):
                await self.conn.close()
            self.conn = None
            raise

    # ── encryption bookkeeping ────────────────────────────────────────────
    async def _meta_get(self, key: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM crypto_meta WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def _has_pii(self) -> bool:
        """True if there is anything here that would need decrypting."""
        cur = await self.conn.execute("SELECT 1 FROM verified_users LIMIT 1")
        if await cur.fetchone():
            return True
        cur = await self.conn.execute(
            "SELECT 1 FROM warnings WHERE identity_key != '' LIMIT 1"
        )
        return await cur.fetchone() is not None

    async def _check_encryption_key(self) -> None:
        """Refuse to run against a database written with a different key.

        Without this, a wrong key would decrypt nothing, `student_id_hash` would
        match nothing, and the bot would cheerfully re-verify everybody into a
        second set of rows encrypted under the new key — a mess with no clean
        way back. Failing before the first write is much kinder.

        A fresh database has no fingerprint and simply adopts the current key.
        A populated but unstamped one is pre-encryption: _migrate() handles it
        and stamps the fingerprint itself.
        """
        stamped = await self._meta_get("key_fingerprint")
        current = crypto.load().fingerprint
        if stamped is None:
            if not await self._has_pii():
                await self.conn.execute(
                    "INSERT OR REPLACE INTO crypto_meta (key, value) VALUES (?, ?)",
                    ("key_fingerprint", current),
                )
                await self.conn.execute(
                    "INSERT OR REPLACE INTO crypto_meta (key, value) VALUES (?, ?)",
                    ("pii_schema_version", "1"),
                )
                await self.conn.commit()
            return  # populated + unstamped -> _migrate() encrypts and stamps
        if stamped != current:
            raise EncryptionKeyMismatch(
                f"ENCRYPTION_KEY does not match this database ({self.path}).\n"
                f"  database was encrypted with fingerprint : {stamped}\n"
                f"  the current ENCRYPTION_KEY produces     : {current}\n"
                "Refusing to start: reading would fail and writing would mix two "
                "keys' ciphertext in one table. Restore the original "
                "ENCRYPTION_KEY, or restore the pre-encryption backup "
                f"({self.path}.pre-encrypt.bak) and start over with the new key."
            )

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

        # Encrypt PII at rest. MUST STAY LAST in this method — see _encrypt_pii.
        await self._encrypt_pii()

    async def _encrypt_pii(self) -> None:
        """One-time migration: encrypt verified_users, blind-index the lookups.

        This runs LAST in _migrate() and must stay there, for two independent
        reasons that are easy to break by tidying:

        1. The identity_key backfill above reads `verified_users.email` in
           cleartext SQL. If email were already encrypted it would compute
           lower(substr('v1:AbCd...', 1, -1)) = '' for every warning, silently
           wiping every member's cross-server warning history.
        2. `executescript` commits implicitly, and the levels rebuild above uses
           it. An open transaction of ours would be committed out from under us
           mid-migration. So we come after every executescript and use none.

        Everything after the backup runs in a single explicit transaction, so a
        crash at any point rolls back to the pre-migration database and the next
        start simply retries.
        """
        cur = await self.conn.execute("PRAGMA table_info(verified_users)")
        vcols = {r[1] for r in await cur.fetchall()}
        needs_rebuild = bool(vcols) and "student_id_hash" not in vcols
        stamped = await self._meta_get("key_fingerprint")

        # Runs on EVERY start, like idx_warnings_identity, and for the same
        # reason: SCHEMA can't create it (the column may not exist yet on an old
        # database), but a *fresh* database gets the column from SCHEMA and its
        # fingerprint from connect(), so it reaches here having skipped the
        # rebuild below — and would otherwise never get the constraint at all.
        if "student_id_hash" in vcols:
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_verified_users_student_id_hash "
                "ON verified_users (student_id_hash)"
            )
            await self.conn.commit()

        if not needs_rebuild and stamped is not None:
            return  # already encrypted under this key

        fingerprint = crypto.load().fingerprint

        # 1. Two accounts sharing one RIT student id cannot both survive
        #    UNIQUE(student_id_hash), and choosing a survivor is a human
        #    decision, not a migration's. Checked before ANY side effect.
        cur = await self.conn.execute(
            "SELECT lower(substr(email, 1, instr(email, '@') - 1)) AS sid, "
            "       GROUP_CONCAT(discord_id) AS ids, COUNT(*) AS n "
            "FROM verified_users GROUP BY sid HAVING n > 1"
        )
        dupes = await cur.fetchall()
        if dupes:
            detail = "\n".join(
                f"  student id {r['sid'] or '(blank)'}: discord ids {r['ids']}"
                for r in dupes
            )
            raise DuplicateStudentIdError(
                "Cannot encrypt verified_users: these Discord accounts share one RIT "
                "student id, which the new UNIQUE(student_id_hash) forbids.\n"
                f"{detail}\n"
                "Keep the row with the highest verified_at, delete the other(s), then "
                "restart. Nothing has been modified."
            )

        # 2. Last plaintext copy, taken through SQLite's own backup API so it is
        #    consistent even against a live connection. Never overwrite an
        #    existing one — that would destroy the only copy if this is a retry.
        backup_path = self.path + ".pre-encrypt.bak"
        if needs_rebuild and not os.path.exists(backup_path):
            dst = await aiosqlite.connect(backup_path)
            try:
                await self.conn.backup(dst)
            finally:
                await dst.close()

        await self.conn.commit()
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            if needs_rebuild:
                # 3. Encryption happens in Python; SQLite cannot do it in SQL.
                cur = await self.conn.execute(
                    "SELECT discord_id, discord_username, real_name, email, "
                    "guild_id, verified_at, last_recovery_at FROM verified_users"
                )
                rows = await cur.fetchall()
                await cur.close()  # release the read before dropping the table

                payload = []
                for r in rows:
                    did = r["discord_id"]
                    payload.append((
                        did,
                        crypto.encrypt_if_plaintext(
                            r["discord_username"],
                            crypto.aad("verified_users", "discord_username", did)),
                        crypto.encrypt_if_plaintext(
                            r["real_name"],
                            crypto.aad("verified_users", "real_name", did)),
                        crypto.encrypt_if_plaintext(
                            r["email"],
                            crypto.aad("verified_users", "email", did)),
                        crypto.blind_index(_student_id_from_email(r["email"])),
                        r["guild_id"], r["verified_at"], r["last_recovery_at"],
                    ))

                # 4. Rebuild the table to drop UNIQUE(email). SQLite cannot drop a
                #    constraint via ALTER. Safe without any foreign-key dance:
                #    this schema has no foreign keys, triggers or views.
                await self.conn.execute("DROP TABLE IF EXISTS verified_users_new")
                await self.conn.execute(
                    """CREATE TABLE verified_users_new (
                           discord_id       INTEGER PRIMARY KEY,
                           discord_username TEXT    NOT NULL,
                           real_name        TEXT    NOT NULL,
                           email            TEXT    NOT NULL,
                           student_id_hash  TEXT    NOT NULL,
                           guild_id         INTEGER NOT NULL,
                           verified_at      INTEGER NOT NULL,
                           last_recovery_at INTEGER NOT NULL DEFAULT 0
                       )"""
                )
                await self.conn.executemany(
                    "INSERT INTO verified_users_new (discord_id, discord_username, "
                    "real_name, email, student_id_hash, guild_id, verified_at, "
                    "last_recovery_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    payload,
                )
                await self.conn.execute("DROP TABLE verified_users")
                await self.conn.execute(
                    "ALTER TABLE verified_users_new RENAME TO verified_users"
                )

            # 5. The rebuild above created a new table, so the index has to be
            #    (re)created here too — the one made before the gate was dropped
            #    along with the old table.
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_verified_users_student_id_hash "
                "ON verified_users (student_id_hash)"
            )

            # 6. identity_key: cleartext student id -> blind index. Per DISTINCT
            #    value, so this is O(people) rather than O(warnings). Blanks are
            #    excluded so they stay blank.
            cur = await self.conn.execute(
                "SELECT DISTINCT identity_key FROM warnings WHERE identity_key != ''"
            )
            for r in await cur.fetchall():
                old = r["identity_key"]
                new = crypto.blind_index(old)
                if new != old:
                    await self.conn.execute(
                        "UPDATE warnings SET identity_key = ? WHERE identity_key = ?",
                        (new, old),
                    )

            # 7. Stamp the key identity last, inside the same transaction.
            await self.conn.execute(
                "INSERT OR REPLACE INTO crypto_meta (key, value) VALUES (?, ?)",
                ("key_fingerprint", fingerprint),
            )
            await self.conn.execute(
                "INSERT OR REPLACE INTO crypto_meta (key, value) VALUES (?, ?)",
                ("pii_schema_version", "1"),
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

        # Plaintext lingers in freed pages after the table rebuild, which would
        # defeat the whole exercise for anyone reading the raw file. VACUUM
        # rewrites the database without them. Outside the transaction: VACUUM
        # cannot run inside one.
        if needs_rebuild:
            await self.conn.execute("VACUUM")
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

    # ── verified users ────────────────────────────────────────────────────
    # There is deliberately no email_is_registered(): `email` is encrypted with a
    # random nonce, so an exact-match lookup on it can never hit. Ask by student
    # id instead, which is what every caller actually meant.

    async def student_id_is_registered(self, student_id: str) -> bool:
        """True if any verified email shares this local part (the student ID),
        regardless of which RIT domain it used (@rit.edu vs @g.rit.edu)."""
        cur = await self.conn.execute(
            "SELECT 1 FROM verified_users WHERE student_id_hash = ?",
            (crypto.blind_index(student_id.lower()),),
        )
        return await cur.fetchone() is not None

    async def user_is_verified(self, discord_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        return await cur.fetchone() is not None

    async def add_verified_user(
        self,
        discord_id: int,
        discord_username: str,
        real_name: str,
        email: str,
        guild_id: int,
        verified_at: int | None = None,
        last_recovery_at: int = 0,
    ) -> None:
        """Store a verification, encrypting the three PII columns on the way in.

        verified_at/last_recovery_at are overridable so tooling (seed_fake_db.py)
        can write realistic historical rows through this method rather than
        hand-rolling INSERTs that would land in the table as cleartext.
        """
        email = email.lower()
        async with self._tx():
            await self.conn.execute(
                """INSERT OR REPLACE INTO verified_users
                   (discord_id, discord_username, real_name, email, student_id_hash,
                    guild_id, verified_at, last_recovery_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    discord_id,
                    crypto.encrypt(
                        discord_username,
                        crypto.aad("verified_users", "discord_username", discord_id)),
                    crypto.encrypt(
                        real_name,
                        crypto.aad("verified_users", "real_name", discord_id)),
                    crypto.encrypt(
                        email, crypto.aad("verified_users", "email", discord_id)),
                    crypto.blind_index(_student_id_from_email(email)),
                    guild_id,
                    int(time.time()) if verified_at is None else verified_at,
                    last_recovery_at,
                ),
            )

    async def get_verified_user(self, discord_id: int) -> dict | None:
        """The decrypted record as a plain dict, or None.

        A dict rather than the usual aiosqlite.Row because Row is immutable and
        the PII columns have to be decrypted before callers see them. Callers
        keep indexing it exactly as before (row["real_name"]).
        """
        cur = await self.conn.execute(
            "SELECT * FROM verified_users WHERE discord_id = ?", (discord_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        for col in ("discord_username", "real_name", "email"):
            out[col] = crypto.decrypt(
                out[col], crypto.aad("verified_users", col, discord_id)
            )
        return out

    async def remove_verified_user(self, discord_id: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM verified_users WHERE discord_id = ?", (discord_id,)
            )

    async def verified_discord_id_for(self, student_id: str) -> int | None:
        """The Discord ID currently linked to this RIT student id (local part of the
        email), or None. Used to find the OLD account during recovery."""
        cur = await self.conn.execute(
            "SELECT discord_id FROM verified_users WHERE student_id_hash = ?",
            (crypto.blind_index(student_id.lower()),),
        )
        row = await cur.fetchone()
        return row["discord_id"] if row else None

    async def last_recovery_at_for(self, student_id: str) -> int:
        """When this RIT identity was last transferred to a new account (0 if never).
        Drives the recovery rate limit so accounts can't be shuffled rapidly."""
        cur = await self.conn.execute(
            "SELECT last_recovery_at FROM verified_users WHERE student_id_hash = ?",
            (crypto.blind_index(student_id.lower()),),
        )
        row = await cur.fetchone()
        return (row["last_recovery_at"] or 0) if row else 0

    async def transfer_verification(
        self, student_id: str, new_discord_id: int, new_username: str, guild_id: int
    ) -> bool:
        """Re-point an existing verified record (matched by RIT student id, i.e. the
        local part before '@', so both domains count) to a NEW Discord account.

        Used for account recovery when someone loses their old Discord. Stamps
        last_recovery_at for the rate limit. Returns True if a record was moved.

        This has to decrypt and re-encrypt rather than just UPDATE the id: the
        ciphertexts are authenticated against discord_id (see crypto.aad), so
        moving the row while leaving them alone would leave them bound to the OLD
        account and permanently un-decryptable — breaking /whois for exactly the
        people who just went through account recovery.
        """
        sid_hash = crypto.blind_index(student_id.lower())
        cur = await self.conn.execute(
            "SELECT discord_id, real_name, email FROM verified_users "
            "WHERE student_id_hash = ?",
            (sid_hash,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        old_id = row["discord_id"]
        real_name = crypto.decrypt(
            row["real_name"], crypto.aad("verified_users", "real_name", old_id))
        email = crypto.decrypt(
            row["email"], crypto.aad("verified_users", "email", old_id))

        now = int(time.time())
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE verified_users SET discord_id = ?, discord_username = ?, "
                "real_name = ?, email = ?, guild_id = ?, verified_at = ?, "
                "last_recovery_at = ? WHERE student_id_hash = ?",
                (
                    new_discord_id,
                    crypto.encrypt(
                        new_username,
                        crypto.aad("verified_users", "discord_username", new_discord_id)),
                    crypto.encrypt(
                        real_name,
                        crypto.aad("verified_users", "real_name", new_discord_id)),
                    crypto.encrypt(
                        email, crypto.aad("verified_users", "email", new_discord_id)),
                    guild_id, now, now, sid_hash,
                ),
            )
        return cur.rowcount > 0

    # ── encrypted roster export / restore ─────────────────────────────────
    # The one place callers legitimately handle ciphertext. features/backup.py
    # uploads these rows to an Eboard-only Discord channel so the roster survives
    # a wiped host, and restore_roster.py puts them back. Both sides move the
    # bytes VERBATIM and keep each row's original discord_id, because crypto.aad
    # binds every ciphertext to it — re-keying a row on the way through would
    # produce a table that nothing can decrypt.

    async def export_encrypted_rows(self, guild_id: int) -> list[dict]:
        """One guild's verified_users rows, still encrypted.

        Deliberately NOT decrypted: this is the only accessor that hands out
        ciphertext, and it exists so a backup can be stored somewhere less
        trusted than the database itself.

        Scoped by guild_id (where the member ran /verify), not by current
        membership, so the union of every guild's export reconstructs the whole
        table exactly once. See features/backup.py for the rows that scoping
        cannot reach.
        """
        cur = await self.conn.execute(
            f"SELECT {', '.join(ENCRYPTED_EXPORT_COLUMNS)} FROM verified_users "
            "WHERE guild_id = ? ORDER BY discord_id",
            (guild_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def import_encrypted_rows(
        self, rows: list[dict], fingerprint: str
    ) -> tuple[int, int]:
        """Restore exported rows verbatim. Returns (inserted, skipped).

        Refuses the whole batch if `fingerprint` isn't this key's — rows written
        under a different ENCRYPTION_KEY would insert cleanly and then fail to
        decrypt forever, which is worse than not restoring at all.

        INSERT OR IGNORE, so restoring over a live database can only ever add
        people back. A row already present (same discord_id, or same
        student_id_hash under UNIQUE) is left exactly as it is: the live copy is
        by definition newer than the backup.
        """
        expected = crypto.load().fingerprint
        if fingerprint != expected:
            raise EncryptionKeyMismatch(
                "This backup was written with a different ENCRYPTION_KEY.\n"
                f"  backup was written with fingerprint : {fingerprint}\n"
                f"  current ENCRYPTION_KEY fingerprint  : {expected}\n"
                "Restore with the key the backup was made under. Importing it "
                "under this key would store rows that can never be decrypted."
            )

        inserted = 0
        async with self._tx():
            for r in rows:
                cur = await self.conn.execute(
                    "INSERT OR IGNORE INTO verified_users "
                    f"({', '.join(ENCRYPTED_EXPORT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(ENCRYPTED_EXPORT_COLUMNS))})",
                    tuple(r[c] for c in ENCRYPTED_EXPORT_COLUMNS),
                )
                inserted += cur.rowcount or 0
        return inserted, len(rows) - inserted

    # ── dashboard roster export cooldown ──────────────────────────────────

    async def last_roster_export(self, guild_id: int) -> int:
        """When this guild last exported a decrypted roster (0 if never)."""
        cur = await self.conn.execute(
            "SELECT last_export_at FROM roster_exports WHERE guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        return (row["last_export_at"] or 0) if row else 0

    async def record_roster_export(
        self, guild_id: int, user_id: int, row_count: int
    ) -> None:
        """Stamp an export: starts the cooldown and records who pulled the PII."""
        async with self._tx():
            await self.conn.execute(
                "INSERT OR REPLACE INTO roster_exports "
                "(guild_id, last_export_at, exported_by, row_count) VALUES (?, ?, ?, ?)",
                (guild_id, int(time.time()), user_id, row_count),
            )

    async def count_all_verified(self) -> int:
        """Every verified row, across all guilds. Lets features/backup.py notice
        rows that no guild's backup covers (their guild_id points somewhere the
        bot no longer is)."""
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM verified_users")
        row = await cur.fetchone()
        return row["c"] if row else 0

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
    @staticmethod
    def identity_key_for_student_id(student_id: str) -> str:
        """The value warnings.identity_key stores for this RIT student id.

        Exposed so tooling (seed_fake_db.py) can write realistic warning rows
        without importing crypto or knowing how the blind index is built.
        """
        return crypto.blind_index(student_id.lower())

    async def student_id_for(self, discord_id: int) -> str:
        """This account's RIT identity as a blind index, or '' if not verified.

        Returns the HMAC rather than the readable student id — which is exactly
        what warnings.identity_key stores, so every caller below compares like
        with like and needs no changes. Both @rit.edu and @g.rit.edu collapse to
        the same value, which is the point.
        """
        cur = await self.conn.execute(
            "SELECT student_id_hash FROM verified_users WHERE discord_id = ?",
            (discord_id,),
        )
        row = await cur.fetchone()
        return (row["student_id_hash"] or "") if row else ""

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
            "SELECT s.*, f.url, f.kind, f.path_prefix, f.last_polled, f.fail_count, "
            "f.last_error "
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

    # ── banned servers ────────────────────────────────────────────────────────

    async def is_guild_banned(self, guild_id: int) -> bool:
        """True if this server is blocked from using the bot. Bans never expire —
        they're lifted explicitly from the dashboard."""
        cur = await self.conn.execute(
            "SELECT 1 FROM banned_guilds WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchone() is not None

    async def ban_guild(
        self, guild_id: int, name: str = "", banned_by: int = 0, reason: str = ""
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                """INSERT INTO banned_guilds (guild_id, name, banned_by, banned_at, reason)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       name      = excluded.name,
                       banned_by = excluded.banned_by,
                       banned_at = excluded.banned_at,
                       reason    = excluded.reason""",
                (guild_id, name[:100], banned_by, int(time.time()), reason[:300]),
            )

    async def unban_guild(self, guild_id: int) -> bool:
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM banned_guilds WHERE guild_id = ?", (guild_id,)
            )
        return cur.rowcount > 0

    async def get_guild_ban(self, guild_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM banned_guilds WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchone()

    async def list_banned_guilds(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM banned_guilds ORDER BY banned_at DESC"
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
