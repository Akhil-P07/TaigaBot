"""Discord OAuth2 login for the dashboard.

Only the `identify` scope is requested. We deliberately do *not* ask for
`guilds`: which servers you can manage is derived from the bot's own membership
data (see `manageable_guilds`), so signing in never hands us a list of every
server you're in.

Session design: the cookie carries a 256-bit random token; the database stores
only its SHA-256. A leaked database therefore can't be replayed as a login.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import socket
import urllib.parse
from functools import wraps

import aiohttp
from aiohttp import web

import config

log = logging.getLogger("taigabot.web.auth")

SESSION_COOKIE = "taiga_session"
STATE_COOKIE = "taiga_oauth_state"
DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}/api/auth/callback"


def _secure_cookie_kwargs() -> dict:
    """Cookie flags. `Secure` is set whenever the site is served over HTTPS —
    which it is in every real deployment; local http:// dev is the exception."""
    return {
        "httponly": True,
        "samesite": "Lax",
        "secure": config.PUBLIC_BASE_URL.startswith("https://"),
        "path": "/",
    }


def _session_ttl() -> int:
    return config.SESSION_TTL_DAYS * 86400


async def current_user(request: web.Request):
    """The logged-in user's session row, or None. Cheap: one indexed lookup."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await request.app["bot"].db.get_session(_hash(token))


def is_owner(user_id: int) -> bool:
    """Master privileges. Explicit BOT_OWNER_IDS wins; otherwise nobody is an
    owner over the web, because the Discord application-owner lookup needs an
    API round trip we don't want on every request."""
    return user_id in config.BOT_OWNER_IDS


def require_login(handler):
    """Decorator: 401 unless a valid session cookie is present. Injects the
    session row as `request["session"]`."""

    @wraps(handler)
    async def wrapper(request: web.Request):
        session = await current_user(request)
        if session is None:
            return web.json_response({"error": "Not signed in."}, status=401)
        request["session"] = session
        return await handler(request)

    return wrapper


def require_owner(handler):
    """Decorator: 403 unless the caller holds master privileges."""

    @wraps(handler)
    @require_login
    async def wrapper(request: web.Request):
        if not is_owner(request["session"]["user_id"]):
            return web.json_response({"error": "Owner only."}, status=403)
        return await handler(request)

    return wrapper


# ── routes ────────────────────────────────────────────────────────────────────

async def login(request: web.Request) -> web.Response:
    """Kick off the OAuth dance, pinning a random `state` in a cookie so the
    callback can prove the request started here (CSRF defence)."""
    if not config.dashboard_ready():
        return web.json_response(
            {"error": "Dashboard login isn't configured on this instance."}, status=503
        )

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",  # skip the consent screen for people who've already approved
    }
    resp = web.HTTPFound(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")
    resp.set_cookie(STATE_COOKIE, state, max_age=600, **_secure_cookie_kwargs())
    return resp


async def callback(request: web.Request) -> web.Response:
    """Exchange the code for a token, read the user's identity, open a session.

    The Discord access token is used once, here, and never stored: everything
    the dashboard needs afterwards comes from the bot's own state.
    """
    if not config.dashboard_ready():
        return web.HTTPFound("/?error=oauth_unconfigured")

    expected = request.cookies.get(STATE_COOKIE)
    got = request.query.get("state")
    if not expected or not got or not secrets.compare_digest(expected, got):
        log.warning("OAuth callback with bad state (possible CSRF).")
        return web.HTTPFound("/?error=bad_state")

    if request.query.get("error"):
        return web.HTTPFound("/?error=denied")

    code = request.query.get("code")
    if not code:
        return web.HTTPFound("/?error=no_code")

    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=20)
        ) as sess:
            async with sess.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "client_id": config.DISCORD_CLIENT_ID,
                    "client_secret": config.DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as tok_resp:
                if tok_resp.status != 200:
                    log.warning("OAuth token exchange failed: HTTP %s", tok_resp.status)
                    return web.HTTPFound("/?error=token_exchange")
                token = (await tok_resp.json()).get("access_token")

            if not token:
                return web.HTTPFound("/?error=token_exchange")

            async with sess.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {token}"},
            ) as me_resp:
                if me_resp.status != 200:
                    return web.HTTPFound("/?error=identify")
                me = await me_resp.json()
    except Exception:  # noqa: BLE001
        log.exception("OAuth callback failed.")
        return web.HTTPFound("/?error=oauth_failed")

    raw = secrets.token_urlsafe(32)
    await request.app["bot"].db.create_session(
        _hash(raw),
        int(me["id"]),
        me.get("global_name") or me.get("username") or "",
        me.get("avatar") or "",
        _session_ttl(),
    )

    resp = web.HTTPFound("/dashboard")
    resp.set_cookie(SESSION_COOKIE, raw, max_age=_session_ttl(), **_secure_cookie_kwargs())
    resp.del_cookie(STATE_COOKIE, path="/")
    log.info("Dashboard login: user %s", me["id"])
    return resp


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await request.app["bot"].db.delete_session(_hash(token))
    resp = web.json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/auth/login", login)
    app.router.add_get("/api/auth/callback", callback)
    app.router.add_post("/api/auth/logout", logout)
