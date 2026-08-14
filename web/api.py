"""JSON API for the dashboard.

Runs in the bot process, so "which servers can this person manage?" is answered
from the bot's own guild/member cache rather than from Discord's API or from an
OAuth scope. That's both faster and less invasive: we only ever see servers the
bot is already in.
"""
from __future__ import annotations

import logging
import time

import discord
from aiohttp import web

import config
from utils import guildutils as gu
from utils.checks import member_has_role
from web.auth import current_user, is_owner, require_login, require_owner

log = logging.getLogger("taigabot.web.api")

MAX_SUBJECT = 150
MAX_BODY = 4000
MAX_EJECT_REASON = 500


def _guild_icon(guild: discord.Guild) -> str:
    return str(guild.icon.url) if guild.icon else ""


async def _tier(db, guild_id: int) -> str:
    return "premium" if await db.is_premium(guild_id) else "free"


def manageable_guilds(bot, user_id: int) -> list[discord.Guild]:
    """Every guild where this user is Eboard (or a server admin) *and* the bot is
    present. Derived from the bot's member cache — no Discord API calls, and no
    visibility into servers the bot isn't in.

    Requires the members intent (bot.py enables it) for the role check to work on
    uncached members; falls back to whatever is cached otherwise.
    """
    out = []
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is None:
            continue
        if member.guild_permissions.administrator or member_has_role(
            member, config.EBOARD_ROLE_NAME
        ):
            out.append(guild)
    return out


# ── identity ──────────────────────────────────────────────────────────────────

async def me(request: web.Request) -> web.Response:
    """Who am I? Unauthenticated callers get a 200 with authenticated=false, so
    the frontend can render the logged-out state without treating it as an error."""
    session = await current_user(request)
    if session is None:
        return web.json_response(
            {"authenticated": False, "loginEnabled": config.dashboard_ready()}
        )
    uid = session["user_id"]
    return web.json_response({
        "authenticated": True,
        "loginEnabled": True,
        "id": str(uid),
        "username": session["username"],
        "avatar": (
            f"https://cdn.discordapp.com/avatars/{uid}/{session['avatar']}.png"
            if session["avatar"] else ""
        ),
        "isOwner": is_owner(uid),
    })


# ── servers ───────────────────────────────────────────────────────────────────

@require_login
async def my_guilds(request: web.Request) -> web.Response:
    """Servers this user can manage, with their tier."""
    bot = request.app["bot"]
    db = bot.db
    uid = request["session"]["user_id"]

    out = []
    for guild in manageable_guilds(bot, uid):
        member = guild.get_member(uid)
        out.append({
            "id": str(guild.id),
            "name": guild.name,
            "icon": _guild_icon(guild),
            "memberCount": guild.member_count,
            "tier": await _tier(db, guild.id),
            "role": (
                "admin" if member.guild_permissions.administrator else "eboard"
            ),
        })
    out.sort(key=lambda g: g["name"].lower())
    return web.json_response({"guilds": out})


@require_login
async def guild_detail(request: web.Request) -> web.Response:
    """Detail for one server, including its news subscriptions and feed limit.

    Authorization is re-checked here rather than trusted from the list call —
    the guild id comes from the URL and a client can put anything there.
    """
    bot = request.app["bot"]
    gid = int(request.match_info["guild_id"])
    uid = request["session"]["user_id"]

    if not any(g.id == gid for g in manageable_guilds(bot, uid)):
        return web.json_response({"error": "You don't manage that server."}, status=403)

    guild = bot.get_guild(gid)
    premium = await bot.db.is_premium(gid)
    row = await bot.db.get_premium(gid)
    subs = await bot.db.get_guild_news_subs(gid)

    return web.json_response({
        "id": str(guild.id),
        "name": guild.name,
        "icon": _guild_icon(guild),
        "memberCount": guild.member_count,
        "tier": "premium" if premium else "free",
        "premiumExpiresAt": row["expires_at"] if row else 0,
        "limits": {
            "customFeeds": (
                config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS if premium
                else config.NEWS_MAX_CUSTOM_FEEDS
            ),
            "customFeedsPremium": config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS,
        },
        "news": [
            {
                "feedId": s["feed_id"],
                "label": s["label"],
                # This server's own name for the feed; blank means it uses the
                # default (built-in label or hostname).
                "name": s["display_name"],
                "url": s["url"],
                "channelId": str(s["channel_id"]),
                "channelName": getattr(guild.get_channel(s["channel_id"]), "name", None),
                "lastPolled": s["last_polled"],
                "failCount": s["fail_count"],
                "lastError": s["last_error"],
            }
            for s in subs
        ],
    })


