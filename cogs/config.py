from __future__ import annotations

import discord
from discord.ext import commands

from services.guild_config import CONFIG_SPECS, GuildConfigService


class ConfigCog(commands.Cog, name="Config"):
    def __init__(self, config_service: GuildConfigService) -> None:
        self._config = config_service

    @commands.group(name="config", invoke_without_command=True)
    @commands.guild_only()
    async def config(self, ctx: commands.Context) -> None:
        await ctx.send("Usage: **!config <show|keys|set|reset>**")

    @config.command(name="show")
    @commands.guild_only()
    async def config_show(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        cfg = await self._config.get(ctx.guild.id)

        embed = discord.Embed(
            title="Guild Command Config",
            colour=discord.Colour.blurple(),
            description="Current values for configurable command behavior.",
        )
        embed.add_field(name="reminder_preview_limit", value=str(cfg.reminder_preview_limit), inline=True)
        embed.add_field(name="roundchanges_max_lines", value=str(cfg.roundchanges_max_lines), inline=True)
        embed.add_field(name="voicehours_max_lines", value=str(cfg.voicehours_max_lines), inline=True)
        embed.add_field(name="voice_check_interval_seconds", value=str(cfg.voice_check_interval_seconds), inline=True)
        embed.add_field(name="voice_confirm_timeout_seconds", value=str(cfg.voice_confirm_timeout_seconds), inline=True)
        await ctx.send(embed=embed)

    @config.command(name="keys")
    @commands.guild_only()
    async def config_keys(self, ctx: commands.Context) -> None:
        lines: list[str] = []
        for key, (_, minimum, maximum, description) in CONFIG_SPECS.items():
            lines.append(f"`{key}` [{minimum}..{maximum}] - {description}")
        await ctx.send("\n".join(lines))

    @config.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_set(self, ctx: commands.Context, key: str, value: int) -> None:
        assert ctx.guild is not None
        normalized_key = key.lower()
        try:
            cfg = await self._config.set_value(ctx.guild.id, normalized_key, value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        updated_value = getattr(cfg, normalized_key)
        await ctx.send(f"Set `{normalized_key}` to `{updated_value}`.")

    @config.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_reset(self, ctx: commands.Context, key: str = "all") -> None:
        assert ctx.guild is not None

        normalized_key = key.lower()
        if normalized_key == "all":
            await self._config.reset_all(ctx.guild.id)
            await ctx.send("Reset all config keys to defaults.")
            return

        try:
            cfg = await self._config.reset_key(ctx.guild.id, normalized_key)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        updated_value = getattr(cfg, normalized_key)
        await ctx.send(f"Reset `{normalized_key}` to default (`{updated_value}`).")

    @config_set.error
    async def config_set_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to change config values.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!config set <key> <integer_value>**")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Value must be an integer.")
            return
        raise error

    @config_reset.error
    async def config_reset_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to reset config values.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigCog(getattr(bot, "guild_config")))
