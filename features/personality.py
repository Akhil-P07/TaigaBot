"""Tsundere personality feature.

Reacts when the bot is @mentioned and exposes a couple of fun commands. All the
actual lines live in the top-level `personality.py` file so they're easy to edit.
"""
from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

import config
import personality


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not personality.ENABLED:
            return
        # Only react to a direct mention of the bot (not @everyone/@here).
        if self.bot.user in message.mentions and not message.mention_everyone:
            lowered = message.content.lower()
            asks_height = (
                "height" in lowered or "how tall" in lowered or "how short" in lowered
            )
            # "Ryuji" → flusters and denies knowing him. Height → threatens to punch.
            # Thanks → a thanks line. These always reply; a plain mention replies at
            # the usual chance.
            if "ryuji" in lowered:
                situation, react = "ryuji", True
            elif asks_height:
                situation, react = "height", True
            elif "thank" in lowered:
                situation, react = "thanks", True
            else:
                situation, react = "mention", random.random() <= personality.MENTION_REPLY_CHANCE
            if react:
                line = personality.say(situation, name=message.author.display_name)
                if line:
                    try:
                        await message.reply(line, mention_author=False)
                    except discord.HTTPException:
                        pass

    @app_commands.command(name="taiga", description="Get a random remark from TaigaBot. 🐯")
    @app_commands.checks.cooldown(1, 5.0)  # 1 use / 5s per user (anti-spam)
    async def taiga(self, interaction: discord.Interaction):
        line = personality.say("random") or "..."
        await interaction.response.send_message(line)

    @app_commands.command(name="hello", description="Say hi to TaigaBot.")
    @app_commands.checks.cooldown(1, 5.0)  # 1 use / 5s per user (anti-spam)
    async def hello(self, interaction: discord.Interaction):
        line = personality.say("greeting", name=interaction.user.display_name) or "Hello!"
        await interaction.response.send_message(line)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