# ── premium (owner only) ──────────────────────────────────────────────────────

@require_owner
async def premium_list(request: web.Request) -> web.Response:
    """Every server the bot is in, with its tier — the owner's grant surface.
    Includes free servers so premium can be granted without knowing an ID."""
    bot = request.app["bot"]
    granted = {r["guild_id"]: r for r in await bot.db.list_premium()}

    out = []
    for guild in bot.guilds:
        row = granted.pop(guild.id, None)
        out.append({
            "id": str(guild.id),
            "name": guild.name,
            "icon": _guild_icon(guild),
            "memberCount": guild.member_count,
            "tier": "premium" if await bot.db.is_premium(guild.id) else "free",
            "grantedAt": row["granted_at"] if row else 0,
            "expiresAt": row["expires_at"] if row else 0,
            "note": row["note"] if row else "",
        })
    # Grants for servers the bot has since left — surfaced so they can be cleaned up.
    for gid, row in granted.items():
        out.append({
            "id": str(gid), "name": f"(bot not in server)", "icon": "",
            "memberCount": 0,
            "tier": "premium" if (row["expires_at"] == 0 or row["expires_at"] > time.time()) else "free",
            "grantedAt": row["granted_at"], "expiresAt": row["expires_at"],
            "note": row["note"], "orphaned": True,
        })
    out.sort(key=lambda g: (g["tier"] != "premium", g["name"].lower()))
    return web.json_response({"servers": out})


