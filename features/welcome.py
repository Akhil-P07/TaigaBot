"""Welcome & onboarding.

When a member joins:
  • they're given the Unverified role automatically
  • a friendly welcome goes to #welcome
  • TaigaBot DMs them a short walkthrough of how to verify

Includes a `/verifyhelp` command anyone can run if they get stuck.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu


def onboarding_embed(guild_name: str) -> discord.Embed:
    allowed = " or ".join(f"`@{d}`" for d in config.ALLOWED_EMAIL_DOMAINS)
    embed = discord.Embed(
        title=f"👋 Welcome to {guild_name}!",
        description=(
            "I'm **TaigaBot** 🐯, the AI Club assistant. To keep the club to RIT "
            "students, you need to verify with your university email before you can "
            "access the server.\n\n"
            "**How to verify (2 steps):**\n"
            "1️⃣ Run `/verify name:Your Name email:you@rit.edu`\n"
            "2️⃣ Check your email for a 6-digit code, then run `/confirm code:XXXXXX`\n\n"
            "💬 You can run these right here **or in a DM to me** — one verification "
            "unlocks every TaigaBot server you're in.\n\n"
            f"Your email must be {allowed}. Until you verify, you can only chat in "
            f"**#{config.UNVERIFIED_CHANNEL_NAME}**.\n\n"
            "**Lost your old Discord account?** Run `/recover email:you@rit.edu` "
            "instead — it moves your verification to this account.\n\n"
            "Once verified, the whole server unlocks. See you inside! 🚀"
        ),
        color=config.BOT_COLOR,
    )
    embed.set_footer(text="Stuck? Run /verifyhelp or ping an Eboard member.")
    return embed


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild

        # If they verified before (anywhere this bot runs), restore access
        # instantly — no Unverified role, no OTP. Otherwise gate them as new.
        already_verified = await self.bot.db.user_is_verified(member.id)
        if already_verified:
            await gu.promote_to_verified(member)
        else:
            unverified = gu.unverified_role(guild)
            if unverified:
                try:
                    await member.add_roles(unverified, reason="New member — needs verification")
                except discord.Forbidden:
                    pass

        # Public welcome.
        welcome = gu.welcome_channel(guild)
        if welcome:
            if already_verified:
                desc = (
                    f"Welcome back {member.mention}! 🎉 You're already verified — "
                    f"full access restored."
                )
            else:
                desc = (
                    f"Welcome {member.mention}! 🎉 Verify with `/verify` to unlock "
                    f"the server. Check your DMs for instructions."
                )
            try:
                await welcome.send(
                    content=member.mention,
                    embed=discord.Embed(description=desc, color=config.BOT_COLOR),
                )
            except discord.HTTPException:
                pass

        # DM the verification walkthrough only to members who still need it.
        if not already_verified:
            try:
                await member.send(embed=onboarding_embed(guild.name))
            except discord.HTTPException:
                pass  # DMs closed

    @app_commands.command(
        name="verifyhelp", description="Show how to verify your university email."
    )
    async def verifyhelp(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=onboarding_embed(interaction.guild.name if interaction.guild else "the AI Club"),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
