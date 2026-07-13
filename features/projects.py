"""Project management — create, browse, join, and drop projects.

/createproject (Eboard): pick the team lead(s) → modal for name/description/tags
  → category picker → creates a role (given to the leads), a gated channel, an
  intro embed, and a DB record. Joining is via /joinproject (lead approval), so
  no self-assign reaction role is created.
  If the name already matches an existing project, NO new channel is made —
  instead the Eboard picks the existing role's @ (and any leads) and the bot
  links them to the existing channel/record.

/editproject (Eboard): pick a project → modal prefilled with its name/description/
  tags. Saving updates the DB, renames the role/channel if the name changed, and
  deletes + reposts the intro message in the project channel with the new details.

/dropproject (Eboard): select from DB-tracked projects to delete channel + role.

/deletetag [tag] (Eboard): remove a tag from every project that carries it
  (updates the DB and refreshes each affected project's intro embed in place).
  Leave the tag blank to pick it from a dropdown of existing tags — the same
  tag list the /projects filter shows — so it no longer appears there.

/joinproject [tag]: anyone can browse projects (optionally filtered by tag),
  pick one, and request to join. Every project lead gets a DM with persistent
  Approve/Deny buttons; any lead can decide. On approval the role is granted and
  the requester is DM'd the outcome.
  Projects tagged `open-source` (case/space/hyphen-insensitive) skip approval —
  /joinproject grants the role instantly.

/leaveproject: a member leaves a project they're in (drops the role). Project
  leads can't leave this way — they ask Eboard to /dropproject instead.

/projects [tag]: browse all projects, optionally filtered by tag.

Shared "Project Lead" role: every lead/co-lead also gets a single server-wide
role (name via PROJECT_LEAD_ROLE_NAME, default "Project Lead"). It's created on
demand, granted at project create/link time, and reconciled from the projects
table once on startup (startup sync is grant-only). /dropproject removes it
from the dropped project's leads unless they still lead another project.
"""
from __future__ import annotations

import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildutils as gu
from utils.checks import is_eboard, member_has_role

log = logging.getLogger("taigabot.projects")

NEW_CATEGORY_SENTINEL = "__new__"


def _parse_leads(row) -> list[int]:
    """Lead Discord IDs for a project row. Uses lead_ids (comma-separated) when
    present, else falls back to the single lead_id (legacy rows)."""
    raw = (row["lead_ids"] or "").strip() if "lead_ids" in row.keys() else ""
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    return ids or [row["lead_id"]]


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


# Projects carrying this tag let anyone join via /joinproject instantly — no lead
# approval. Matched loosely (case/space/hyphen-insensitive), so "open-source",
# "opensource", and "open source" all count.
AUTO_ACCEPT_TAG = "open-source"


def _is_auto_accept(project) -> bool:
    """True if a project opts into instant joining via the auto-accept tag."""
    target = AUTO_ACCEPT_TAG.replace("-", "").replace(" ", "")
    for t in (project["tags"] or "").split(","):
        if t.strip().lower().replace("-", "").replace(" ", "") == target:
            return True
    return False


def _is_reserved_name(name: str, bot, guild: discord.Guild) -> bool:
    """True if a project can't safely use this name because the matching role
    would collide with the bot's own role or a system/managed role — which breaks
    role hierarchy and permissions (e.g. a project literally named 'TaigaBot')."""
    n = name.strip().lower()
    reserved = {
        config.VERIFIED_ROLE_NAME.lower(),
        config.UNVERIFIED_ROLE_NAME.lower(),
        config.EBOARD_ROLE_NAME.lower(),
        "everyone", "here", "@everyone", "@here",
    }
    if bot.user:
        reserved.add(bot.user.name.lower())
    if n in reserved:
        return True
    # Also block names matching an existing managed/integration role, or any role
    # at or above the bot's own — creating/assigning those would fail or clobber.
    me_top = guild.me.top_role if guild.me else None
    for r in guild.roles:
        if r.name.lower() == n and (r.managed or (me_top is not None and r >= me_top)):
            return True
    return False


def _distinct_tags(rows) -> list[str]:
    """Sorted list of every unique tag across the given project rows."""
    tags: set[str] = set()
    for row in rows:
        for t in (row["tags"] or "").split(","):
            t = t.strip().lower()
            if t:
                tags.add(t)
    return sorted(tags)


