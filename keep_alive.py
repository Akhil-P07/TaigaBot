"""Public web pages for TaigaBot: a landing page (with an invite button),
auto-generated command docs, and a health endpoint.

Runs in the SAME process as the bot, so it costs nothing extra: the bot's
outbound gateway connection and this inbound web server coexist fine. On Railway,
open Service → Settings → Networking → Generate Domain to expose these pages at a
public URL (and attach a custom domain later if you want).

Routes:
  /              landing page + "Invite to your server" button
  /commands      auto-generated list of every slash command (stays in sync)
  /setup         step-by-step server setup guide
  /terms         Terms of Service (link this in the Discord dev portal)
  /privacy       Privacy Policy (link this in the Discord dev portal)
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
import time
import urllib.parse
from collections import defaultdict, deque

import discord
from aiohttp import web

import config

log = logging.getLogger("taigabot.web")

ASSETS_DIR = pathlib.Path(__file__).parent / "assets"
_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")

# Per-IP rate limit for the public web pages (free, in-process). Stops a single
# client from hammering the bot's event loop. Not a substitute for an edge WAF
# against large volumetric DDoS, but plenty for a club bot.
RATE_LIMIT_MAX = 30        # requests allowed...
RATE_LIMIT_WINDOW = 60.0   # ...per this many seconds, per IP
_hits: dict[str, deque] = defaultdict(deque)
_last_prune = 0.0


def _client_ip(request: web.Request) -> str:
    """Real client IP for rate-limiting. Behind a single trusted proxy
    (Railway/Caddy) the peer is the proxy, which APPENDS the real client IP as the
    LAST X-Forwarded-For hop. The leftmost entries are client-supplied and
    spoofable, so use the last hop — otherwise an attacker could forge XFF to
    dodge the per-IP limit."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote or "unknown"


def _prune(now: float) -> None:
    """Drop IPs with no recent hits so the table can't grow unbounded."""
    global _last_prune
    if now - _last_prune < RATE_LIMIT_WINDOW:
        return
    _last_prune = now
    for ip in list(_hits):
        dq = _hits[ip]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if not dq:
            del _hits[ip]


@web.middleware
async def _rate_limit(request: web.Request, handler):
    now = time.time()
    _prune(now)
    dq = _hits[_client_ip(request)]
    while dq and now - dq[0] > RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_MAX:
        return web.Response(status=429, text="Too many requests. Slow down.")
    dq.append(now)
    return await handler(request)

