"""Project management — create, browse, join, and drop projects.

/createproject (Eboard): modal for name, description, tags, team lead, and
  reaction emoji → category picker → creates role, gated channel, intro embed,
  reaction-role entry in #roles, and a DB record.

/dropproject (Eboard): select from DB-tracked projects to delete channel + role.

/joinproject [tag]: anyone can browse projects (optionally filtered by tag),
  pick one, and request to join. The project lead gets a DM with persistent
  Approve/Deny buttons. On decision, the requester gets a DM and the role is
  granted automatically on approval.

/projects [tag]: browse all projects, optionally filtered by tag.
"""
from __future__ import annotations

import asyncio
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu
from utils.checks import is_eboard

NEW_CATEGORY_SENTINEL = "__new__"
EMOJI_TIMEOUT = 60  # seconds to react with the join emoji during /createproject


# ── Helpers ───────────────────────────────────────────────────────────────────

def _channel_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", raw.lower().strip()).strip("-")[:100]


def _fmt_tags(tags_str: str) -> str:
    if not tags_str.strip():
        return ""
    return " ".join(f"`#{t.strip()}`" for t in tags_str.split(",") if t.strip())


def _norm_tags(raw: str) -> str:
    """Normalize tag string: lowercase, strip spaces, dedupe."""
    seen = []
    for t in raw.split(","):
        t = t.strip().lower()
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen)


def _distinct_tags(rows) -> list[str]:
    """Sorted list of every unique tag across the given project rows."""
    tags: set[str] = set()
    for row in rows:
        for t in (row["tags"] or "").split(","):
            t = t.strip().lower()
            if t:
                tags.add(t)
    return sorted(tags)


def _build_projects_embed(guild: discord.Guild, rows, tag: str | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🗂️ Projects{f' — #{tag}' if tag else ''}",
        color=discord.Color(config.BOT_COLOR),
        description=f"{len(rows)} project(s)" + (" found." if tag else "."),
    )
    for row in rows[:15]:
        tags_str = _fmt_tags(row["tags"]) if row["tags"] else "—"
        channel = guild.get_channel(row["channel_id"])
        ch_ref = channel.mention if channel else f"`#{_channel_name(row['name'])}`"
        embed.add_field(
            name=row["name"],
            value=(
                f"{row['description'][:120]}\n"
                f"**Lead:** <@{row['lead_id']}> | **Channel:** {ch_ref}\n"
                f"**Tags:** {tags_str}"
            ),
            inline=False,
        )
    if len(rows) > 15:
        embed.set_footer(text=f"Showing 15 of {len(rows)}.")
    return embed


# ── Persistent approval buttons (survive bot restarts) ───────────────────────

async def _handle_decision(
    interaction: discord.Interaction, request_id: int, approved: bool
) -> None:
    db = interaction.client.db
    req = await db.get_project_request(request_id)

    if req is None or req["status"] != "pending":
        await interaction.response.send_message(
            "This request has already been handled.", ephemeral=True
        )
        return

    status = "approved" if approved else "denied"
    await db.update_request_status(request_id, status)

    guild = interaction.client.get_guild(req["guild_id"])
    project = await db.get_project(req["channel_id"]) if guild else None

    project_name = project["name"] if project else f"channel {req['channel_id']}"

    # Disable the buttons on the lead's DM.
    label = f"{'✅ Approved' if approved else '❌ Denied'} — {project_name}"
    await interaction.response.edit_message(content=label, view=None)

    # Grant role on approval.
    if approved and guild and project:
        member = guild.get_member(req["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(req["user_id"])
            except discord.NotFound:
                member = None
        if member:
            role = guild.get_role(project["role_id"])
            if role:
                try:
                    await member.add_roles(role, reason="Project join approved")
                except discord.Forbidden:
                    pass

    # DM the requester.
    requester = interaction.client.get_user(req["user_id"])
    if requester is None:
        try:
            requester = await interaction.client.fetch_user(req["user_id"])
        except discord.NotFound:
            requester = None
    if requester:
        try:
            if approved:
                embed = discord.Embed(
                    title="✅ Project join request approved!",
                    description=(
                        f"Your request to join **{project_name}** was approved.\n"
                        "You now have access to the project channel."
                    ),
                    color=discord.Color.green(),
                )
            else:
                embed = discord.Embed(
                    title="❌ Project join request denied",
                    description=(
                        f"Your request to join **{project_name}** was denied by "
                        "the project lead."
                    ),
                    color=discord.Color.red(),
                )
            await requester.send(embed=embed)
        except discord.HTTPException:
            pass


class _ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"proj_approve:(?P<id>[0-9]+)",
):
    def __init__(self, request_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="✅ Approve",
                style=discord.ButtonStyle.green,
                custom_id=f"proj_approve:{request_id}",
            )
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "_ApproveButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_decision(interaction, self.request_id, approved=True)


class _DenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"proj_deny:(?P<id>[0-9]+)",
):
    def __init__(self, request_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="❌ Deny",
                style=discord.ButtonStyle.red,
                custom_id=f"proj_deny:{request_id}",
            )
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "_DenyButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_decision(interaction, self.request_id, approved=False)


