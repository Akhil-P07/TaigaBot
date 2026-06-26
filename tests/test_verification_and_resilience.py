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
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import time

# Make the repo root importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import config  # noqa: E402
from database import Database  # noqa: E402
from utils import guildutils as gu  # noqa: E402
import features.verification as v  # noqa: E402

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
        raised = False
        try:
            async with db._tx():
                await db.conn.execute(
                    "INSERT INTO verified_users "
                    "(discord_id, discord_username, real_name, email, guild_id, verified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (2, "u2", "Two", "b@rit.edu", None, 123),
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


async def main():
    print("Running verification + resilience tests...\n")
    await test_email_pool_isolation()
    await test_dm_fanout_and_data_preserved()
    await test_no_shared_server()
    await test_recovery_fanout()
    await test_db_rollback_hygiene()
    print("\nALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
