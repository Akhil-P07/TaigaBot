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

# ── Paths ──────────────────────────────────────────────────────────────────
DB_PATH: str = _get("DB_PATH", "taigabot.db")

# ── Backups ──────────────────────────────────────────────────────────────────
# Channel ID the bot uploads database snapshots to. MUST be a private,
# Eboard-only channel — the DB contains real names and emails. Blank = disabled.
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
