from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Callable, Literal, Optional

import discord
from discord.ext import commands

from db.repository import VoiceFeatureRepository
from services.discord_output import send_context_lines_chunks, send_context_text_chunks, split_lines_chunks
from services.guild_config import GuildConfigService
from services.voice_service import VoiceService

if TYPE_CHECKING:
    from services.coach_secretary import CoachSecretaryBase
    from services.dynamic_voice import DynamicVoiceManager

_VOICE_SERVICE = VoiceService()
logger = logging.getLogger(__name__)
SoloRemovalAction = Literal["afk", "disconnect"]


def _hours(seconds: float) -> str:
    return _VOICE_SERVICE.hours(seconds)


def _rank_prefix(rank: int) -> str:
    return _VOICE_SERVICE.rank_prefix(rank)


def _is_solo_channel(channel: Optional[discord.abc.GuildChannel]) -> bool:
    return channel is not None and _VOICE_SERVICE.is_solo_channel_name(channel.name)


def _has_guest_role(member: discord.Member) -> bool:
    return any(role.name.strip().casefold() == "guest" for role in getattr(member, "roles", ()))


class WorkConfirmationResult(Enum):
    CONFIRMED = "confirmed"
    TIMED_OUT = "timed_out"
    DM_FAILED = "dm_failed"


