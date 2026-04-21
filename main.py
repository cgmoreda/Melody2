import logging
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from db.repository import UserRepository
from services.cf_client import CodeforcesClient
from services.role_assigner import RoleAssigner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

load_dotenv()

EXTENSIONS = [
    "cogs.verification",
]


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True  # needed for role management

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        if bot.user is not None:
            logging.info("Bot is ready as %s", bot.user)

    @bot.command(name="ping")
    async def ping(ctx: commands.Context) -> None:
        await ctx.send("pong")

    return bot


async def _setup_services(bot: commands.Bot) -> None:
    """Instantiate shared services and attach them to the bot instance.

    These are later picked up by each cog's ``setup()`` function so that
    cogs depend on abstractions, not concrete construction (DIP).
    """
    session = aiohttp.ClientSession()
    bot.http_session = session  # type: ignore[attr-defined]

    bot.cf_client = CodeforcesClient(session)  # type: ignore[attr-defined]
    bot.role_assigner = RoleAssigner()  # type: ignore[attr-defined]

    repo = UserRepository()
    await repo.init()
    bot.user_repo = repo  # type: ignore[attr-defined]


async def _teardown_services(bot: commands.Bot) -> None:
    """Gracefully close shared resources."""
    repo: UserRepository | None = getattr(bot, "user_repo", None)
    if repo:
        await repo.close()

    session: aiohttp.ClientSession | None = getattr(bot, "http_session", None)
    if session:
        await session.close()


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")

    bot = create_bot()

    @bot.event
    async def setup_hook() -> None:
        await _setup_services(bot)
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
            logging.info("Loaded extension %s", ext)

    @bot.event
    async def on_close() -> None:
        await _teardown_services(bot)

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