# Permissions the bot needs to do its job, encoded into the invite URL so any RIT
# server that adds it gets the right access out of the box.
INVITE_PERMISSIONS = discord.Permissions(
    manage_roles=True,
    manage_channels=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
    manage_messages=True,
    view_audit_log=True,  # read who deleted a message for the mod-log delete audit
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
    icon = _taiga_src() or _club_logo_src()
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
    """'Made by RIT AI' with the club logo as a small inline mark that links to the
    club website (config.CLUB_URL)."""
    logo = _club_logo_src()
    if not logo:
        return "Made by RIT AI"
    img = f'<img class="footer-logo" src="{logo}" alt="RIT AI">'
    url = config.CLUB_URL
    mark = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{img}</a>' if url else img
    return f"{mark}Made by RIT AI"


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
        students-only. Verify once, recognized across every server running the bot,
        and recover your status on a new account with <code>/recover</code>.</p></div>
      <div class="card"><h3>🛡️ Moderation</h3><p>Automod with spam auto-warns, an
        on-device ML phishing/scam filter, and a contact-info/solicitation filter —
        with per-channel/category exemptions when a filter shouldn't run somewhere —
        plus kick / ban / timeout / warn tools for your Eboard, a deleted-message
        audit log (who sent it, who deleted it), and an anonymous cross-server
        repeat-offender check (a count only, no names).</p></div>
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
    } • <a href="/terms">Terms</a> • <a href="/privacy">Privacy</a></footer>
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
      <p class="tag">Add TaigaBot to your server, then run three quick steps.</p>
      <div class="actions">
        <a class="btn secondary" href="/">← Back to home</a>
        <a class="btn secondary" href="/commands">📖 Commands</a>
      </div>
    </header>
    <div class="prose">

      <h2>1. Invite TaigaBot</h2>
      <p>Click the <a href="/">invite button on the home page</a> and pick your server
      (you need the <strong>Manage Server</strong> permission there). The invite already
      includes every permission the bot needs, so just accept the prompt.</p>

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

      <h2>3. Run /setup once</h2>
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
          correct:</strong> your server has <strong>"Require 2FA for moderation"</strong>
          on (Server Settings → Safety Setup), which blocks Manage Roles/Channels, kick,
          ban, and more unless the bot's owner account has 2FA. Turn that requirement off
          while running setup, or ask an admin who can.</li>
        <li><strong>"Missing Access" / "Couldn't edit" some channels during setup:</strong>
          those channels already deny <code>@everyone</code> view, so the bot can't see
          them to gate them. Grant TaigaBot <strong>View Channel</strong> on them, or run
          <code>/setup</code> once with the bot temporarily set to Administrator, then
          re-run.</li>
        <li><strong>Members who already had roles still see everything after setup:</strong>
          gating can't override a role that allows view. Use the <strong>role reset</strong>
          toggle in <code>/setup</code> (needs TaigaBot above everyone) so old roles are
          stripped until members re-verify.</li>
        <li><strong>Slash commands don't appear right away:</strong> Discord can take up
          to about an hour to show a bot's commands in a newly joined server the first
          time. Give it a bit, then refresh Discord.</li>
      </ul>

    </div>
    <footer>{_club_footer()} • <a href="/">Home</a> • <a href="/commands">Commands</a></footer>
    """
    return _page("TaigaBot | Setup", body)


_LAST_UPDATED = "July 13, 2026"


def _contact_html() -> str:
    """Contact line for the legal pages: GitHub issues and/or the club site,
    depending on what's configured."""
    parts = []
    if config.GITHUB_URL:
        parts.append(
            f'opening an issue on <a href="{html.escape(config.GITHUB_URL)}">GitHub</a>'
        )
    if config.CLUB_URL:
        parts.append(
            f'reaching the club through <a href="{html.escape(config.CLUB_URL)}">its website</a>'
        )
    if not parts:
        return "contacting your server's Eboard"
    return " or ".join(parts)


def _legal_footer() -> str:
    return (
        f"<footer>{_club_footer()} • "
        '<a href="/">Home</a> • <a href="/terms">Terms</a> • '
        '<a href="/privacy">Privacy</a></footer>'
    )


def _terms_html() -> str:
    body = f"""
    <header>
      {_taiga_html()}
      <h1>Terms of Service</h1>
      <p class="tag">Last updated: {_LAST_UPDATED}</p>
      <div class="actions">
        <a class="btn secondary" href="/">← Back to home</a>
        <a class="btn secondary" href="/privacy">🔒 Privacy Policy</a>
      </div>
    </header>
    <div class="prose">

      <h2>1. What TaigaBot is</h2>
      <p>TaigaBot ("the bot", "the service") is a free, open-source Discord bot
      built by the RIT AI Club for RIT student club servers. It provides
      university-email verification, moderation, leveling, projects, and related
      features. By adding the bot to a server or using its commands, you agree to
      these terms.</p>

      <h2>2. Who may use it</h2>
      <ul>
        <li>You must comply with Discord's
          <a href="https://discord.com/terms">Terms of Service</a> and
          <a href="https://discord.com/guidelines">Community Guidelines</a>,
          including Discord's minimum age requirement.</li>
        <li>Email verification is intended for holders of a valid university
          email address on the domains the hosting club has configured. Server
          admins may additionally grant access manually at their discretion.</li>
      </ul>

      <h2>3. Acceptable use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>verify with an email address that isn't yours, or otherwise
          impersonate another person;</li>
        <li>attempt to evade moderation (spam, ban evasion, alt accounts —
          verification is deliberately one account per university email);</li>
        <li>abuse, overload, or attempt to disrupt the bot, its commands, or
          this website;</li>
        <li>use the bot's features (e.g. <code>/ask</code>) to generate or
          spread content that violates Discord's rules or applicable law.</li>
      </ul>
      <p>Server moderators ("Eboard") may warn, time out, kick, or ban members
      who break server rules; the bot's automod may delete messages and issue
      automatic warnings. Moderation decisions belong to each server's Eboard,
      not to the bot's developers.</p>

      <h2>4. Server admins' responsibilities</h2>
      <ul>
        <li>Admins who add the bot are responsible for telling their members that
          verification stores a real name and university email (see the
          <a href="/privacy">Privacy Policy</a>).</li>
        <li>Backup rosters and moderation logs contain personal data — admins
          must keep the <code>#taiga-backups</code> and <code>#mod-log</code>
          channels restricted to Eboard.</li>
      </ul>

      <h2>5. Third-party services</h2>
      <p>Some features rely on third parties: verification emails are delivered
      via Brevo, and <code>/ask</code> sends your prompt to Google Gemini. Their
      terms apply to those interactions. See the
      <a href="/privacy">Privacy Policy</a> for details.</p>

      <h2>6. No warranty</h2>
      <p>The bot is a volunteer-run student project provided <strong>"as is"
      and "as available"</strong>, without warranties of any kind. We don't
      guarantee uptime, that data (XP, warnings, verification records) will
      never be lost, or that any feature will keep working. To the maximum
      extent permitted by law, the developers and the RIT AI Club are not liable
      for any damages arising from use of the bot.</p>

      <h2>7. Termination</h2>
      <p>You can stop using the bot at any time; server admins can remove it at
      any time, which stops all collection for that server. We may block users
      or servers that abuse the service. You may request deletion of your
      stored data as described in the <a href="/privacy">Privacy Policy</a>.</p>

      <h2>8. Changes</h2>
      <p>We may update these terms as the bot evolves; the "Last updated" date
      above will change when we do. Continued use after a change means you
      accept the new terms.</p>

      <h2>9. Contact</h2>
      <p>Questions about these terms are best raised by {_contact_html()}.</p>

    </div>
    {_legal_footer()}
    """
    return _page("TaigaBot | Terms of Service", body)


