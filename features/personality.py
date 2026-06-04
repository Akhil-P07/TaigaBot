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
            if random.random() <= personality.MENTION_REPLY_CHANCE:
                line = personality.say("mention", name=message.author.display_name)
                if line:
                    try:
                        await message.reply(line, mention_author=False)
                    except discord.HTTPException:
                        pass

    @app_commands.command(name="taiga", description="Get a random remark from TaigaBot. 🐯")
    async def taiga(self, interaction: discord.Interaction):
        line = personality.say("random") or "..."
        await interaction.response.send_message(line)

    @app_commands.command(name="hello", description="Say hi to TaigaBot.")
    async def hello(self, interaction: discord.Interaction):
        line = personality.say("greeting", name=interaction.user.display_name) or "Hello!"
        await interaction.response.send_message(line)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
