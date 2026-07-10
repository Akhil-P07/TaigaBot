"""Auto-moderation + Eboard moderation commands.

Automod (toggle each filter on/off with /automod):
  • filter_words    — deletes messages containing banned words
  • filter_invites  — deletes Discord invite links
  • filter_spam     — flags rapid repeated/identical messages, auto-warns the
                      offender, and DMs the Eboard
  • filter_mentions — deletes messages with too many user mentions
  • filter_caps     — deletes very long ALL-CAPS messages (off by default)
  • filter_phishing — deletes suspected phishing/scam messages using a small
                      ML model (trained offline; see utils/phishing.py), then
                      auto-warns the offender and alerts the Eboard

Moderation commands (Eboard role only): /kick /ban /timeout /warn /warnings
/clearwarnings /purge. Every command checks the caller's role via is_eboard().
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

import config
import personality
from utils import guildutils as gu
from utils.checks import is_eboard, member_has_role
from utils.phishing import MODEL as PHISHING_MODEL

log = logging.getLogger("taigabot.automod")

INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)", re.I)

# ── personal-contact / solicitation filter ────────────────────────────────
# Blocks the common shapes of solicitation: sharing personal contact info
# (phone/email), payment handles, and "reach me off-server" pitches. Tuned for
# precision so ordinary chat isn't caught.
#
# Phone: optional +country code then a 10-digit number with common separators.
# The digit boundaries stop it matching inside long ID strings (e.g. 17-19-digit
# Discord snowflakes or order numbers).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Payment handles are almost always solicitation in a club chat.
_PAYMENT_RE = re.compile(
    r"\b(cash\s?app|\$[A-Za-z][A-Za-z0-9]{2,}|venmo|zelle|paypal(?:\.me)?|wire\s+transfer)\b",
    re.I,
)
# Off-platform solicitation: an external messenger named alongside a "<verb> me"
# pitch (requiring the "me/us" object keeps ordinary mentions from tripping it).
_PLATFORM_RE = re.compile(
    r"\b(whats\s?app|telegram|signal\s+(?:me|app|group)|snapchat|kik|wechat)\b", re.I
)
_SOLICIT_VERB_RE = re.compile(
    r"\b(?:dm|pm|text|call|contact|reach|add|message|msg|ping)\s+(?:me|us)\b"
    r"|\bhmu\b|\bhit\s+me\s+up\b|\binbox\s+me\b",
    re.I,
)


def _contact_reason(content: str) -> str | None:
    """Return a short reason if `content` shares contact info / solicits, else None."""
    if _PHONE_RE.search(content):
        return "sharing personal phone numbers isn't allowed here."
    if _EMAIL_RE.search(content):
        return "sharing personal email addresses isn't allowed here."
    if _PAYMENT_RE.search(content):
        return "soliciting payments or money transfers isn't allowed here."
    if _PLATFORM_RE.search(content) and _SOLICIT_VERB_RE.search(content):
        return "soliciting people to contact you off-server isn't allowed here."
    return None
MAX_MENTIONS = 10  # delete messages with MORE than this many user+role pings (mention-bomb guard)
SPAM_WINDOW_SEC = 7
SPAM_THRESHOLD = 5  # messages within window
CAPS_MIN_LEN = 12
# Cross-channel duplicate spam: the SAME message posted in this many DISTINCT
# channels within the window is flagged (catches low-rate cross-posting that the
# message-rate check above misses).
CROSSPOST_CHANNELS = 3
CROSSPOST_WINDOW_SEC = 20

# Auto-warn on spam: record a warning (by the bot). Rate-limited per user so one
# burst — or a persistent spammer — can't flood the warning log.
AUTOWARN_SPAM = True
AUTOWARN_COOLDOWN_SEC = 60
# Only escalate to the Eboard (DM them) once a user has racked up this many total
# warnings, and then only every this-many afterwards (e.g. at 3, 6, 9, …) so the
# Eboard's DMs are never flooded by a single repeat offender.
SPAM_WARN_ESCALATE = 3


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> recent message timestamps
        self._recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=SPAM_THRESHOLD))
        # (guild_id, user_id) -> recent (ts, channel_id, content_key, message) for
        # the cross-channel duplicate-content check.
        self._recent_posts: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=25))
        # (guild_id, user_id) -> last auto-warn timestamp (anti-flood)
        self._last_autowarn: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> count of auto-warns this session (drives the
        # "DM Eboard every Nth" escalation, independent of manual warns/clears)
        self._autowarn_count: dict[tuple[int, int], int] = {}

    # ── deleted-message audit log ─────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Post an audit entry to the mod-log when a message is deleted.

        The content comes from discord.py's in-memory message cache, so this adds
        no storage and no extra RAM (the cache is already on). It therefore fires
        only for messages still in the cache — i.e. recently-posted ones, which
        are the ones that actually get deleted. Who deleted the message is NOT in
        the gateway event, so we read it from the guild's audit log (requires the
        bot's "View Audit Log" permission). Discord writes NO audit entry when a
        user deletes their own message, so an unmatched delete is reported as a
        self-delete.
        """
        if message.guild is None or message.author.bot:
            return

        content = message.content or ""
        attachments = ", ".join(a.filename for a in message.attachments)
        deleter = await self._who_deleted(message)

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Sent by",
            value=f"{message.author.mention} ({message.author} · `{message.author.id}`)",
            inline=False,
        )
        if deleter is not None and deleter.id != message.author.id:
            deleted_by = f"{deleter.mention} ({deleter} · `{deleter.id}`)"
        else:
            deleted_by = f"{message.author.mention} (self-delete or unknown)"
        embed.add_field(name="Deleted by", value=deleted_by, inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(
            name="Content", value=(content[:1024] if content else "*(no text)*"), inline=False
        )
        if attachments:
            embed.add_field(name="Attachments", value=attachments[:1024], inline=False)

        await gu.log_mod_action(message.guild, embed)

    async def _who_deleted(self, message: discord.Message):
        """Best-effort: who deleted `message`, from the guild audit log.

        Returns the deleter, or None when we can't tell (usually a self-delete —
        Discord logs no entry for those). Delete entries are batched by Discord,
        so we match on author + channel within a short recency window.
        """
        guild = message.guild
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(
                limit=5, action=discord.AuditLogAction.message_delete
            ):
                extra_channel = getattr(entry.extra, "channel", None)
                if (
                    entry.target is not None
                    and entry.target.id == message.author.id
                    and extra_channel is not None
                    and extra_channel.id == message.channel.id
                    and (discord.utils.utcnow() - entry.created_at).total_seconds() < 15
                ):
                    return entry.user
        except discord.Forbidden:
            pass
        return None

    # ── automod listener ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        # Never moderate Eboard / admins.
        if isinstance(message.author, discord.Member) and (
            message.author.guild_permissions.administrator
            or member_has_role(message.author, config.EBOARD_ROLE_NAME)
        ):
            return

        settings = await self.bot.db.get_settings(message.guild.id)
        if not settings["automod_enabled"]:
            return

        content = message.content
        lowered = content.lower()

        async def announce(reason: str):
            """Post the in-channel notice and mod-log entry (no deletion)."""
            sass = personality.say("automod")
            try:
                await message.channel.send(
                    f"{message.author.mention} {sass or reason}", delete_after=6
                )
            except discord.HTTPException:
                pass
            embed = discord.Embed(
                title="🤖 Automod",
                description=f"{reason}\n**User:** {message.author} (`{message.author.id}`)\n"
                f"**Channel:** {message.channel.mention}",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Content", value=(content[:1000] or "*(empty)*"), inline=False)
            await gu.log_mod_action(message.guild, embed)

        async def punish(reason: str):
            try:
                await message.delete()
            except discord.HTTPException:
                return
            await announce(reason)

        # banned words
        if settings["filter_words"]:
            words = await self.bot.db.get_banned_words(message.guild.id)
            if any(w in lowered for w in words):
                await punish("that message contained a banned word.")
                return

        # invite links
        if settings["filter_invites"] and INVITE_RE.search(content):
            await punish("posting invite links isn't allowed here.")
            return

        # phishing / scam — a small ML model (trained offline on a Discord
        # phishing dataset; see utils/phishing.py) scores the message. Tuned for
        # precision so legit chatter isn't deleted. Escalates to the Eboard on
        # every hit (scams are rarer and more serious than rate-spam).
        if settings["filter_phishing"] and PHISHING_MODEL is not None and content.strip():
            if PHISHING_MODEL.is_phishing(content):
                await punish("that message looks like a phishing or scam link.")
                if AUTOWARN_SPAM:
                    await self._autowarn(
                        message,
                        reason="Automod: suspected phishing/scam message",
                        label="posting a suspected phishing/scam message",
                        escalate_every=1,
                    )
                return

        # personal contact info / solicitation — blocks phone numbers, emails,
        # payment handles, and "reach me off-server" pitches to curb solicitation.
        if settings["filter_contact"] and content.strip():
            reason = _contact_reason(content)
            if reason:
                await punish(reason)
                return

        # mass mentions — guards against mention bombing. Covers @everyone/@here
        # (not counted in message.mentions) and too many pings. Uses raw_* counts,
        # which include REPEATS of the same user/role (message.mentions dedupes,
        # so 20x @same-person would otherwise read as 1).
        if settings["filter_mentions"]:
            if message.mention_everyone:
                await punish("please don't ping @everyone or @here.")
                return
            ping_count = len(message.raw_mentions) + len(message.raw_role_mentions)
            if ping_count > MAX_MENTIONS:
                await punish("please don't mass-mention people.")
                return

        # caps
        if settings["filter_caps"] and len(content) >= CAPS_MIN_LEN:
            letters = [c for c in content if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
                await punish("please don't type in all caps.")
                return

        # spam
        if settings["filter_spam"]:
            key = (message.guild.id, message.author.id)
            now = time.time()

            # (a) cross-channel duplicate content: the SAME message in several
            # DISTINCT channels within the window (catches slow cross-posting).
            content_key = content.strip().lower()
            if content_key:
                posts = self._recent_posts[key]
                posts.append((now, message.channel.id, content_key, message))
                while posts and now - posts[0][0] > CROSSPOST_WINDOW_SEC:
                    posts.popleft()
                dupes = [p for p in posts if p[2] == content_key]
                if len({p[1] for p in dupes}) >= CROSSPOST_CHANNELS:
                    burst = [p[3] for p in dupes]
                    posts.clear()
                    await self._bulk_delete(message.channel, burst)
                    await announce("please don't post the same message across channels.")
                    if AUTOWARN_SPAM:
                        await self._autowarn(
                            message,
                            reason="Automod: spamming (too many messages too fast)",
                            label="spamming",
                        )
                    return

            # (b) rapid messages (any content) within the short window.
            dq = self._recent[key]
            dq.append((now, message))
            if len(dq) == SPAM_THRESHOLD and (now - dq[0][0]) < SPAM_WINDOW_SEC:
                # Delete the whole burst by this user, not just the last message.
                burst = [m for _, m in dq]
                dq.clear()
                await self._bulk_delete(message.channel, burst)
                await announce("you're sending messages too fast — slow down.")
                if AUTOWARN_SPAM:
                    await self._autowarn(
                        message,
                        reason="Automod: spamming (too many messages too fast)",
                        label="spamming",
                    )
                return

    # ── automod config (Eboard) ───────────────────────────────────────────
    automod = app_commands.Group(
        name="automod", description="Configure auto-moderation (Eboard only)."
    )

    _FILTERS = {
        "all": "automod_enabled",
        "words": "filter_words",
        "invites": "filter_invites",
        "spam": "filter_spam",
        "mentions": "filter_mentions",
        "caps": "filter_caps",
        "phishing": "filter_phishing",
        "contact": "filter_contact",
    }

    @automod.command(name="enable", description="Enable automod, or one specific filter.")
    @app_commands.describe(filter="Which filter (default: all)")
    @app_commands.choices(
        filter=[app_commands.Choice(name=k, value=k) for k in _FILTERS]
    )
    @is_eboard()
    async def automod_enable(
        self, interaction: discord.Interaction, filter: app_commands.Choice[str] | None = None
    ):
        key = self._FILTERS[filter.value if filter else "all"]
        await self.bot.db.set_setting(interaction.guild_id, key, 1)
        await interaction.response.send_message(
            f"✅ Enabled `{filter.value if filter else 'all'}`.", ephemeral=True
        )

    @automod.command(name="disable", description="Disable automod, or one specific filter.")
    @app_commands.describe(filter="Which filter (default: all)")
    @app_commands.choices(
        filter=[app_commands.Choice(name=k, value=k) for k in _FILTERS]
    )
    @is_eboard()
    async def automod_disable(
        self, interaction: discord.Interaction, filter: app_commands.Choice[str] | None = None
    ):
        key = self._FILTERS[filter.value if filter else "all"]
        await self.bot.db.set_setting(interaction.guild_id, key, 0)
        await interaction.response.send_message(
            f"🛑 Disabled `{filter.value if filter else 'all'}`.", ephemeral=True
        )

    @automod.command(name="status", description="Show current automod settings.")
    @is_eboard()
    async def automod_status(self, interaction: discord.Interaction):
        s = await self.bot.db.get_settings(interaction.guild_id)
        words = await self.bot.db.get_banned_words(interaction.guild_id)

        def onoff(v):
            return "🟢 on" if v else "🔴 off"

        embed = discord.Embed(title="🤖 Automod status", color=config.BOT_COLOR)
        embed.add_field(name="Master switch", value=onoff(s["automod_enabled"]), inline=False)
        embed.add_field(name="Banned words", value=onoff(s["filter_words"]))
        embed.add_field(name="Invite links", value=onoff(s["filter_invites"]))
        embed.add_field(name="Spam", value=onoff(s["filter_spam"]))
        embed.add_field(name="Mass mentions", value=onoff(s["filter_mentions"]))
        embed.add_field(name="All-caps", value=onoff(s["filter_caps"]))
        phishing_state = onoff(s["filter_phishing"])
        if PHISHING_MODEL is None:
            phishing_state += " ⚠️ *(model not loaded)*"
        embed.add_field(name="Phishing/scam", value=phishing_state)
        embed.add_field(name="Contact/solicitation", value=onoff(s["filter_contact"]))
        embed.add_field(
            name=f"Banned word list ({len(words)})",
            value=", ".join(f"`{w}`" for w in words) if words else "*(empty)*",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod.command(name="addword", description="Add a banned word.")
    @app_commands.describe(word="The word/phrase to ban")
    @is_eboard()
    async def addword(self, interaction: discord.Interaction, word: str):
        await self.bot.db.add_banned_word(interaction.guild_id, word)
        await interaction.response.send_message(
            f"✅ Added `{word.lower()}` to the banned list.", ephemeral=True
        )

    @automod.command(name="removeword", description="Remove a banned word.")
    @app_commands.describe(word="The word/phrase to unban")
    @is_eboard()
    async def removeword(self, interaction: discord.Interaction, word: str):
        await self.bot.db.remove_banned_word(interaction.guild_id, word)
        await interaction.response.send_message(
            f"✅ Removed `{word.lower()}` from the banned list.", ephemeral=True
        )

    # ── moderation commands (Eboard) ──────────────────────────────────────
    @staticmethod
    def _forbidden_text(e: discord.Forbidden, action: str, perm: str) -> str:
        """A clear message for a 403 — distinguishing the server's 2FA-for-mods
        requirement (which blocks even a fully-permissioned bot) from a plain
        missing-permission / role-position problem."""
        if e.code == 60003:
            return (
                f"⛔ This server **requires 2FA for moderation actions**, but the bot's "
                f"owner account doesn't have 2FA enabled — so Discord blocks {action} "
                f"even with permissions. Enable 2FA on the bot owner's Discord account, "
                f"or turn off **Server Settings → Safety Setup → Require 2FA**."
            )
        return (
            f"⛔ I couldn't {action} — I need the **{perm}** permission and my role must "
            f"be above theirs."
        )

    @staticmethod
    def _cannot_act(interaction: discord.Interaction, member: discord.Member) -> str | None:
        """Return a human-readable reason this action can't proceed, or None if
        it's fine. Catches the usual failures up front (self/owner/hierarchy) so
        we give a clear message instead of a generic 'something went wrong'."""
        if member.id == interaction.user.id:
            return "🙃 You can't do that to yourself."
        if member.id == interaction.guild.owner_id:
            return "⛔ I can't action the server owner."
        if member.id == interaction.guild.me.id:
            return "🙃 I'm not going to do that to myself."
        if member.top_role >= interaction.guild.me.top_role:
            return (
                "⛔ Their highest role is above (or equal to) mine, so Discord won't "
                "let me. Move my **TaigaBot** role above theirs in "
                "**Server Settings → Roles**."
            )
        return None

    @app_commands.command(name="kick", description="(Eboard) Kick a member.")
    @app_commands.describe(member="Member to kick", reason="Reason")
    @is_eboard()
    async def kick(
        self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
    ):
        await interaction.response.defer(ephemeral=True)
        blocked = self._cannot_act(interaction, member)
        if blocked:
            await interaction.followup.send(blocked, ephemeral=True)
            return
        # DM before kicking — afterwards we can't reach them.
        dmed = await self._dm_action(
            member, interaction.guild.name, "Kicked", reason, discord.Color.orange()
        )
        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
        except discord.Forbidden as e:
            await interaction.followup.send(
                self._forbidden_text(e, "kick them", "Kick Members"), ephemeral=True
            )
            return
        note = "" if dmed else "\n*(couldn't DM them — DMs may be off.)*"
        await interaction.followup.send(f"👢 Kicked {member} — {reason}{note}", ephemeral=True)
        await self._log("Kick", interaction, member, reason)

    @app_commands.command(name="ban", description="(Eboard) Ban a member.")
    @app_commands.describe(member="Member to ban", reason="Reason")
    @is_eboard()
    async def ban(
        self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
    ):
        await interaction.response.defer(ephemeral=True)
        blocked = self._cannot_act(interaction, member)
        if blocked:
            await interaction.followup.send(blocked, ephemeral=True)
            return
        # DM before banning — afterwards we can't reach them.
        dmed = await self._dm_action(
            member, interaction.guild.name, "Banned", reason, discord.Color.red()
        )
        try:
            await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=1)
        except discord.Forbidden as e:
            await interaction.followup.send(
                self._forbidden_text(e, "ban them", "Ban Members"), ephemeral=True
            )
            return
        note = "" if dmed else "\n*(couldn't DM them — DMs may be off.)*"
        await interaction.followup.send(f"🔨 Banned {member} — {reason}{note}", ephemeral=True)
        await self._log("Ban", interaction, member, reason)

    @app_commands.command(name="timeout", description="(Eboard) Timeout a member for N minutes.")
    @app_commands.describe(member="Member", minutes="Duration in minutes", reason="Reason")
    @is_eboard()
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str = "No reason given",
    ):
        import datetime

        await interaction.response.defer(ephemeral=True)
        blocked = self._cannot_act(interaction, member)
        if blocked:
            await interaction.followup.send(blocked, ephemeral=True)
            return
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        try:
            await member.timeout(until, reason=f"{interaction.user}: {reason}")
        except discord.Forbidden as e:
            await interaction.followup.send(
                self._forbidden_text(e, "time them out", "Moderate Members"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"⏲️ Timed out {member} for {minutes} min — {reason}", ephemeral=True
        )
        await self._log("Timeout", interaction, member, f"{minutes}m — {reason}")

    @app_commands.command(name="warn", description="(Eboard) Warn a member.")
    @app_commands.describe(member="Member", reason="Reason")
    @is_eboard()
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ):
        n = await self.bot.db.add_warning(
            interaction.guild_id, member.id, interaction.user.id, reason
        )
        count = len(await self.bot.db.get_warnings(interaction.guild_id, member.id))
        await interaction.response.send_message(
            f"⚠️ Warned {member} (warning #{n}, {count} total) — {reason}", ephemeral=True
        )
        try:
            await member.send(
                f"⚠️ You were warned in **{interaction.guild.name}**: {reason}"
            )
        except discord.HTTPException:
            pass
        await self._log("Warn", interaction, member, reason)

    @app_commands.command(name="warnings", description="(Eboard) List a member's warnings.")
    @app_commands.describe(member="Member")
    @is_eboard()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.get_warnings(interaction.guild_id, member.id)
        # Hidden cross-server repeat-offender marker (Eboard-only): a count of how
        # many OTHER TaigaBot servers have warned this user — no details/names.
        other_servers, other_warns = await self.bot.db.cross_server_warnings(
            member.id, interaction.guild_id
        )
        cross = (
            f"🌐 **Repeat offender:** also warned in **{other_servers}** other "
            f"TaigaBot server(s) — {other_warns} warning(s) total there."
            if other_servers else ""
        )

        if not rows:
            msg = f"{member} has no warnings here. 🎉"
            if cross:
                msg += f"\n{cross}"
            await interaction.response.send_message(msg, ephemeral=True)
            return

        embed = discord.Embed(title=f"Warnings — {member}", color=discord.Color.orange())
        for r in rows[:25]:
            embed.add_field(
                name=f"#{r['id']} • <t:{r['created_at']}:d>",
                value=f"{r['reason']} — <@{r['moderator_id']}>",
                inline=False,
            )
        if cross:
            embed.add_field(name="🌐 Cross-server", value=cross, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="(Eboard) Clear a member's warnings.")
    @app_commands.describe(member="Member")
    @is_eboard()
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        n = await self.bot.db.clear_warnings(interaction.guild_id, member.id)
        await interaction.response.send_message(
            f"🧹 Cleared {n} warning(s) for {member}.", ephemeral=True
        )

    @app_commands.command(name="purge", description="(Eboard) Bulk-delete recent messages.")
    @app_commands.describe(amount="How many messages to delete (1-100)")
    @is_eboard()
    async def purge(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)

    async def _autowarn(
        self, message: discord.Message, *, reason: str, label: str,
        escalate_every: int = SPAM_WARN_ESCALATE,
    ) -> None:
        """Record an automatic automod warning and (on a schedule) DM the Eboard.

        `reason` is the text stored on the warning; `label` is the human phrase
        used in the offender's DM and the Eboard alert (e.g. "spamming" or
        "posting a suspected phishing/scam message").

        The warning is attributed to the bot itself (moderator_id = bot id), so it
        shows up in /warnings alongside manual ones. Rate-limited per user via
        AUTOWARN_COOLDOWN_SEC so a single burst (or a persistent offender) doesn't
        rack up dozens of warnings — the message is still deleted every time by
        punish(), only the warning is throttled.

        The Eboard is DMed every `escalate_every` auto-warns for this user, so a
        single repeat offender can never flood their DMs. Spam uses the default
        (every Nth); phishing passes 1 so the Eboard hears about every scam.
        """
        guild = message.guild
        member = message.author
        key = (guild.id, member.id)
        now = time.time()
        if now - self._last_autowarn.get(key, 0) < AUTOWARN_COOLDOWN_SEC:
            return
        self._last_autowarn[key] = now

        await self.bot.db.add_warning(guild.id, member.id, self.bot.user.id, reason)
        total = len(await self.bot.db.get_warnings(guild.id, member.id))
        # Count auto-warns ourselves so the "every Nth" escalation is reliable —
        # the DB total mixes in manual warnings and resets on /clearwarnings.
        streak = self._autowarn_count.get(key, 0) + 1
        self._autowarn_count[key] = streak
        log.info(
            "Auto-warned %s in guild %s (DB total=%d, auto-streak=%d).",
            member, guild.id, total, streak,
        )

        # Let the offender know (best-effort — they may have DMs closed, and
        # Discord blocks repeated unsolicited bot DMs; the in-channel notice and
        # the recorded warning still happen regardless).
        try:
            await member.send(
                f"⚠️ You were automatically warned in **{guild.name}** for {label}. "
                f"You now have **{total}** warning(s) on record."
            )
        except discord.HTTPException:
            pass

        # Escalate to the Eboard every `escalate_every` auto-warns so they're
        # notified about a repeat offender without being flooded.
        if streak % escalate_every != 0:
            return

        other_servers, other_warns = await self.bot.db.cross_server_warnings(
            member.id, guild.id
        )
        cross = (
            f"\n🌐 **Repeat offender:** also warned in **{other_servers}** other "
            f"TaigaBot server(s) ({other_warns} warning(s) total)."
            if other_servers else ""
        )
        embed = discord.Embed(
            title="⚠️ Automod auto-warn",
            description=(
                f"**User:** {member.mention} ({member} `{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Auto-warns this session:** {streak}\n"
                f"**Total warnings on record:** {total}\n"
                f"**Reason:** {reason}{cross}"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"{guild.name} • automod")
        reached = await self._dm_eboard(guild, embed)
        log.info(
            "Escalated repeat offender %s to Eboard: DMed %d member(s)%s.",
            member, reached,
            "" if reached else " (none reached — Eboard DMs closed or role empty)",
        )
        # Always mirror to #mod-log so it lands even if Eboard DMs are closed.
        await gu.log_mod_action(guild, embed)

    @staticmethod
    async def _bulk_delete(channel: discord.abc.Messageable, messages: list) -> None:
        """Delete a batch of messages best-effort.

        Tries the bulk endpoint first (one API call for the whole burst), then
        falls back to deleting individually — bulk delete needs a real text
        channel and rejects messages older than 14 days. Already-gone messages
        are ignored."""
        if not messages:
            return
        try:
            await channel.delete_messages(messages)
            return
        except (discord.HTTPException, AttributeError):
            pass
        for m in messages:
            try:
                await m.delete()
            except discord.HTTPException:
                pass

    @staticmethod
    async def _dm_eboard(guild: discord.Guild, embed: discord.Embed) -> int:
        """DM an embed to every (non-bot) member holding the Eboard role.

        Returns how many were reached. Best-effort: members with closed DMs are
        skipped silently. Relies on the members intent (enabled in bot.py)."""
        role = gu.eboard_role(guild)
        if role is None:
            return 0
        sent = 0
        for m in role.members:
            if m.bot:
                continue
            try:
                await m.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                pass
        return sent

    @staticmethod
    async def _dm_action(
        member: discord.Member, guild_name: str, action_label: str,
        reason: str, color: discord.Color,
    ) -> bool:
        """Best-effort DM telling a member they were kicked/banned, with the
        reason. MUST be called BEFORE the kick/ban — afterwards the bot can no
        longer reach them. Returns False if their DMs are closed."""
        embed = discord.Embed(
            title=f"{action_label} from {guild_name}",
            description=f"You have been **{action_label.lower()}** from **{guild_name}**.",
            color=color,
        )
        embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
        try:
            await member.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    async def _log(
        self, action: str, interaction: discord.Interaction, member: discord.Member, reason: str
    ):
        embed = discord.Embed(title=f"🛡️ {action}", color=discord.Color.red())
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        await gu.log_mod_action(interaction.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
