"""/help — lists the commands available to the caller.

Anyone can run it. If the caller is Eboard (or a server admin) they get the full
reference including moderation/setup commands; everyone else gets the member
commands only.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.checks import member_has_role


def _is_staff(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.administrator or member_has_role(
        user, config.EBOARD_ROLE_NAME
    )


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="List the TaigaBot commands you can use.")
    async def help_cmd(self, interaction: discord.Interaction):
        staff = _is_staff(interaction.user)
        embed = discord.Embed(
            title="🐯 TaigaBot — commands",
            color=config.BOT_COLOR,
            description=(
                "Here's everything you can run. Eboard-only commands are shown too. 🛡️"
                if staff
                else "Here's what you can run. Verify first to unlock the server!"
            ),
        )

        # ── Member commands (shown to everyone) ──────────────────────────────
        embed.add_field(
            name="✅ Verification",
            value=(
                "`/verify name email` — start verifying with your RIT email\n"
                "`/confirm code` — finish with the 6-digit code\n"
                "`/recover email` — moved Discord accounts? move your verification here\n"
                "`/verifyhelp` — how verification works"
            ),
            inline=False,
        )
        embed.add_field(
            name="🗂️ Projects",
            value=(
                "`/projects [tag]` — browse all projects, optionally filtered by tag\n"
                "`/projecttags` — list all project tags\n"
                "`/joinproject [tag]` — request to join a project (lead approves/denies via DM)\n"
                "`/leaveproject` — leave a project you've joined"
            ),
            inline=False,
        )
        embed.add_field(
            name="📈 Leveling",
            value=(
                "`/rank [member]` — your (or someone's) level & XP\n"
                "`/leaderboard` — top members by XP"
            ),
            inline=False,
        )
        embed.add_field(
            name="📚 AI/ML",
            value=(
                "`/ask prompt` — ask the AI assistant a question\n"
                "`/paper query` — search for papers\n"
                "`/resource` — curated learning resources\n"
                "`/aiterm term` — learn an AI/ML term"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎭 Fun",
            value="`/taiga` — a remark from Taiga\n`/hello` — say hi",
            inline=False,
        )

        if staff:
            embed.add_field(
                name="🗂️ Projects (🛡️ Eboard)",
                value=(
                    "`/createproject lead:@member` — create a project (role, channel, lead(s))\n"
                    "`/editproject` — edit a project's name/description/tags (reposts the intro)\n"
                    "`/dropproject` — delete a project's channel, role, and entry (select from list)\n"
                    "`/deletetag [tag]` — remove a tag from every project (blank = pick from a dropdown)"
                ),
                inline=False,
            )
            embed.add_field(
                name="🔧 Server (🛡️ Eboard)",
                value=(
                    "`/setup` — *owner/admin only:* create roles/channels & gate the server\n"
                    "`/health` — config & role/channel status\n"
                    "`/backup` — back up this server's data now"
                ),
                inline=False,
            )
            embed.add_field(
                name="✅ Verification (🛡️ Eboard)",
                value="`/whois member` — look up a member's verified info",
                inline=False,
            )
            embed.add_field(
                name="🤖 Auto-moderation (🛡️ Eboard)",
                value=(
                    "`/automod enable|disable [filter]` — toggle automod or one filter\n"
                    "  *filters:* `words` `invites` `spam` `mentions` `caps` `phishing`\n"
                    "`/automod status` — show current settings\n"
                    "`/automod addword|removeword word` — manage banned words\n"
                    "🛡️ *Phishing/scam detection uses an on-device ML model — no data leaves the bot.*"
                ),
                inline=False,
            )
            embed.add_field(
                name="🛡️ Moderation (🛡️ Eboard)",
                value=(
                    "`/kick member [reason]` — kick (DMs the user)\n"
                    "`/ban member [reason]` — ban (DMs the user)\n"
                    "`/timeout member minutes [reason]` — timeout\n"
                    "`/warn member reason` — warn (DMs the user)\n"
                    "`/warnings member` • `/clearwarnings member` — view/clear warnings "
                    "(flags cross-server repeat offenders)\n"
                    "`/purge amount` — bulk-delete recent messages"
                ),
                inline=False,
            )
            embed.add_field(
                name="🎟️ Reaction roles (🛡️ Eboard)",
                value=(
                    "`/reactionrole post title description` — post a role message\n"
                    "`/reactionrole add message_id emoji role` — bind an emoji to a role\n"
                    "`/reactionrole remove message_id emoji` • `/reactionrole list`"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
