"""Public web pages for TaigaBot: a landing page (with an invite button),
auto-generated command docs, and a health endpoint.

Runs in the SAME process as the bot, so it costs nothing extra: the bot's
outbound gateway connection and this inbound web server coexist fine. On Railway,
open Service → Settings → Networking → Generate Domain to expose these pages at a
public URL (and attach a custom domain later if you want).

Routes:
  /              landing page + "Invite to your server" button
  /commands      auto-generated list of every slash command (stays in sync)
  /health        plain "OK" for uptime pingers
  /assets/...    static files (the RIT AI Club logo lives here)

Drop the club logo (any image: png/jpg/jpeg/webp/svg/gif, any filename) into the
assets/ folder and it appears in the header + as the favicon automatically. A 🐯
emoji is used as a fallback if no image is present.
"""
from __future__ import annotations

import html
import logging
import os
import pathlib
import urllib.parse

import discord
from aiohttp import web

import config

log = logging.getLogger("taigabot.web")

ASSETS_DIR = pathlib.Path(__file__).parent / "assets"
_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")

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


def _find_asset(*keywords: str) -> str | None:
    """Public URL for the first image in assets/ whose filename contains any of
    the keywords (case-insensitive). Filename is URL-encoded so spaces are fine."""
    if not ASSETS_DIR.is_dir():
        return None
    for p in sorted(ASSETS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if p.is_file() and p.suffix.lower() in _LOGO_EXTS:
            if any(k in p.name.lower() for k in keywords):
                return "/assets/" + urllib.parse.quote(p.name)
    return None


def _taiga_src() -> str | None:
    """Hero image (Taiga Aisaka) shown above the title, e.g. assets/TaigaBot.png."""
    return _find_asset("taigabot", "taiga", "aisaka")


def _club_logo_src() -> str | None:
    """RIT AI Club logo: any image in assets/ with 'logo' in the name."""
    return _find_asset("logo")


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
/* ---- Theme colors (tweak these to rebrand) ---- */
:root {
  --accent:  #E8552D;
  --accent2: #ff7a4d;
  --bg:      #0d0f14;
  --card:    #161922;
  --line:    #242a36;
  --text:    #eceef2;
  --muted:   #99a0ad;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  min-height: 100vh;
  color: var(--text);
  line-height: 1.65;
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background:
    radial-gradient(1200px 600px at 50% -10%, rgba(232, 85, 45, .18), transparent 60%),
    var(--bg);
}

.wrap {
  max-width: 860px;
  margin: 0 auto;
  padding: 56px 20px 40px;
}

/* ---- Header + hero avatar ---- */
header {
  margin-bottom: 36px;
  text-align: center;
}

.hero-img {
  display: block;
  width: 150px;
  height: 150px;
  margin: 0 auto 16px;
  object-fit: cover;
  border: 4px solid var(--accent);
  border-radius: 50%;
  box-shadow: 0 10px 30px rgba(232, 85, 45, .32);
}

.logo.emoji {
  display: block;
  margin-bottom: 6px;
  font-size: 84px;
  line-height: 1;
}

h1 {
  margin: .15em 0;
  font-size: 2.7rem;
  letter-spacing: -.5px;
}

.tag {
  max-width: 620px;
  margin: 0 auto;
  color: var(--muted);
  font-size: 1.14rem;
}

/* ---- Action buttons ---- */
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  margin-top: 22px;
}

.btn {
  display: inline-block;
  padding: 13px 26px;
  border-radius: 12px;
  font-weight: 650;
  text-decoration: none;
  transition: transform .12s ease, box-shadow .12s ease;
}

.btn.primary {
  color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 8px 24px rgba(232, 85, 45, .35);
}

.btn.secondary {
  color: var(--text);
  background: transparent;
  border: 1px solid var(--line);
}

.btn:hover { transform: translateY(-2px); }

/* ---- Feature cards ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 16px;
  margin: 36px 0;
}

.card {
  padding: 20px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  transition: border-color .15s ease, transform .15s ease;
}

.card:hover {
  border-color: var(--accent);
  transform: translateY(-3px);
}

.card h3 {
  margin: .1em 0 .35em;
  font-size: 1.12rem;
}

.card p {
  margin: 0;
  color: var(--muted);
  font-size: .95rem;
}

/* ---- Command list (/commands page) ---- */
.cmd {
  display: flex;
  flex-direction: column;
  padding: 13px 16px;
  margin: 10px 0;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 11px;
}

