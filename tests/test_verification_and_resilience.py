"""Offline regression tests for DM verification + crash-resilience.

Pure standard library (no pytest) — run directly:

    python tests/test_verification_and_resilience.py

These prove, without Discord / email / a live bot:
  C1  verification's email thread pool can't starve the default executor that
      /ask's aiohttp DNS uses (the AI-service-lag fix).
  C2  /verify + /confirm in a DM fans the Verified role out to every shared
      server, writes a non-null guild_id, posts a welcome only where newly
      verified, and leaves pre-existing rows untouched; plus the no-shared-server
      and recovery paths.
  C3  a failed DB write rolls back and leaves the shared connection usable for
      every other feature (the cascade fix).
  C4  PII is encrypted at rest: names/emails never hit the disk in cleartext,
      lookups still work through the blind index, and the one-time migration of
      a legacy plaintext database is atomic, idempotent and key-checked.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sqlite3
import sys
import tempfile
import time

# Make the repo root importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# MUST precede `import config`: config reads the environment at import time, and
# load_dotenv() does not override variables that are already set. Setting it here
# both gives the tests a deterministic key and stops a developer's real .env key
# from being used to write temp databases.
os.environ.setdefault("ENCRYPTION_KEY", "11" * 32)

import discord  # noqa: E402

import config  # noqa: E402
import crypto  # noqa: E402
from database import (  # noqa: E402
    Database,
    DuplicateStudentIdError,
    EncryptionKeyMismatch,
)
from utils import guildutils as gu  # noqa: E402
import features.verification as v  # noqa: E402
import features.backup as backup  # noqa: E402
import restore_roster  # noqa: E402

VERIFIED = config.VERIFIED_ROLE_NAME
UNVERIFIED = config.UNVERIFIED_ROLE_NAME


# ── Minimal Discord mocks (only what the code under test touches) ────────────
class FakeRole:
    def __init__(self, name): self.name = name


class FakeChannel:
    def __init__(self): self.sent = []
    async def send(self, *a, **k): self.sent.append(k.get("embed") or (a[0] if a else None))


class FakeMember:
    def __init__(self, uid, guild, roles):
        self.id = uid
        self.guild = guild
        self.roles = list(roles)
        self.display_name = f"User{uid}"
        self.mention = f"<@{uid}>"
    def __str__(self): return f"user{self.id}"
    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)
    async def remove_roles(self, role, reason=None):
        if role in self.roles:
            self.roles.remove(role)


class FakeGuild:
    def __init__(self, gid, name):
        self.id = gid
        self.name = name
        self.roles = [FakeRole(VERIFIED), FakeRole(UNVERIFIED)]
        self._members = {}
        self._welcome = FakeChannel()
    def add_member(self, m): self._members[m.id] = m
    def get_member(self, uid): return self._members.get(uid)
    async def fetch_member(self, uid):
        m = self._members.get(uid)
        if m is None:
            class _R:
                status = 404
                reason = "Not Found"
            raise discord.NotFound(_R(), "not a member")
        return m


class FakeBot:
    def __init__(self, guilds, db=None):
        self.guilds = guilds
        self.db = db


class FakeResponse:
    def __init__(self): self.done = False; self.messages = []
    def is_done(self): return self.done
    async def defer(self, **k): self.done = True
    async def send_message(self, content=None, **k):
        self.done = True
        self.messages.append(content)


class FakeFollowup:
    def __init__(self): self.messages = []
    async def send(self, content=None, **k): self.messages.append(content)


class FakeInteraction:
    def __init__(self, user, guild=None):
        self.user = user
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _tmp_db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "test.db")


# ── C1: email pool can't starve the default executor ─────────────────────────
async def test_email_pool_isolation():
    cog = v.Verification(FakeBot([]))
    orig = v.send_otp_email
    v.send_otp_email = lambda *a, **k: time.sleep(1.5)  # simulate a slow Brevo call
    try:
        loop = asyncio.get_running_loop()
        # Saturate the cog's dedicated 2-thread email pool with slow sends.
        sends = [asyncio.create_task(cog._send_otp("a@x", "1", "n", "g")) for _ in range(4)]
        await asyncio.sleep(0.1)  # let them grab the email-pool threads
        # A default-executor task (this is where aiohttp's DNS runs) must stay snappy.
        t0 = time.perf_counter()
        await loop.run_in_executor(None, time.sleep, 0.05)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"default executor was starved by email sends ({elapsed:.2f}s)"
        await asyncio.gather(*sends, return_exceptions=True)
    finally:
        v.send_otp_email = orig
        cog.cog_unload()
    print(f"  C1 default-executor latency under email load: {elapsed*1000:.0f}ms  ✅")


# ── C2: DM verification fan-out ──────────────────────────────────────────────
def _patch_guildutils(monkey: dict):
    monkey["welcome_channel"] = gu.welcome_channel
    monkey["log_mod_action"] = gu.log_mod_action
    gu.welcome_channel = lambda guild: getattr(guild, "_welcome", None)
    async def _noop_log(guild, embed): pass
    gu.log_mod_action = _noop_log


def _unpatch_guildutils(monkey: dict):
    gu.welcome_channel = monkey["welcome_channel"]
    gu.log_mod_action = monkey["log_mod_action"]


async def test_dm_fanout_and_data_preserved():
    db = Database(_tmp_db_path())
    await db.connect()
    # A pre-existing verified member on ANOTHER account — must stay untouched.
    await db.add_verified_user(111, "old#1", "Existing Member", "existing@rit.edu", 555)
    before = dict(await db.get_verified_user(111))

    a, b, c = FakeGuild(1, "Alpha"), FakeGuild(2, "Beta"), FakeGuild(3, "Gamma")
    ALT = 999
    a.add_member(FakeMember(ALT, a, [a.roles[1]]))  # in Alpha, only Unverified
    b.add_member(FakeMember(ALT, b, [b.roles[1]]))  # in Beta,  only Unverified
    # not in Gamma at all

    cog = v.Verification(FakeBot([a, b, c], db))
    monkey = {}
    _patch_guildutils(monkey)
    try:
        # Simulate /verify already done (skip the email): seed a pending code.
        cog.pending[ALT] = v.PendingVerification(
            code="123456", email="alt@rit.edu", real_name="Alt User",
            guild_id=None, created_at=time.time(),
        )
        alt_user = FakeMember(ALT, a, [])  # interaction.user in a DM (no guild)
        inter = FakeInteraction(alt_user, guild=None)
        await v.Verification.confirm.callback(cog, inter, "123456")

        # Role applied in both shared servers, not the one they're not in.
        assert any(r.name == VERIFIED for r in a.get_member(ALT).roles), "no Verified in Alpha"
        assert any(r.name == VERIFIED for r in b.get_member(ALT).roles), "no Verified in Beta"
        assert c.get_member(ALT) is None
        # Unverified stripped in Alpha.
        assert not any(r.name == UNVERIFIED for r in a.get_member(ALT).roles)
        # Welcome posted in both newly-verified servers.
        assert a._welcome.sent and b._welcome.sent, "welcome not posted in both servers"
        # DB row written with a NON-NULL guild_id.
        row = await db.get_verified_user(ALT)
        assert row is not None and row["guild_id"] in (1, 2), f"bad guild_id: {row and row['guild_id']}"
        # Pre-existing member untouched.
        after = dict(await db.get_verified_user(111))
        assert after == before, "pre-existing row changed!"
        # Pending cleared.
        assert ALT not in cog.pending
    finally:
        _unpatch_guildutils(monkey)
        cog.cog_unload()
        await db.close()
    print(f"  C2 fan-out: Verified in {{Alpha,Beta}}, guild_id={row['guild_id']}, existing row intact  ✅")


async def test_no_shared_server():
    db = Database(_tmp_db_path())
    await db.connect()
    cog = v.Verification(FakeBot([FakeGuild(1, "Alpha")], db))  # alt is in NO guild
    monkey = {}
    _patch_guildutils(monkey)
    try:
        ALT = 777
        cog.pending[ALT] = v.PendingVerification(
            code="000000", email="lonely@rit.edu", real_name="Lone User",
            guild_id=None, created_at=time.time(),
        )
        inter = FakeInteraction(FakeMember(ALT, None, []), guild=None)
        await v.Verification.confirm.callback(cog, inter, "000000")
        assert await db.get_verified_user(ALT) is None, "should not write without a shared server"
        assert ALT in cog.pending, "pending should be kept for retry after joining"
        assert any("server" in (m or "").lower() for m in inter.followup.messages)
    finally:
        _unpatch_guildutils(monkey)
        cog.cog_unload()
        await db.close()
    print("  C2 no-shared-server: nothing written, pending kept, friendly message  ✅")


async def test_recovery_fanout():
    db = Database(_tmp_db_path())
    await db.connect()
    # Old account verified with this RIT identity.
    await db.add_verified_user(111, "old#1", "Same Person", "person@rit.edu", 555)

    a, b = FakeGuild(1, "Alpha"), FakeGuild(2, "Beta")
    NEW = 222
    a.add_member(FakeMember(NEW, a, [a.roles[1]]))
    b.add_member(FakeMember(NEW, b, [b.roles[1]]))
    cog = v.Verification(FakeBot([a, b], db))
    monkey = {}
    _patch_guildutils(monkey)
    try:
        cog.pending[NEW] = v.PendingVerification(
            code="424242", email="person@rit.edu", real_name="",
            guild_id=None, created_at=time.time(), recovery=True,
        )
        inter = FakeInteraction(FakeMember(NEW, None, []), guild=None)
        await v.Verification.confirm.callback(cog, inter, "424242")
        # Record moved to the new account, old id gone, guild_id non-null.
        assert await db.verified_discord_id_for("person") == NEW
        assert await db.get_verified_user(111) is None
        row = await db.get_verified_user(NEW)
        assert row is not None and row["guild_id"] in (1, 2)
        # New account got the role in both shared servers.
        assert any(r.name == VERIFIED for r in a.get_member(NEW).roles)
        assert any(r.name == VERIFIED for r in b.get_member(NEW).roles)
    finally:
        _unpatch_guildutils(monkey)
        cog.cog_unload()
        await db.close()
    print("  C2 recovery: record transferred to new account + role fanned out  ✅")


# ── C3: a failed write can't poison the shared connection ────────────────────
async def test_db_rollback_hygiene():
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(1, "u1", "One", "a@rit.edu", 100)

        # Force the exact original failure: a NULL into the NOT NULL guild_id.
        # student_id_hash is supplied (it is also NOT NULL now) so that guild_id
        # stays the reason this raises.
        raised = False
        try:
            async with db._tx():
                await db.conn.execute(
                    "INSERT INTO verified_users "
                    "(discord_id, discord_username, real_name, email, "
                    "student_id_hash, guild_id, verified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (2, "u2", "Two", "b@rit.edu", "deadbeef", None, 123),
                )
        except Exception:
            raised = True
        assert raised, "the bad write should have raised"

        # The connection must remain fully usable for EVERY other feature.
        await db.add_verified_user(3, "u3", "Three", "c@rit.edu", 100)  # write still works
        assert await db.user_is_verified(1)          # read works
        assert await db.user_is_verified(3)
        assert not await db.user_is_verified(2)       # failed insert rolled back
        settings = await db.get_settings(424242)      # another feature's write+read
        assert settings["filter_phishing"] == 1
        await db.upsert_level(3, 50, 1, time.time())  # leveling still works
        assert (await db.get_level_row(3))["xp"] == 50
    finally:
        await db.close()
    print("  C3 hygiene: failed write rolled back; connection still serves all features  ✅")


# ── C4: encryption at rest ───────────────────────────────────────────────────
LEGACY_SCHEMA = """
CREATE TABLE verified_users (
    discord_id       INTEGER PRIMARY KEY,
    discord_username TEXT    NOT NULL,
    real_name        TEXT    NOT NULL,
    email            TEXT    NOT NULL UNIQUE,
    guild_id         INTEGER NOT NULL,
    verified_at      INTEGER NOT NULL,
    last_recovery_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    identity_key TEXT    NOT NULL DEFAULT ''
);
"""


def _make_legacy_db(rows, warnings=()) -> str:
    """A pre-encryption database, written with raw sqlite3 as an old build would."""
    path = _tmp_db_path()
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    c.executemany(
        "INSERT INTO verified_users (discord_id, discord_username, real_name, "
        "email, guild_id, verified_at, last_recovery_at) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    c.executemany(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, "
        "created_at, identity_key) VALUES (?,?,?,?,?,?)",
        warnings,
    )
    c.commit()
    c.close()
    return path


def _raw(path: str, sql: str):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


async def test_crypto_envelope():
    a = crypto.aad("verified_users", "real_name", 1)
    b = crypto.aad("verified_users", "real_name", 2)

    assert crypto.decrypt(crypto.encrypt("Aisha Okafor", a), a) == "Aisha Okafor"
    assert crypto.encrypt("x", a) != crypto.encrypt("x", a), "must be randomized"
    assert crypto.decrypt(crypto.encrypt("", a), a) == "", "empty string round-trip"

    # Ciphertext is bound to its row: it must not decrypt under another id.
    assert crypto.try_decrypt(crypto.encrypt("Aisha", a), b) is None

    # Tampering is detected rather than silently returning garbage.
    e = crypto.encrypt("Aisha", a)
    flipped = e[:20] + ("A" if e[20] != "A" else "B") + e[21:]
    assert crypto.try_decrypt(flipped, a) is None

    # A member legitimately named "v1:..." is not mistaken for ciphertext.
    assert crypto.try_decrypt("v1:Tiger", a) is None
    assert crypto.decrypt(crypto.encrypt("v1:Tiger", a), a) == "v1:Tiger"
    assert crypto.encrypt_if_plaintext("v1:Tiger", a) != "v1:Tiger"
    # ...and encrypting twice is a no-op, which is what makes the migration safe
    # to re-run.
    assert crypto.encrypt_if_plaintext(e, a) == e

    # The blind index is deterministic, but '' is never hashed — see below.
    assert crypto.blind_index("") == ""
    assert crypto.blind_index("aap1234") == crypto.blind_index("aap1234")
    assert len(crypto.blind_index("aap1234")) == 64
    assert crypto.blind_index("aap1234") != crypto.blind_index("aap1235")
    print("  C4 crypto: AEAD round-trip, row binding, tamper + 'v1:' handling  ✅")


async def test_pii_encrypted_at_rest():
    path = _tmp_db_path()
    db = Database(path)
    await db.connect()
    try:
        await db.add_verified_user(1, "riley_b", "Riley Barnes", "rab1045@rit.edu", 9)

        raw = _raw(path, "SELECT discord_username, real_name, email, student_id_hash "
                         "FROM verified_users")[0]
        for value in raw[:3]:
            assert value.startswith("v1:"), f"stored in cleartext: {value!r}"
        for secret in ("Riley Barnes", "rab1045@rit.edu", "riley_b", "rab1045"):
            assert secret not in str(raw), f"{secret!r} readable in the raw row"
        assert raw[3] == crypto.blind_index("rab1045")

        # ...but callers still see plaintext, unchanged.
        u = await db.get_verified_user(1)
        assert u["real_name"] == "Riley Barnes"
        assert u["email"] == "rab1045@rit.edu"
        assert u["discord_username"] == "riley_b"
    finally:
        await db.close()
    print("  C4 at rest: PII is ciphertext on disk, plaintext to callers  ✅")


async def test_blind_index_lookups():
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(1, "u", "A Person", "aap1234@rit.edu", 9)
        assert await db.student_id_is_registered("aap1234")
        assert await db.student_id_is_registered("AAP1234"), "must be case-insensitive"
        assert not await db.student_id_is_registered("zzz9999")
        assert await db.verified_discord_id_for("aap1234") == 1
        assert await db.verified_discord_id_for("nobody") is None
        assert await db.last_recovery_at_for("aap1234") == 0
        # student_id_for returns the blind index, which is what identity_key holds.
        assert await db.student_id_for(1) == crypto.blind_index("aap1234")
        assert await db.student_id_for(12345) == ""

        # The same person on the other RIT domain is the same student id, and the
        # UNIQUE constraint now enforces what every read already assumed.
        await db.add_verified_user(2, "u2", "A Person", "aap1234@g.rit.edu", 9)
        assert _raw(db.path, "SELECT COUNT(*) FROM verified_users")[0][0] == 1
        assert await db.verified_discord_id_for("aap1234") == 2
    finally:
        await db.close()
    print("  C4 lookups: student-id queries resolve through the blind index  ✅")


async def test_transfer_reencrypts():
    """Regression: ciphertext is bound to discord_id, so /recover must re-encrypt.

    A plain UPDATE of discord_id would leave the values authenticated against the
    old account and permanently undecryptable — breaking /whois for exactly the
    people who just recovered their account."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(111, "old_handle", "Sam Rivera", "sxr9001@rit.edu", 9)
        assert await db.transfer_verification("sxr9001", 222, "new_handle", 9)

        moved = await db.get_verified_user(222)      # must not raise
        assert moved["real_name"] == "Sam Rivera"
        assert moved["email"] == "sxr9001@rit.edu"
        assert moved["discord_username"] == "new_handle"
        assert moved["last_recovery_at"] > 0
        assert await db.get_verified_user(111) is None
        assert await db.verified_discord_id_for("sxr9001") == 222
        assert not await db.transfer_verification("nobody", 333, "x", 9)
    finally:
        await db.close()
    print("  C4 recovery: transfer re-encrypts under the new account id  ✅")


