"""University email verification via 6-digit OTP.

Flow
----
1. /verify name:<real name> email:<...@rit.edu | ...@g.rit.edu>
   → validates the email domain, checks it isn't already registered, emails a
     6-digit code, and stores a pending request in memory.
2. /confirm code:<123456>
   → checks the code; on success stores the member in the database (discord
     username, real name, email) and grants the Verified role across EVERY
     server the user shares with the bot, posting a celebration in each one's
     #welcome. Works in a DM as well as inside a server.

OTP requests live in memory only (cleared on restart); the permanent record of
who is verified lives in the database.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import random
import re
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

import config
import personality
from utils import guildutils as gu
from utils.emailer import EmailError, send_otp_email

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+)$")

# Account recovery: how long a RIT identity must wait between transfers, so
# accounts can't be shuffled rapidly (durable — stored on the record).
RECOVERY_COOLDOWN_DAYS = 7


@dataclass
class PendingVerification:
    code: str
    email: str
    real_name: str
    guild_id: int | None  # None when started in a DM; resolved at /confirm time
    created_at: float
    attempts: int = 0
    recovery: bool = False  # True = transfer an existing record to this new account


def _valid_domain(email: str) -> bool:
    m = EMAIL_RE.match(email.strip())
    if not m:
        return False
    return m.group(1).lower() in config.ALLOWED_EMAIL_DOMAINS


def _student_id(email: str) -> str:
    """The local part before '@' — the RIT student-id token, lowercased.

    Identity is keyed on this, so axp1234@rit.edu and axp1234@g.rit.edu are
    treated as the same student (can't double-verify across the two domains).
    """
    return email.strip().lower().split("@", 1)[0]


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # discord_id -> PendingVerification
        self.pending: dict[int, PendingVerification] = {}
        # Dedicated thread pool for the BLOCKING urllib email send. Kept separate
        # from asyncio's default executor on purpose: that default pool is also
        # what aiohttp uses for DNS resolution (getaddrinfo), so routing slow
        # 20s email sends through it would starve /ask's HTTP calls and make the
        # AI assistant lag until the emails drained. Its own 2-thread pool isolates
        # that I/O so no other feature is affected.
        self._email_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="otp-email"
        )

    def cog_unload(self) -> None:
        self._email_pool.shutdown(wait=False, cancel_futures=True)

    async def _send_otp(self, email: str, code: str, display_name: str, guild_name: str) -> None:
        """Send the OTP email on the dedicated pool (raises EmailError on failure)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._email_pool,
            functools.partial(send_otp_email, email, code, display_name, guild_name),
        )

    def _expired(self, p: PendingVerification) -> bool:
        return (time.time() - p.created_at) > config.OTP_TTL_MINUTES * 60

    def _live_pending_by_others(
        self, student_id: str, exclude_id: int
    ) -> PendingVerification | None:
        """Return another user's still-active pending verification for this RIT
        student ID, or None. Prunes any expired pendings it scans past.

        This is how an "abandoned" request frees up: there's no event when a user
        walks away, so we rely on the TTL — once expired, the entry is pruned here
        and no longer blocks anyone. So a given email is reserved for at most
        OTP_TTL_MINUTES. (Confirmed/failed requests are already deleted elsewhere.)
        """
        for uid, p in list(self.pending.items()):
            if self._expired(p):
                del self.pending[uid]
                continue
            if uid != exclude_id and _student_id(p.email) == student_id:
                return p
        return None

    # ── cross-server role fan-out ──────────────────────────────────────────
    async def _shared_members(self, user_id: int) -> list[discord.Member]:
        """Every guild TaigaBot shares with this user, as Member objects.

        Verification is global, so a DM `/verify` (where `interaction.user` is a
        User, not a Member) can still fan the Verified role out to every server
        the user is in with the bot. `get_member` hits the member cache (the
        members intent is on); a cache miss falls back to a fetch, and people who
        aren't in that guild raise and are skipped."""
        out: list[discord.Member] = []
        for g in self.bot.guilds:
            m = g.get_member(user_id)
            if m is None:
                try:
                    m = await g.fetch_member(user_id)
                except discord.HTTPException:
                    m = None
            if m is not None:
                out.append(m)
        return out

    async def _fan_out_verified(
        self, members: list[discord.Member]
    ) -> tuple[list[discord.Member], list[str], list[str]]:
        """Grant Verified to the user in each shared guild.

        Returns (newly_verified, ok_names, failed_names): members who just gained
        the role (so we welcome them there), names of guilds where the role is now
        applied, and names where the bot's role is too low to assign it."""
        newly: list[discord.Member] = []
        ok: list[str] = []
        failed: list[str] = []
        for m in members:
            vrole = gu.verified_role(m.guild)
            was_new = vrole is not None and vrole not in m.roles
            if await gu.promote_to_verified(m):
                if vrole is not None:
                    ok.append(m.guild.name)
                if was_new:
                    newly.append(m)
            else:
                failed.append(m.guild.name)
        return newly, ok, failed

    async def _post_welcome(self, member: discord.Member) -> None:
        """Best-effort public 'just verified' celebration in the guild's #welcome."""
        welcome = gu.welcome_channel(member.guild)
        if welcome is None:
            return
        sass = personality.say("verify_success", name=member.display_name)
        desc = f"🎉 {member.mention} just verified and joined the club! Say hi 👋"
        if sass:
            desc += f"\n*{sass}*"
        try:
            await welcome.send(embed=discord.Embed(description=desc, color=config.BOT_COLOR))
        except discord.HTTPException:
            pass

    @app_commands.command(
        name="verify", description="Verify with your RIT email to unlock the server."
    )
    @app_commands.describe(
        name="Your real name (first and last)",
        email="Your university email (@rit.edu or @g.rit.edu)",
    )
    @app_commands.checks.cooldown(1, 60.0)  # 1 /verify per 60s per user (anti email-spam)
    async def verify(self, interaction: discord.Interaction, name: str, email: str):
        member = interaction.user
        email = email.strip().lower()
        name = name.strip()

        # Already verified (anywhere this bot runs)? Re-apply the Verified role
        # across every server they share with the bot — no OTP needed.
        if await self.bot.db.user_is_verified(member.id):
            members = await self._shared_members(member.id)
            newly, ok, _ = await self._fan_out_verified(members)
            for m in newly:
                await self._post_welcome(m)
            extra = f" Re-applied your role in **{len(ok)}** server(s)." if ok else ""
            await interaction.response.send_message(
                "✅ You're already verified! Welcome back." + extra, ephemeral=True
            )
            return

        if len(name.split()) < 2:
            await interaction.response.send_message(
                "Please provide your **first and last name**, e.g. `/verify name:Jane Doe ...`",
                ephemeral=True,
            )
            return

        if not _valid_domain(email):
            allowed = " or ".join(f"`@{d}`" for d in config.ALLOWED_EMAIL_DOMAINS)
            await interaction.response.send_message(
                f"❌ That doesn't look like a valid university email. Use {allowed}.",
                ephemeral=True,
            )
            return

        # One account per student ID (both RIT domains count as the same person).
        if await self.bot.db.student_id_is_registered(_student_id(email)):
            await interaction.response.send_message(
                "❌ That RIT account is already linked to another verified member. "
                "If this is a mistake, contact an Eboard member.",
                ephemeral=True,
            )
            return

        # Someone else already mid-verification for this same RIT account? Block
        # the duplicate so we don't email a second code to the same inbox. Expired
        # / abandoned pendings are pruned in the scan, so this frees up after the TTL.
        conflict = self._live_pending_by_others(_student_id(email), member.id)
        if conflict is not None:
            secs_left = config.OTP_TTL_MINUTES * 60 - (time.time() - conflict.created_at)
            mins_left = max(1, int(secs_left // 60) + 1)
            await interaction.response.send_message(
                "❌ Someone is already verifying that RIT account right now. "
                f"If that's you on another device, finish with `/confirm` — otherwise "
                f"try again in about {mins_left} minute(s).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        code = f"{random.randint(0, 999999):06d}"
        try:
            guild_name = interaction.guild.name if interaction.guild else "TaigaBot"
            await self._send_otp(email, code, member.display_name, guild_name)
        except EmailError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        self.pending[member.id] = PendingVerification(
            code=code,
            email=email,
            real_name=name,
            guild_id=interaction.guild_id,
            created_at=time.time(),
        )
        await interaction.followup.send(
            f"📧 I emailed a 6-digit code to **{email}**.\n"
            f"Run `/confirm code:XXXXXX` within {config.OTP_TTL_MINUTES} minutes to finish.\n"
            f"*(Check spam/junk if you don't see it.)*",
            ephemeral=True,
        )

    @app_commands.command(
        name="recover",
        description="Lost your old Discord? Recover your verification on this account with your RIT email.",
    )
    @app_commands.describe(email="The RIT email you originally verified with")
    @app_commands.checks.cooldown(1, 60.0)  # 1 /recover per 60s per user (anti email-spam)
    async def recover(self, interaction: discord.Interaction, email: str):
        member = interaction.user
        email = email.strip().lower()

        # Already verified on THIS account — nothing to recover.
        if await self.bot.db.user_is_verified(member.id):
            await interaction.response.send_message(
                "✅ This account is already verified — nothing to recover.", ephemeral=True
            )
            return

        if not _valid_domain(email):
            allowed = " or ".join(f"`@{d}`" for d in config.ALLOWED_EMAIL_DOMAINS)
            await interaction.response.send_message(
                f"❌ That doesn't look like a valid university email. Use {allowed}.",
                ephemeral=True,
            )
            return

        sid = _student_id(email)

        # Must already be registered to recover; otherwise it's a normal /verify.
        if not await self.bot.db.student_id_is_registered(sid):
            await interaction.response.send_message(
                "❌ That RIT email isn't linked to any account yet — use `/verify` to "
                "verify normally.",
                ephemeral=True,
            )
            return

        # Rate limit: one transfer per RIT identity per RECOVERY_COOLDOWN_DAYS, so
        # accounts can't be shuffled rapidly.
        last = await self.bot.db.last_recovery_at_for(sid)
        window = RECOVERY_COOLDOWN_DAYS * 86400
        if last and time.time() - last < window:
            days_left = max(1, int((window - (time.time() - last)) // 86400) + 1)
            await interaction.response.send_message(
                f"⏳ That RIT account was recovered recently. Try again in about "
                f"{days_left} day(s), or ask an Eboard member for help.",
                ephemeral=True,
            )
            return

        # Someone else already mid-verification/recovery for this account?
        conflict = self._live_pending_by_others(sid, member.id)
        if conflict is not None:
            await interaction.response.send_message(
                "❌ Someone is already verifying that RIT account right now. Try again "
                "shortly.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        code = f"{random.randint(0, 999999):06d}"
        try:
            guild_name = interaction.guild.name if interaction.guild else "TaigaBot"
            await self._send_otp(email, code, member.display_name, guild_name)
        except EmailError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        self.pending[member.id] = PendingVerification(
            code=code, email=email, real_name="", guild_id=interaction.guild_id,
            created_at=time.time(), recovery=True,
        )
        await interaction.followup.send(
            f"📧 To move your verification here, I emailed a 6-digit code to **{email}**.\n"
            f"Run `/confirm code:XXXXXX` within {config.OTP_TTL_MINUTES} minutes. This "
            f"transfers your verification to **this** account and removes the Verified role "
            f"from your old one across every server.\n*(Check spam/junk.)*",
            ephemeral=True,
        )

    @app_commands.command(
        name="confirm", description="Enter the 6-digit code from your verification email."
    )
    @app_commands.describe(code="The 6-digit code sent to your email")
    async def confirm(self, interaction: discord.Interaction, code: str):
        member = interaction.user
        guild = interaction.guild
        p = self.pending.get(member.id)

        if p is None:
            await interaction.response.send_message(
                "You don't have a pending verification. Start with `/verify`.", ephemeral=True
            )
            return

        if self._expired(p):
            del self.pending[member.id]
            await interaction.response.send_message(
                "⏳ That code expired. Run `/verify` again to get a new one.", ephemeral=True
            )
            return

        code = code.strip().replace(" ", "")
        if code != p.code:
            p.attempts += 1
            remaining = config.OTP_MAX_ATTEMPTS - p.attempts
            if remaining <= 0:
                del self.pending[member.id]
                await interaction.response.send_message(
                    "❌ Too many wrong attempts. Run `/verify` to start over.", ephemeral=True
                )
                return
            sass = personality.say("verify_wrong_code")
            await interaction.response.send_message(
                f"❌ Wrong code. {remaining} attempt(s) left." + (f"\n*{sass}*" if sass else ""),
                ephemeral=True,
            )
            return

        # ── Account recovery: move the record here, then auto-strip the OLD
        #    account's Verified role across every server (no Eboard action) ──
        if p.recovery:
            await interaction.response.defer(ephemeral=True, thinking=True)
            members = await self._shared_members(member.id)
            if not members:
                await interaction.followup.send(
                    "❌ I couldn't find any server we both share. Join a server that has "
                    "TaigaBot, then run `/confirm` again.",
                    ephemeral=True,
                )
                return  # keep pending so a quick join + retry works within the TTL
            primary_guild_id = interaction.guild_id or members[0].guild.id
            sid = _student_id(p.email)
            old_id = await self.bot.db.verified_discord_id_for(sid)
            moved = await self.bot.db.transfer_verification(
                sid, member.id, str(member), primary_guild_id
            )
            del self.pending[member.id]
            if not moved:
                await interaction.followup.send(
                    "❌ Couldn't find a verification to recover — it may have been "
                    "removed. Try `/verify` instead.",
                    ephemeral=True,
                )
                return

            # Automatically remove the old account's Verified role in EVERY server
            # the bot is in (and re-gate it as Unverified).
            revoked = 0
            if old_id and old_id != member.id:
                for g in self.bot.guilds:
                    old_member = g.get_member(old_id)
                    if old_member and await gu.demote_to_unverified(
                        old_member,
                        reason="TaigaBot: verification transferred to a new account",
                    ):
                        revoked += 1

            # Grant Verified on the NEW account across every shared server.
            newly, ok, _ = await self._fan_out_verified(members)
            for m in newly:
                await self._post_welcome(m)

            # Audit trail for Eboard.
            embed = discord.Embed(
                title="♻️ Account recovery",
                description=(
                    f"Verification transferred to {member.mention} (`{member.id}`).\n"
                    f"Old account `{old_id}` had its Verified role removed across "
                    f"**{revoked}** server(s)."
                ),
                color=config.BOT_COLOR,
            )
            await gu.log_mod_action(guild or members[0].guild, embed)

            await interaction.followup.send(
                "✅ Recovered! Your verification now lives on **this** account, your old "
                "account's Verified role was removed everywhere, and I re-applied your role "
                f"in **{len(ok)}** server(s). Welcome back!",
                ephemeral=True,
            )
            return

        # Re-check the student ID wasn't claimed between /verify and /confirm.
        if await self.bot.db.student_id_is_registered(_student_id(p.email)):
            del self.pending[member.id]
            await interaction.response.send_message(
                "❌ That RIT account was just registered by someone else.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        members = await self._shared_members(member.id)
        if not members:
            await interaction.followup.send(
                "❌ I couldn't find any server we both share. Join a server that has "
                "TaigaBot, then run `/confirm` again to finish.",
                ephemeral=True,
            )
            return  # keep pending; nothing written to the DB yet

        # Store one shared guild as the record's home (guild_id is NOT NULL); the
        # role itself is applied across every shared server below.
        primary_guild_id = interaction.guild_id or members[0].guild.id
        await self.bot.db.add_verified_user(
            discord_id=member.id,
            discord_username=str(member),
            real_name=p.real_name,
            email=p.email,
            guild_id=primary_guild_id,
        )
        del self.pending[member.id]

        # Fan the Verified role out to every shared server, posting a welcome
        # wherever the role was newly granted.
        newly, ok, failed = await self._fan_out_verified(members)
        for m in newly:
            await self._post_welcome(m)

        sass = personality.say("verify_success", name=member.display_name)
        if ok:
            names = ", ".join(f"**{n}**" for n in ok)
            head = f"🎉 You're verified! Unlocked **{len(ok)}** server(s): {names}."
        else:
            head = "🎉 You're verified! Your access applies wherever TaigaBot manages roles."
        msg = head + (f"\n\n*{sass}*" if sass else "")
        if failed:
            msg += (
                f"\n\n⚠️ I couldn't assign the role in: {', '.join(failed)} "
                "(my role may be too low there — ping an Eboard member)."
            )
        await interaction.followup.send(msg, ephemeral=True)

    # ── Eboard tools ──────────────────────────────────────────────────────
    @app_commands.command(
        name="whois", description="(Eboard) Look up a member's verified info."
    )
    @app_commands.describe(member="The member to look up")
    async def whois(self, interaction: discord.Interaction, member: discord.Member):
        from utils.checks import member_has_role

        if not (
            interaction.user.guild_permissions.administrator
            or member_has_role(interaction.user, config.EBOARD_ROLE_NAME)
        ):
            await interaction.response.send_message(
                f"⛔ Only **{config.EBOARD_ROLE_NAME}** can use this.", ephemeral=True
            )
            return

        # Looking up the bot itself? Skip the DB and sass them.
        if member.id == self.bot.user.id:
            quip = random.choice([
                "tf you mean who am I? I'm the Palmtop Tiger. Obviously.",
                "Run /whois on me again. I dare you. ...I'm the one in charge here.",
                "Me? I don't need verifying, I AM the verification. Hmph.",
                "Who am I? Rude. I'm TaigaBot. Your better, basically.",
            ])
            await interaction.response.send_message(quip, ephemeral=True)
            return

        row = await self.bot.db.get_verified_user(member.id)
        if row is None:
            await interaction.response.send_message(
                f"{member.mention} is not verified.", ephemeral=True
            )
            return
        embed = discord.Embed(title=f"Verified info — {member}", color=config.BOT_COLOR)
        embed.add_field(name="Real name", value=row["real_name"], inline=False)
        embed.add_field(name="Email", value=row["email"], inline=False)
        embed.add_field(name="Discord", value=row["discord_username"], inline=False)
        embed.add_field(
            name="Verified", value=f"<t:{row['verified_at']}:R>", inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
