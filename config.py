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

# ── Email / OTP ────────────────────────────────────────────────────────────
GMAIL_ADDRESS: str = _get("GMAIL_ADDRESS")
# Google displays app passwords in 4 space-separated groups ("abcd efgh ..."),
# but the spaces are only for readability — strip them so a copy-paste with
# spaces still authenticates.
GMAIL_APP_PASSWORD: str = _get("GMAIL_APP_PASSWORD").replace(" ", "")
ALLOWED_EMAIL_DOMAINS: list[str] = [
    d.strip().lower() for d in _get("ALLOWED_EMAIL_DOMAINS", "rit.edu,g.rit.edu").split(",") if d.strip()
]
OTP_TTL_MINUTES: int = int(_get("OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS: int = int(_get("OTP_MAX_ATTEMPTS", "5"))

# ── Role / channel names ────────────────────────────────────────────────────
EBOARD_ROLE_NAME: str = _get("EBOARD_ROLE_NAME", "Eboard")
UNVERIFIED_ROLE_NAME: str = _get("UNVERIFIED_ROLE_NAME", "Unverified")
VERIFIED_ROLE_NAME: str = _get("VERIFIED_ROLE_NAME", "Verified")
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
# bot uploads DB snapshots there. BACKUP_CHANNEL_ID is an optional override to
# point backups at a specific channel by ID instead of resolving by name.
BACKUP_CHANNEL_ID: int | None = (
    int(_get("BACKUP_CHANNEL_ID")) if _get("BACKUP_CHANNEL_ID").isdigit() else None
)
BACKUP_INTERVAL_HOURS: int = int(_get("BACKUP_INTERVAL_HOURS", "12"))

# Bot branding
BOT_COLOR = 0xE8552D  # warm orange, "Taiga"


def validate() -> list[str]:
    """Return a list of human-readable problems with the current config."""
    problems = []
    if not DISCORD_TOKEN:
        problems.append("DISCORD_TOKEN is not set.")
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        problems.append(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — email verification will fail."
        )
    return problems