async def test_blank_identity_stays_blank():
    """'' means "unverified, match by account". If it were hashed, every
    unverified member everywhere would merge into one shared identity."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_warning(1, 500, 9, "spam")       # unverified member
        await db.add_warning(2, 501, 9, "spam")       # a different unverified member
        assert _raw(db.path, "SELECT identity_key FROM warnings")[0][0] == ""
        # Each is counted on their own account, not merged together.
        assert (await db.global_warnings(500))[1] == 1
        assert (await db.global_warnings(501))[1] == 1

        # A verified member's warning is stamped with their blind index.
        await db.add_verified_user(600, "u", "V Person", "vvv1234@rit.edu", 1)
        await db.add_warning(1, 600, 9, "rude")
        await db.add_warning(2, 600, 9, "rude again")
        keys = [r[0] for r in _raw(db.path, "SELECT identity_key FROM warnings")]
        assert crypto.blind_index("vvv1234") in keys
        assert "vvv1234" not in keys, "student id stored in cleartext"
        assert (await db.global_warnings(600)) == (2, 2)
        assert (await db.cross_server_warnings(600, 1)) == (1, 1)
    finally:
        await db.close()
    print("  C4 identity: blank keys stay blank; verified keys are hashed  ✅")


async def test_legacy_migration():
    path = _make_legacy_db(
        rows=[
            (1, "riley_b", "Riley Barnes", "rab1045@rit.edu", 9, 1000, 0),
            (2, "sam_r", "Sam Rivera", "sxr9001@g.rit.edu", 9, 1001, 55),
        ],
        warnings=[
            (9, 1, 3, "spam", 1200, "rab1045"),   # cleartext student id
            (9, 5, 3, "spam", 1201, ""),          # legacy blank
        ],
    )
    db = Database(path)
    await db.connect()
    try:
        u = await db.get_verified_user(1)
        assert u["real_name"] == "Riley Barnes" and u["email"] == "rab1045@rit.edu"
        assert (await db.get_verified_user(2))["last_recovery_at"] == 55

        # Nothing readable survives in the file itself.
        blob = open(path, "rb").read()
        for secret in (b"Riley Barnes", b"rab1045@rit.edu", b"Sam Rivera", b"rab1045"):
            assert secret not in blob, f"{secret!r} still in the raw database file"

        keys = [r[0] for r in _raw(path, "SELECT identity_key FROM warnings ORDER BY id")]
        assert keys == [crypto.blind_index("rab1045"), ""], keys

        # The email UNIQUE constraint is gone, replaced by one on student_id_hash.
        idx = _raw(path, "PRAGMA index_list(verified_users)")
        assert not any(r[3] == "u" for r in idx), "UNIQUE(email) autoindex survived"
        assert any(r[1] == "idx_verified_users_student_id_hash" and r[2] for r in idx)

        # And the lookups work on migrated data.
        assert await db.verified_discord_id_for("sxr9001") == 2
        assert await db.student_id_for(1) == crypto.blind_index("rab1045")

        bak = path + ".pre-encrypt.bak"
        assert os.path.exists(bak), "no pre-encryption backup was written"
        assert _raw(bak, "SELECT real_name FROM verified_users")[0][0] == "Riley Barnes"
    finally:
        await db.close()
    print("  C4 migration: legacy plaintext DB encrypted, indexes swapped, .bak kept  ✅")


async def test_migration_idempotent():
    path = _make_legacy_db([(1, "u", "Ada Lovelace", "aal1000@rit.edu", 9, 1000, 0)])
    db = Database(path)
    await db.connect()
    await db.close()
    bak = path + ".pre-encrypt.bak"
    stamp = (os.path.getmtime(bak), os.path.getsize(bak))
    row = _raw(path, "SELECT * FROM verified_users")[0]

    db = Database(path)
    await db.connect()
    try:
        assert (await db.get_verified_user(1))["real_name"] == "Ada Lovelace"
        assert _raw(path, "SELECT * FROM verified_users")[0] == row, "row was rewritten"
        assert (os.path.getmtime(bak), os.path.getsize(bak)) == stamp, ".bak overwritten"
    finally:
        await db.close()
    print("  C4 migration: re-running is a no-op (no double-encryption)  ✅")


async def test_migration_atomic_on_crash():
    """The whole migration is one transaction, so a crash leaves the ORIGINAL
    database behind — never a half-encrypted one."""
    path = _make_legacy_db([
        (i, f"u{i}", f"Person {i}", f"aaa{1000 + i}@rit.edu", 9, 1000, 0)
        for i in range(1, 6)
    ])
    before = _raw(path, "SELECT * FROM verified_users ORDER BY discord_id")

    real = crypto.encrypt_if_plaintext
    calls = {"n": 0}

    def boom(value, associated):
        calls["n"] += 1
        if calls["n"] == 7:                      # part-way through row 3
            raise RuntimeError("simulated crash mid-migration")
        return real(value, associated)

    crypto.encrypt_if_plaintext = boom
    try:
        raised = False
        try:
            await Database(path).connect()
        except RuntimeError:
            raised = True
        assert raised, "the simulated crash should have propagated"

        cols = {r[1] for r in _raw(path, "PRAGMA table_info(verified_users)")}
        assert "student_id_hash" not in cols, "schema change survived a crash"
        assert _raw(path, "SELECT * FROM verified_users ORDER BY discord_id") == before
        assert _raw(path, "SELECT COUNT(*) FROM crypto_meta "
                          "WHERE key='key_fingerprint'")[0][0] == 0
    finally:
        crypto.encrypt_if_plaintext = real

    # ...and the retry afterwards succeeds.
    db = Database(path)
    await db.connect()
    try:
        assert (await db.get_verified_user(3))["real_name"] == "Person 3"
    finally:
        await db.close()
    print("  C4 migration: crash rolls back completely; retry then succeeds  ✅")


async def test_wrong_key_refuses():
    path = _make_legacy_db([(1, "u", "Grace Hopper", "gbh1906@rit.edu", 9, 1000, 0)])
    db = Database(path)
    await db.connect()
    await db.close()
    before = _raw(path, "SELECT * FROM verified_users")

    crypto.load("99" * 32)                        # a different deployment's key
    try:
        raised = False
        try:
            await Database(path).connect()
        except EncryptionKeyMismatch:
            raised = True
        assert raised, "a mismatched ENCRYPTION_KEY must refuse to start"
        assert _raw(path, "SELECT * FROM verified_users") == before, "disk was modified"
    finally:
        crypto.load(os.environ["ENCRYPTION_KEY"])  # restore for later tests

    db = Database(path)
    await db.connect()
    try:
        assert (await db.get_verified_user(1))["real_name"] == "Grace Hopper"
    finally:
        await db.close()
    print("  C4 key: a mismatched key is refused, and the right one still works  ✅")


async def test_duplicate_student_id_aborts():
    """Two accounts on one student id can't both satisfy UNIQUE(student_id_hash),
    and picking a survivor is a human decision — so abort, touching nothing."""
    path = _make_legacy_db([
        (1, "u1", "One Person", "dup1234@rit.edu", 9, 1000, 0),
        (2, "u2", "Two Person", "dup1234@g.rit.edu", 9, 1001, 0),
    ])
    before = _raw(path, "SELECT * FROM verified_users ORDER BY discord_id")

    raised = None
    try:
        await Database(path).connect()
    except DuplicateStudentIdError as e:
        raised = str(e)
    assert raised, "duplicate student ids must abort the migration"
    assert "dup1234" in raised and "1" in raised and "2" in raised

    assert _raw(path, "SELECT * FROM verified_users ORDER BY discord_id") == before
    assert not os.path.exists(path + ".pre-encrypt.bak"), "wrote a .bak before aborting"
    cols = {r[1] for r in _raw(path, "PRAGMA table_info(verified_users)")}
    assert "student_id_hash" not in cols

    # Resolve it the way the error message says, and the migration then runs.
    c = sqlite3.connect(path)
    c.execute("DELETE FROM verified_users WHERE discord_id = 1")
    c.commit()
    c.close()
    db = Database(path)
    await db.connect()
    try:
        assert (await db.get_verified_user(2))["email"] == "dup1234@g.rit.edu"
    finally:
        await db.close()
    print("  C4 migration: duplicate student ids abort cleanly, then migrate  ✅")


# ── C5: encrypted roster backup / restore ────────────────────────────────────

async def test_backup_payload_is_ciphertext():
    """The whole reason this feature could come back: the file uploaded to a
    Discord channel must carry no readable PII."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(1, "ada_l", "Ada Lovelace", "ael1815@rit.edu", 9)
        await db.add_verified_user(2, "alan_t", "Alan Turing", "amt1912@rit.edu", 9)
        await db.add_verified_user(3, "other", "Other Server", "oth9999@rit.edu", 77)

        rows = await db.export_encrypted_rows(9)
        text = backup.render_backup_csv(rows, 9, crypto.load().fingerprint, "20260819")

        assert len(rows) == 2, "export must be scoped to one guild"
        for secret in ("Ada Lovelace", "ael1815@rit.edu", "ada_l", "ael1815",
                       "Alan Turing", "amt1912@rit.edu"):
            assert secret not in text, f"{secret!r} readable in the backup file"
        # Other guilds' members aren't in this guild's file at all. Checked on the
        # parsed ids, not by substring: base64 ciphertext contains every digit.
        assert {r["discord_id"] for r in rows} == {1, 2}
        assert "Other Server" not in text
        assert crypto.load().fingerprint in text, "no key fingerprint to check on restore"
        for r in rows:
            for col in ("discord_username", "real_name", "email"):
                assert r[col].startswith("v1:"), f"{col} left the DB decrypted"
    finally:
        await db.close()
    print("  C5 backup: uploaded roster is ciphertext, scoped to one guild  ✅")


