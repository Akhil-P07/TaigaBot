"""The bot's HTTP server: the React dashboard, the JSON API, and /health.

Replaces the old keep_alive.py. Two things from that file are load-bearing and
kept here deliberately:

* it binds 0.0.0.0:$PORT — Railway routes to that port;
* it serves **/health** — railway.json's healthcheckPath points at it, and the
  deploy is marked failed if it stops answering.

Everything else (the hand-written HTML pages) now lives in the React app under
frontend/, built to frontend/dist and served from here.

Security posture, since this is the bot's only internet-facing surface:
  * tiered per-IP rate limits, strictest on the auth endpoints;
  * a hard cap on request body size;
  * Origin checks on every state-changing request (CSRF), on top of SameSite;
  * a strict Content-Security-Policy and the usual hardening headers;
  * no-store on API responses so nothing personal lands in a shared cache;
  * generic 500s — exception detail goes to the log, never to the client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
import urllib.parse
from collections import defaultdict, deque

import discord
from aiohttp import web

import config
from web import api as api_routes
from web import auth as auth_routes

log = logging.getLogger("taigabot.web")

ROOT = pathlib.Path(__file__).parent.parent
ASSETS_DIR = ROOT / "assets"
DIST_DIR = ROOT / "frontend" / "dist"

# Request bodies are small JSON documents; anything larger is either a mistake
# or an attempt to tie up memory.
MAX_REQUEST_BYTES = 64 * 1024

# Per-IP rate limits: (max requests, window seconds), chosen per route class.
# Auth is deliberately tight — it's the only unauthenticated endpoint that costs
# us outbound Discord API calls.
LIMIT_AUTH = (10, 60.0)
LIMIT_API = (90, 60.0)
LIMIT_DEFAULT = (240, 60.0)

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_last_prune = 0.0

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _client_ip(request: web.Request) -> str:
    """Real client IP for rate-limiting. Behind a single trusted proxy
    (Railway/Caddy) the peer is the proxy, which APPENDS the real client IP as
    the LAST X-Forwarded-For hop. The leftmost entries are client-supplied and
    spoofable, so use the last hop — otherwise an attacker could forge XFF to
    dodge the per-IP limit."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote or "unknown"


def _bucket_for(path: str) -> tuple[str, tuple[int, float]]:
    if path.startswith("/api/auth"):
        return "auth", LIMIT_AUTH
    if path.startswith("/api/"):
        return "api", LIMIT_API
    return "web", LIMIT_DEFAULT


def _prune(now: float) -> None:
    """Drop idle buckets so the table can't grow unbounded."""
    global _last_prune
    if now - _last_prune < 60.0:
        return
    _last_prune = now
    for key in list(_hits):
        dq = _hits[key]
        while dq and now - dq[0] > 300.0:
            dq.popleft()
        if not dq:
            del _hits[key]


@web.middleware
async def rate_limit_mw(request: web.Request, handler):
    now = time.time()
    _prune(now)
    bucket, (limit, window) = _bucket_for(request.path)
    dq = _hits[(_client_ip(request), bucket)]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        retry = int(window - (now - dq[0])) + 1
        return web.json_response(
            {"error": "Too many requests. Slow down."},
            status=429,
            headers={"Retry-After": str(retry)},
        )
    dq.append(now)
    return await handler(request)


def _allowed_origins() -> set[str]:
    origins = set()
    if config.PUBLIC_BASE_URL:
        origins.add(config.PUBLIC_BASE_URL.rstrip("/"))
    return origins


@web.middleware
async def csrf_mw(request: web.Request, handler):
    """Reject cross-site state-changing requests.

    The session cookie is SameSite=Lax, which already blocks it from riding on
    cross-site POSTs. This is defence in depth for the case where that is
    bypassed or a browser misbehaves: a mutating request must carry an Origin
    (or Referer) matching our own site.
    """
    if request.method in SAFE_METHODS or not request.path.startswith("/api/"):
        return await handler(request)

    allowed = _allowed_origins()
    if not allowed:
        # No PUBLIC_BASE_URL configured (local dev) — nothing to compare against.
        return await handler(request)

    origin = request.headers.get("Origin")
    if origin is None:
        referer = request.headers.get("Referer", "")
        if referer:
            parts = urllib.parse.urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"

    if origin not in allowed:
        log.warning(
            "Blocked cross-origin %s %s (origin=%r).", request.method, request.path, origin
        )
        return web.json_response({"error": "Cross-origin request blocked."}, status=403)
    return await handler(request)


@web.middleware
async def security_headers_mw(request: web.Request, handler):
    """Hardening headers, plus generic error bodies.

    The CSP is deliberately tight: the dashboard loads no third-party scripts,
    fonts or frames. Avatars come from Discord's CDN, which is the only external
    origin allowed, and only for images.
    """
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        # Never surface tracebacks or exception text to the internet.
        log.exception("Unhandled error serving %s %s", request.method, request.path)
        response = web.json_response({"error": "Internal server error."}, status=500)

    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://cdn.discordapp.com; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self' https://discord.com; "
        "frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
    )
    if config.PUBLIC_BASE_URL.startswith("https://"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    # Anything under /api can contain personal data — keep it out of caches.
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, private"

    return response


# ── small public endpoints ────────────────────────────────────────────────────

INVITE_PERMISSIONS = discord.Permissions(
    manage_roles=True,
    manage_channels=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
    manage_messages=True,
    view_audit_log=True,
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    add_reactions=True,
    bypass_slowmode=True,
)


def _invite_url(bot) -> str:
    """OAuth2 invite URL. Uses DISCORD_CLIENT_ID, falling back to the bot's own
    id once it's logged in. Empty if neither is available yet."""
    client_id = config.DISCORD_CLIENT_ID or (str(bot.user.id) if bot.user else "")
    if not client_id:
        return ""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        f"&permissions={INVITE_PERMISSIONS.value}"
        "&scope=bot+applications.commands"
    )


