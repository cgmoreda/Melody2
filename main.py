import logging
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from db.repository import UserRepository
from services.cf_client import CodeforcesClient
from services.contest_reminder import ContestReminderService
from services.role_assigner import RoleAssigner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

load_dotenv()

EXTENSIONS = [
    "cogs.verification",
]


class MelodyBot(commands.Bot):
    async def setup_hook(self) -> None:
        await _setup_services(self)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            logging.info("Loaded extension %s", ext)

    async def close(self) -> None:
        await _teardown_services(self)
        await super().close()


def create_bot() -> MelodyBot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = MelodyBot(command_prefix="!", intents=intents)

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

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    repo = UserRepository(database_url)
    await repo.init()
    bot.user_repo = repo  # type: ignore[attr-defined]

    poll_raw = os.getenv("CF_REMINDER_POLL_SECONDS", "300")
    try:
        poll_seconds = int(poll_raw)
    except ValueError:
        poll_seconds = 300

    reminder = ContestReminderService(
        session=session,
        bot=bot,
        repo=repo,
        poll_seconds=poll_seconds,
    )
    await reminder.initialize()
    reminder.start()
    bot.contest_reminder = reminder  # type: ignore[attr-defined]
    logging.info("Contest reminder service started")


async def _teardown_services(bot: commands.Bot) -> None:
    """Gracefully close shared resources."""
    reminder: ContestReminderService | None = getattr(bot, "contest_reminder", None)
    if reminder:
        await reminder.stop()

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
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