.cmd code {
  color: var(--accent);
  font-weight: 650;
  font-size: 1rem;
}

.cmd span {
  margin-top: 3px;
  color: var(--muted);
  font-size: .95rem;
}

/* ---- Open-source banner ---- */
.banner {
  padding: 18px 20px;
  text-align: center;
  color: var(--muted);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
}

.banner strong { color: var(--text); }

/* ---- Footer ---- */
.footer-logo {
  height: 22px;
  padding: 2px 3px;
  margin-right: 7px;
  vertical-align: middle;
  background: #fff;
  border-radius: 5px;
}

footer {
  margin-top: 46px;
  text-align: center;
  color: var(--muted);
  font-size: .9rem;
}

a { color: var(--accent); }

/* ---- Setup guide (prose) ---- */
.prose {
  text-align: left;
}

.prose h2 {
  margin: 34px 0 10px;
  font-size: 1.35rem;
}

.prose p,
.prose li {
  color: var(--muted);
}

.prose ul,
.prose ol {
  padding-left: 22px;
}

.prose li {
  margin: 7px 0;
}

.prose strong {
  color: var(--text);
}

.prose code {
  padding: 1px 6px;
  color: var(--accent);
  background: #11141b;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: .92rem;
}

.warn {
  margin: 14px 0;
  padding: 14px 16px;
  color: var(--text);
  background: rgba(232, 85, 45, .08);
  border: 1px solid var(--accent);
  border-radius: 12px;
}

.warn strong {
  color: var(--accent);
}