def _privacy_html() -> str:
    body = f"""
    <header>
      {_taiga_html()}
      <h1>Privacy Policy</h1>
      <p class="tag">Last updated: {_LAST_UPDATED}</p>
      <div class="actions">
        <a class="btn secondary" href="/">← Back to home</a>
        <a class="btn secondary" href="/terms">📜 Terms of Service</a>
      </div>
    </header>
    <div class="prose">

      <p>TaigaBot is an open-source Discord bot run by the RIT AI Club for RIT
      student club servers. This page explains what data the bot stores, why,
      who can see it, and how to get it removed. The source code is public, so
      everything here can be checked against what the bot actually does.</p>

      <h2>1. What we collect and why</h2>
      <ul>
        <li><strong>Verification records</strong> — when you run
          <code>/verify</code> and <code>/confirm</code>, the bot stores your
          Discord user ID, Discord username, the real name you entered, and
          your university email address. This is the core feature: it keeps
          club servers students-only and lets clubs know who their members are.
          Verification is shared across every server the bot is in, so you only
          verify once.</li>
        <li><strong>Moderation records</strong> — warnings (who was warned, by
          whom, when, and the reason) are stored per server. Other servers'
          moderators can see only a <em>count</em> of your warnings elsewhere —
          never the reasons, server names, or details.</li>
        <li><strong>Leveling</strong> — a message count/XP total per user,
          shared across servers so your rank follows you. No message content is
          stored for leveling.</li>
        <li><strong>Server configuration</strong> — per-server settings such as
          automod toggles, banned-word lists, reaction-role bindings, and
          project records (project names, channels, and member/lead user IDs).</li>
        <li><strong>Message content, transiently</strong> — automod (spam,
          banned words, invite links, phishing, contact-info filters) reads
          messages as they arrive but does not store them. The phishing filter
          runs a small on-device model; message text never leaves the bot for
          filtering. When automod or a moderator deletes a message, an entry
          (author, channel, the deleted content when available, and who deleted
          it) is posted to the server's Eboard-only <code>#mod-log</code>
          channel so moderation is auditable.</li>
        <li><strong>This website</strong> — visitor IP addresses are held
          briefly in memory for rate limiting only; they are not logged or
          stored. No cookies, no analytics.</li>
      </ul>

      <h2>2. Third parties we share data with</h2>
      <ul>
        <li><strong>Brevo</strong> — your email address (and the one-time code)
          is passed to Brevo to deliver verification emails.</li>
        <li><strong>Google Gemini</strong> — if you use <code>/ask</code>, your
          prompt is sent to Google's Gemini API to generate the answer. Don't
          put personal information in prompts.</li>
        <li><strong>Discord</strong> — the bot runs on Discord; everything it
          posts (mod-log entries, backup rosters, replies) lives in Discord
          channels and is subject to
          <a href="https://discord.com/privacy">Discord's privacy policy</a>.</li>
      </ul>
      <p>We never sell data or share it with anyone else.</p>

      <h2>3. Who can see what</h2>
      <ul>
        <li><strong>Everyone in a server</strong> can see ranks and the
          leaderboard, and public project info.</li>
        <li><strong>Eboard / admins of each server</strong> can look up the
          verified name and email of that server's members
          (<code>/whois</code>), see that server's warnings, the mod-log, and
          the periodic <strong>backup roster</strong> — a CSV of the server's
          current verified members' names and emails, posted to the Eboard-only
          <code>#taiga-backups</code> channel so membership survives a hosting
          wipe. If you verified on one server and joined another, that server's
          Eboard can see your name and email too — verified membership is
          intentionally visible to the leadership of every server you join.</li>
        <li><strong>The bot's host operator</strong> (the RIT AI Club) has
          access to the underlying database.</li>
      </ul>

      <h2>4. Retention and deletion</h2>
      <ul>
        <li>Verification, warning, XP, and project records are kept until they
          are deleted by a moderator (e.g. <code>/clearwarnings</code>) or on
          request.</li>
        <li>Lost your Discord account? <code>/recover</code> <em>moves</em> your
          verification to the new account and removes it from the old one —
          nothing is duplicated.</li>
        <li><strong>To have your data removed</strong>, ask your server's Eboard
          or contact us by {_contact_html()}. We'll delete your verification
          record and associated data; note you'd lose access to gated channels
          until you verify again.</li>
        <li>Leaving a server does not by itself delete your verification —
          it's account-wide, so rejoining or joining a sibling server still
          works. Ask for deletion if you want it gone entirely.</li>
      </ul>

      <h2>5. Security</h2>
      <p>Data is stored in a database on the bot's host, accessible only to the
      operators. Name/email rosters are only ever posted to Eboard-restricted
      channels. One-time verification codes are held in memory only, expire after
      a few minutes, and are never written to the database. That said, this is a
      volunteer student project — don't use it for data you consider highly
      sensitive.</p>

      <h2>6. Children</h2>
      <p>The bot is intended for university students and follows Discord's
      minimum age requirements; it is not directed at children.</p>

      <h2>7. Changes</h2>
      <p>If what we collect or share changes, this page and its "Last updated"
      date will be updated. Material changes will be announced in the servers
      the bot serves.</p>

      <h2>8. Contact</h2>
      <p>Privacy questions or deletion requests: {_contact_html()}, or ask any
      Eboard member of your server to pass the request on.</p>

    </div>
    {_legal_footer()}
    """
    return _page("TaigaBot | Privacy Policy", body)