def _iter_commands(tree: discord.app_commands.CommandTree):
    """Flatten the command tree into (display_name, description) pairs, expanding
    groups into their subcommands. Sorted for stable output."""
    items: list[tuple[str, str]] = []
    for cmd in sorted(tree.get_commands(), key=lambda c: c.name):
        if isinstance(cmd, discord.app_commands.Group):
            for sub in sorted(cmd.commands, key=lambda c: c.name):
                items.append((f"/{cmd.name} {sub.name}", sub.description or ""))
        else:
            items.append((f"/{cmd.name}", getattr(cmd, "description", "") or ""))
    return items


async def _meta(request: web.Request) -> web.Response:
    """Public, non-sensitive site metadata for the frontend to render."""
    bot = request.app["bot"]
    return web.json_response({
        "name": bot.user.name if bot.user else "TaigaBot",
        "inviteUrl": _invite_url(bot),
        "githubUrl": config.GITHUB_URL,
        "clubUrl": config.CLUB_URL,
        "serverCount": len(bot.guilds),
        "loginEnabled": config.dashboard_ready(),
    })


async def _commands(request: web.Request) -> web.Response:
    items = _iter_commands(request.app["bot"].tree)
    return web.json_response(
        {"commands": [{"name": n, "description": d} for n, d in items]}
    )


async def _health(_request: web.Request) -> web.Response:
    """Railway's healthcheck target — see railway.json. Must stay cheap and must
    not require the bot to be logged in."""
    return web.Response(text="OK")


# ── SPA serving ───────────────────────────────────────────────────────────────

def _index_html() -> str | None:
    index = DIST_DIR / "index.html"
    return index.read_text(encoding="utf-8") if index.is_file() else None


_MISSING_BUILD_PAGE = """<!doctype html><meta charset="utf-8">
<title>TaigaBot</title>
<style>body{font-family:system-ui,sans-serif;background:#0d0f14;color:#eceef2;
max-width:640px;margin:80px auto;padding:0 20px;line-height:1.6}
code{background:#161922;padding:2px 6px;border-radius:6px}</style>
<h1>TaigaBot is running</h1>
<p>The bot is up, but the web frontend hasn't been built yet.</p>
<p>Build it with:</p>
<p><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>The API and <code>/health</code> work regardless.</p>
"""


async def _spa(request: web.Request) -> web.Response:
    """Serve the React app, letting client-side routing own every non-API path.

    A missing build is reported plainly rather than 404ing, because it's an
    operator mistake (forgot `npm run build`), not a visitor error.
    """
    html = _index_html()
    if html is None:
        return web.Response(text=_MISSING_BUILD_PAGE, content_type="text/html", status=503)
    return web.Response(text=html, content_type="text/html")


async def _prune_sessions_task(bot) -> None:
    """Drop expired login sessions hourly so the table stays small and stale
    cookies can't linger past their TTL."""
    while True:
        try:
            await asyncio.sleep(3600)
            removed = await bot.db.prune_sessions()
            if removed:
                log.info("Pruned %d expired web session(s).", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Session prune failed.")


def build_app(bot) -> web.Application:
    """Assemble the aiohttp application. Split out from `start_web` so tests can
    drive the real routes and middleware without binding a port."""
    ASSETS_DIR.mkdir(exist_ok=True)

    app = web.Application(
        middlewares=[security_headers_mw, rate_limit_mw, csrf_mw],
        client_max_size=MAX_REQUEST_BYTES,
    )
    app["bot"] = bot

    # API first — these must win over the SPA catch-all.
    app.router.add_get("/health", _health)
    app.router.add_get("/api/meta", _meta)
    app.router.add_get("/api/commands", _commands)
    auth_routes.add_routes(app)
    api_routes.add_routes(app)

    # The bot's own images (logo, favicon).
    app.router.add_static("/assets", ASSETS_DIR)
    # Vite's hashed JS/CSS bundles, emitted to dist/static (see vite.config.js,
    # which moves them off /assets so the two don't collide).
    if (DIST_DIR / "static").is_dir():
        app.router.add_static("/static", DIST_DIR / "static")

    # Everything else falls through to the SPA.
    app.router.add_get("/{tail:.*}", _spa)
    return app


async def start_web(bot) -> None:
    """Start the web server in the background. Binds 0.0.0.0:$PORT so Railway
    can route to it."""
    port = int(os.getenv("PORT", "8080"))
    app = build_app(bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    # NB: asyncio.create_task, not bot.loop.create_task — start_web runs BEFORE
    # bot.start(), and discord.py raises on `bot.loop` until the client is
    # logged in. We're already inside the running loop here, so this is both
    # correct and independent of the bot's connection state.
    # Held in app state so the task isn't garbage-collected mid-flight.
    app["prune_task"] = asyncio.create_task(_prune_sessions_task(bot))

    built = "built" if _index_html() else "NOT BUILT — run `cd frontend && npm run build`"
    log.info("Web server listening on 0.0.0.0:%d (frontend: %s)", port, built)
