"""TaigaBot — AI Club Discord bot.

Entry point. Connects to the database, auto-loads every feature module in the
`features/` folder, and syncs slash commands.

To add a new feature later: drop a `features/your_feature.py` file that defines
an async `setup(bot)` function (the standard discord.py cog pattern). It will be
discovered and loaded automatically on the next restart — no edits here needed.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import traceback

import discord
from discord.ext import commands

import config
import crypto
import personality
from database import Database
from web.server import start_web
from utils import guildutils as gu
from utils.checks import NotBotOwner, NotEboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
log = logging.getLogger("taigabot")

FEATURES_DIR = pathlib.Path(__file__).parent / "features"


class TaigaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed for automod + XP
        intents.members = True          # needed for join events + member iteration
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("Database ready at %s", config.DB_PATH)

        # Auto-load every feature module (any features/*.py that isn't private).
        loaded = 0
        for path in sorted(FEATURES_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            module = f"features.{path.stem}"
            try:
                await self.load_extension(module)
                loaded += 1
                log.info("Loaded feature: %s", path.stem)
            except Exception:  # noqa: BLE001
                log.error("Failed to load %s:\n%s", module, traceback.format_exc())
        log.info("Loaded %d feature module(s).", loaded)

        # Sync slash commands GLOBALLY so they work in every server the bot is in
        # (and any it's later invited to). Global sync can take up to ~1 hour to
        # propagate the first time a command changes.
        synced = await self.tree.sync()
        log.info("Synced %d command(s) globally.", len(synced))
        # If GUILD_ID is set, CLEAR that guild's command scope. An earlier version
        # also copied the global commands into GUILD_ID for instant updates, but
        # that registered every command twice in that guild (global copy + guild
        # copy) — users saw each command duplicated. Clearing the guild scope
        # leaves only the global set, removing those duplicates. Self-healing:
        # keeping GUILD_ID set will scrub the dupes on the next start.
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(
                "Cleared guild-scoped command copies for guild %s (removes duplicates).",
                config.GUILD_ID,
            )

    async def _enforce_ban(self, guild: discord.Guild) -> bool:
        """Leave `guild` if it's on the ban list, telling it why first. True if
        the bot left. Delivery is best-effort — the leave happens regardless."""
        row = await self.db.get_guild_ban(guild.id)
        if row is None:
            return False
        embed, plain = gu.blocked_embed(row["reason"])
        await gu.announce_to_guild(guild, embed, plain)
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception("Banned guild %s (%s) — leave failed.", guild.name, guild.id)
            return False
        log.warning(
            "Refused banned guild: %s (id=%s). Reason: %s",
            guild.name, guild.id, row["reason"],
        )
        return True

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if await self._enforce_ban(guild):
            return
        log.info("Joined guild: %s (id=%s, %d members)", guild.name, guild.id, guild.member_count)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Left guild: %s (id=%s)", guild.name, guild.id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        # Catches servers banned by ID while the bot was already in them, or
        # invited while this process was down. Idempotent, so re-firing on a
        # gateway reconnect is harmless.
        for guild in list(self.guilds):
            await self._enforce_ban(guild)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="/verify | AI Club 🐯"
            )
        )

    async def on_tree_error(self) -> None:  # placeholder; see tree.error below
        pass

    async def close(self) -> None:
        await self.db.close()
        await super().close()


bot = TaigaBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
) -> None:
    """Friendly, centralized handling for slash-command errors."""
    if isinstance(error, NotEboard):
        sass = personality.say("permission_denied")
        msg = f"⛔ {error}" + (f"\n*{sass}*" if sass else "")
    elif isinstance(error, NotBotOwner):
        msg = f"⛔ {error}"
    elif isinstance(error, discord.app_commands.CommandOnCooldown):
        # NB: must come before the generic CheckFailure branch below —
        # CommandOnCooldown subclasses CheckFailure, so order matters.
        msg = f"⏳ Slow down, try again in {error.retry_after:.0f}s."
    elif isinstance(error, discord.app_commands.CheckFailure):
        msg = "⛔ You don't have permission to use this command."
    else:
        log.error("Command error: %s\n%s", error, traceback.format_exc())
        msg = "⚠️ Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:  # noqa: BLE001 - the error handler must never itself raise
        pass


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Log stray exceptions from background tasks instead of letting them bubble
    up and risk tearing down the process. Keeps one misbehaving task from taking
    the whole bot (and the web server it shares a process with) down."""
    exc = context.get("exception")
    log.error("Unhandled event-loop exception: %s", context.get("message") or exc, exc_info=exc)


async def main() -> None:
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    problems = config.validate()
    for p in problems:
        log.warning("CONFIG: %s", p)
    if not config.DISCORD_TOKEN:
        log.error("No DISCORD_TOKEN set. Copy .env.example to .env and fill it in.")
        return
    # Checked here, before anything opens the database, so a bad key exits
    # cleanly instead of throwing out of setup_hook. Whether the key is the RIGHT
    # one is a question only the database can answer — Database.connect() raises
    # EncryptionKeyMismatch for that.
    try:
        crypto.load()
    except crypto.MissingKey as e:
        log.error("%s", e)
        return
    await start_web(bot)  # serves the React dashboard, the JSON API, and /health
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
