"""XP / leveling system.

Members earn XP for chatting (with a per-user cooldown so spamming doesn't help).
Level-ups are announced, and /rank and /leaderboard show progress.

Tuning knobs are the constants below — tweak freely.
"""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu

XP_COOLDOWN_SEC = 60          # min seconds between XP-earning messages per user
XP_PER_MESSAGE = (15, 25)     # random XP range per qualifying message
ANNOUNCE_LEVELUPS = True


def xp_for_level(level: int) -> int:
    """Total XP needed to REACH a given level (gentle curve)."""
    return 5 * (level ** 2) + 50 * level + 100


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level


def xp_into_level(xp: int) -> tuple[int, int]:
    """Return (xp_into_current_level, xp_needed_for_next)."""
    level = 0
    remaining = xp
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return remaining, xp_for_level(level)


class Leveling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.content.startswith(("!", "/")):
            return

        settings = await self.bot.db.get_settings(message.guild.id)
        if not settings["levels_enabled"]:
            return

        row = await self.bot.db.get_level_row(message.author.id)
        now = time.time()
        last = row["last_msg_ts"] if row else 0
        if now - last < XP_COOLDOWN_SEC:
            return

        xp = (row["xp"] if row else 0) + random.randint(*XP_PER_MESSAGE)
        old_level = row["level"] if row else 0
        new_level = level_from_xp(xp)
        await self.bot.db.upsert_level(message.author.id, xp, new_level, now)

        if ANNOUNCE_LEVELUPS and new_level > old_level:
            try:
                await message.channel.send(
                    f"📈 {message.author.mention} leveled up to **level {new_level}**! "
                    f"Keep it up. ...N-not that I'm impressed or anything.",
                    delete_after=15,
                )
            except discord.HTTPException:
                pass

    @app_commands.command(name="rank", description="Show your (or someone's) level and XP.")
    @app_commands.describe(member="Whose rank to show (default: you)")
    async def rank(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
        member = member or interaction.user
        row = await self.bot.db.get_level_row(member.id)
        if row is None or row["xp"] == 0:
            await interaction.response.send_message(
                f"{member.display_name} hasn't earned any XP yet.", ephemeral=True
            )
            return
        into, needed = xp_into_level(row["xp"])
        rank = await self.bot.db.rank(member.id)
        bar_len = 20
        filled = int(bar_len * into / needed) if needed else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        embed = discord.Embed(title=f"📊 {member.display_name}'s rank", color=config.BOT_COLOR)
        embed.add_field(name="Level", value=str(row["level"]))
        embed.add_field(name="Total XP", value=str(row["xp"]))
        embed.add_field(name="Global rank", value=f"#{rank}")
        embed.add_field(name="Progress", value=f"`{bar}` {into}/{needed} XP", inline=False)
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Top members by XP.")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.leaderboard(limit=10)
        if not rows:
            await interaction.response.send_message(
                "No one's earned XP yet. Get chatting!", ephemeral=True
            )
            return
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = []
        for i, r in enumerate(rows):
            member = interaction.guild.get_member(r["user_id"])
            name = member.display_name if member else f"User {r['user_id']}"
            lines.append(f"{medals[i]} **{name}** — level {r['level']} ({r['xp']} XP)")
        embed = discord.Embed(
            title="🏆 AI Club Leaderboard",
            description="\n".join(lines),
            color=config.BOT_COLOR,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