def _projects_cooldown(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """/projects rate limit: 3 uses per 5 minutes per member. Eboard (and
    admins, matching is_eboard()) are exempt — returning None skips the
    cooldown entirely. CommandOnCooldown is handled centrally in bot.py."""
    member = interaction.user
    if isinstance(member, discord.Member) and (
        member.guild_permissions.administrator
        or member_has_role(member, config.EBOARD_ROLE_NAME)
    ):
        return None
    return app_commands.Cooldown(3, 300.0)


def _build_projects_embed(guild: discord.Guild, rows, tag: str | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🗂️ Projects{f' — #{tag}' if tag else ''}",
        color=discord.Color(config.BOT_COLOR),
        description=f"{len(rows)} project(s)" + (" found." if tag else "."),
    )
    shown = 0
    for row in rows[:15]:
        tags_str = _fmt_tags(row["tags"]) if row["tags"] else "—"
        channel = guild.get_channel(row["channel_id"])
        ch_ref = channel.mention if channel else f"`#{_channel_name(row['name'])}`"
        lead_ids = _parse_leads(row)
        leads_str = ", ".join(f"<@{i}>" for i in lead_ids)
        lead_label = "Leads" if len(lead_ids) > 1 else "Lead"
        meta = (
            f"\n**{lead_label}:** {leads_str} | **Channel:** {ch_ref}\n"
            f"**Tags:** {tags_str}"
        )
        # Full description, bounded only by Discord's hard caps: 1024 chars per
        # field value and 6000 chars per embed (stop adding fields near it).
        desc = row["description"] or ""
        room = 1024 - len(meta)
        if len(desc) > room:
            desc = desc[: room - 1] + "…"
        if len(embed) + len(row["name"]) + len(desc) + len(meta) > 5900:
            break
        embed.add_field(name=row["name"], value=desc + meta, inline=False)
        shown += 1
    if shown < len(rows):
        embed.set_footer(text=f"Showing {shown} of {len(rows)}.")
    return embed


def _build_intro_embed(name, desc, tags, lead_mentions: list[str], role) -> discord.Embed:
    """The pinned 'intro' embed posted inside a project channel. Shared by
    /createproject and /editproject so both render identically."""
    embed = discord.Embed(
        title=f"📌 {name}", description=desc, color=discord.Color(config.BOT_COLOR)
    )
    label = "Team Lead" + ("s" if len(lead_mentions) > 1 else "")
    embed.add_field(name=label, value=", ".join(lead_mentions) or "—", inline=True)
    if role is not None:
        embed.add_field(name="Role", value=role.mention, inline=True)
    if tags:
        embed.add_field(name="Tags", value=_fmt_tags(tags), inline=False)
    embed.set_footer(text="Welcome to the team! Leads approve new members from their DMs.")
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

    guild = interaction.client.get_guild(req["guild_id"])
    project = await db.get_project(req["channel_id"]) if guild else None

    # Authorization: only a CURRENT project lead (or an admin/Eboard) may decide.
    # The buttons live in a lead's DM, but don't rely on that alone — verify the
    # clicker is still authorized (e.g. a former lead could retain the DM).
    is_lead = project is not None and interaction.user.id in _parse_leads(project)
    member = guild.get_member(interaction.user.id) if guild else None
    is_staff = member is not None and (
        member.guild_permissions.administrator
        or member_has_role(member, config.EBOARD_ROLE_NAME)
    )
    if not (is_lead or is_staff):
        await interaction.response.send_message(
            "⛔ Only a current lead of this project can decide this request.",
            ephemeral=True,
        )
        return

    status = "approved" if approved else "denied"
    await db.update_request_status(request_id, status)

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


class _LeadSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Add co-leads (optional)…",
            min_values=0,
            max_values=10,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _CategoryView(discord.ui.View):
    def __init__(self, categories: list[discord.CategoryChannel], primary_lead: discord.Member):
        super().__init__(timeout=120)
        self.primary_lead = primary_lead
        self.select = _CategorySelect(categories)
        self.add_item(self.select)
        self.lead_select = _LeadSelect()
        self.add_item(self.lead_select)
        self.confirmed = False
        self.category_value: str = NEW_CATEGORY_SENTINEL
        self.leads: list[discord.Member] = [primary_lead]

    @discord.ui.button(label="Create project", style=discord.ButtonStyle.green, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.category_value = (
            self.select.values[0] if self.select.values else NEW_CATEGORY_SENTINEL
        )
        # Primary lead first, then any co-leads picked, deduped, no bots.
        chosen = {self.primary_lead.id: self.primary_lead}
        for m in self.lead_select.values:
            if not m.bot:
                chosen.setdefault(m.id, m)
        self.leads = list(chosen.values())
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⚙️ Creating project…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()


# ── Link-existing picker (shown when the project name already exists) ─────────

class _LinkRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Pick the project's role…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _LinkView(discord.ui.View):
    """Shown when /createproject is used with a name that already exists. Instead
    of making a new channel, the Eboard picks the existing role's @ (and optionally
    extra leads); we then attach that role and add the lead(s) to the project."""

    def __init__(self, primary_lead: discord.Member):
        super().__init__(timeout=120)
        self.primary_lead = primary_lead
        self.role_select = _LinkRoleSelect()
        self.add_item(self.role_select)
        self.lead_select = _LeadSelect()
        self.add_item(self.lead_select)
        self.confirmed = False
        self.role: discord.Role | None = None
        self.leads: list[discord.Member] = [primary_lead]

    @discord.ui.button(label="Link project", style=discord.ButtonStyle.green, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.role_select.values:
            await interaction.response.send_message(
                "Pick the project's role first.", ephemeral=True
            )
            return
        self.role = self.role_select.values[0]
        chosen = {self.primary_lead.id: self.primary_lead}
        for m in self.lead_select.values:
            if not m.bot:
                chosen.setdefault(m.id, m)
        self.leads = list(chosen.values())
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="🔗 Linking project…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=2)
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
        name = self.project_name.value.strip()

        # Block names that would collide with the bot's own role or a system role
        # (e.g. a project named "TaigaBot"), which breaks role hierarchy.
        if _is_reserved_name(name, self.bot, guild):
            await interaction.response.send_message(
                f"⛔ **{name}** can't be used as a project name — it collides with a "
                "bot or system role and would break role permissions. Pick a "
                "different name.",
                ephemeral=True,
            )
            return

        # If a project with this name already exists, don't make a new channel —
        # link to the existing one: ask for the role @ and the lead instead.
        existing = await self._find_existing(guild.id, name)
        if existing is not None:
            await self._link_existing(interaction, guild, existing)
            return

        tags_fmt = _fmt_tags(self.tags.value or "")
        view = _CategoryView(guild.categories, self.lead)
        await interaction.response.send_message(
            f"**Project:** {self.project_name.value}\n"
            f"**Tags:** {tags_fmt or 'none'}\n"
            f"**Lead:** {self.lead.mention}  *(add co-leads below if you want)*\n\n"
            "Pick a category, optionally add co-leads, then **Create project**.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return

        await self._create(interaction, guild, view.leads, view.category_value)

    async def _find_existing(self, guild_id: int, name: str):
        """Return the DB row for a project with this (case-insensitive) name in
        the guild, or None. Source of truth is the project database."""
        name_l = name.lower()
        for row in await self.bot.db.list_projects(guild_id):
            if (row["name"] or "").strip().lower() == name_l:
                return row
        return None

    async def _link_existing(self, interaction: discord.Interaction, guild: discord.Guild, existing):
        """Project name already exists: reuse its channel, attach a role the
        Eboard picks, add the lead(s), and update the DB record. No new channel
        or role is created."""
        name = existing["name"]
        view = _LinkView(self.lead)
        await interaction.response.send_message(
            f"⚠️ A project named **{name}** already exists — I won't create a new "
            f"channel.\nPick its **role** to attach (and add co-leads if you want), "
            f"then **Link project**.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed or view.role is None:
            return

        role = view.role
        leads = view.leads

        # Give every lead the project role (plus the shared Project Lead role).
        lead_role = await gu.ensure_project_lead_role(guild)
        for lead in leads:
            try:
                if role not in lead.roles:
                    await lead.add_roles(role, reason=f"Team lead of {name}")
                if lead_role and lead_role not in lead.roles:
                    await lead.add_roles(lead_role, reason=f"Team lead of {name}")
            except discord.Forbidden:
                pass

        # Merge new lead(s) into the existing lead list (existing primary kept).
        merged = list(dict.fromkeys(_parse_leads(existing) + [m.id for m in leads]))

        # Update the DB record in place (same channel_id PK → INSERT OR REPLACE),
        # keeping the existing description and tags.
        await self.bot.db.add_project(
            existing["channel_id"], guild.id, name, role.id, merged,
            existing["description"], existing["tags"],
        )

        channel = guild.get_channel(existing["channel_id"])
        ch_ref = channel.mention if channel else f"`#{_channel_name(name)}` *(channel missing)*"
        leads_str = ", ".join(m.mention for m in leads)
        await interaction.followup.send(
            f"🔗 Linked **{name}** to {role.mention} — no new channel created.\n"
            f"• Channel: {ch_ref} (reused)\n"
            f"• Lead(s) added: {leads_str} (role granted)\n"
            f"Members still join via `/joinproject`.",
            ephemeral=True,
        )

    async def _create(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        leads: list[discord.Member],
        category_value: str,
    ):
        name = self.project_name.value.strip()
        desc = self.description.value.strip()
        tags = _norm_tags(self.tags.value or "")
        ch_name = _channel_name(name)

        # Create the role, category, and channel. These need Manage Roles /
        # Manage Channels; if we lack them (or our role sits too low) Discord
        # raises Forbidden — surface that instead of leaving the modal stuck on
        # "⚙️ Creating project…".
        try:
            # Category.
            if category_value == NEW_CATEGORY_SENTINEL:
                existing = discord.utils.find(
                    lambda c: c.name.lower() == "projects", guild.categories
                )
                category = existing or await guild.create_category(
                    "Projects", reason=f"TaigaBot: project {name}"
                )
            else:
                category = guild.get_channel(int(category_value))  # None if deleted

            # Role (reuse one with the same name if it exists).
            role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
            if role is None:
                role = await guild.create_role(
                    name=name,
                    color=discord.Color(config.BOT_COLOR),
                    reason=f"TaigaBot: project role for {name}",
                )

            # Give every team lead the project role (plus the shared Project
            # Lead role) right away.
            lead_role = await gu.ensure_project_lead_role(guild)
            for lead in leads:
                try:
                    if role not in lead.roles:
                        await lead.add_roles(role, reason=f"Team lead of {name}")
                    if lead_role and lead_role not in lead.roles:
                        await lead.add_roles(lead_role, reason=f"Team lead of {name}")
                except discord.Forbidden:
                    pass

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
        except discord.Forbidden as e:
            if e.code == 60003:
                msg = (
                    "⛔ This server **requires 2FA for moderation actions**, but the "
                    "bot's owner account doesn't have 2FA enabled — so Discord blocks "
                    "this even though my permissions are fine. Enable 2FA on the bot "
                    "owner's Discord account, or turn off **Server Settings → Safety "
                    "Setup → Require 2FA for moderation**."
                )
            else:
                msg = (
                    f"⛔ Discord blocked the project setup (Forbidden: "
                    f"{e.text or 'missing access'}). Check I have **Manage Roles** and "
                    "**Manage Channels** and that my **TaigaBot** role sits near the top "
                    "in **Server Settings → Roles**."
                )
            await interaction.followup.send(msg, ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Discord rejected the project setup ({e}). Try again.",
                ephemeral=True,
            )
            return

        # Intro embed (best-effort — the channel already exists either way).
        leads_str = ", ".join(m.mention for m in leads)
        embed = _build_intro_embed(name, desc, tags, [m.mention for m in leads], role)
        try:
            intro = await channel.send(embed=embed)
            await self.bot.db.set_intro_message(channel.id, intro.id)
        except discord.HTTPException:
            pass

        # Persist to the DB. Joining is via /joinproject (lead approval), so we
        # deliberately do NOT auto-create a self-assign reaction role.
        await self.bot.db.add_project(
            channel.id, guild.id, name, role.id, [m.id for m in leads], desc, tags
        )

        await interaction.followup.send(
            f"✅ **{name}** is ready!\n"
            f"• Channel: {channel.mention}\n"
            f"• Role: {role.mention} (given to {leads_str})\n"
            f"• Category: **{category.name if category else 'none'}**\n"
            f"• Tags: {_fmt_tags(tags) or 'none'}\n"
            f"Members join via `/joinproject` — leads approve requests by DM.",
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


# ── /leaveproject views ───────────────────────────────────────────────────────

class _LeaveSelect(discord.ui.Select):
    def __init__(self, projects: list):
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["channel_id"]),
                description=(
                    _fmt_tags(row["tags"]).replace("`", "")[:100]
                    if row["tags"] else "No tags"
                ),
            )
            for row in projects[:25]
        ]
        super().__init__(
            placeholder="Pick a project to leave…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _LeaveView(discord.ui.View):
    def __init__(self, projects: list):
        super().__init__(timeout=120)
        self.select = _LeaveSelect(projects)
        self.add_item(self.select)
        self.confirmed = False
        self.channel_id: int | None = None

    @discord.ui.button(label="👋 Leave project", style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.select.values:
            await interaction.response.send_message(
                "Please select a project first.", ephemeral=True
            )
            return
        self.channel_id = int(self.select.values[0])
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="👋 Leaving…", view=self)
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
        # Rebuild the dropdown from the CURRENT tag list. The options were baked
        # in when the message was posted, so a tag deleted since (/deletetag) or
        # added since would otherwise keep showing / stay missing here.
        all_rows = await interaction.client.db.list_projects(interaction.guild_id)
        if self.view is not None:
            self.view.stop()
        await interaction.response.edit_message(
            embed=embed, view=_TagFilterView(_distinct_tags(all_rows))
        )


class _TagFilterView(discord.ui.View):
    def __init__(self, tags: list[str]):
        super().__init__(timeout=180)
        self.add_item(_TagSelect(tags))


# ── /deletetag tag picker (same tag list as the /projects filter) ────────────

class _DeleteTagSelect(discord.ui.Select):
    def __init__(self, tags: list[str]):
        options = [discord.SelectOption(label=f"#{t}", value=t) for t in tags[:25]]
        super().__init__(
            placeholder="Pick the tag to delete…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _DeleteTagView(discord.ui.View):
    """Lets the Eboard pick a tag to strip from every project — the same tag
    list the /projects filter shows, so deleting one removes it from there too."""

    def __init__(self, tags: list[str]):
        super().__init__(timeout=120)
        self.select = _DeleteTagSelect(tags)
        self.add_item(self.select)
        self.confirmed = False
        self.tag: str | None = None

    @discord.ui.button(label="🗑️ Delete tag", style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.select.values:
            await interaction.response.send_message(
                "Pick a tag to delete first.", ephemeral=True
            )
            return
        self.tag = self.select.values[0]
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="🗑️ Deleting tag…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)
        self.stop()


# ── /editproject (pick a project → modal prefilled with its details) ──────────

class _EditModal(discord.ui.Modal, title="Edit project details"):
    def __init__(self, bot: commands.Bot, project):
        super().__init__()
        self.bot = bot
        self.project = project
        self.name_input = discord.ui.TextInput(
            label="Project name", default=project["name"], max_length=50
        )
        self.desc_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=project["description"],
            required=False,
            max_length=500,
        )
        self.tags_input = discord.ui.TextInput(
            label="Tags (comma-separated)",
            default=project["tags"],
            required=False,
            max_length=100,
        )
        self.add_item(self.name_input)
        self.add_item(self.desc_input)
        self.add_item(self.tags_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        channel_id = self.project["channel_id"]
        name = self.name_input.value.strip() or self.project["name"]
        desc = self.desc_input.value.strip()
        tags = _norm_tags(self.tags_input.value or "")

        await self.bot.db.update_project_details(channel_id, name, desc, tags)

        channel = guild.get_channel(channel_id)
        role = guild.get_role(self.project["role_id"])

        # If the name changed, best-effort rename the role + channel to match.
        if name != self.project["name"]:
            if role:
                try:
                    await role.edit(name=name, reason="TaigaBot: project renamed")
                except discord.HTTPException:
                    pass
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.edit(
                        name=_channel_name(name), reason="TaigaBot: project renamed"
                    )
                except discord.HTTPException:
                    pass

        # Delete the old intro message and repost it with the updated details.
        reposted = False
        if isinstance(channel, discord.TextChannel):
            old_id = self.project["intro_message_id"] if "intro_message_id" in self.project.keys() else 0
            if old_id:
                try:
                    old = await channel.fetch_message(old_id)
                    await old.delete()
                except discord.HTTPException:
                    pass  # already gone / can't fetch — just post a fresh one
            lead_mentions = [f"<@{i}>" for i in _parse_leads(self.project)]
            embed = _build_intro_embed(name, desc, tags, lead_mentions, role)
            try:
                new_msg = await channel.send(embed=embed)
                await self.bot.db.set_intro_message(channel_id, new_msg.id)
                reposted = True
            except discord.HTTPException:
                pass

        ch_ref = channel.mention if isinstance(channel, discord.TextChannel) else "the project channel"
        note = "• Reposted the intro with the new details.\n" if reposted else ""
        await interaction.followup.send(
            f"✅ Updated **{name}**.\n{note}"
            f"• Channel: {ch_ref}\n"
            f"• Tags: {_fmt_tags(tags) or 'none'}",
            ephemeral=True,
        )


class _EditSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot, projects: list):
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["channel_id"]),
                description=(
                    _fmt_tags(row["tags"]).replace("`", "")[:100]
                    if row["tags"] else "No tags"
                ),
            )
            for row in projects[:25]
        ]
        super().__init__(
            placeholder="Select the project to edit…",
            min_values=1, max_values=1, options=options,
        )
        self.bot = bot
        self._map = {str(row["channel_id"]): row for row in projects}

    async def callback(self, interaction: discord.Interaction):
        # Opening the modal must be the *first* response on this interaction.
        await interaction.response.send_modal(
            _EditModal(self.bot, self._map[self.values[0]])
        )


class _EditView(discord.ui.View):
    def __init__(self, bot: commands.Bot, projects: list):
        super().__init__(timeout=120)
        self.add_item(_EditSelect(bot, projects))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Projects(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lead_role_synced = False

    # ── Startup: shared Project Lead role ───────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready re-fires on every reconnect; reconcile only once per process.
        if self._lead_role_synced:
            return
        self._lead_role_synced = True
        for guild in self.bot.guilds:
            try:
                await self._sync_project_lead_role(guild)
            except Exception:  # noqa: BLE001 - one bad guild must not stop the rest
                log.exception("Project Lead role sync failed for guild %s", guild.id)

    async def _sync_project_lead_role(self, guild: discord.Guild) -> None:
        """Ensure the shared Project Lead role exists and every lead/co-lead of
        a DB-tracked project carries it. Grant-only — removal happens in
        /dropproject when a lead's last project is dropped."""
        rows = await self.bot.db.list_projects(guild.id)
        if not rows:
            return
        lead_role = await gu.ensure_project_lead_role(guild)
        if lead_role is None:
            log.warning(
                "Can't create the '%s' role in %s — missing Manage Roles.",
                config.PROJECT_LEAD_ROLE_NAME, guild.name,
            )
            return

        lead_ids = {i for row in rows for i in _parse_leads(row)}
        granted = 0
        for uid in lead_ids:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue  # lead left the server (or invalid id)
            if lead_role in member.roles:
                continue
            try:
                await member.add_roles(lead_role, reason="TaigaBot: project lead")
                granted += 1
            except discord.Forbidden:
                # Role sits above the bot — it'll fail for everyone, stop here.
                log.warning(
                    "Can't grant '%s' in %s — move TaigaBot's role above it.",
                    lead_role.name, guild.name,
                )
                return
        if granted:
            log.info(
                "Granted '%s' to %d project lead(s) in %s.",
                lead_role.name, granted, guild.name,
            )

    # ── /createproject ─────────────────────────────────────────────────────

    @app_commands.command(
        name="createproject",
        description="(Eboard) Create a new project role, channel, and reaction-role entry.",
    )
    @app_commands.describe(lead="The project's team lead (pick the member)")
    @is_eboard()
    async def createproject(self, interaction: discord.Interaction, lead: discord.Member):
        await interaction.response.send_modal(_ProjectModal(self.bot, lead))

    # ── /editproject ───────────────────────────────────────────────────────

    @app_commands.command(
        name="editproject",
        description="(Eboard) Edit a project's name, description, or tags.",
    )
    @is_eboard()
    async def editproject(self, interaction: discord.Interaction):
        projects = await self.bot.db.list_projects(interaction.guild_id)
        if not projects:
            await interaction.response.send_message(
                "No projects to edit. Create one with `/createproject` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "**Edit a project**\nPick one — a form will pop up with its current "
            "details. Saving reposts the intro in the project channel.",
            view=_EditView(self.bot, projects),
            ephemeral=True,
        )

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

        # Strip the shared Project Lead role from this project's leads — unless
        # they still lead another project in this guild.
        lead_role = gu.project_lead_role(guild)
        if lead_role:
            remaining = await self.bot.db.list_projects(guild.id)
            still_leading = {i for r in remaining for i in _parse_leads(r)}
            removed = []
            for uid in _parse_leads(row):
                if uid in still_leading:
                    continue
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except discord.HTTPException:
                        continue  # lead left the server
                if lead_role not in member.roles:
                    continue
                try:
                    await member.remove_roles(
                        lead_role, reason=f"TaigaBot: project {row['name']} dropped"
                    )
                    removed.append(member.mention)
                except discord.Forbidden:
                    results.append(
                        f"⚠️ Couldn't remove `@{lead_role.name}` — check permissions."
                    )
                    break
            if removed:
                results.append(f"Removed `@{lead_role.name}` from {', '.join(removed)}.")

        await interaction.followup.send(
            f"🗑️ **{row['name']}** dropped:\n" + "\n".join(f"• {r}" for r in results),
            ephemeral=True,
        )

    # ── /joinproject ───────────────────────────────────────────────────────

    @app_commands.command(
        name="joinproject",
        description="Join a project (open-source ones are instant; others need lead approval).",
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

        # Open-source projects auto-accept — grant the role now, no lead approval.
        if _is_auto_accept(project):
            if role is None:
                await interaction.followup.send(
                    "⚠️ That project's role is missing — ask an Eboard member to fix it.",
                    ephemeral=True,
                )
                return
            try:
                await member.add_roles(role, reason="Auto-join: open-source project")
            except discord.Forbidden:
                await interaction.followup.send(
                    "⛔ I couldn't grant the role — my role may be too low. Ask an Eboard member.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"✅ Joined **{project['name']}**! It's **open-source**, so no approval "
                f"needed — you now have {role.mention}. Welcome aboard! 🎉",
                ephemeral=True,
            )
            return

        request_id = await self.bot.db.add_project_request(
            guild.id, view.channel_id, interaction.user.id
        )

        # DM every lead — any of them can approve/deny the shared request.
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
        embed.set_footer(text=f"Request ID: {request_id} • the first lead to decide wins")

        notified = 0
        for lead_id in _parse_leads(project):
            lead = guild.get_member(lead_id)
            if lead is None:
                try:
                    lead = await guild.fetch_member(lead_id)
                except discord.NotFound:
                    continue
            try:
                await lead.send(embed=embed, view=_ApprovalView(request_id))
                notified += 1
            except discord.HTTPException:
                pass  # that lead has DMs closed — try the others

        if notified:
            await interaction.followup.send(
                f"✅ Request sent to the lead(s) of **{project['name']}** — "
                "you'll be DM'd once it's reviewed.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "⚠️ Couldn't reach any project lead (DMs closed or they left). "
                "Ask an Eboard member to add you manually.",
                ephemeral=True,
            )

    # ── /leaveproject ──────────────────────────────────────────────────────

    @app_commands.command(
        name="leaveproject",
        description="Leave a project you've joined (removes its role).",
    )
    async def leaveproject(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        # Projects this member is actually in (holds the role).
        mine = [
            p for p in await self.bot.db.list_projects(guild.id)
            if (r := guild.get_role(p["role_id"])) is not None and r in member.roles
        ]
        if not mine:
            await interaction.response.send_message(
                "You're not in any projects.", ephemeral=True
            )
            return

        view = _LeaveView(mine)
        await interaction.response.send_message(
            "**Leave a project**\nPick the one you want to leave.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed or view.channel_id is None:
            return

        project = await self.bot.db.get_project(view.channel_id)
        if project is None:
            await interaction.followup.send("That project no longer exists.", ephemeral=True)
            return

        # Leads can't leave on their own — they must ask Eboard to drop the project.
        if member.id in _parse_leads(project):
            await interaction.followup.send(
                f"⛔ You're a lead of **{project['name']}**, so you can't leave it "
                "yourself. Ask an Eboard member to `/dropproject` instead.",
                ephemeral=True,
            )
            return

        role = guild.get_role(project["role_id"])
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Left project via /leaveproject")
            except discord.Forbidden:
                await interaction.followup.send(
                    "⛔ I couldn't remove the role — my role may be too low. "
                    "Ask an Eboard member.",
                    ephemeral=True,
                )
                return
        await interaction.followup.send(
            f"👋 You left **{project['name']}**.", ephemeral=True
        )

    # ── /projects ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="projects",
        description="Browse all projects, optionally filtered by tag.",
    )
    @app_commands.describe(tag="Filter by tag (optional)")
    @app_commands.checks.dynamic_cooldown(
        _projects_cooldown, key=lambda i: (i.guild_id, i.user.id)
    )
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

    # ── /projecttags ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="projecttags",
        description="List all project tags and how many projects use each.",
    )
    async def projecttags(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_projects(interaction.guild_id)
        counts: dict[str, int] = {}
        for r in rows:
            for t in (r["tags"] or "").split(","):
                t = t.strip().lower()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        if not counts:
            await interaction.response.send_message(
                "No tags yet. Add them when creating a project with `/createproject`.",
                ephemeral=True,
            )
            return
        lines = [f"`#{t}` — {n} project(s)" for t, n in sorted(counts.items())]
        embed = discord.Embed(
            title="🏷️ Project tags",
            description="\n".join(lines),
            color=discord.Color(config.BOT_COLOR),
        )
        embed.set_footer(text="Filter with /projects tag:<tag> or the dropdown in /projects.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /deletetag ────────────────────────────────────────────────────────────

    async def _refresh_intro(self, guild: discord.Guild, project, new_tags: str) -> None:
        """Best-effort: edit a project's intro embed in place so it reflects the
        updated tags. Editing (vs. delete+repost) keeps the message in position
        and avoids spamming the channel when many projects are touched at once."""
        channel = guild.get_channel(project["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        intro_id = project["intro_message_id"] if "intro_message_id" in project.keys() else 0
        if not intro_id:
            return
        role = guild.get_role(project["role_id"])
        lead_mentions = [f"<@{i}>" for i in _parse_leads(project)]
        embed = _build_intro_embed(
            project["name"], project["description"], new_tags, lead_mentions, role
        )
        try:
            msg = await channel.fetch_message(intro_id)
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass  # message gone / can't edit — the DB is still updated

    async def _apply_tag_deletion(self, interaction: discord.Interaction, target: str) -> None:
        """Strip `target` from every project that carries it, update the DB and
        refresh each affected intro embed, then report the result. Assumes the
        interaction has already been responded to or deferred (uses followup)."""
        rows = await self.bot.db.list_projects(interaction.guild_id)
        affected = []  # (row, new_tags_csv)
        for row in rows:
            existing = [t.strip().lower() for t in (row["tags"] or "").split(",") if t.strip()]
            if target in existing:
                new_tags = ",".join(t for t in existing if t != target)
                affected.append((row, new_tags))

        if not affected:
            await interaction.followup.send(
                f"`#{target}` isn't on any project — tags only live on projects, so "
                f"it's already gone from the database. If an older dropdown still "
                f"lists it, that message is just stale; re-run `/projects` or "
                f"`/deletetag` for a current list.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        for row, new_tags in affected:
            await self.bot.db.update_project_details(
                row["channel_id"], row["name"], row["description"], new_tags
            )
            await self._refresh_intro(guild, row, new_tags)

        names = ", ".join(f"**{r['name']}**" for r, _ in affected[:10])
        more = f" (+{len(affected) - 10} more)" if len(affected) > 10 else ""
        await interaction.followup.send(
            f"🏷️ Removed `#{target}` from **{len(affected)}** project(s): {names}{more}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="deletetag",
        description="(Eboard) Remove a tag from every project that uses it.",
    )
    @app_commands.describe(tag="The tag to delete (leave blank to pick from a dropdown)")
    @is_eboard()
    async def deletetag(self, interaction: discord.Interaction, tag: str | None = None):
        # Tag typed out: delete it directly.
        if tag is not None:
            target = tag.strip().lower().lstrip("#")
            if not target:
                await interaction.response.send_message(
                    "Give a tag to delete, e.g. `/deletetag tag:web`.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._apply_tag_deletion(interaction, target)
            return

        # No tag given: show a dropdown of every existing tag — the same list the
        # /projects filter shows — so picking one removes it from there too.
        # The picker is REBUILT from the DB after every deletion, so its options
        # never go stale and several tags can be deleted in one sitting.
        tags = _distinct_tags(await self.bot.db.list_projects(interaction.guild_id))
        if not tags:
            await interaction.response.send_message(
                "No tags to delete yet. Add them when creating a project with "
                "`/createproject`.",
                ephemeral=True,
            )
            return

        prompt = (
            "**Delete a tag**\nPick the tag to strip from every project that uses "
            "it — it'll also drop out of the `/projects` filter dropdown."
        )
        view = _DeleteTagView(tags)
        await interaction.response.send_message(prompt, view=view, ephemeral=True)

        while True:
            timed_out = await view.wait()
            if timed_out:
                # Grey out the dead components so the stale picker can't be
                # mistaken for a live one.
                for item in view.children:
                    item.disabled = True
                try:
                    await interaction.edit_original_response(
                        content="⏰ Timed out — run `/deletetag` again.", view=view
                    )
                except discord.HTTPException:
                    pass
                return
            if not view.confirmed or view.tag is None:  # cancelled
                return
            await self._apply_tag_deletion(interaction, view.tag)

            # Refresh the picker from what's left so it always mirrors the DB.
            tags = _distinct_tags(await self.bot.db.list_projects(interaction.guild_id))
            if not tags:
                await interaction.edit_original_response(
                    content="🏷️ That was the last tag — none left to delete.", view=None
                )
                return
            view = _DeleteTagView(tags)
            await interaction.edit_original_response(content=prompt, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Projects(bot))
    bot.add_dynamic_items(_ApproveButton, _DenyButton)