async def _landing(request: web.Request) -> web.Response:
    return web.Response(text=_landing_html(request.app["bot"]), content_type="text/html")


async def _commands(request: web.Request) -> web.Response:
    return web.Response(text=_commands_html(request.app["bot"]), content_type="text/html")


async def _setup(_request: web.Request) -> web.Response:
    return web.Response(text=_setup_html(), content_type="text/html")


async def _terms(_request: web.Request) -> web.Response:
    return web.Response(text=_terms_html(), content_type="text/html")


async def _privacy(_request: web.Request) -> web.Response:
    return web.Response(text=_privacy_html(), content_type="text/html")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_keep_alive(bot) -> None:
    """Start the web server (landing page + command docs + health) in the
    background. Binds 0.0.0.0:$PORT so Railway/Render can route to it."""
    port = int(os.getenv("PORT", "8080"))
    ASSETS_DIR.mkdir(exist_ok=True)  # so add_static works even before a logo is added
    app = web.Application(middlewares=[_rate_limit])
    app["bot"] = bot
    app.router.add_get("/", _landing)
    app.router.add_get("/commands", _commands)
    app.router.add_get("/setup", _setup)
    app.router.add_get("/terms", _terms)
    app.router.add_get("/privacy", _privacy)
    app.router.add_get("/health", _health)
    app.router.add_static("/assets", ASSETS_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(
        "Web server listening on 0.0.0.0:%d (/, /commands, /setup, /terms, /privacy, /health)",
        port,
    )
