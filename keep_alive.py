"""Public web pages for TaigaBot — a landing page (with an invite button) and
auto-generated command docs — plus a health endpoint.

Runs in the SAME process as the bot, so it costs nothing extra: the bot's
outbound gateway connection and this inbound web server coexist fine. On Railway,
open Service → Settings → Networking → Generate Domain to expose these pages at a
public URL (and attach a custom domain later if you want).

Routes:
  /              landing page + "Invite to your server" button
  /commands      auto-generated list of every slash command (stays in sync)
  /health        plain "OK" for uptime pingers
  /assets/...    static files (the RIT AI Club logo lives here)

Drop the club logo at assets/aiclub-logo.png and it appears in the header + as the
favicon automatically (a 🐯 emoji is used as a fallback if it's missing).
"""
from __future__ import annotations

import html
import logging
import os
import pathlib

import discord
from aiohttp import web

import config

log = logging.getLogger("taigabot.web")

ASSETS_DIR = pathlib.Path(__file__).parent / "assets"
LOGO_FILE = "aiclub-logo.png"

# Permissions the bot needs to do its job, encoded into the invite URL so any RIT
# server that adds it gets the right access out of the box.
INVITE_PERMISSIONS = discord.Permissions(
    manage_roles=True,
    manage_channels=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
    manage_messages=True,
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    add_reactions=True,
)


def _logo_src() -> str | None:
    """Public path to the club logo, or None if it hasn't been added yet."""
    return f"/assets/{LOGO_FILE}" if (ASSETS_DIR / LOGO_FILE).exists() else None


def _invite_url(bot) -> str | None:
    """OAuth2 invite URL. Uses DISCORD_CLIENT_ID, falling back to the bot's own id
    once it's logged in. None if neither is available yet."""
    client_id = config.DISCORD_CLIENT_ID or (str(bot.user.id) if bot.user else "")
    if not client_id:
        return None
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
            desc = getattr(cmd, "description", "") or ""
            items.append((f"/{cmd.name}", desc))
    return items


_CSS = """
:root{--accent:#E8552D;--accent2:#ff7a4d;--bg:#0d0f14;--card:#161922;--line:#242a36;--text:#eceef2;--muted:#99a0ad;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--text);line-height:1.65;
  background:radial-gradient(1200px 600px at 50% -10%,rgba(232,85,45,.18),transparent 60%),var(--bg);min-height:100vh;}
.wrap{max-width:860px;margin:0 auto;padding:56px 20px 40px;}
header{text-align:center;margin-bottom:36px;}
.logo{width:104px;height:104px;object-fit:contain;margin-bottom:6px;filter:drop-shadow(0 6px 22px rgba(232,85,45,.25));}
.logo.emoji{font-size:84px;line-height:1;}
h1{font-size:2.7rem;margin:.15em 0;letter-spacing:-.5px;}
.tag{color:var(--muted);font-size:1.14rem;max-width:620px;margin:0 auto;}
.actions{margin-top:22px;}
.btn{display:inline-block;text-decoration:none;padding:13px 26px;border-radius:12px;font-weight:650;margin:8px 6px;transition:transform .12s ease,box-shadow .12s ease;}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 8px 24px rgba(232,85,45,.35);}
.btn.secondary{background:transparent;border:1px solid var(--line);color:var(--text);}
.btn:hover{transform:translateY(-2px);}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin:36px 0;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;transition:border-color .15s ease,transform .15s ease;}
.card:hover{border-color:var(--accent);transform:translateY(-3px);}
.card h3{margin:.1em 0 .35em;font-size:1.12rem;}
.card p{color:var(--muted);margin:0;font-size:.95rem;}
.cmd{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:11px;padding:13px 16px;margin:10px 0;}
.cmd code{color:var(--accent);font-weight:650;font-size:1rem;}
.cmd span{color:var(--muted);margin-top:3px;font-size:.95rem;}
.banner{text-align:center;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;color:var(--muted);}
.banner strong{color:var(--text);}
a{color:var(--accent);}
hr{border:none;border-top:1px solid var(--line);margin:28px 0;}
footer{text-align:center;color:var(--muted);margin-top:46px;font-size:.9rem;}
"""