@require_owner
async def premium_grant(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Expected JSON."}, status=400)

    raw_id = str(data.get("guildId", "")).strip()
    if not raw_id.isdigit():
        return web.json_response({"error": "guildId must be numeric."}, status=400)
    guild_id = int(raw_id)

    days = data.get("days") or 0
    try:
        days = int(days)
    except (TypeError, ValueError):
        return web.json_response({"error": "days must be a number."}, status=400)

    expires_at = int(time.time()) + days * 86400 if days > 0 else 0
    await request.app["bot"].db.grant_premium(
        guild_id,
        granted_by=request["session"]["user_id"],
        expires_at=expires_at,
        note=str(data.get("note", ""))[:300],
    )
    log.info(
        "Premium granted to %s by %s (days=%s).",
        guild_id, request["session"]["user_id"], days or "never",
    )
    return web.json_response({"ok": True, "guildId": str(guild_id), "expiresAt": expires_at})


@require_owner
async def premium_revoke(request: web.Request) -> web.Response:
    guild_id = int(request.match_info["guild_id"])
    removed = await request.app["bot"].db.revoke_premium(guild_id)
    log.info("Premium revoked from %s by %s.", guild_id, request["session"]["user_id"])
    return web.json_response({"ok": True, "removed": removed})


# ── eject / server bans (owner only) ──────────────────────────────────────────

async def _eject_guild(bot, guild: discord.Guild, reason: str) -> tuple[bool, str]:
    """Tell the server why, then leave it. Returns (announced, channel name).

    The announcement goes out FIRST: once the bot has left it can't reach the
    server at all — the same ordering `moderation._dm_action` relies on. Delivery
    is best-effort, because a server that has locked the bot out of every channel
    must still be leavable; a failed send is reported, not raised.

    Premium is revoked only after the leave succeeds, so a refused leave changes
    nothing. Raises discord.HTTPException if Discord refuses.
    """
    embed, plain = gu.leaving_embed(reason)
    channel = await gu.announce_to_guild(guild, embed, plain)
    await guild.leave()
    await bot.db.revoke_premium(guild.id)
    return channel is not None, getattr(channel, "name", "")


async def _json_body(request: web.Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


@require_owner
async def servers_list(request: web.Request) -> web.Response:
    """Every server the bot is in, plus the ban list — the eject/ban surface."""
    bot = request.app["bot"]
    banned = {r["guild_id"]: r for r in await bot.db.list_banned_guilds()}

    servers = []
    for guild in bot.guilds:
        servers.append({
            "id": str(guild.id),
            "name": guild.name,
            "icon": _guild_icon(guild),
            "memberCount": guild.member_count,
            "tier": await _tier(bot.db, guild.id),
            # A banned server the bot is still in: banned by ID while it was
            # offline, or the leave failed. Surfaced so it can be ejected.
            "banned": guild.id in banned,
        })
    servers.sort(key=lambda g: g["name"].lower())

    return web.json_response({
        "servers": servers,
        "banned": [
            {
                "id": str(gid),
                # Prefer the live name; fall back to whatever it was called when
                # it was banned. Blank for servers banned by ID, never joined.
                "name": (
                    bot.get_guild(gid).name if bot.get_guild(gid) else row["name"]
                ) or "(bot never joined)",
                "reason": row["reason"],
                "bannedAt": row["banned_at"],
                "present": bot.get_guild(gid) is not None,
            }
            for gid, row in banned.items()
        ],
    })


@require_owner
async def guild_eject(request: web.Request) -> web.Response:
    """Post a reason in the server, leave it, and optionally ban it."""
    bot = request.app["bot"]
    gid = int(request.match_info["guild_id"])
    uid = request["session"]["user_id"]

    data = await _json_body(request)
    if data is None:
        return web.json_response({"error": "Expected JSON."}, status=400)

    reason = str(data.get("reason", "")).strip()
    if not reason:
        return web.json_response({"error": "A reason is required."}, status=400)
    if len(reason) > MAX_EJECT_REASON:
        return web.json_response(
            {"error": f"Reason max {MAX_EJECT_REASON} characters."}, status=400
        )
    ban = bool(data.get("ban"))

    guild = bot.get_guild(gid)
    if guild is None:
        return web.json_response({"error": "The bot isn't in that server."}, status=404)

    name = guild.name
    try:
        announced, channel = await _eject_guild(bot, guild, reason)
    except discord.HTTPException:
        log.exception("Failed to leave guild %s.", gid)
        return web.json_response(
            {"error": "Discord refused to remove the bot from that server."}, status=502
        )

    if ban:
        await bot.db.ban_guild(gid, name=name, banned_by=uid, reason=reason)

    log.warning(
        "Ejected from guild %s (%s) by %s%s. Reason: %s",
        name, gid, uid, " and banned" if ban else "", reason,
    )
    return web.json_response(
        {"ok": True, "announced": announced, "channel": channel, "banned": ban}
    )


@require_owner
async def ban_add(request: web.Request) -> web.Response:
    """Block a server from using the bot. If the bot is currently in it, it is
    ejected too — a ban that leaves the bot sitting in the server does nothing."""
    bot = request.app["bot"]
    uid = request["session"]["user_id"]

    data = await _json_body(request)
    if data is None:
        return web.json_response({"error": "Expected JSON."}, status=400)

    raw_id = str(data.get("guildId", "")).strip()
    if not raw_id.isdigit():
        return web.json_response({"error": "guildId must be numeric."}, status=400)
    gid = int(raw_id)

    reason = str(data.get("reason", "")).strip()
    if not reason:
        return web.json_response({"error": "A reason is required."}, status=400)
    if len(reason) > MAX_EJECT_REASON:
        return web.json_response(
            {"error": f"Reason max {MAX_EJECT_REASON} characters."}, status=400
        )

    guild = bot.get_guild(gid)
    name = guild.name if guild else ""
    announced, channel = False, ""
    if guild is not None:
        try:
            announced, channel = await _eject_guild(bot, guild, reason)
        except discord.HTTPException:
            log.exception("Failed to leave guild %s while banning it.", gid)
            return web.json_response(
                {"error": "Discord refused to remove the bot from that server."},
                status=502,
            )

    await bot.db.ban_guild(gid, name=name, banned_by=uid, reason=reason)
    log.warning("Banned guild %s (%s) by %s. Reason: %s", name or "unknown", gid, uid, reason)
    return web.json_response(
        {"ok": True, "ejected": guild is not None, "announced": announced, "channel": channel}
    )


@require_owner
async def ban_remove(request: web.Request) -> web.Response:
    gid = int(request.match_info["guild_id"])
    removed = await request.app["bot"].db.unban_guild(gid)
    log.warning("Unbanned guild %s by %s.", gid, request["session"]["user_id"])
    return web.json_response({"ok": True, "removed": removed})


# ── tickets ───────────────────────────────────────────────────────────────────

def _ticket_json(row, bot) -> dict:
    guild = bot.get_guild(row["guild_id"]) if row["guild_id"] else None
    return {
        "id": row["id"],
        "subject": row["subject"],
        "category": row["category"],
        "status": row["status"],
        "userId": str(row["user_id"]),
        "username": row["username"],
        "guildId": str(row["guild_id"]) if row["guild_id"] else "",
        "guildName": guild.name if guild else "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


@require_login
async def tickets_list(request: web.Request) -> web.Response:
    """Owners see the whole queue; everyone else sees only their own tickets."""
    bot = request.app["bot"]
    uid = request["session"]["user_id"]
    owner = is_owner(uid)

    if owner and request.query.get("scope") == "all":
        rows = await bot.db.list_all_tickets(request.query.get("status", ""))
    else:
        rows = await bot.db.list_tickets_for_user(uid)

    return web.json_response({
        "tickets": [_ticket_json(r, bot) for r in rows],
        "isOwner": owner,
        "openCount": await bot.db.count_open_tickets() if owner else 0,
    })


@require_login
async def ticket_create(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    uid = request["session"]["user_id"]

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Expected JSON."}, status=400)

    subject = str(data.get("subject", "")).strip()
    body = str(data.get("body", "")).strip()
    if not subject or not body:
        return web.json_response(
            {"error": "Both a subject and a description are required."}, status=400
        )
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        return web.json_response(
            {"error": f"Subject max {MAX_SUBJECT} chars, description max {MAX_BODY}."},
            status=400,
        )

    # Anyone with a Discord account can reach this form, so rate-limit it.
    if await bot.db.count_recent_tickets(uid, 3600) >= config.TICKET_RATE_PER_HOUR:
        return web.json_response(
            {"error": "You've opened too many tickets this hour. Try again later."},
            status=429,
        )

    raw_guild = str(data.get("guildId", "")).strip()
    guild_id = int(raw_guild) if raw_guild.isdigit() else 0
    # Only accept a server the requester actually manages, so tickets can't be
    # filed against arbitrary servers.
    if guild_id and not any(g.id == guild_id for g in manageable_guilds(bot, uid)):
        guild_id = 0

    category = str(data.get("category", "general")).strip().lower()
    if category not in ("general", "premium", "bug", "feature"):
        category = "general"

    ticket_id = await bot.db.create_ticket(
        uid, request["session"]["username"], subject, body, guild_id, category
    )
    log.info("Ticket #%s opened by %s (%s).", ticket_id, uid, category)
    return web.json_response({"ok": True, "id": ticket_id}, status=201)


@require_login
async def ticket_detail(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    uid = request["session"]["user_id"]
    ticket_id = int(request.match_info["ticket_id"])

    row = await bot.db.get_ticket(ticket_id)
    if row is None:
        return web.json_response({"error": "No such ticket."}, status=404)
    if row["user_id"] != uid and not is_owner(uid):
        return web.json_response({"error": "Not your ticket."}, status=403)

    messages = await bot.db.get_ticket_messages(ticket_id)
    return web.json_response({
        **_ticket_json(row, bot),
        "messages": [
            {
                "id": m["id"],
                "authorId": str(m["author_id"]),
                "authorName": m["author_name"],
                "isStaff": bool(m["is_staff"]),
                "body": m["body"],
                "createdAt": m["created_at"],
            }
            for m in messages
        ],
    })


@require_login
async def ticket_reply(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    uid = request["session"]["user_id"]
    ticket_id = int(request.match_info["ticket_id"])

    row = await bot.db.get_ticket(ticket_id)
    if row is None:
        return web.json_response({"error": "No such ticket."}, status=404)
    owner = is_owner(uid)
    if row["user_id"] != uid and not owner:
        return web.json_response({"error": "Not your ticket."}, status=403)
    if row["status"] == "closed":
        return web.json_response({"error": "That ticket is closed."}, status=409)

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Expected JSON."}, status=400)

    body = str(data.get("body", "")).strip()
    if not body:
        return web.json_response({"error": "Reply can't be empty."}, status=400)
    if len(body) > MAX_BODY:
        return web.json_response({"error": f"Max {MAX_BODY} characters."}, status=400)

    await bot.db.add_ticket_message(
        ticket_id, uid, request["session"]["username"], body, is_staff=owner
    )
    return web.json_response({"ok": True})


@require_login
async def ticket_set_status(request: web.Request) -> web.Response:
    """Owners can set any status; the requester may only close their own ticket."""
    bot = request.app["bot"]
    uid = request["session"]["user_id"]
    ticket_id = int(request.match_info["ticket_id"])

    row = await bot.db.get_ticket(ticket_id)
    if row is None:
        return web.json_response({"error": "No such ticket."}, status=404)
    owner = is_owner(uid)
    if row["user_id"] != uid and not owner:
        return web.json_response({"error": "Not your ticket."}, status=403)

    try:
        status = str((await request.json()).get("status", "")).strip()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Expected JSON."}, status=400)

    if status not in ("open", "answered", "closed"):
        return web.json_response({"error": "Invalid status."}, status=400)
    if not owner and status != "closed":
        return web.json_response({"error": "You can only close your own ticket."}, status=403)

    await bot.db.set_ticket_status(ticket_id, status)
    return web.json_response({"ok": True, "status": status})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/guilds", my_guilds)
    app.router.add_get("/api/guilds/{guild_id:\\d+}", guild_detail)
    app.router.add_post("/api/guilds/{guild_id:\\d+}/eject", guild_eject)

    app.router.add_get("/api/servers", servers_list)
    app.router.add_post("/api/bans", ban_add)
    app.router.add_delete("/api/bans/{guild_id:\\d+}", ban_remove)

    app.router.add_get("/api/premium", premium_list)
    app.router.add_post("/api/premium", premium_grant)
    app.router.add_delete("/api/premium/{guild_id:\\d+}", premium_revoke)

    app.router.add_get("/api/tickets", tickets_list)
    app.router.add_post("/api/tickets", ticket_create)
    app.router.add_get("/api/tickets/{ticket_id:\\d+}", ticket_detail)
    app.router.add_post("/api/tickets/{ticket_id:\\d+}/reply", ticket_reply)
    app.router.add_post("/api/tickets/{ticket_id:\\d+}/status", ticket_set_status)