class WorkConfirmationView(discord.ui.View):
    def __init__(self, member_id: int, timeout_seconds: int) -> None:
        super().__init__(timeout=timeout_seconds)
        self.member_id = member_id
        self.confirmed = False

    @discord.ui.button(label="Yes, still working", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
            return

        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Confirmed. Keeping your session active.", view=self)
        self.stop()


class VoiceLoggingCog(commands.Cog, name="VoiceLogging"):
    def __init__(
        self,
        bot: commands.Bot,
        repo: VoiceFeatureRepository,
        config_service: GuildConfigService,
        dynamic_voice: Optional[DynamicVoiceManager] = None,
        coach_secretary: Optional[CoachSecretaryBase] = None,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._config = config_service
        self._dynamic_voice = dynamic_voice
        self._coach_secretary = coach_secretary
        self._voice_service = VoiceService()
        self._watchdogs: dict[int, asyncio.Task[None]] = {}
        self._tracked_keywords_cache: dict[int, list[str]] = {}

    def cog_unload(self) -> None:
        for task in self._watchdogs.values():
            task.cancel()
        self._watchdogs.clear()

    async def _get_tracked_keywords(self, guild_id: int) -> list[str]:
        cached = self._tracked_keywords_cache.get(guild_id)
        if cached is not None:
            return cached
        raw = await self._config.get_text(guild_id, "voice_tracked_keywords")
        keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
        self._tracked_keywords_cache[guild_id] = keywords
        return keywords

    async def _is_tracked_channel(self, guild_id: int, channel: discord.VoiceChannel) -> bool:
        """Return True if channel counts for voice hours.

        A channel is tracked when any of these hold:
        - It is a solo channel (name starts with 'solo #' or 'solo room').
        - It is a dynamically-created channel (solo, duo, or team).
        - Its name contains a configured tracked keyword.

        The dynamic-channel check uses both the in-memory manager state (populated
        after ``rebuild_state`` completes) and a name-pattern fallback so that
        channels are correctly identified even during the ``on_ready`` window before
        ``DynamicVoiceCog.on_ready`` has finished rebuilding state.
        """
        if _is_solo_channel(channel):
            return True
        if self._dynamic_voice is not None and (
            self._dynamic_voice.is_tracked(channel.id)
            or self._dynamic_voice.is_dynamic_channel_name(channel.name)
        ):
            return True
        keywords = await self._get_tracked_keywords(guild_id)
        if not keywords:
            return False
        name_lower = channel.name.lower()
        return any(kw in name_lower for kw in keywords)

    async def _start_voice_session(
        self,
        *,
        guild_id: int,
        member_id: int,
        channel: discord.VoiceChannel,
        started_at: datetime,
    ) -> None:
        tracked = await self._is_tracked_channel(guild_id, channel)
        logger.info(
            "Starting voice session: member=%s channel=%s (%s) tracked=%s",
            member_id,
            channel.name,
            channel.id,
            tracked,
        )
        await self._repo.start_voice_session(
            guild_id=guild_id,
            discord_id=member_id,
            channel_id=channel.id,
            channel_name=channel.name,
            is_tracked=tracked,
            started_at=started_at,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        now = datetime.now(tz=UTC)
        for guild in self.bot.guilds:
            active_tracked_by_member: dict[int, discord.VoiceChannel] = {}
            for voice_channel in guild.voice_channels:
                is_tracked = await self._is_tracked_channel(guild.id, voice_channel)
                if not is_tracked:
                    continue
                for member in voice_channel.members:
                    if member.bot:
                        continue
                    active_tracked_by_member[member.id] = voice_channel

            active_member_ids = set(active_tracked_by_member)
            open_tracked_member_ids = await self._repo.get_open_tracked_voice_member_ids(guild.id)

            for stale_member_id in open_tracked_member_ids - active_member_ids:
                await self._repo.close_open_voice_sessions(guild.id, stale_member_id, now)

            for member_id, voice_channel in active_tracked_by_member.items():
                if member_id not in open_tracked_member_ids:
                    await self._start_voice_session(
                        guild_id=guild.id,
                        member_id=member_id,
                        channel=voice_channel,
                        started_at=now,
                    )

                # Watchdog only for strict solo channels
                if _is_solo_channel(voice_channel):
                    member = guild.get_member(member_id)
                    if member is not None:
                        self._start_watchdog(member)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if before.channel is after.channel:
            return

        now = datetime.now(tz=UTC)
        logger.debug(
            "Voice state change: member=%s before=%s after=%s",
            member.id,
            getattr(before.channel, "name", None),
            getattr(after.channel, "name", None),
        )
        await self._repo.close_open_voice_sessions(member.guild.id, member.id, now)
        self._stop_watchdog(member.id)

        if not isinstance(after.channel, discord.VoiceChannel):
            return

        await self._start_voice_session(
            guild_id=member.guild.id,
            member_id=member.id,
            channel=after.channel,
            started_at=now,
        )
        if _is_solo_channel(after.channel):
            self._start_watchdog(member)

    def _start_watchdog(self, member: discord.Member) -> None:
        self._stop_watchdog(member.id)
        self._watchdogs[member.id] = asyncio.create_task(
            self._watchdog_loop(member.guild.id, member.id),
            name=f"solo-watchdog-{member.id}",
        )

    def _stop_watchdog(self, member_id: int) -> None:
        task = self._watchdogs.pop(member_id, None)
        if task is not None:
            task.cancel()

    def _clear_watchdog_if_current(self, member_id: int, task: asyncio.Task[None]) -> None:
        if self._watchdogs.get(member_id) is task:
            self._watchdogs.pop(member_id, None)

    async def _resolve_handles(self, guild: discord.Guild) -> dict[int, str]:
        verified = await self._repo.get_all(guild.id)
        handle_by_discord_id = {row.discord_id: row.cf_handle for row in verified}
        return handle_by_discord_id

    def _parse_window_tokens(
        self,
        *,
        now: datetime,
        tokens: tuple[str, ...],
    ) -> tuple[Optional[datetime], str]:
        return self._voice_service.parse_window_tokens(now=now, tokens=tokens)

    @staticmethod
    def _sorted_totals(totals: dict[int, float]) -> list[tuple[int, float]]:
        return _VOICE_SERVICE.sorted_totals(totals)

    def _leaderboard_lines(
        self,
        *,
        guild: discord.Guild,
        totals: dict[int, float],
        handle_by_discord_id: dict[int, str],
    ) -> list[str]:
        return self._voice_service.leaderboard_lines(
            totals=totals,
            handle_by_discord_id=handle_by_discord_id,
            display_name_lookup=lambda discord_id: (
                member.display_name if (member := guild.get_member(discord_id)) else None
            ),
        )

    @staticmethod
    def _render_ranked_message(*, title: str, lines: list[str], max_lines: int, overflow_label: str) -> str:
        return _VOICE_SERVICE.render_ranked_message(
            title=title,
            lines=lines,
            max_lines=max_lines,
            overflow_label=overflow_label,
        )

    @staticmethod
    def _find_rank(totals: dict[int, float], discord_id: int) -> Optional[tuple[int, float]]:
        return _VOICE_SERVICE.find_rank(totals, discord_id)

    @staticmethod
    def _severity_index(*, minimum_hours: float, worked_hours: float) -> int:
        return _VOICE_SERVICE.severity_index(minimum_hours=minimum_hours, worked_hours=worked_hours)

    @staticmethod
    def _pick_scolding(*, minimum_hours: float, worked_hours: float) -> str:
        return _VOICE_SERVICE.pick_scolding(minimum_hours=minimum_hours, worked_hours=worked_hours)

    async def _training_members(
        self,
        guild: discord.Guild,
    ) -> tuple[str, list[discord.Role], list[discord.Member]]:
        training_substring = await self._config.get_text(guild.id, "training_role_substring")
        training_key = training_substring.casefold().strip()
        matching_roles = [
            role
            for role in guild.roles
            if training_key
            and training_key in role.name.casefold()
            and role.name != "@everyone"
        ]

        members_by_id: dict[int, discord.Member] = {}
        for role in matching_roles:
            for member in role.members:
                if getattr(member, "bot", False) or _has_guest_role(member):
                    continue
                members_by_id[member.id] = member

        members = sorted(
            members_by_id.values(),
            key=lambda member: (member.display_name.casefold(), member.id),
        )
        return training_substring, matching_roles, members

    @staticmethod
    def _label_lookup(
        guild: discord.Guild,
        handle_by_discord_id: dict[int, str],
    ) -> Callable[[int], Optional[str]]:
        def lookup(discord_id: int) -> Optional[str]:
            handle = handle_by_discord_id.get(discord_id)
            if handle is not None:
                return handle
            member = guild.get_member(discord_id)
            if member is not None:
                return member.display_name
            return None

        return lookup

    @staticmethod
    def _timesheet_usage() -> str:
        return (
            "Usage:\n"
            "`!timesheet last <days> days`\n"
            "`!timesheet top <limit> max <day/week/month/range> ...`\n"
            "`!timesheet max <day/week/month/range> ... top <limit>`\n"
            "`!timesheet max day last <amount> <day/week/month>`\n"
            "`!timesheet max week last <amount> <week/month>`\n"
            "`!timesheet max month last <amount> months`\n"
            "`!timesheet max range <amount> <hour/day/week/month> last <lookback_amount> <hour/day/week/month>`"
        )

    @staticmethod
    def _parse_top_limit(raw: str) -> int:
        try:
            limit = int(raw)
        except ValueError as exc:
            raise ValueError("`limit` must be a positive integer.") from exc
        if limit <= 0:
            raise ValueError("`limit` must be greater than 0.")
        return min(limit, 100)

    @classmethod
    def _split_trailing_top_limit(
        cls,
        args: tuple[str, ...],
        *,
        existing_limit: Optional[int] = None,
    ) -> tuple[tuple[str, ...], Optional[int]]:
        if len(args) < 2 or args[-2].strip().lower() != "top":
            return args, existing_limit
        if existing_limit is not None:
            raise ValueError("`top` limit can appear before or after `max`, not both.")
        return args[:-2], cls._parse_top_limit(args[-1])

    async def _send_training_timesheet(
        self,
        ctx: commands.Context,
        args: tuple[str, ...],
        *,
        config: object,
        now: datetime,
        handle_by_discord_id: dict[int, str],
    ) -> None:
        assert ctx.guild is not None
        try:
            days = self._voice_service.parse_timesheet_days(args)
        except ValueError as err:
            await ctx.send(str(err))
            return

        training_substring, matching_roles, members = await self._training_members(ctx.guild)
        if not matching_roles:
            await ctx.send(
                f"No role found matching training substring `{training_substring}`.\n"
                "Use `!config text set training_role_substring <value>` to adjust it."
            )
            return
        if not members:
            await ctx.send(f"No non-guest members found in roles matching `{training_substring}`.")
            return

        window = self._voice_service.timesheet_window(now=now, days=days)
        intervals = await self._repo.get_tracked_voice_intervals(
            ctx.guild.id,
            since=window.since_utc,
            now=window.until_utc,
        )
        report = self._voice_service.build_timesheet_report(
            intervals=intervals,
            user_ids=[member.id for member in members],
            now=now,
            days=days,
        )
        lines = self._voice_service.timesheet_lines(
            report=report,
            label_lookup=self._label_lookup(ctx.guild, handle_by_discord_id),
        )
        content = self._render_ranked_message(
            title=f"**Training Voice Timesheet ({report.window.label}, Egypt day 05:00)**",
            lines=lines,
            max_lines=config.voicehours_max_lines,
            overflow_label="users",
        )
        await send_context_text_chunks(ctx, content)

    async def _send_training_max_report(
        self,
        ctx: commands.Context,
        args: tuple[str, ...],
        *,
        config: object,
        now: datetime,
        handle_by_discord_id: dict[int, str],
        max_lines: Optional[int] = None,
    ) -> None:
        assert ctx.guild is not None
        try:
            args, max_lines = self._split_trailing_top_limit(args, existing_limit=max_lines)
            request = self._voice_service.parse_max_tokens(args)
        except ValueError as err:
            await ctx.send(str(err))
            return

        training_substring, matching_roles, members = await self._training_members(ctx.guild)
        if not matching_roles:
            await ctx.send(
                f"No role found matching training substring `{training_substring}`.\n"
                "Use `!config text set training_role_substring <value>` to adjust it."
            )
            return
        if not members:
            await ctx.send(f"No non-guest members found in roles matching `{training_substring}`.")
            return

        since_utc, until_utc = self._voice_service.max_lookback_window(now=now, request=request)
        intervals = await self._repo.get_tracked_voice_intervals(
            ctx.guild.id,
            since=since_utc,
            now=until_utc,
        )
        report = self._voice_service.build_max_report(
            intervals=intervals,
            user_ids=[member.id for member in members],
            now=now,
            request=request,
        )
        lines = self._voice_service.max_report_lines(
            report=report,
            label_lookup=self._label_lookup(ctx.guild, handle_by_discord_id),
        )
        title = (
            f"**Max Tracked Voice {request.mode.title()} "
            f"({request.window_label}, {request.lookback_label}, Egypt time)**"
        )
        content = self._render_ranked_message(
            title=title,
            lines=lines,
            max_lines=max_lines if max_lines is not None else config.voicehours_max_lines,
            overflow_label="users",
        )
        await send_context_text_chunks(ctx, content)

    async def _handle_timesheet_command(
        self,
        ctx: commands.Context,
        args: tuple[str, ...],
        *,
        config: object,
        now: datetime,
        handle_by_discord_id: dict[int, str],
    ) -> None:
        if not args:
            await ctx.send(self._timesheet_usage())
            return

        mode = args[0].strip().lower()
        if mode == "last":
            await self._send_training_timesheet(
                ctx,
                args,
                config=config,
                now=now,
                handle_by_discord_id=handle_by_discord_id,
            )
            return
        if mode == "max":
            await self._send_training_max_report(
                ctx,
                args[1:],
                config=config,
                now=now,
                handle_by_discord_id=handle_by_discord_id,
            )
            return
        if mode == "top":
            if len(args) < 3 or args[2].strip().lower() != "max":
                await ctx.send(self._timesheet_usage())
                return
            try:
                limit = self._parse_top_limit(args[1])
            except ValueError as err:
                await ctx.send(str(err))
                return
            await self._send_training_max_report(
                ctx,
                args[3:],
                config=config,
                now=now,
                handle_by_discord_id=handle_by_discord_id,
                max_lines=limit,
            )
            return

        await ctx.send(self._timesheet_usage())

    async def _watchdog_loop(self, guild_id: int, member_id: int) -> None:
        try:
            while True:
                config = await self._config.get(guild_id)
                await asyncio.sleep(config.voice_check_interval_seconds)

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    break

                member = guild.get_member(member_id)
                if member is None:
                    break

                voice_channel = member.voice.channel if member.voice else None
                if not _is_solo_channel(voice_channel):
                    break

                confirmation = await self._ask_still_working(member, config.voice_confirm_timeout_seconds)
                if confirmation is WorkConfirmationResult.CONFIRMED:
                    continue

                if confirmation is WorkConfirmationResult.DM_FAILED:
                    await self._notify_watchdog_dm_failure(guild, member)

                removal_action: SoloRemovalAction | None = None
                if member.voice is not None:
                    removal_action = await self._move_member_out_of_solo_check(member)

                if removal_action is None:
                    if confirmation is WorkConfirmationResult.TIMED_OUT:
                        try:
                            await member.send(
                                "You did not confirm in time. I could not move you to AFK or disconnect you due to missing permissions."
                            )
                        except discord.Forbidden:
                            pass
                    continue

                await self._repo.close_open_voice_sessions(guild.id, member.id, datetime.now(tz=UTC))
                if confirmation is WorkConfirmationResult.TIMED_OUT:
                    try:
                        if removal_action == "afk":
                            await member.send("You were moved to AFK because you did not confirm in time.")
                        else:
                            await member.send("You were disconnected because you did not confirm in time.")
                    except discord.Forbidden:
                        pass
                break
        except asyncio.CancelledError:
            pass
        finally:
            task = asyncio.current_task()
            if task is not None:
                self._clear_watchdog_if_current(member_id, task)

    @staticmethod
    async def _move_member_out_of_solo_check(member: discord.Member) -> SoloRemovalAction | None:
        target_channel = member.guild.afk_channel
        if target_channel is not None:
            try:
                await member.move_to(target_channel, reason="Failed or missed solo-channel work check")
                return "afk"
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            await member.move_to(None, reason="Failed or missed solo-channel work check")
            return "disconnect"
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _ask_still_working(self, member: discord.Member, timeout_seconds: int) -> WorkConfirmationResult:
        view = WorkConfirmationView(member.id, timeout_seconds)
        timeout_minutes = max(1, timeout_seconds // 60)
        prompt = (
            "Are you still working in the solo channel?\n"
            f"Click **Yes, still working** within {timeout_minutes} minutes to keep your session."
        )
        try:
            message = await member.send(prompt, view=view)
        except (discord.Forbidden, discord.HTTPException):
            return WorkConfirmationResult.DM_FAILED

        await view.wait()
        if view.confirmed:
            return WorkConfirmationResult.CONFIRMED

        for child in view.children:
            child.disabled = True
        try:
            await message.edit(content="No response received in time.", view=view)
        except discord.HTTPException:
            pass
        return WorkConfirmationResult.TIMED_OUT

    async def _resolve_watchdog_alert_recipient(self, guild: discord.Guild) -> Optional[discord.Member]:
        if self._coach_secretary is not None:
            try:
                config = await self._coach_secretary.get_config(guild.id)
            except Exception:
                logger.exception("Failed loading coach config for solo watchdog alert in guild %s", guild.id)
                config = None
            if config is not None:
                coach = guild.get_member(config.coach_id)
                if coach is not None and not getattr(coach, "bot", False):
                    return coach

        for member in getattr(guild, "members", ()):
            if getattr(member, "bot", False):
                continue
            names = (
                getattr(member, "name", ""),
                getattr(member, "display_name", ""),
                getattr(member, "global_name", ""),
            )
            if any(isinstance(name, str) and name.casefold() == "__reda" for name in names):
                return member
        return None

    async def _notify_watchdog_dm_failure(self, guild: discord.Guild, member: discord.Member) -> None:
        recipient = await self._resolve_watchdog_alert_recipient(guild)
        if recipient is None:
            logger.warning("No coach or __reda fallback found for solo watchdog alert in guild %s", guild.id)
            return

        guild_name = getattr(guild, "name", str(guild.id))
        member_name = getattr(member, "display_name", str(member.id))
        try:
            await recipient.send(
                f"I could not DM {member_name} ({member.id}) for the solo-channel work check "
                f"in {guild_name}. Disconnecting them and closing the session."
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Failed sending solo watchdog alert to %s in guild %s",
                getattr(recipient, "id", "unknown"),
                guild.id,
            )

    @commands.command(name="timesheet")
    @commands.guild_only()
    async def timesheet(self, ctx: commands.Context, *args: str) -> None:
        """Show training-role tracked voice timesheets and max voice-hour windows."""
        assert ctx.guild is not None
        config = await self._config.get(ctx.guild.id)
        now = datetime.now(tz=UTC)
        handle_by_discord_id = await self._resolve_handles(ctx.guild)
        await self._handle_timesheet_command(
            ctx,
            args,
            config=config,
            now=now,
            handle_by_discord_id=handle_by_discord_id,
        )

    @commands.command(name="voicehours", aliases=["solohours"])
    @commands.guild_only()
    async def voicehours(self, ctx: commands.Context, *args: str) -> None:
        """Show tracked voice-channel hours.

        Hours are counted for channels whose names start with ``solo #`` as well
        as any channels whose names contain a keyword configured via
        ``!voicehours track add``.

        Usage:
        - `!voicehours`
        - `!voicehours last <x> <hour/day/week/month>`
        - `!voicehours tahzeeq <x> [last <x> <hour/day/week/month>]`
        - `!voicehours me [last <x> <hour/day/week/month>]`
        - `!voicehours user <@member> [last <x> <hour/day/week/month>]`
        - `!voicehours role <@role> [last <x> <hour/day/week/month>]`
        - `!voicehours roles [last <x> <hour/day/week/month>]`  (alias: teams)
        - `!voicehours unis [last <x> <hour/day/week/month>]`
        - `!voicehours top [limit] [last <x> <hour/day/week/month>]`
        - `!voicehours timesheet last <days> days`
        - `!voicehours max <day/week/month/range> ...`
        - `!voicehours top <limit> max <day/week/month/range> ...`
        - `!voicehours max <day/week/month/range> ... top <limit>`
        - `!voicehours track list`
        - `!voicehours track add <keyword>`
        - `!voicehours track remove <keyword>`
        """
        assert ctx.guild is not None

        config = await self._config.get(ctx.guild.id)
        now = datetime.now(tz=UTC)
        handle_by_discord_id = await self._resolve_handles(ctx.guild)

        if args:
            mode = args[0].lower()

            if mode == "timesheet":
                await self._handle_timesheet_command(
                    ctx,
                    args[1:],
                    config=config,
                    now=now,
                    handle_by_discord_id=handle_by_discord_id,
                )
                return

            if mode == "max":
                await self._handle_timesheet_command(
                    ctx,
                    args,
                    config=config,
                    now=now,
                    handle_by_discord_id=handle_by_discord_id,
                )
                return

            if mode == "top":
                limit = config.voicehours_max_lines
                remaining = args[1:]
                parsed_explicit_limit = False
                limit_token: Optional[str] = None
                if remaining and remaining[0].isdigit():
                    limit_token = remaining[0]
                    limit = max(1, min(int(limit_token), 100))
                    remaining = remaining[1:]
                    parsed_explicit_limit = True
                elif remaining and remaining[0].strip().lower() == "max":
                    await ctx.send("Usage: `!voicehours top <limit> max <day/week/month/range> ...`")
                    return
                elif len(remaining) >= 2 and remaining[1].strip().lower() == "max":
                    await ctx.send("`limit` must be a positive integer.")
                    return

                if parsed_explicit_limit and remaining and remaining[0].strip().lower() == "max":
                    try:
                        limit = self._parse_top_limit(limit_token or "")
                    except ValueError as err:
                        await ctx.send(str(err))
                        return
                    await self._send_training_max_report(
                        ctx,
                        remaining[1:],
                        config=config,
                        now=now,
                        handle_by_discord_id=handle_by_discord_id,
                        max_lines=limit,
                    )
                    return

                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(remaining))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                if not totals:
                    await ctx.send("No tracked-channel voice logs found for that period.")
                    return

                lines = self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                content = self._render_ranked_message(
                    title=f"**Top Tracked Voice Hours ({label})**",
                    lines=lines,
                    max_lines=limit,
                    overflow_label="users",
                )
                rank_data = self._find_rank(totals, ctx.author.id)
                if rank_data is not None:
                    rank, seconds = rank_data
                    content = f"{content}\nYour rank: **#{rank}** with **{_hours(seconds)}**"
                await send_context_text_chunks(ctx, content)
                return

            if mode == "tahzeeq":
                if len(args) < 2:
                    await ctx.send("Usage: `!voicehours tahzeeq <x> [last <x> <hour/day/week/month>]`")
                    return
                try:
                    minimum_hours = float(args[1])
                except ValueError:
                    await ctx.send("`x` must be a number (hours).")
                    return
                if minimum_hours <= 0:
                    await ctx.send("`x` must be greater than 0.")
                    return

                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[2:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                training_substring, matching_roles, members = await self._training_members(ctx.guild)
                if not matching_roles:
                    await ctx.send(
                        f"No role found matching training substring `{training_substring}`.\n"
                        "Use `!config text set training_role_substring <value>` to adjust it."
                    )
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                threshold_seconds = minimum_hours * 3600.0
                if not members:
                    await ctx.send(
                        f"No non-guest members found in roles matching `{training_substring}`."
                    )
                    return

                below: list[tuple[discord.Member, float]] = []
                for member in members:
                    worked = totals.get(member.id, 0.0)
                    if worked < threshold_seconds:
                        below.append((member, worked))

                if not below:
                    await ctx.send(
                        f"Everyone in `{training_substring}` roles met the target: "
                        f"**{minimum_hours:.2f}h** in {label}."
                    )
                    return

                below.sort(key=lambda item: item[1])
                lines = [
                    f"**Tahzeeq Report ({label})**",
                    f"Target: **{minimum_hours:.2f}h** | "
                    f"Training substring: `{training_substring}` | Matching roles: {len(matching_roles)}",
                    "",
                ]
                for member, worked in below:
                    worked_hours = worked / 3600.0
                    deficit = max(0.0, minimum_hours - worked_hours)
                    scolding = self._pick_scolding(minimum_hours=minimum_hours, worked_hours=worked_hours)
                    lines.append(
                        f"{member.mention} - worked {_hours(worked)} (short by {deficit:.2f}h). "
                        f"{scolding}"
                    )

                await send_context_lines_chunks(ctx, lines)
                return

            if mode == "me":
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[1:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                rank_data = self._find_rank(totals, ctx.author.id)
                if rank_data is None:
                    await ctx.send(f"You have no tracked voice logs for {label}.")
                    return
                rank, seconds = rank_data
                await ctx.send(f"**{ctx.author.display_name}** in {label}: **{_hours(seconds)}** (rank **#{rank}**)")
                return

            if mode == "user":
                if len(args) < 2:
                    await ctx.send("Usage: `!voicehours user <@member> [last <x> <hour/day/week/month>]`")
                    return
                try:
                    target_member = await commands.MemberConverter().convert(ctx, args[1])
                except commands.BadArgument:
                    await ctx.send("Could not resolve that member.")
                    return
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[2:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                seconds = totals.get(target_member.id, 0.0)
                rank_data = self._find_rank(totals, target_member.id)
                rank_text = f" (rank **#{rank_data[0]}**)" if rank_data is not None else ""
                await ctx.send(f"**{target_member.display_name}** in {label}: **{_hours(seconds)}**{rank_text}")
                return

            if mode == "role":
                if len(args) < 2:
                    await ctx.send("Usage: `!voicehours role <@role> [last <x> <hour/day/week/month>]`")
                    return
                try:
                    target_role = await commands.RoleConverter().convert(ctx, args[1])
                except commands.BadArgument:
                    await ctx.send("Could not resolve that role.")
                    return
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[2:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                all_totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                member_ids = {member.id for member in target_role.members if not member.bot}
                totals = {discord_id: value for discord_id, value in all_totals.items() if discord_id in member_ids}
                if not totals:
                    await ctx.send(f"No tracked voice logs found for role {target_role.mention} in {label}.")
                    return

                lines = self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                await send_context_text_chunks(
                    ctx,
                    self._render_ranked_message(
                        title=f"**Tracked Voice Hours for {target_role.name} ({label})**",
                        lines=lines,
                        max_lines=config.voicehours_max_lines,
                        overflow_label="users",
                    ),
                )
                return

            if mode in {"roles", "teams"}:
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[1:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                team_roles = [
                    role
                    for role in ctx.guild.roles
                    if role.name.lower().startswith("team ") and role.name != "@everyone"
                ]
                if not team_roles:
                    await ctx.send("No roles starting with `team ` (case-insensitive) were found in this server.")
                    return

                role_totals: list[tuple[str, float, int]] = []
                for role in team_roles:
                    non_bot_members = [member for member in role.members if not member.bot]
                    seconds_sum = 0.0
                    for member in non_bot_members:
                        seconds_sum += totals.get(member.id, 0.0)
                    role_totals.append((role.name, seconds_sum, len(non_bot_members)))

                role_totals.sort(key=lambda item: (-item[1], item[0].lower()))

                lines = ["rk   role                                 total    members"]
                for index, (role_name, seconds, member_count) in enumerate(role_totals, start=1):
                    lines.append(f"{_rank_prefix(index):<4} {role_name:<36.36} {_hours(seconds):<8} {member_count}")

                await send_context_text_chunks(
                    ctx,
                    self._render_ranked_message(
                        title=f"**Team Role Voice Standings ({label})**",
                        lines=lines,
                        max_lines=config.voicehours_max_lines,
                        overflow_label="roles",
                    ),
                )
                return

            if mode == "unis":
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args[1:]))
                except ValueError as err:
                    await ctx.send(str(err))
                    return

                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                uni_roles = [
                    role
                    for role in ctx.guild.roles
                    if "uni" in role.name.lower() and role.name != "@everyone"
                ]
                if not uni_roles:
                    await ctx.send("No roles containing `uni` (case-insensitive) were found in this server.")
                    return

                role_totals: list[tuple[str, float, int]] = []
                for role in uni_roles:
                    non_bot_members = [member for member in role.members if not member.bot]
                    seconds_sum = 0.0
                    for member in non_bot_members:
                        seconds_sum += totals.get(member.id, 0.0)
                    role_totals.append((role.name, seconds_sum, len(non_bot_members)))

                role_totals.sort(key=lambda item: (-item[1], item[0].lower()))

                lines = ["rk   role               total    members"]
                for index, (role_name, seconds, member_count) in enumerate(role_totals, start=1):
                    lines.append(f"{_rank_prefix(index):<4} {role_name:<18.18} {_hours(seconds):<8} {member_count}")

                await send_context_text_chunks(
                    ctx,
                    self._render_ranked_message(
                        title=f"**Uni Role Voice Standings ({label})**",
                        lines=lines,
                        max_lines=config.voicehours_max_lines,
                        overflow_label="unis",
                    ),
                )
                return

            if mode == "last":
                try:
                    since, label = self._parse_window_tokens(now=now, tokens=tuple(args))
                except ValueError as err:
                    await ctx.send(str(err))
                    return
                totals = await self._repo.get_tracked_voice_totals(ctx.guild.id, now=now, since=since)
                if not totals:
                    await ctx.send("No tracked-channel voice logs found for that period.")
                    return
                lines = self._leaderboard_lines(guild=ctx.guild, totals=totals, handle_by_discord_id=handle_by_discord_id)
                await send_context_text_chunks(
                    ctx,
                    self._render_ranked_message(
                        title=f"**Tracked Voice Hours ({label})**",
                        lines=lines,
                        max_lines=config.voicehours_max_lines,
                        overflow_label="users",
                    ),
                )
                return

            if mode == "track":
                await self._handle_track(ctx, args[1:])
                return

            await ctx.send(
                "Unknown mode.\n"
                "Use one of: `last`, `tahzeeq`, `me`, `user`, `role`, `roles`, `teams`, `unis`, "
                "`top`, `timesheet`, `max`, `track`.\n"
                "Example: `!voicehours top 20 last 2 weeks`"
            )
            return

        week_since = now - timedelta(days=7)
        month_since = now - timedelta(days=30)
        summary = await self._repo.get_tracked_voice_summary(
            ctx.guild.id,
            now=now,
            week_since=week_since,
            month_since=month_since,
        )
        week = {discord_id: row["week"] for discord_id, row in summary.items()}
        month = {discord_id: row["month"] for discord_id, row in summary.items()}
        all_time = {discord_id: row["all_time"] for discord_id, row in summary.items()}

        if not all_time and not week and not month:
            await ctx.send("No tracked-channel voice logs found yet.")
            return

        all_ids = set(week) | set(month) | set(all_time)
        ordered_ids = sorted(all_ids, key=lambda user_id: all_time.get(user_id, 0.0), reverse=True)
        lines: list[str] = []
        for discord_id in ordered_ids:
            handle = handle_by_discord_id.get(discord_id)
            if handle is None:
                member = ctx.guild.get_member(discord_id)
                handle = member.display_name if member else str(discord_id)
            rank = _rank_prefix(len(lines) + 1)
            lines.append(
                f"{rank:<4} `{handle}` | {_hours(week.get(discord_id, 0.0))} | "
                f"{_hours(month.get(discord_id, 0.0))} | {_hours(all_time.get(discord_id, 0.0))}"
            )

        message_lines = ["**Rank | Handle | Last Week | Last Month | All Time**"]
        displayed_lines = lines[: config.voicehours_max_lines]
        message_lines.extend(displayed_lines)
        extra = len(lines) - len(displayed_lines)
        if extra > 0:
            message_lines.append(f"... and {extra} more users")
        rank_data = self._find_rank(all_time, ctx.author.id)
        if rank_data is not None:
            rank, seconds = rank_data
            message_lines.append(f"Your all-time rank: **#{rank}** with **{_hours(seconds)}**")
        for chunk in split_lines_chunks(message_lines):
            await ctx.send(chunk)

    # ── track subcommand logic ──────────────────────────────────

    async def _handle_track(self, ctx: commands.Context, args: tuple[str, ...]) -> None:
        """Handle `!voicehours track add|remove|list`."""
        assert ctx.guild is not None

        if not args:
            await ctx.send(
                "Usage:\n"
                "`!voicehours track list`\n"
                "`!voicehours track add <keyword>`\n"
                "`!voicehours track remove <keyword>`"
            )
            return

        action = args[0].lower()

        if action == "list":
            raw = await self._config.get_text(ctx.guild.id, "voice_tracked_keywords")
            keywords = [k.strip() for k in raw.split(",") if k.strip()]
            if not keywords:
                await ctx.send(
                    "No tracked keywords configured. Only `solo #` channels count.\n"
                    "Use `!voicehours track add <keyword>` to add one."
                )
                return
            listing = "\n".join(f"• `{kw}`" for kw in keywords)
            await ctx.send(
                f"**Tracked keywords** (channels containing any of these count in voice hours):\n"
                f"{listing}\n\n`solo #` channels are always tracked."
            )
            return

        if action == "add":
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("You need Administrator permission for this.")
                return
            if len(args) < 2:
                await ctx.send("Usage: `!voicehours track add <keyword>`")
                return
            keyword = " ".join(args[1:]).strip().lower()
            if not keyword:
                await ctx.send("Keyword cannot be empty.")
                return

            raw = await self._config.get_text(ctx.guild.id, "voice_tracked_keywords")
            existing = [k.strip().lower() for k in raw.split(",") if k.strip()]
            if keyword in existing:
                await ctx.send(f"`{keyword}` is already tracked.")
                return

            existing.append(keyword)
            await self._config.set_text(ctx.guild.id, "voice_tracked_keywords", ",".join(existing))
            self._tracked_keywords_cache.pop(ctx.guild.id, None)
            await ctx.send(f"Added `{keyword}`. Channels containing it will now count in voice hours.")
            return

        if action == "remove":
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("You need Administrator permission for this.")
                return
            if len(args) < 2:
                await ctx.send("Usage: `!voicehours track remove <keyword>`")
                return
            keyword = " ".join(args[1:]).strip().lower()

            raw = await self._config.get_text(ctx.guild.id, "voice_tracked_keywords")
            existing = [k.strip().lower() for k in raw.split(",") if k.strip()]
            if keyword not in existing:
                await ctx.send(f"`{keyword}` is not currently tracked.")
                return

            existing.remove(keyword)
            if existing:
                await self._config.set_text(ctx.guild.id, "voice_tracked_keywords", ",".join(existing))
            else:
                await self._config.reset_text(ctx.guild.id, "voice_tracked_keywords")
            self._tracked_keywords_cache.pop(ctx.guild.id, None)
            await ctx.send(f"Removed `{keyword}` from tracked keywords.")
            return

        await ctx.send(
            "Usage:\n"
            "`!voicehours track list`\n"
            "`!voicehours track add <keyword>`\n"
            "`!voicehours track remove <keyword>`"
        )


async def setup(bot: commands.Bot) -> None:
    dynamic_voice: Optional[DynamicVoiceManager] = getattr(bot, "dynamic_voice", None)
    coach_secretary: Optional[CoachSecretaryBase] = getattr(bot, "coach_secretary", None)
    await bot.add_cog(
        VoiceLoggingCog(
            bot,
            getattr(bot, "user_repo"),
            getattr(bot, "guild_config"),
            dynamic_voice=dynamic_voice,
            coach_secretary=coach_secretary,
        )
    )

