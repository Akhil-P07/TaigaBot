"""Reaction roles — let members self-assign interest roles by reacting.

Eboard workflow:
  1. /reactionrole post title:"Pick your interests" description:"React below!"
     → bot posts an embed and remembers its message id.
  2. /reactionrole add message_id:<id> emoji:🤖 role:@ML
     → bot adds the emoji to that message; reacting grants/removes the role.
  /reactionrole remove message_id:<id> emoji:🤖
  /reactionrole list

Works with both standard unicode emoji (🤖) and custom server emoji.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.checks import is_eboard


def _emoji_key(emoji: discord.PartialEmoji | str) -> str:
    """Stable string key for an emoji (custom emojis keyed by id)."""
    if isinstance(emoji, str):
        return emoji
    if emoji.id:
        return str(emoji.id)
    return emoji.name


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="reactionrole", description="Manage self-assign reaction roles (Eboard only)."
    )

    @group.command(name="post", description="Post a new reaction-role message.")
    @app_commands.describe(title="Embed title", description="Embed body text")
    @is_eboard()
    async def post(
        self, interaction: discord.Interaction, title: str, description: str
    ):
        embed = discord.Embed(
            title=title, description=description, color=config.BOT_COLOR
        )
        embed.set_footer(text="React to get a role • un-react to remove it")
        msg = await interaction.channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Posted. Now add roles with:\n"
            f"`/reactionrole add message_id:{msg.id} emoji:<emoji> role:<@role>`",
            ephemeral=True,
        )

    @group.command(name="add", description="Bind an emoji on a message to a role.")
    @app_commands.describe(
        message_id="ID of the message to react to",
        emoji="The emoji members will click",
        role="The role to grant",
    )
    @is_eboard()
    async def add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        role: discord.Role,
    ):
        # Resolve the target message in this channel.
        try:
            msg = await interaction.channel.fetch_message(int(message_id))
        except (ValueError, discord.NotFound):
            await interaction.response.send_message(
                "❌ I couldn't find that message in this channel. "
                "Run this command in the same channel as the reaction-role message.",
                ephemeral=True,
            )
            return

        # Safety: bot can't assign a role at/above its own top role.
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                f"❌ I can't manage {role.mention} — it's above my own role. "
                f"Move my role higher in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ That doesn't look like a valid emoji I can use.", ephemeral=True
            )
            return

        # Normalize the stored key the same way the listener will see it.
        partial = discord.PartialEmoji.from_str(emoji)
        await self.bot.db.add_reaction_role(
            interaction.guild_id, msg.id, _emoji_key(partial), role.id
        )
        await interaction.response.send_message(
            f"✅ {emoji} on that message now grants {role.mention}.", ephemeral=True
        )

    @group.command(name="remove", description="Unbind an emoji from its role.")
    @app_commands.describe(message_id="Message ID", emoji="The emoji to unbind")
    @is_eboard()
    async def remove(
        self, interaction: discord.Interaction, message_id: str, emoji: str
    ):
        partial = discord.PartialEmoji.from_str(emoji)
        await self.bot.db.remove_reaction_role(int(message_id), _emoji_key(partial))
        await interaction.response.send_message(
            f"✅ Unbound {emoji}.", ephemeral=True
        )

    @group.command(name="list", description="List configured reaction roles.")
    @is_eboard()
    async def list_rr(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_reaction_roles(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "No reaction roles configured yet.", ephemeral=True
            )
            return
        lines = []
        for r in rows:
            role = interaction.guild.get_role(r["role_id"])
            lines.append(
                f"• msg `{r['message_id']}` — `{r['emoji']}` → "
                f"{role.mention if role else 'deleted role'}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── reaction listeners ────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, add=False)

    async def _handle(self, payload: discord.RawReactionActionEvent, add: bool):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        role_id = await self.bot.db.get_reaction_role(
            payload.message_id, _emoji_key(payload.emoji)
        )
        if role_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role is None or member is None or member.bot:
            return
        try:
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