async def test_backup_restore_round_trip():
    """Export -> wipe -> restore returns the original plaintext.

    This is the assertion that proves ciphertext may be moved verbatim: the AAD
    binds each value to its discord_id, so the restore only works because the id
    is preserved."""
    path = _tmp_db_path()
    db = Database(path)
    await db.connect()
    try:
        await db.add_verified_user(111, "sam_r", "Sam Rivera", "sxr9001@rit.edu", 9)
        await db.add_verified_user(222, "kim_j", "Jordan Kim", "jk9284@rit.edu", 9)
        rows = await db.export_encrypted_rows(9)
        fingerprint = crypto.load().fingerprint

        await db.conn.execute("DELETE FROM verified_users")
        await db.conn.commit()
        assert await db.get_verified_user(111) is None

        inserted, skipped = await db.import_encrypted_rows(rows, fingerprint)
        assert (inserted, skipped) == (2, 0)

        u = await db.get_verified_user(111)
        assert u["real_name"] == "Sam Rivera"
        assert u["email"] == "sxr9001@rit.edu"
        assert u["discord_username"] == "sam_r"
        # The blind index came back too, so lookups work without re-deriving it.
        assert await db.verified_discord_id_for("jk9284") == 222

        # Re-importing is a no-op: nothing is duplicated or clobbered.
        inserted, skipped = await db.import_encrypted_rows(rows, fingerprint)
        assert (inserted, skipped) == (0, 2)
        assert (await db.get_verified_user(111))["real_name"] == "Sam Rivera"
    finally:
        await db.close()
    print("  C5 restore: round-trip returns plaintext; re-import is a no-op  ✅")


