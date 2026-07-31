"""Central configuration for TaigaBot.

All values are read from environment variables (loaded from a .env file).
Import `config` anywhere and read attributes, e.g. `config.EBOARD_ROLE_NAME`.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ── Discord ──────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = _get("DISCORD_TOKEN")
GUILD_ID: int | None = int(_get("GUILD_ID")) if _get("GUILD_ID").isdigit() else None
# Application (Client) ID — used to build the bot's invite link on the web page.
# Find it in the Developer Portal → your app → General Information. Optional: the
# bot falls back to its own user id once it's logged in.
DISCORD_CLIENT_ID: str = _get("DISCORD_CLIENT_ID")
# Public repo URL, shown as a "Build on GitHub" link on the landing page. Blank
# hides it.
GITHUB_URL: str = _get("GITHUB_URL")
# Discord user IDs allowed to run bot-owner commands (e.g. /premium). Comma
# separated. Leave blank to fall back to the application owner/team that Discord
# reports for this bot. Server admins never qualify — this is authority over the
# bot itself, not over any one server.
BOT_OWNER_IDS: set[int] = {
    int(x) for x in _get("BOT_OWNER_IDS").replace(" ", "").split(",") if x.isdigit()
}
# Club website — the footer logo links here. Blank makes the logo non-clickable.
CLUB_URL: str = _get("CLUB_URL", "https://campusgroups.rit.edu/ritai/home/")

# ── Email / OTP ────────────────────────────────────────────────────────────
# OTP emails are sent via Brevo's HTTP API (port 443), because hosts like
# Railway/Render block outbound SMTP. Free account → verify a sender address
# (no domain needed) → create an API key. https://app.brevo.com
BREVO_API_KEY: str = _get("BREVO_API_KEY")
# The "From" address; must be a Brevo-verified sender. Falls back to GMAIL_ADDRESS
# so older configs keep working without renaming the variable.
GMAIL_ADDRESS: str = _get("GMAIL_ADDRESS")
EMAIL_FROM: str = _get("EMAIL_FROM") or GMAIL_ADDRESS
EMAIL_FROM_NAME: str = _get("EMAIL_FROM_NAME", "TaigaBot")
ALLOWED_EMAIL_DOMAINS: list[str] = [
    d.strip().lower() for d in _get("ALLOWED_EMAIL_DOMAINS", "rit.edu,g.rit.edu").split(",") if d.strip()
]
OTP_TTL_MINUTES: int = int(_get("OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS: int = int(_get("OTP_MAX_ATTEMPTS", "5"))

# ── Role / channel names ────────────────────────────────────────────────────
EBOARD_ROLE_NAME: str = _get("EBOARD_ROLE_NAME", "Eboard")
UNVERIFIED_ROLE_NAME: str = _get("UNVERIFIED_ROLE_NAME", "Unverified")
VERIFIED_ROLE_NAME: str = _get("VERIFIED_ROLE_NAME", "Verified")
PROJECT_LEAD_ROLE_NAME: str = _get("PROJECT_LEAD_ROLE_NAME", "Project Lead")
UNVERIFIED_CHANNEL_NAME: str = _get("UNVERIFIED_CHANNEL_NAME", "unverified")
WELCOME_CHANNEL_NAME: str = _get("WELCOME_CHANNEL_NAME", "welcome")
MODLOG_CHANNEL_NAME: str = _get("MODLOG_CHANNEL_NAME", "mod-log")
BACKUP_CHANNEL_NAME: str = _get("BACKUP_CHANNEL_NAME", "taiga-backups")
ROLES_CHANNEL_NAME: str = _get("ROLES_CHANNEL_NAME", "roles")


def _flag(name: str, default: str = "0") -> bool:
    return _get(name, default).lower() in ("1", "true", "yes", "on")


# ── Channels /setup should NOT gate ──────────────────────────────────────────
# Comma-separated channel/category IDs that /setup leaves untouched (no
# @everyone-deny / Verified-allow). Put a CATEGORY id to skip every channel in
# it — e.g. a "Projects/Interests" category you gate behind interest roles
# yourself. Right-click -> Copy Channel/Category ID (Developer Mode on).
GATING_IGNORE_IDS: set[int] = {
    int(x) for x in _get("GATING_IGNORE").replace(" ", "").split(",") if x.isdigit()
}

# ── Fresh-start role reset (DESTRUCTIVE, opt-in) ─────────────────────────────
# When True, /setup first removes every member's roles (except Eboard, Verified,
# Unverified, managed/bot roles, and any role above TaigaBot) so that nobody
# keeps access granted by old self-assign/interest roles until they re-verify
# and re-pick in the #roles channel. This re-runs on EVERY /setup, so the
# intended use is: enable it, run /setup once, then set it back to off.
RESET_ROLES_ON_SETUP: bool = _flag("RESET_ROLES_ON_SETUP", "0")

# ── Paths ──────────────────────────────────────────────────────────────────
DB_PATH: str = _get("DB_PATH", "taigabot.db")

# ── Backups ──────────────────────────────────────────────────────────────────
# /setup auto-creates an Eboard-only channel named BACKUP_CHANNEL_NAME and the
# bot uploads verified-member rosters there. BACKUP_CHANNEL_ID is an optional
# override to point backups at a specific channel by ID instead of by name.
BACKUP_CHANNEL_ID: int | None = (
    int(_get("BACKUP_CHANNEL_ID")) if _get("BACKUP_CHANNEL_ID").isdigit() else None
)
BACKUP_INTERVAL_HOURS: int = int(_get("BACKUP_INTERVAL_HOURS", "12"))

# ── Gemini AI assistant (/ask) ───────────────────────────────────────────────
# Free Gemini API key from https://aistudio.google.com/apikey . Leave blank to
# disable /ask. GEMINI_MODEL can be any free-tier model name.
GEMINI_API_KEY: str = _get("GEMINI_API_KEY")
GEMINI_MODEL: str = _get("GEMINI_MODEL", "gemini-2.0-flash")

# ── News feed watcher (/news) ────────────────────────────────────────────────
# How often to poll subscribed feeds. Floored at 15 minutes: polls use
# conditional GET so the steady state is a bodyless 304, but a tighter interval
# still means more requests against third-party CDNs for no practical gain.
NEWS_POLL_MINUTES: int = max(15, int(_get("NEWS_POLL_MINUTES", "15")))
# Per-guild cap on custom (user-supplied) feed URLs. This is what bounds total
# poll volume across the whole deployment — without it one server could add
# hundreds of feeds and the bot would dutifully poll them all.
NEWS_MAX_CUSTOM_FEEDS: int = max(1, int(_get("NEWS_MAX_CUSTOM_FEEDS", "5")))
# Premium servers get a higher cap. Still a cap, not unlimited: poll volume is
# what the whole cost model rests on, so it stays bounded for every tier.
NEWS_PREMIUM_MAX_CUSTOM_FEEDS: int = max(
    NEWS_MAX_CUSTOM_FEEDS, int(_get("NEWS_PREMIUM_MAX_CUSTOM_FEEDS", "25"))
)

# ── Dashboard (Discord OAuth2 login) ─────────────────────────────────────────
# The web dashboard signs people in with Discord so they can see their servers'
# tier, and so the owner can grant premium. Only the `identify` scope is
# requested — which servers you can manage is worked out from the bot's own
# membership data, not from your Discord account, so the bot never asks for a
# list of your servers.
DISCORD_CLIENT_SECRET: str = _get("DISCORD_CLIENT_SECRET")
# Public base URL of the site, e.g. https://taigabot.up.railway.app — used to
# build the OAuth redirect URI, which must match the Developer Portal exactly.
PUBLIC_BASE_URL: str = _get("PUBLIC_BASE_URL").rstrip("/")
# Secret for signing session cookies. Generate with: python -c "import secrets;
# print(secrets.token_hex(32))". Changing it logs everyone out.
SESSION_SECRET: str = _get("SESSION_SECRET")
SESSION_TTL_DAYS: int = int(_get("SESSION_TTL_DAYS", "7"))

# Max tickets one user may open per hour, so a public form can't be flooded.
TICKET_RATE_PER_HOUR: int = int(_get("TICKET_RATE_PER_HOUR", "5"))

# Bot branding
BOT_COLOR = 0xE8552D  # warm orange, "Taiga"


def dashboard_ready() -> bool:
    """True when the dashboard has everything it needs to log people in."""
    return bool(
        DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and PUBLIC_BASE_URL and SESSION_SECRET
    )


def validate() -> list[str]:
    """Return a list of human-readable problems with the current config."""
    problems = []
    if not DISCORD_TOKEN:
        problems.append("DISCORD_TOKEN is not set.")
    if not BREVO_API_KEY:
        problems.append("BREVO_API_KEY not set — email verification will fail.")
    if not EMAIL_FROM:
        problems.append(
            "EMAIL_FROM (or GMAIL_ADDRESS) not set — no verified sender address for email."
        )
    if not dashboard_ready():
        missing = [
            n for n, v in (
                ("DISCORD_CLIENT_ID", DISCORD_CLIENT_ID),
                ("DISCORD_CLIENT_SECRET", DISCORD_CLIENT_SECRET),
                ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
                ("SESSION_SECRET", SESSION_SECRET),
            ) if not v
        ]
        problems.append(
            f"Dashboard login disabled — missing {', '.join(missing)}. "
            "The public site still works."
        )
    if dashboard_ready() and not BOT_OWNER_IDS:
        problems.append(
            "BOT_OWNER_IDS not set — premium management falls back to the Discord "
            "application owner."
        )
    return problems
