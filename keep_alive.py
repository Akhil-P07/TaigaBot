"""Tiny HTTP server so uptime pingers (e.g. UptimeRobot) can keep the host awake.

Discord bots don't listen on a port, but free hosts like Replit sleep a process
unless something hits an HTTP endpoint periodically. This serves a trivial "OK"
page on the port the host expects (Replit/Render set $PORT), letting an external
pinger keep the Repl alive. It's a no-op when nothing pings it.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

log = logging.getLogger("taigabot.keepalive")


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="TaigaBot is alive 🐯")


async def start_keep_alive() -> None:
    """Start the keep-alive web server in the background (non-blocking)."""
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", _ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Keep-alive server listening on 0.0.0.0:%d", port)
