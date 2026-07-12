from __future__ import annotations

import discord
from discord.ext import commands

from services.guild_config import CONFIG_SPECS, TEXT_CONFIG_SPECS, GuildConfigService


class ConfigCog(commands.Cog, name="Config"):
    def __init__(self, config_service: GuildConfigService) -> None:
        self._config = config_service

    @commands.group(name="config", invoke_without_command=True)
    @commands.guild_only()
    async def config(self, ctx: commands.Context) -> None:
        """Show or modify per-server bot configuration keys."""
        await ctx.send("Usage: **!config <show|keys|set|reset|text>**")

    @config.command(name="show")
    @commands.guild_only()
    async def config_show(self, ctx: commands.Context) -> None:
        """Display current config values for this server."""
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
        text_cfg = await self._config.get_text_all(ctx.guild.id)
        embed.add_field(name="training_role_substring", value=text_cfg["training_role_substring"], inline=False)
        embed.add_field(name="coach_role_substring", value=text_cfg["coach_role_substring"], inline=False)
        await ctx.send(embed=embed)

    @config.command(name="keys")
    @commands.guild_only()
    async def config_keys(self, ctx: commands.Context) -> None:
        """List configurable keys with allowed ranges."""
        lines: list[str] = []
        for key, (_, minimum, maximum, description) in CONFIG_SPECS.items():
            lines.append(f"`{key}` [{minimum}..{maximum}] - {description}")
        lines.append("")
        lines.append("Text keys:")
        for key, (_, description) in TEXT_CONFIG_SPECS.items():
            lines.append(f"`{key}` - {description}")
        await ctx.send("\n".join(lines))

    @config.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_set(self, ctx: commands.Context, key: str, *args: str) -> None:
        """Set one config key to a value."""
        assert ctx.guild is not None
        normalized_key = key.lower()

        if normalized_key == "voice_check_interval":
            if len(args) != 2:
                await ctx.send("Usage: `!config set voice_check_interval <channel_type> <range>`")
                return
            channel_type = args[0].lower()
            interval = args[1]
            if channel_type not in ("solo", "duo", "team", "invite"):
                await ctx.send("Channel type must be one of: solo, duo, team, invite.")
                return

            try:
                if "-" not in interval:
                    raise ValueError
                min_str, max_str = interval.split("-", 1)
                min_val = int(min_str.strip())
                max_val = int(max_str.strip())
                if min_val <= 0 or max_val <= 0 or min_val > max_val:
                    raise ValueError
            except ValueError:
                await ctx.send("Interval must be in the format MIN-MAX (e.g. '45-60'), where MIN and MAX are positive integers and MIN <= MAX.")
                return

            text_key = f"voice_check_interval_{channel_type}"
            try:
                await self._config.set_text(ctx.guild.id, text_key, interval)
                await ctx.send(f"Set `{text_key}` to `{interval}`.")
            except ValueError as exc:
                await ctx.send(str(exc))
            return

        if len(args) != 1:
            await ctx.send("Usage: `!config set <key> <value>`")
            return

        try:
            value_int = int(args[0])
        except ValueError:
            await ctx.send("Value must be an integer.")
            return

        try:
            cfg = await self._config.set_value(ctx.guild.id, normalized_key, value_int)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        updated_value = getattr(cfg, normalized_key)
        await ctx.send(f"Set `{normalized_key}` to `{updated_value}`.")

    @config.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_reset(self, ctx: commands.Context, key: str = "all") -> None:
        """Reset one key, or all keys, back to defaults."""
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

    @config.group(name="text", invoke_without_command=True)
    @commands.guild_only()
    async def config_text(self, ctx: commands.Context) -> None:
        """Show or modify text config keys."""
        await ctx.send("Usage: **!config text <show|keys|set|reset>**")

    @config_text.command(name="show")
    @commands.guild_only()
    async def config_text_show(self, ctx: commands.Context) -> None:
        """Display text config values for this server."""
        assert ctx.guild is not None
        values = await self._config.get_text_all(ctx.guild.id)
        lines = [f"`{key}` = `{value}`" for key, value in values.items()]
        await ctx.send("\n".join(lines))

    @config_text.command(name="keys")
    @commands.guild_only()
    async def config_text_keys(self, ctx: commands.Context) -> None:
        """List configurable text keys."""
        lines = [f"`{key}` - {description}" for key, (_, description) in TEXT_CONFIG_SPECS.items()]
        await ctx.send("\n".join(lines))

    @config_text.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_text_set(self, ctx: commands.Context, key: str, *, value: str) -> None:
        """Set one text config key."""
        assert ctx.guild is not None
        try:
            updated = await self._config.set_text(ctx.guild.id, key.lower(), value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Set `{key.lower()}` to `{updated}`.")

    @config_text.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config_text_reset(self, ctx: commands.Context, key: str = "all") -> None:
        """Reset one text key, or all text keys, to defaults."""
        assert ctx.guild is not None
        normalized = key.lower()
        if normalized == "all":
            values = await self._config.reset_text_all(ctx.guild.id)
            lines = [f"`{k}` = `{v}`" for k, v in values.items()]
            await ctx.send("Reset all text config keys.\n" + "\n".join(lines))
            return

        try:
            reset_value = await self._config.reset_text(ctx.guild.id, normalized)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Reset `{normalized}` to default (`{reset_value}`).")

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

    @config_text_set.error
    async def config_text_set_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to change text config values.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: **!config text set <key> <value>**")
            return
        raise error

    @config_text_reset.error
    async def config_text_reset_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Server** permission to reset text config values.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigCog(getattr(bot, "guild_config")))
