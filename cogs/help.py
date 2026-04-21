from __future__ import annotations

import discord
from discord.ext import commands


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """Show all commands, or detailed help for one command."""
        if query is None:
            await self._send_general_help(ctx)
            return

        command = self.bot.get_command(query)
        if command is None:
            await ctx.send(f"No command named `{query}` was found.")
            return

        await self._send_command_help(ctx, command)

    async def _send_general_help(self, ctx: commands.Context) -> None:
        grouped: dict[str, list[commands.Command]] = {}
        for command in sorted(self.bot.commands, key=lambda cmd: cmd.name):
            if command.hidden:
                continue
            cog_name = command.cog_name or "General"
            grouped.setdefault(cog_name, []).append(command)

        embed = discord.Embed(
            title="Bot Commands",
            description="Use `!help <command>` for details on one command.",
            colour=discord.Colour.blurple(),
        )

        for cog_name in sorted(grouped):
            commands_in_cog = grouped[cog_name]
            lines = [f"`{self._signature(cmd)}` - {self._short_help(cmd)}" for cmd in commands_in_cog]
            for index, chunk in enumerate(self._chunk_lines(lines), start=1):
                field_name = cog_name if index == 1 else f"{cog_name} (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)

        await ctx.send(embed=embed)

    async def _send_command_help(self, ctx: commands.Context, command: commands.Command) -> None:
        embed = discord.Embed(
            title=f"Command: !{command.qualified_name}",
            colour=discord.Colour.blurple(),
            description=command.help or "No detailed description.",
        )
        embed.add_field(name="Usage", value=f"`!{self._signature(command)}`", inline=False)

        aliases = ", ".join(f"`{alias}`" for alias in command.aliases) if command.aliases else "None"
        embed.add_field(name="Aliases", value=aliases, inline=True)

        if isinstance(command, commands.Group):
            visible_subcommands = [sub for sub in command.commands if not sub.hidden]
            if visible_subcommands:
                lines = [f"`{sub.qualified_name}` - {self._short_help(sub)}" for sub in visible_subcommands]
                embed.add_field(name="Subcommands", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    @staticmethod
    def _signature(command: commands.Command) -> str:
        if command.signature:
            return f"{command.qualified_name} {command.signature}".strip()
        return command.qualified_name

    @staticmethod
    def _short_help(command: commands.Command) -> str:
        if command.brief:
            return command.brief
        if command.help:
            return command.help.splitlines()[0]
        return "No description."

    @staticmethod
    def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            extra = len(line) + (1 if current else 0)
            if current and size + extra > limit:
                chunks.append("\n".join(current))
                current = [line]
                size = len(line)
            else:
                current.append(line)
                size += extra

        if current:
            chunks.append("\n".join(current))
        return chunks


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