class _ApprovalView(discord.ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.add_item(_ApproveButton(request_id))
        self.add_item(_DenyButton(request_id))


# ── Category picker (shown after /createproject modal submit) ─────────────────

class _CategorySelect(discord.ui.Select):
    def __init__(self, categories: list[discord.CategoryChannel]):
        options = [
            discord.SelectOption(
                label="➕ Create new Projects category",
                value=NEW_CATEGORY_SENTINEL,
                description="Bot will create a new 'Projects' category",
            )
        ] + [
            discord.SelectOption(
                label=f"📁 {cat.name[:90]}",
                value=str(cat.id),
                description=f"{len(cat.channels)} channel(s) inside",
            )
            for cat in categories[:24]
        ]
        super().__init__(
            placeholder="Pick a category for the project channel…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _CategoryView(discord.ui.View):
    def __init__(self, categories: list[discord.CategoryChannel]):
        super().__init__(timeout=120)
        self.select = _CategorySelect(categories)
        self.add_item(self.select)
        self.confirmed = False
        self.category_value: str = NEW_CATEGORY_SENTINEL

    @discord.ui.button(label="Create project", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category_value = (
            self.select.values[0] if self.select.values else NEW_CATEGORY_SENTINEL
        )
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⚙️ Creating project…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()


# ── /createproject modal ─────────────────────────────────────────────────────

class _ProjectModal(discord.ui.Modal, title="Create a new project"):
    project_name = discord.ui.TextInput(
        label="Project name",
        placeholder="e.g. RIT Vision Lab",
        max_length=50,
    )
    description = discord.ui.TextInput(
        label="Project description",
        style=discord.TextStyle.paragraph,
        placeholder="What does this project do?",
        max_length=500,
    )
    tags = discord.ui.TextInput(
        label="Tags (comma-separated)",
        placeholder="e.g. ml, vision, nlp",
        required=False,
        max_length=100,
    )

    def __init__(self, bot: commands.Bot, lead: discord.Member):
        super().__init__()
        self.bot = bot
        self.lead = lead  # passed in from the slash command (a real mention)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        tags_fmt = _fmt_tags(self.tags.value or "")
        view = _CategoryView(guild.categories)
        await interaction.response.send_message(
            f"**Project:** {self.project_name.value}\n"
            f"**Tags:** {tags_fmt or 'none'}\n"
            f"**Lead:** {self.lead.mention}\n\n"
            "Where should the project channel go?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return

        await self._create(interaction, guild, self.lead, view.category_value)

    async def _capture_emoji(self, interaction: discord.Interaction, name: str) -> str | None:
        """Ask the creator to react with the join emoji — this opens Discord's
        real emoji picker (modals can't). Returns the emoji string, or None on
        timeout. Posts a temporary prompt in the command channel."""
        prompt = await interaction.channel.send(
            f"{interaction.user.mention} — react to **this message** with the emoji "
            f"members will use to join **{name}** (you have {EMOJI_TIMEOUT}s)."
        )

        def check(payload: discord.RawReactionActionEvent) -> bool:
            return (
                payload.message_id == prompt.id
                and payload.user_id == interaction.user.id
            )

        try:
            payload = await self.bot.wait_for(
                "raw_reaction_add", check=check, timeout=EMOJI_TIMEOUT
            )
            emoji = str(payload.emoji)
        except asyncio.TimeoutError:
            emoji = None
        try:
            await prompt.delete()
        except discord.HTTPException:
            pass
        return emoji

    async def _create(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        lead: discord.Member,
        category_value: str,
    ):
        name = self.project_name.value.strip()
        desc = self.description.value.strip()
        tags = _norm_tags(self.tags.value or "")
        ch_name = _channel_name(name)

        # Emoji can't be picked in a modal — ask the creator to react instead.
        emoji_str = await self._capture_emoji(interaction, name)

        # Category.
        if category_value == NEW_CATEGORY_SENTINEL:
            existing = discord.utils.find(
                lambda c: c.name.lower() == "projects", guild.categories
            )
            category = existing or await guild.create_category(
                "Projects", reason=f"TaigaBot: project {name}"
            )
        else:
            category = guild.get_channel(int(category_value))

        # Role.
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role is None:
            role = await guild.create_role(
                name=name,
                color=discord.Color(config.BOT_COLOR),
                reason=f"TaigaBot: project role for {name}",
            )

        # Channel.
        eboard_role = gu.eboard_role(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if eboard_role:
            overwrites[eboard_role] = discord.PermissionOverwrite(view_channel=True)

        channel = discord.utils.find(
            lambda c: c.name == ch_name and isinstance(c, discord.TextChannel),
            guild.channels,
        )
        if channel is None:
            channel = await guild.create_text_channel(
                ch_name,
                category=category,
                overwrites=overwrites,
                reason=f"TaigaBot: project channel for {name}",
            )

        # Intro embed.
        embed = discord.Embed(
            title=f"📌 {name}",
            description=desc,
            color=discord.Color(config.BOT_COLOR),
        )
        embed.add_field(name="Team Lead", value=lead.mention, inline=True)
        embed.add_field(name="Role", value=role.mention, inline=True)
        if tags:
            embed.add_field(name="Tags", value=_fmt_tags(tags), inline=False)
        if emoji_str:
            embed.set_footer(text=f"React with {emoji_str} in #roles to join this project.")
        await channel.send(embed=embed)

        # Reaction role in #roles.
        roles_ch = gu.get_channel(guild, config.ROLES_CHANNEL_NAME)
        rr_note = ""
        if not emoji_str:
            rr_note = (
                "⚠️ No emoji chosen (timed out) — add the reaction role later with "
                f"`/reactionrole add … role:{role.mention}`."
            )
        elif roles_ch:
            rows = await self.bot.db.list_reaction_roles(guild.id)
            rr_msg = None
            if rows:
                try:
                    rr_msg = await roles_ch.fetch_message(rows[-1]["message_id"])
                except discord.NotFound:
                    rr_msg = None
            if rr_msg is None:
                rr_embed = discord.Embed(
                    title="🎟️ Pick your projects",
                    description="React below to join a project channel.",
                    color=discord.Color(config.BOT_COLOR),
                )
                rr_embed.set_footer(text="React to get a role • un-react to remove it")
                rr_msg = await roles_ch.send(embed=rr_embed)
            try:
                await rr_msg.add_reaction(emoji_str)
                partial = discord.PartialEmoji.from_str(emoji_str)
                emoji_key = str(partial.id) if partial.id else (partial.name or emoji_str)
                await self.bot.db.add_reaction_role(guild.id, rr_msg.id, emoji_key, role.id)
                rr_note = f"Added {emoji_str} → {role.mention} in {roles_ch.mention}."
            except discord.HTTPException:
                rr_note = (
                    f"⚠️ Couldn't add emoji — add manually: "
                    f"`/reactionrole add message_id:{rr_msg.id} emoji:{emoji_str} role:{role.mention}`."
                )
        else:
            rr_note = f"⚠️ No `#{config.ROLES_CHANNEL_NAME}` — run `/setup` first."

        # Persist to DB.
        await self.bot.db.add_project(
            channel.id, guild.id, name, role.id, lead.id, desc, tags
        )

        await interaction.followup.send(
            f"✅ **{name}** is ready!\n"
            f"• Channel: {channel.mention}\n"
            f"• Role: {role.mention}\n"
            f"• Category: **{category.name}**\n"
            f"• Tags: {_fmt_tags(tags) or 'none'}\n"
            f"• {rr_note}",
            ephemeral=True,
        )


# ── /joinproject views ────────────────────────────────────────────────────────

class _JoinSelect(discord.ui.Select):
    def __init__(self, projects: list):
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["channel_id"]),
                description=(
                    (_fmt_tags(row["tags"]).replace("`", "") + " — " if row["tags"] else "")
                    + row["description"][:80]
                ).strip(" —")[:100] or "No description",
            )
            for row in projects[:25]
        ]
        super().__init__(
            placeholder="Pick a project to request joining…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _JoinView(discord.ui.View):
    def __init__(self, projects: list):
        super().__init__(timeout=120)
        self.select = _JoinSelect(projects)
        self.add_item(self.select)
        self.confirmed = False
        self.channel_id: int | None = None

    @discord.ui.button(label="📬 Request to join", style=discord.ButtonStyle.green, row=1)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.select.values:
            await interaction.response.send_message(
                "Please select a project first.", ephemeral=True
            )
            return
        self.channel_id = int(self.select.values[0])
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="📬 Sending request…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()


# ── /dropproject views ────────────────────────────────────────────────────────

class _DropSelect(discord.ui.Select):
    def __init__(self, projects: list):
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["channel_id"]),
                description=(
                    _fmt_tags(row["tags"]).replace("`", "")[:100]
                    if row["tags"]
                    else "No tags"
                ),
            )
            for row in projects[:25]
        ]
        super().__init__(
            placeholder="Select the project to drop…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _DropView(discord.ui.View):
    def __init__(self, projects: list):
        super().__init__(timeout=120)
        self._map = {str(row["channel_id"]): row for row in projects}
        self.select = _DropSelect(projects)
        self.add_item(self.select)
        self.confirmed = False
        self.chosen = None

    @discord.ui.button(label="⚠️ Drop project", style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.select.values:
            await interaction.response.defer()
            return
        self.chosen = self._map.get(self.select.values[0])
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="🗑️ Dropping project…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()


# ── /projects tag filter (scrollable dropdown of existing tags) ──────────────

ALL_TAGS_SENTINEL = "__all__"


class _TagSelect(discord.ui.Select):
    def __init__(self, tags: list[str]):
        options = [
            discord.SelectOption(label="All projects", value=ALL_TAGS_SENTINEL, emoji="🗂️")
        ] + [discord.SelectOption(label=f"#{t}", value=t) for t in tags[:24]]
        super().__init__(
            placeholder="Filter by tag…", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        tag = None if self.values[0] == ALL_TAGS_SENTINEL else self.values[0]
        rows = await interaction.client.db.list_projects(interaction.guild_id, tag=tag)
        embed = _build_projects_embed(interaction.guild, rows, tag)
        await interaction.response.edit_message(embed=embed, view=self.view)


class _TagFilterView(discord.ui.View):
    def __init__(self, tags: list[str]):
        super().__init__(timeout=180)
        self.add_item(_TagSelect(tags))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Projects(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /createproject ─────────────────────────────────────────────────────

    @app_commands.command(
        name="createproject",
        description="(Eboard) Create a new project role, channel, and reaction-role entry.",
    )
    @app_commands.describe(lead="The project's team lead (pick the member)")
    @is_eboard()
    async def createproject(self, interaction: discord.Interaction, lead: discord.Member):
        await interaction.response.send_modal(_ProjectModal(self.bot, lead))

    # ── /dropproject ───────────────────────────────────────────────────────

    @app_commands.command(
        name="dropproject",
        description="(Eboard) Delete a project's channel, role, and reaction-role entry.",
    )
    @is_eboard()
    async def dropproject(self, interaction: discord.Interaction):
        projects = await self.bot.db.list_projects(interaction.guild_id)
        if not projects:
            await interaction.response.send_message(
                "No projects found. Create one with `/createproject` first.",
                ephemeral=True,
            )
            return

        view = _DropView(projects)
        await interaction.response.send_message(
            "**Drop a project**\nThis permanently deletes the channel and role.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed or view.chosen is None:
            return

        row = view.chosen
        guild = interaction.guild
        results = []

        # Remove reaction role bindings.
        rr_rows = await self.bot.db.list_reaction_roles(guild.id)
        roles_ch = gu.get_channel(guild, config.ROLES_CHANNEL_NAME)
        for rr in rr_rows:
            if rr["role_id"] == row["role_id"]:
                await self.bot.db.remove_reaction_role(rr["message_id"], rr["emoji"])
                if roles_ch:
                    try:
                        msg = await roles_ch.fetch_message(rr["message_id"])
                        await msg.clear_reaction(rr["emoji"])
                    except discord.HTTPException:
                        pass
                results.append("Removed reaction role binding from `#roles`.")

        # Delete channel.
        channel = guild.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.delete(reason=f"TaigaBot: project dropped by {interaction.user}")
                results.append(f"Deleted channel `#{channel.name}`.")
            except discord.Forbidden:
                results.append(f"⚠️ Couldn't delete channel — check permissions.")
        else:
            results.append("Channel already deleted.")

        # Delete role.
        role = guild.get_role(row["role_id"])
        if role:
            try:
                await role.delete(reason="TaigaBot: project dropped")
                results.append(f"Deleted role `@{role.name}`.")
            except discord.Forbidden:
                results.append("⚠️ Couldn't delete role — check permissions.")
        else:
            results.append("Role already deleted.")

        # Remove from DB.
        await self.bot.db.delete_project(row["channel_id"])
        results.append("Removed from project database.")

        await interaction.followup.send(
            f"🗑️ **{row['name']}** dropped:\n" + "\n".join(f"• {r}" for r in results),
            ephemeral=True,
        )

    # ── /joinproject ───────────────────────────────────────────────────────

    @app_commands.command(
        name="joinproject",
        description="Request to join a project. The project lead will approve or deny.",
    )
    @app_commands.describe(tag="Filter projects by tag (optional)")
    async def joinproject(self, interaction: discord.Interaction, tag: str | None = None):
        projects = await self.bot.db.list_projects(interaction.guild_id, tag=tag)
        if not projects:
            msg = (
                f"No projects with tag **{tag}** found." if tag
                else "No projects yet. Ask Eboard to create one with `/createproject`."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        view = _JoinView(projects)
        header = f"**Projects{f' — #{tag}' if tag else ''}**\n"
        await interaction.response.send_message(
            header + "Select a project and click **Request to join**.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed or view.channel_id is None:
            return

        guild = interaction.guild
        project = await self.bot.db.get_project(view.channel_id)
        if project is None:
            await interaction.followup.send("That project no longer exists.", ephemeral=True)
            return

        # Block duplicate pending requests.
        if await self.bot.db.has_pending_request(view.channel_id, interaction.user.id):
            await interaction.followup.send(
                "You already have a pending request for that project.", ephemeral=True
            )
            return

        # Block if they already have the role.
        role = guild.get_role(project["role_id"])
        member = interaction.user
        if role and role in member.roles:
            await interaction.followup.send(
                "You're already a member of that project!", ephemeral=True
            )
            return

        request_id = await self.bot.db.add_project_request(
            guild.id, view.channel_id, interaction.user.id
        )

        # DM the project lead.
        lead = guild.get_member(project["lead_id"])
        if lead is None:
            try:
                lead = await guild.fetch_member(project["lead_id"])
            except discord.NotFound:
                lead = None

        if lead:
            embed = discord.Embed(
                title="📬 New project join request",
                description=(
                    f"{interaction.user.mention} (`{interaction.user}`) wants to join "
                    f"**{project['name']}**."
                ),
                color=discord.Color(config.BOT_COLOR),
            )
            embed.add_field(name="Project", value=project["name"], inline=True)
            if project["tags"]:
                embed.add_field(name="Tags", value=_fmt_tags(project["tags"]), inline=True)
            embed.set_footer(text=f"Request ID: {request_id}")
            try:
                await lead.send(embed=embed, view=_ApprovalView(request_id))
                await interaction.followup.send(
                    f"✅ Request sent! The project lead for **{project['name']}** will review it.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                # Lead has DMs closed — auto-approve as fallback? No — just notify requester.
                await interaction.followup.send(
                    f"⚠️ Couldn't DM the project lead (DMs may be closed). "
                    "Ask an Eboard member to manually add you to the project.",
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                "⚠️ The project lead isn't in this server anymore. "
                "Ask an Eboard member to add you manually.",
                ephemeral=True,
            )

    # ── /projects ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="projects",
        description="Browse all projects, optionally filtered by tag.",
    )
    @app_commands.describe(tag="Filter by tag (optional)")
    async def projects_list(self, interaction: discord.Interaction, tag: str | None = None):
        all_rows = await self.bot.db.list_projects(interaction.guild_id)
        if not all_rows:
            await interaction.response.send_message(
                "No projects yet. Eboard can create one with `/createproject`.",
                ephemeral=True,
            )
            return

        if tag:
            rows = await self.bot.db.list_projects(interaction.guild_id, tag=tag)
            if not rows:
                await interaction.response.send_message(
                    f"No projects with tag **#{tag}**.", ephemeral=True
                )
                return
        else:
            rows = all_rows

        embed = _build_projects_embed(interaction.guild, rows, tag)
        # Scrollable dropdown of every existing tag, so anyone can filter without
        # typing. Always lists all tags, regardless of the current filter.
        tags = _distinct_tags(all_rows)
        view = _TagFilterView(tags) if tags else None
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Projects(bot))
    bot.add_dynamic_items(_ApproveButton, _DenyButton)
