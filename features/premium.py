"""Premium tier — read-only in Discord.

Granting and revoking happen on the TaigaBot website, not here: it's a paid
tier settled offline, and the owner-facing management UI lives in the dashboard
where there's room for notes, expiry dates and an audit trail. This cog exposes
exactly one command, `/premium`, so a server can check what tier it's on
without leaving Discord.

Other features gate on `bot.db.is_premium(guild_id)`; see `features/news.py`
for the first consumer (a higher custom-feed cap).
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("taigabot.premium")


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="premium", description="Check whether this server has premium."
    )
    async def premium(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this inside a server.", ephemeral=True
            )
            return

        active = await self.bot.db.is_premium(interaction.guild_id)
        row = await self.bot.db.get_premium(interaction.guild_id)

        embed = discord.Embed(
            title="✨ Premium status",
            description=(
                "**Active** — thanks for supporting TaigaBot!"
                if active
                else "This server is on the free tier."
            ),
            color=config.BOT_COLOR,
        )
        if active and row and row["expires_at"]:
            embed.add_field(name="Renews/expires", value=f"<t:{row['expires_at']}:D>")
        elif active:
            embed.add_field(name="Expires", value="Never")

        embed.add_field(
            name="Custom news feeds",
            value=(
                f"Up to {config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS}"
                if active
                else f"Up to {config.NEWS_MAX_CUSTOM_FEEDS} "
                f"({config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS} on premium)"
            ),
        )
        if not active:
            embed.set_footer(text="Manage your tier on the TaigaBot dashboard.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Premium(bot))
