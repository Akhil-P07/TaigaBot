"""Classroom setup commands for TaigaBot."""

from os import name

import discord
from discord import app_commands
from discord import guild
from discord import role
from discord.ext import commands


class Classroom(commands.Cog):
    """Commands for configuring classroom Discord servers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ensure_classroom_role(
        self, guild: discord.Guild, name: str
    ) -> discord.Role:
        """Find an existing classroom role or create it."""
        role = discord.utils.get(guild.roles, name=name)

        if role is None:
            role = await guild.create_role(
                name=name,
                reason="TaigaBot classroom setup",
            )

        return role

    @app_commands.command(
        name="classroom-setup",
        description="Configure this Discord server for a course.",
    )
    @app_commands.default_permissions(administrator=True)
    async def classroom_setup(
        self,
        interaction: discord.Interaction,
        course_name: str,
    ):
        """Set up a classroom server."""
        guild = interaction.guild

        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Explicit permission check to protect against command overrides.
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can run this command.",
                ephemeral=True,
            )
            return

        # Normalize and validate the course name.
        course_name = course_name.strip()

        if not 1 <= len(course_name) <= 80:
            await interaction.response.send_message(
                "Course names must be between 1 and 80 characters.",
                ephemeral=True,
            )
            return

        # Check the bot's permissions before making changes.
        bot_member = guild.me

        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
        ):
            await interaction.response.send_message(
                "TaigaBot needs the Manage Roles permission "
                "to configure classroom roles.",
                ephemeral=True,
            )
            return

        # Role creation may take longer than Discord's initial
        # interaction response window.
        await interaction.response.defer(ephemeral=True, thinking=True)

        role_names = ("Professor", "TA", "SI", "Student")
        created_roles = []

        try:
            for role_name in role_names:
                role = await self._ensure_classroom_role(
                    guild, role_name
                )
                created_roles.append(role)

        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Classroom role setup failed. Check TaigaBot's "
                "permissions and try again. Some roles may "
                "have been created already.",
                ephemeral=True,
            )
            return

        # Store the configuration only after roles are ready.
        try:
            await self.bot.db.set_server_setup(
                guild.id,
                "classroom",
                course_name,
            )
        except Exception:
            await interaction.followup.send(
                "Classroom roles are ready, but the configuration "
                "could not be saved. Please contact the bot administrator.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Classroom roles ready for "
            f"**{discord.utils.escape_markdown(course_name)}**!\n"
            + "\n".join(role.mention for role in created_roles),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Classroom(bot))