def _page(title: str, body: str) -> str:
    logo = _logo_src()
    favicon = f'<link rel="icon" href="{logo}">' if logo else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>{favicon}<style>{_CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    )


def _logo_html() -> str:
    logo = _logo_src()
    return (
        f'<img class="logo" src="{logo}" alt="RIT AI Club">'
        if logo
        else '<div class="logo emoji">🐯</div>'
    )


def _landing_html(bot) -> str:
    name = bot.user.name if bot.user else "TaigaBot"
    invite = _invite_url(bot)
    invite_btn = (
        f'<a class="btn primary" href="{invite}">➕ Invite to your server</a>'
        if invite
        else '<p class="tag">Invite link unavailable — set <code>DISCORD_CLIENT_ID</code>.</p>'
    )
    github = config.GITHUB_URL
    gh_btn = (
        f'<a class="btn secondary" href="{html.escape(github)}">⭐ Build on GitHub</a>'
        if github else ""
    )
    contribute = (
        f'<div class="banner">🛠️ <strong>Open-source</strong> — built for RIT clubs, '
        f'by RIT students. Contributions welcome on '
        f'<a href="{html.escape(github)}">GitHub</a>.</div>'
        if github else ""
    )
    body = f"""
    <header>
      {_logo_html()}
      <h1>{html.escape(name)}</h1>
      <p class="tag">The Discord bot for RIT clubs — RIT-email verification,
      moderation, leveling, and a full projects system. Built to work in
      <strong>any</strong> RIT server.</p>
      <div class="actions">{invite_btn}
        <a class="btn secondary" href="/commands">📖 View commands</a>{gh_btn}</div>
    </header>
    <div class="grid">
      <div class="card"><h3>✅ Verification</h3><p>RIT-email OTP keeps your server
        students-only. Verify once, recognized across every server running the bot.</p></div>
      <div class="card"><h3>🛡️ Moderation</h3><p>Automod with spam auto-warns, plus
        kick / ban / timeout / warn tools for your Eboard.</p></div>
      <div class="card"><h3>📊 Leveling</h3><p>Members earn XP for chatting, with
        <code>/rank</code> and a global leaderboard.</p></div>
      <div class="card"><h3>🗂️ Projects</h3><p>Spin up gated project channels with
        roles and a request-to-join approval flow.</p></div>
    </div>
    {contribute}
    <footer>Made for the RIT AI Club • <a href="/commands">Commands</a>{
        f' • <a href="{html.escape(github)}">GitHub</a>' if github else ''
    }</footer>
    """
    return _page(f"{name} — RIT club Discord bot", body)


def _commands_html(bot) -> str:
    items = _iter_commands(bot.tree)
    if items:
        rows = "\n".join(
            f'<div class="cmd"><code>{html.escape(n)}</code>'
            f"<span>{html.escape(d)}</span></div>"
            for n, d in items
        )
    else:
        rows = '<p class="tag">Commands are still loading — refresh in a moment.</p>'
    body = f"""
    <header>
      {_logo_html()}
      <h1>Commands</h1>
      <p class="tag">Every slash command TaigaBot provides. Some are Eboard-only
      (the command will tell you if so).</p>
      <div class="actions"><a class="btn secondary" href="/">← Back to home</a></div>
    </header>
    {rows}
    <footer><a href="/">Home</a></footer>
    """
    return _page("TaigaBot — Commands", body)


async def _landing(request: web.Request) -> web.Response:
    return web.Response(text=_landing_html(request.app["bot"]), content_type="text/html")


async def _commands(request: web.Request) -> web.Response:
    return web.Response(text=_commands_html(request.app["bot"]), content_type="text/html")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_keep_alive(bot) -> None:
    """Start the web server (landing page + command docs + health) in the
    background. Binds 0.0.0.0:$PORT so Railway/Render can route to it."""
    port = int(os.getenv("PORT", "8080"))
    ASSETS_DIR.mkdir(exist_ok=True)  # so add_static works even before a logo is added
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _landing)
    app.router.add_get("/commands", _commands)
    app.router.add_get("/health", _health)
    app.router.add_static("/assets", ASSETS_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Web server listening on 0.0.0.0:%d (/, /commands, /health)", port)