async def test_restore_refuses_wrong_key():
    """Importing under the wrong key would store rows nothing can ever decrypt —
    strictly worse than an empty table."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(1, "u", "Grace Hopper", "gbh1906@rit.edu", 9)
        rows = await db.export_encrypted_rows(9)

        raised = False
        try:
            await db.import_encrypted_rows(rows, "deadbeef" * 4)
        except EncryptionKeyMismatch:
            raised = True
        assert raised, "a foreign fingerprint must be refused"

        await db.conn.execute("DELETE FROM verified_users")
        await db.conn.commit()
        assert await db.get_verified_user(1) is None, "nothing should have been written"
    finally:
        await db.close()
    print("  C5 restore: a backup from a different key is refused  ✅")


async def test_backup_file_parses_back():
    """The file features/backup.py writes is the file restore_roster.py reads.
    Asserted end-to-end so a header change can't silently break restores."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        await db.add_verified_user(1, "u1", "Ada Lovelace", "ael1815@rit.edu", 9)
        rows = await db.export_encrypted_rows(9)
        fingerprint = crypto.load().fingerprint
        text = backup.render_backup_csv(rows, 9, fingerprint, "20260819-142233")

        path = os.path.join(tempfile.gettempdir(), f"roster-test-{time.time_ns()}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            parsed_fp, parsed_rows = restore_roster.parse_backup(path)
        finally:
            os.remove(path)

        assert parsed_fp == fingerprint
        assert parsed_rows == rows, "parse_backup must reproduce the exported rows"

        await db.conn.execute("DELETE FROM verified_users")
        await db.conn.commit()
        assert await db.import_encrypted_rows(parsed_rows, parsed_fp) == (1, 0)
        assert (await db.get_verified_user(1))["real_name"] == "Ada Lovelace"
    finally:
        await db.close()
    print("  C5 backup: written file parses back and restores  ✅")


async def test_roster_export_cooldown():
    """The dashboard CSV is decrypted, so the 12h limit is the control that keeps
    it from becoming an unlimited PII tap."""
    db = Database(_tmp_db_path())
    await db.connect()
    try:
        assert await db.last_roster_export(9) == 0, "never exported -> no cooldown"

        await db.record_roster_export(9, 4242, 17)
        last = await db.last_roster_export(9)
        assert last > 0
        cooldown = config.ROSTER_EXPORT_COOLDOWN_HOURS * 3600
        assert int(time.time()) - last < cooldown, "should still be on cooldown"
        assert config.ROSTER_EXPORT_COOLDOWN_HOURS >= 12, "12h is a floor, not a default"

        # Scoped per guild — one server's export must not gate another's.
        assert await db.last_roster_export(77) == 0

        # Backdate past the window and it opens up again.
        await db.conn.execute(
            "UPDATE roster_exports SET last_export_at = ? WHERE guild_id = ?",
            (int(time.time()) - cooldown - 1, 9),
        )
        await db.conn.commit()
        assert int(time.time()) - await db.last_roster_export(9) > cooldown

        row = _raw(db.path, "SELECT exported_by, row_count FROM roster_exports")[0]
        assert row == (4242, 17), "export must be attributable to who ran it"
    finally:
        await db.close()
    print("  C5 export: roster CSV cooldown is per-guild, stored and audited  ✅")


async def test_backups_disable_switch():
    """BACKUP_INTERVAL_HOURS=-1 turns backups off everywhere."""
    assert config.BACKUP_INTERVAL_HOURS >= 12 or config.BACKUP_INTERVAL_HOURS == -1
    assert config.BACKUPS_ENABLED == (config.BACKUP_INTERVAL_HOURS > 0)

    # The cog is simply not loaded when off, so /backup doesn't exist either.
    loaded = []

    class _FakeBot:
        async def add_cog(self, cog):
            loaded.append(cog)

    real = config.BACKUP_INTERVAL_HOURS, config.BACKUPS_ENABLED
    try:
        config.BACKUP_INTERVAL_HOURS, config.BACKUPS_ENABLED = -1, False
        await backup.setup(_FakeBot())
        assert not loaded, "cog must not load when backups are disabled"
    finally:
        config.BACKUP_INTERVAL_HOURS, config.BACKUPS_ENABLED = real
    print("  C5 config: BACKUP_INTERVAL_HOURS=-1 disables backups globally  ✅")


async def main():
    print("Running verification + resilience tests...\n")
    await test_email_pool_isolation()
    await test_dm_fanout_and_data_preserved()
    await test_no_shared_server()
    await test_recovery_fanout()
    await test_db_rollback_hygiene()
    await test_crypto_envelope()
    await test_pii_encrypted_at_rest()
    await test_blind_index_lookups()
    await test_transfer_reencrypts()
    await test_blank_identity_stays_blank()
    await test_legacy_migration()
    await test_migration_idempotent()
    await test_migration_atomic_on_crash()
    await test_wrong_key_refuses()
    await test_duplicate_student_id_aborts()
    await test_backup_payload_is_ciphertext()
    await test_backup_restore_round_trip()
    await test_restore_refuses_wrong_key()
    await test_backup_file_parses_back()
    await test_roster_export_cooldown()
    await test_backups_disable_switch()
    print("\nALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
