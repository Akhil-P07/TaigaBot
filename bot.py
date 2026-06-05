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
import personality
from database import Database
from keep_alive import start_keep_alive
from utils.checks import NotEboard

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

        # Sync slash commands. Global sync makes commands available in every
        # guild the bot is in (and any it's later invited to), but can take up
        # to ~1 hour to propagate. If GUILD_ID is set, we ALSO copy the commands
        # to that guild for instant updates — handy as a dev/test server.
        synced = await self.tree.sync()
        log.info("Synced %d command(s) globally.", len(synced))
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            log.info(
                "Also synced %d command(s) instantly to dev guild %s.",
                len(guild_synced),
                config.GUILD_ID,
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild: %s (id=%s, %d members)", guild.name, guild.id, guild.member_count)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
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
    elif isinstance(error, discord.app_commands.CommandOnCooldown):
        # NB: must come before the generic CheckFailure branch below —
        # CommandOnCooldown subclasses CheckFailure, so order matters.
        msg = f"⏳ Slow down — try again in {error.retry_after:.0f}s."
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
    except discord.HTTPException:
        pass


async def main() -> None:
    problems = config.validate()
    for p in problems:
        log.warning("CONFIG: %s", p)
    if not config.DISCORD_TOKEN:
        log.error("No DISCORD_TOKEN set. Copy .env.example to .env and fill it in.")
        return
    await start_keep_alive(bot)  # serves the landing page + command docs + health
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
