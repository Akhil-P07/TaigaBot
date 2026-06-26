"""/ask — a Gemini-powered AI assistant for quick questions.

Uses Google's Gemini API (free tier) over plain REST, so no extra dependency is
needed. Set GEMINI_API_KEY in the env to enable it; without a key /ask reports
that it's not configured. Get a free key at https://aistudio.google.com/apikey
"""
from __future__ import annotations

import asyncio
import logging
import socket

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("taigabot.ask")

SYSTEM_PROMPT = (
    "You are TaigaBot, a helpful assistant for the RIT AI Club Discord server. "
    "Answer clearly and concisely — a few short paragraphs at most. Use Discord "
    "markdown when helpful. If you're unsure or the question is outside your "
    "knowledge, say so plainly."
)
MAX_ANSWER = 4000  # embed description limit is 4096; leave headroom


class Ask(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """A single, reused aiohttp session for Gemini calls.

        Reusing one session keeps the TCP connection alive and caches DNS, so
        most /ask calls skip a fresh `getaddrinfo` lookup entirely. That matters
        because DNS resolution runs on asyncio's default thread pool — creating a
        brand-new session every call made /ask sensitive to that pool being busy
        with other blocking work."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    def cog_unload(self) -> None:
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    @app_commands.command(name="ask", description="Ask the AI assistant a question.")
    @app_commands.describe(prompt="Your question for the AI assistant")
    @app_commands.checks.cooldown(1, 10.0)  # 1 use / 10s per user (free-tier friendly)
    async def ask(self, interaction: discord.Interaction, prompt: str):
        if not config.GEMINI_API_KEY:
            await interaction.response.send_message(
                "⚠️ The AI assistant isn't configured — an admin needs to set "
                "`GEMINI_API_KEY`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
        }
        headers = {
            "x-goog-api-key": config.GEMINI_API_KEY,
            "Content-Type": "application/json",
        }
        try:
            session = self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            log.exception("Gemini request failed")
            await interaction.followup.send(
                "⚠️ Couldn't reach the AI service right now. Try again later."
            )
            return

        # 429 = quota / rate limit exhausted on the free tier.
        if status == 429:
            await interaction.followup.send("🪫 Out of Gemini credits — try again later.")
            return
        if status != 200:
            log.error("Gemini returned HTTP %s: %s", status, data)
            await interaction.followup.send(
                "⚠️ The AI service returned an error. Try again later."
            )
            return

        try:
            answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            answer = ""  # usually a safety block or empty candidate
        if not answer:
            await interaction.followup.send(
                "⚠️ I couldn't generate a response to that — try rephrasing."
            )
            return

        embed = discord.Embed(
            title="💬 Answer",
            description=answer[:MAX_ANSWER],
            color=config.BOT_COLOR,
        )
        embed.add_field(name="❓ Question", value=prompt[:1000], inline=False)
        embed.set_footer(
            text=f"Powered by Gemini ({config.GEMINI_MODEL}) • AI responses can be wrong"
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ask(bot))
