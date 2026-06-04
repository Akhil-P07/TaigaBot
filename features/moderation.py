"""Auto-moderation + Eboard moderation commands.

Automod (toggle each filter on/off with /automod):
  • filter_words    — deletes messages containing banned words
  • filter_invites  — deletes Discord invite links
  • filter_spam     — flags rapid repeated/identical messages
  • filter_mentions — deletes messages with too many user mentions
  • filter_caps     — deletes very long ALL-CAPS messages (off by default)

Moderation commands (Eboard role only): /kick /ban /timeout /warn /warnings
/clearwarnings /purge. Every command checks the caller's role via is_eboard().
"""
from __future__ import annotations

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

INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)", re.I)
MAX_MENTIONS = 5
SPAM_WINDOW_SEC = 7
SPAM_THRESHOLD = 5  # messages within window
CAPS_MIN_LEN = 12


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> recent message timestamps
        self._recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=SPAM_THRESHOLD))

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

        async def punish(reason: str):
            try:
                await message.delete()
            except discord.HTTPException:
                return
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

        # mass mentions
        if settings["filter_mentions"] and len(message.mentions) > MAX_MENTIONS:
            await punish("please don't mass-mention people.")
            return

        # caps
        if settings["filter_caps"] and len(content) >= CAPS_MIN_LEN:
            letters = [c for c in content if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
                await punish("please don't type in all caps.")
                return

        # spam (rapid messages)
        if settings["filter_spam"]:
            key = (message.guild.id, message.author.id)
            now = time.time()
            dq = self._recent[key]
            dq.append(now)
            if len(dq) == SPAM_THRESHOLD and (now - dq[0]) < SPAM_WINDOW_SEC:
                dq.clear()
                await punish("you're sending messages too fast — slow down.")
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
    @app_commands.command(name="kick", description="(Eboard) Kick a member.")
    @app_commands.describe(member="Member to kick", reason="Reason")
    @is_eboard()
    async def kick(
        self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
    ):
        # DM before kicking — afterwards we can't reach them.
        dmed = await self._dm_action(
            member, interaction.guild.name, "Kicked", reason, discord.Color.orange()
        )
        await member.kick(reason=f"{interaction.user}: {reason}")
        note = "" if dmed else "\n*(couldn't DM them — DMs may be off.)*"
        await interaction.response.send_message(
            f"👢 Kicked {member} — {reason}{note}", ephemeral=True
        )
        await self._log("Kick", interaction, member, reason)

    @app_commands.command(name="ban", description="(Eboard) Ban a member.")
    @app_commands.describe(member="Member to ban", reason="Reason")
    @is_eboard()
    async def ban(
        self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"
    ):
        # DM before banning — afterwards we can't reach them.
        dmed = await self._dm_action(
            member, interaction.guild.name, "Banned", reason, discord.Color.red()
        )
        await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=1)
        note = "" if dmed else "\n*(couldn't DM them — DMs may be off.)*"
        await interaction.response.send_message(
            f"🔨 Banned {member} — {reason}{note}", ephemeral=True
        )
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

        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        await member.timeout(until, reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(
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
        if not rows:
            await interaction.response.send_message(
                f"{member} has no warnings. 🎉", ephemeral=True
            )
            return
        embed = discord.Embed(title=f"Warnings — {member}", color=discord.Color.orange())
        for r in rows[:25]:
            embed.add_field(
                name=f"#{r['id']} • <t:{r['created_at']}:d>",
                value=f"{r['reason']} — <@{r['moderator_id']}>",
                inline=False,
            )
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
