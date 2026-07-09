import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import timedelta
import discord
from discord.ext import commands

from cogs.moderation import ModerationCog


class DummyRole:
    def __init__(self, position: int):
        self.position = position

    def __ge__(self, other: "DummyRole") -> bool:
        return self.position >= other.position

    def __le__(self, other: "DummyRole") -> bool:
        return self.position <= other.position


@pytest.fixture
def cog() -> ModerationCog:
    bot = MagicMock(spec=commands.Bot)
    bot.user = MagicMock(spec=discord.User)
    return ModerationCog(bot)


@pytest.fixture
def ctx() -> AsyncMock:
    ctx = AsyncMock(spec=commands.Context)
    ctx.author = MagicMock(spec=discord.Member)
    ctx.guild = MagicMock(spec=discord.Guild)
    ctx.guild.owner_id = 999
    ctx.author.id = 100
    
    ctx.author.top_role = DummyRole(50)
    
    ctx.guild.me = MagicMock(spec=discord.Member)
    ctx.guild.me.top_role = DummyRole(50)
    
    return ctx


@pytest.fixture
def member() -> AsyncMock:
    mem = AsyncMock(spec=discord.Member)
    mem.id = 123
    mem.top_role = DummyRole(10)
    mem.mention = "<@123>"
    return mem


@pytest.mark.asyncio
async def test_timeout_success(cog: ModerationCog, ctx: AsyncMock, member: AsyncMock) -> None:
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    member.timeout.assert_called_once()
    assert member.timeout.call_args[0][0] == timedelta(minutes=15)
    ctx.send.assert_called_once()
    assert "✅" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_timeout_self(cog: ModerationCog, ctx: AsyncMock) -> None:
    member = ctx.author
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    ctx.send.assert_called_with("You cannot time yourself out.")


@pytest.mark.asyncio
async def test_timeout_bot(cog: ModerationCog, ctx: AsyncMock) -> None:
    member = cog.bot.user
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    ctx.send.assert_called_with("I cannot time myself out.")


@pytest.mark.asyncio
async def test_timeout_owner(cog: ModerationCog, ctx: AsyncMock, member: AsyncMock) -> None:
    member.id = ctx.guild.owner_id
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    ctx.send.assert_called_with("You cannot time out the server owner.")


@pytest.mark.asyncio
async def test_timeout_hierarchy_author(cog: ModerationCog, ctx: AsyncMock, member: AsyncMock) -> None:
    member.top_role = DummyRole(60)
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    ctx.send.assert_called_with("You cannot time out a member with an equal or higher top role.")


@pytest.mark.asyncio
async def test_timeout_hierarchy_bot(cog: ModerationCog, ctx: AsyncMock, member: AsyncMock) -> None:
    ctx.guild.me.top_role = DummyRole(5)
    await cog.timeout_command.callback(cog, ctx, member, "15", "min")
    ctx.send.assert_called_with("I cannot time out a member with an equal or higher top role.")


@pytest.mark.asyncio
async def test_timeout_invalid_duration(cog: ModerationCog, ctx: AsyncMock, member: AsyncMock) -> None:
    await cog.timeout_command.callback(cog, ctx, member, "15x")
    ctx.send.assert_called_once()
    assert "Invalid duration unit" in ctx.send.call_args[0][0]