/* ---- Responsive ---- */
@media (max-width: 480px) {
  .hero-img { width: 124px; height: 124px; }
}
"""


def _page(title: str, body: str) -> str:
    icon = _club_logo_src() or _taiga_src()
    favicon = f'<link rel="icon" href="{icon}">' if icon else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>{favicon}<style>{_CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    )


def _taiga_html() -> str:
    """The Taiga Aisaka hero image shown above the title (🐯 emoji fallback).
    Cropped into a Discord-style circle via object-fit:cover (no stretching)."""
    src = _taiga_src()
    return (
        f'<img class="hero-img" src="{src}" alt="Taiga Aisaka">'
        if src
        else '<div class="logo emoji">🐯</div>'
    )


def _club_footer() -> str:
    """'Made for the RIT AI Club' with the club logo as a small inline mark."""
    logo = _club_logo_src()
    mark = f'<img class="footer-logo" src="{logo}" alt="RIT AI Club">' if logo else ""
    return f"{mark}Made for the RIT AI Club"


def _landing_html(bot) -> str:
    name = bot.user.name if bot.user else "TaigaBot"
    invite = _invite_url(bot)
    invite_btn = (
        f'<a class="btn primary" href="{invite}">➕ Invite to your server</a>'
        if invite
        else '<p class="tag">Invite link unavailable. Set <code>DISCORD_CLIENT_ID</code>.</p>'
    )
    github = config.GITHUB_URL
    gh_btn = (
        f'<a class="btn secondary" href="{html.escape(github)}">⭐ Build on GitHub</a>'
        if github else ""
    )
    contribute = (
        f'<div class="banner">🛠️ <strong>Open-source</strong>, built for RIT clubs '
        f'by RIT students. Contributions welcome on '
        f'<a href="{html.escape(github)}">GitHub</a>.</div>'
        if github else ""
    )
    body = f"""
    <header>
      {_taiga_html()}
      <h1>{html.escape(name)}</h1>
      <p class="tag">The all-in-one Discord bot for RIT clubs: RIT-email
      verification, moderation, leveling, and a full projects system. Built to
      work in <strong>any</strong> RIT server.</p>
      <div class="actions">{invite_btn}
        <a class="btn secondary" href="/commands">📖 View commands</a>
        <a class="btn secondary" href="/setup">⚙️ Setup guide</a>{gh_btn}</div>
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
      <div class="card"><h3>🤖 AI Assistant</h3><p>Ask questions right in chat with
        <code>/ask</code>, powered by Google Gemini.</p></div>
      <div class="card"><h3>🔬 STEM Tools</h3><p>Search papers, learning resources, and
        AI terms (<code>/paper</code>, <code>/resource</code>, <code>/aiterm</code>),
        with more on the way.</p></div>
    </div>
    {contribute}
    <footer>{_club_footer()} • <a href="/commands">Commands</a> • <a href="/setup">Setup</a>{
        f' • <a href="{html.escape(github)}">GitHub</a>' if github else ''
    }</footer>
    """
    return _page(f"{name} | RIT club Discord bot", body)


def _commands_html(bot) -> str:
    items = _iter_commands(bot.tree)
    if items:
        rows = "\n".join(
            f'<div class="cmd"><code>{html.escape(n)}</code>'
            f"<span>{html.escape(d)}</span></div>"
            for n, d in items
        )
    else:
        rows = '<p class="tag">Commands are still loading. Refresh in a moment.</p>'
    body = f"""
    <header>
      {_taiga_html()}
      <h1>Commands</h1>
      <p class="tag">Every slash command TaigaBot provides. Some are Eboard-only
      (the command will tell you if so).</p>
      <div class="actions"><a class="btn secondary" href="/">← Back to home</a></div>
    </header>
    {rows}
    <footer>{_club_footer()} • <a href="/">Home</a> • <a href="/setup">Setup</a></footer>
    """
    return _page("TaigaBot | Commands", body)


def _setup_html() -> str:
    body = f"""
    <header>
      {_taiga_html()}
      <h1>Setup Guide</h1>
      <p class="tag">Everything an admin needs to get TaigaBot running in a server.</p>
      <div class="actions">
        <a class="btn secondary" href="/">← Back to home</a>
        <a class="btn secondary" href="/commands">📖 Commands</a>
      </div>
    </header>
    <div class="prose">

      <h2>1. Invite the bot</h2>
      <p>Use the <a href="/">invite button on the home page</a>. It already requests
      every permission the bot needs: <strong>Manage Roles, Manage Channels, View
      Channels, Use Application Commands, Kick Members, Ban Members, Moderate
      Members, Manage Messages, Send Messages, Embed Links, Add Reactions, Read
      Message History</strong>.</p>
      <p>In the Discord Developer Portal → your app → <strong>Bot</strong>, enable both
      privileged intents: <strong>Server Members Intent</strong> and <strong>Message
      Content Intent</strong>.</p>
      <div class="warn"><strong>Easy to miss:</strong> <strong>View Channels</strong>
      and <strong>Use Application Commands</strong> are required. Discord only lets the
      bot change a channel's visibility if it holds those permissions itself. Without
      them, <code>/setup</code> fails to gate channels with "Missing Access".</div>

      <h2>2. Put TaigaBot's role at the top</h2>
      <p>In <strong>Server Settings → Roles</strong>, drag the <strong>TaigaBot</strong>
      role <strong>above every role it manages</strong>: Verified, Unverified, project
      roles, and (for the role reset below) <strong>every member's roles</strong>.</p>
      <div class="warn"><strong>Required for the role reset:</strong> Discord never lets
      a bot add or remove a role that sits <strong>at or above its own highest
      role</strong>. So for <code>/setup</code>'s role reset to strip members' old
      roles, TaigaBot's role must be <strong>above everyone's</strong> in the list. Any
      role above TaigaBot is skipped and left untouched. This same rule is why kick,
      ban, timeout, and project roles fail if the bot sits too low.</div>

      <h2>3. Set environment variables</h2>
      <p>Copy <code>.env.example</code> to <code>.env</code> (or set these in your
      host's Variables tab):</p>
      <ul>
        <li><code>DISCORD_TOKEN</code>:bot token (Developer Portal → Bot → Reset Token).</li>
        <li><code>BREVO_API_KEY</code>:Brevo API key for sending OTP emails.</li>
        <li><code>EMAIL_FROM</code>:your Brevo-verified sender address.</li>
        <li><code>DISCORD_CLIENT_ID</code>:your Application ID (powers the invite button here).</li>
        <li><code>GITHUB_URL</code>:repo link for the "Build on GitHub" link (optional).</li>
        <li><code>GEMINI_API_KEY</code>:enables <code>/ask</code> (optional).</li>
      </ul>
      <p><strong>Email (Brevo):</strong> hosts like Railway and Render block outbound
      SMTP, so OTP emails go over Brevo's HTTP API. Make a free Brevo account, verify a
      sender under <strong>Senders &amp; IPs</strong> (no domain needed), and create a
      key under <strong>SMTP &amp; API → API Keys</strong>.</p>

      <h2>4. Run /setup once</h2>
      <p>In Discord, the <strong>server owner or an administrator</strong> (not regular
      Eboard members) runs <code>/setup</code>. It is interactive: you can exclude
      categories/channels from gating and toggle the role reset, then it:</p>
      <ul>
        <li>creates the roles (<code>Unverified</code>, <code>Verified</code>,
          <code>Eboard</code>) and channels (<code>#unverified</code>,
          <code>#welcome</code>, <code>#mod-log</code>, <code>#taiga-backups</code>,
          <code>#roles</code>);</li>
        <li>gates every channel behind <code>Verified</code> (denies
          <code>@everyone</code> view, allows Verified and Eboard);</li>
        <li>assigns <code>Unverified</code> to everyone not yet verified.</li>
      </ul>
      <p><code>/setup</code> is idempotent, so re-run it any time. It reports which
      channels it gated or skipped.</p>

      <h2>Common issues</h2>
      <ul>
        <li><strong>"I can't manage that role" / role reset did nothing / kick, ban,
          and project commands fail:</strong> TaigaBot's role is too low. Move it
          <strong>above</strong> the roles involved (and above everyone for the role
          reset). See step 2.</li>
        <li><strong>Commands fail with a permissions error even though permissions look
          correct:</strong> the server has <strong>"Require 2FA for moderation"</strong>
          on (Server Settings → Safety Setup) and the bot owner's Discord account lacks
          2FA. Enable 2FA on the owner's account, or turn that requirement off. Discord
          blocks Manage Roles/Channels, kick, ban, etc. otherwise.</li>
        <li><strong>"Missing Access" / "Couldn't edit" some channels during setup:</strong>
          those channels already deny <code>@everyone</code> view, so the bot can't see
          them to gate them. Grant TaigaBot <strong>View Channel</strong> on them, or run
          <code>/setup</code> once with the bot temporarily set to Administrator, then
          re-run.</li>
        <li><strong>Members who already had roles still see everything after setup:</strong>
          gating can't override a role that allows view. Use the <strong>role reset</strong>
          toggle in <code>/setup</code> (needs TaigaBot above everyone) so old roles are
          stripped until members re-verify.</li>
        <li><strong>OTP emails aren't sending:</strong> confirm <code>BREVO_API_KEY</code>
          is a v3 key (starts with <code>xkeysib-</code>) and <code>EMAIL_FROM</code>
          exactly matches a Brevo-verified sender. SMTP-only setups won't work on most
          hosts; that's why Brevo's HTTP API is used.</li>
        <li><strong>Data resets after every redeploy (Railway/Render):</strong> the
          container filesystem is ephemeral. Attach a persistent volume (e.g. mount
          <code>/data</code>) and set <code>DB_PATH=/data/taigabot.db</code> so
          verifications, XP, and projects survive deploys.</li>
        <li><strong>Slash commands don't appear:</strong> global sync can take up to ~1
          hour the first time. Set <code>GUILD_ID</code> to your server for instant
          updates while testing.</li>
      </ul>

    </div>
    <footer>{_club_footer()} • <a href="/">Home</a> • <a href="/commands">Commands</a></footer>
    """
    return _page("TaigaBot | Setup", body)


async def _landing(request: web.Request) -> web.Response:
    return web.Response(text=_landing_html(request.app["bot"]), content_type="text/html")


async def _commands(request: web.Request) -> web.Response:
    return web.Response(text=_commands_html(request.app["bot"]), content_type="text/html")


async def _setup(_request: web.Request) -> web.Response:
    return web.Response(text=_setup_html(), content_type="text/html")


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
    app.router.add_get("/setup", _setup)
    app.router.add_get("/health", _health)
    app.router.add_static("/assets", ASSETS_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Web server listening on 0.0.0.0:%d (/, /commands, /health)", port)
