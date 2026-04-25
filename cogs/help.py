from __future__ import annotations

from difflib import get_close_matches

import discord
from discord.ext import commands

from services.discord_output import (
    DISCORD_EMBED_MAX_FIELDS,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TOTAL_CHAR_LIMIT,
    clip_embed_description,
    clip_embed_field_name,
    clip_text,
    split_embed_field_lines,
)


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """Show all commands, or detailed help for one command."""
        if query is None:
            await self._send_general_help(ctx)
            return

        command = self._resolve_command(query)
        if command is None:
            suggestions = self._suggestions(query)
            suggestion_text = f" Did you mean: {', '.join(f'`{name}`' for name in suggestions)}?" if suggestions else ""
            await ctx.send(f"No command named `{query}` was found.{suggestion_text}")
            return

        await self._send_command_help(ctx, command)

    async def _send_general_help(self, ctx: commands.Context) -> None:
        grouped: dict[str, list[commands.Command]] = {}
        for command in sorted(self.bot.commands, key=lambda cmd: cmd.name):
            if command.hidden:
                continue
            cog_name = command.cog_name or "General"
            grouped.setdefault(cog_name, []).append(command)

        base_description = "Use `!help <command>` for details on one command. Prefix commands are shown as `!`."
        fields: list[tuple[str, str, bool]] = []
        for cog_name in sorted(grouped):
            commands_in_cog = grouped[cog_name]
            lines = [f"`{self._signature(cmd)}` - {self._short_help(cmd)}" for cmd in commands_in_cog]
            for index, chunk in enumerate(split_embed_field_lines(lines), start=1):
                field_name = cog_name if index == 1 else f"{cog_name} (cont.)"
                fields.append((clip_embed_field_name(field_name), chunk, False))

        embeds = self._build_general_help_embeds(base_description=base_description, fields=fields)
        for embed in embeds:
            await ctx.send(embed=embed)

    @staticmethod
    def _build_general_help_embeds(
        *,
        base_description: str,
        fields: list[tuple[str, str, bool]],
    ) -> list[discord.Embed]:
        description = clip_embed_description(base_description)
        # Reserve room for optional page footer when output spans multiple embeds.
        page_char_budget = DISCORD_EMBED_TOTAL_CHAR_LIMIT - 32
        base_size = len("Bot Commands") + len(description)

        pages: list[list[tuple[str, str, bool]]] = []
        current: list[tuple[str, str, bool]] = []
        current_size = base_size

        for name, value, inline in fields:
            field_size = len(name) + len(value)
            if current and (
                len(current) >= DISCORD_EMBED_MAX_FIELDS
                or current_size + field_size > page_char_budget
            ):
                pages.append(current)
                current = []
                current_size = base_size

            current.append((name, value, inline))
            current_size += field_size

        if current:
            pages.append(current)
        if not pages:
            pages.append([])

        embeds: list[discord.Embed] = []
        total_pages = len(pages)
        for page_index, page_fields in enumerate(pages, start=1):
            title = "Bot Commands" if total_pages == 1 else f"Bot Commands ({page_index}/{total_pages})"
            embed = discord.Embed(
                title=title,
                description=description,
                colour=discord.Colour.blurple(),
            )
            for name, value, inline in page_fields:
                embed.add_field(name=name, value=value, inline=inline)
            if total_pages > 1:
                embed.set_footer(text=f"Page {page_index}/{total_pages}")
            embeds.append(embed)
        return embeds

    async def _send_command_help(self, ctx: commands.Context, command: commands.Command) -> None:
        embed = discord.Embed(
            title=f"Command: !{command.qualified_name}",
            colour=discord.Colour.blurple(),
            description=clip_embed_description(command.help or "No detailed description."),
        )
        embed.add_field(
            name="Usage",
            value=clip_text(f"`!{self._signature(command)}`", limit=DISCORD_EMBED_FIELD_VALUE_LIMIT),
            inline=False,
        )
        embed.add_field(
            name="Slash",
            value="Yes" if isinstance(command, commands.HybridCommand) else "No",
            inline=True,
        )

        aliases = ", ".join(f"`{alias}`" for alias in command.aliases) if command.aliases else "None"
        embed.add_field(name="Aliases", value=clip_text(aliases, limit=DISCORD_EMBED_FIELD_VALUE_LIMIT), inline=True)

        if isinstance(command, commands.Group):
            visible_subcommands = [sub for sub in command.commands if not sub.hidden]
            if visible_subcommands:
                lines = [f"`{sub.qualified_name}` - {self._short_help(sub)}" for sub in visible_subcommands]
                for index, chunk in enumerate(split_embed_field_lines(lines), start=1):
                    field_name = "Subcommands" if index == 1 else "Subcommands (cont.)"
                    embed.add_field(name=field_name, value=chunk, inline=False)

        await ctx.send(embed=embed)

    def _resolve_command(self, raw: str) -> commands.Command | None:
        token = raw.strip().lstrip("!/").lower()
        if not token:
            return None

        direct = self.bot.get_command(token)
        if direct is not None:
            return direct

        for command in self.bot.commands:
            if command.qualified_name.lower() == token:
                return command
            if any(alias.lower() == token for alias in command.aliases):
                return command
        return None

    def _suggestions(self, raw: str) -> list[str]:
        token = raw.strip().lstrip("!/").lower()
        if not token:
            return []
        names = sorted({command.qualified_name for command in self.bot.commands if not command.hidden})
        lower_map = {name.lower(): name for name in names}
        matches = get_close_matches(token, list(lower_map.keys()), n=3, cutoff=0.45)
        return [lower_map[match] for match in matches]

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
