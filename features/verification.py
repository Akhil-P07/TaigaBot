"""University email verification via 6-digit OTP.

Flow
----
1. /verify name:<real name> email:<...@rit.edu | ...@g.rit.edu>
   → validates the email domain, checks it isn't already registered, emails a
     6-digit code, and stores a pending request in memory.
2. /confirm code:<123456>
   → checks the code; on success stores the member in the database
     (discord username, real name, email), swaps Unverified → Verified, and
     posts a celebration in #welcome.

OTP requests live in memory only (cleared on restart); the permanent record of
who is verified lives in the database.
"""
from __future__ import annotations

import asyncio
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
    guild_id: int
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

        # Already verified (anywhere this bot runs)? Just make sure the roles are
        # right in this guild — no OTP needed.
        if await self.bot.db.user_is_verified(member.id):
            if isinstance(member, discord.Member):
                await gu.promote_to_verified(member)
            await interaction.response.send_message(
                "✅ You're already verified! Welcome back.", ephemeral=True
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
            await asyncio.to_thread(send_otp_email, email, code, member.display_name, guild_name)
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
            await asyncio.to_thread(send_otp_email, email, code, member.display_name, guild_name)
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
            sid = _student_id(p.email)
            old_id = await self.bot.db.verified_discord_id_for(sid)
            moved = await self.bot.db.transfer_verification(
                sid, member.id, str(member), p.guild_id
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

            # Grant Verified on the new account in this server.
            await gu.promote_to_verified(member)

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
            await gu.log_mod_action(guild, embed)

            await interaction.followup.send(
                "✅ Recovered! Your verification now lives on **this** account, and your "
                "old account's Verified role has been removed everywhere. Welcome back!",
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

        await self.bot.db.add_verified_user(
            discord_id=member.id,
            discord_username=str(member),
            real_name=p.real_name,
            email=p.email,
            guild_id=p.guild_id,
        )
        del self.pending[member.id]

        # Swap roles (Unverified → Verified).
        if not await gu.promote_to_verified(member):
            await interaction.followup.send(
                "✅ Verified in the database, but I couldn't change your roles "
                "(my role may be too low). Ping an Eboard member.",
                ephemeral=True,
            )
            return

        sass = personality.say("verify_success", name=member.display_name)
        await interaction.followup.send(
            "🎉 You're verified! The rest of the server is now unlocked. Welcome to the AI Club!"
            + (f"\n\n*{sass}*" if sass else ""),
            ephemeral=True,
        )

        # Public celebration.
        welcome = gu.welcome_channel(guild)
        if welcome:
            desc = f"🎉 {member.mention} just verified and joined the club! Say hi 👋"
            if sass:
                desc += f"\n*{personality.say('verify_success', name=member.display_name)}*"
            embed = discord.Embed(
                description=desc,
                color=config.BOT_COLOR,
            )
            try:
                await welcome.send(embed=embed)
            except discord.HTTPException:
                pass

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

    @app_commands.command(
        name="unverify",
        description="(Eboard) Remove a member's verification (frees their email).",
    )
    @app_commands.describe(member="The member to un-verify")
    async def unverify(self, interaction: discord.Interaction, member: discord.Member):
        from utils.checks import member_has_role

        if not (
            interaction.user.guild_permissions.administrator
            or member_has_role(interaction.user, config.EBOARD_ROLE_NAME)
        ):
            await interaction.response.send_message(
                f"⛔ Only **{config.EBOARD_ROLE_NAME}** can use this.", ephemeral=True
            )
            return

        await self.bot.db.remove_verified_user(member.id)
        guild = interaction.guild
        unverified = gu.unverified_role(guild)
        verified = gu.verified_role(guild)
        try:
            if verified and verified in member.roles:
                await member.remove_roles(verified, reason="Unverified by Eboard")
            if unverified and unverified not in member.roles:
                await member.add_roles(unverified, reason="Unverified by Eboard")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(
            f"♻️ {member.mention} has been un-verified.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
