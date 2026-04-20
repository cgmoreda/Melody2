import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

load_dotenv()


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        if bot.user is not None:
            logging.info("Bot is ready as %s", bot.user)

    @bot.command(name="ping")
    async def ping(ctx: commands.Context) -> None:
        await ctx.send("pong")

    return bot


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")

    bot = create_bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
