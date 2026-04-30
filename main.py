import logging
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from db.repository import UserRepository
from services.cf_client import CodeforcesClient
from services.coach_secretary import CoachSecretary
from services.atcoder_client import AtCoderProvider
from services.contest_reminder import CodeforcesProvider, ContestReminderService
from services.dynamic_voice import DynamicVoiceManager
from services.guild_config import GuildConfigService
from services.role_assigner import RoleAssigner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()

EXTENSIONS: tuple[str, ...] = (
    "cogs.help",
    "cogs.verification",
    "cogs.config",
    "cogs.gym",
    "cogs.coach_secretary",
    "cogs.dynamic_voice",
    "cogs.voice_logging",
)


class MelodyBot(commands.Bot):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._tree_synced = False

    async def setup_hook(self) -> None:
        await _setup_services(self)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            logger.info("Loaded extension %s", extension)

    async def on_ready(self) -> None:
        if self.user is not None:
            logger.info("Bot is ready as %s", self.user)
        if not self._tree_synced:
            try:
                await self.tree.sync()
                logger.info("Synced app command tree")
            except Exception:
                logger.exception("Failed to sync app command tree")
            self._tree_synced = True

    async def close(self) -> None:
        await _teardown_services(self)
        await super().close()


def create_bot() -> MelodyBot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.voice_states = True

    bot = MelodyBot(command_prefix="!", intents=intents, help_command=None)

    @bot.command(name="ping")
    async def ping(ctx: commands.Context) -> None:
        """Check whether the bot is responsive."""
        await ctx.send("pong")

    return bot


async def _setup_services(bot: commands.Bot) -> None:
    """Instantiate shared services and attach them to the bot instance."""
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
        providers=[CodeforcesProvider(), AtCoderProvider()],
        poll_seconds=poll_seconds,
    )
    await reminder.initialize()
    reminder.start()
    bot.contest_reminder = reminder  # type: ignore[attr-defined]
    logger.info("Contest reminder service started")

    secretary = CoachSecretary(repo)
    bot.coach_secretary = secretary  # type: ignore[attr-defined]
    logger.info("Coach secretary service ready")

    guild_config = GuildConfigService(repo)
    bot.guild_config = guild_config  # type: ignore[attr-defined]
    logger.info("Guild config service ready")

    dynamic_voice = DynamicVoiceManager()
    bot.dynamic_voice = dynamic_voice  # type: ignore[attr-defined]
    logger.info("Dynamic voice manager ready")


async def _teardown_services(bot: commands.Bot) -> None:
    """Gracefully close shared resources."""
    reminder: ContestReminderService | None = getattr(bot, "contest_reminder", None)
    if reminder is not None:
        await reminder.stop()

    repo: UserRepository | None = getattr(bot, "user_repo", None)
    if repo is not None:
        await repo.close()

    session: aiohttp.ClientSession | None = getattr(bot, "http_session", None)
    if session is not None:
        await session.close()


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")

    bot = create_bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
