from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord.ext import commands

from db.repository import UserRepositoryBase, WatchJob
from services.cf_client import CodeforcesClient
from services.contest_prediction import ContestPredictionResult, ContestPredictionService
from services.rating_predictor import RatingPredictor

logger = logging.getLogger(__name__)

MAX_PREDICTION_ROWS = 40
DEFAULT_WATCH_INTERVAL_MINUTES = max(1, int(os.getenv("DEFAULT_WATCH_INTERVAL_MINUTES", "5")))


def _format_delta(delta: int) -> str:
    return f"{delta:+d}"


def _parse_handles(raw: str) -> list[str]:
    handles = []
    for chunk in raw.replace(",", " ").split():
        cleaned = chunk.strip()
        if cleaned:
            handles.append(cleaned)
    return handles


class CFPredictorCog(commands.Cog, name="CFPredictor"):
    def __init__(self, bot: commands.Bot, repo: UserRepositoryBase, cf: CodeforcesClient) -> None:
        self.bot = bot
        self._repo = repo
        self._cf = cf
        self._service = ContestPredictionService(cf, repo, RatingPredictor())
        self._watch_tasks: dict[tuple[int, int, int], asyncio.Task[None]] = {}
        self._watch_bootstrapped = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._watch_bootstrapped:
            return
        self._watch_bootstrapped = True
        jobs = await self._repo.get_enabled_watch_jobs()
        for job in jobs:
            self._start_watch_task(job)

    def cog_unload(self) -> None:
        for task in self._watch_tasks.values():
            task.cancel()
        self._watch_tasks.clear()

    async def _build_prediction_result(
        self,
        *,
        guild_id: int,
        contest_id: int,
        server_only: bool,
        show_unofficial: bool,
        handles_filter: Optional[set[str]],
    ) -> Optional[ContestPredictionResult]:
        return await self._service.build_predictions(
            contest_id=contest_id,
            guild_id=guild_id,
            handles_filter=handles_filter,
            server_only=server_only,
            show_unofficial=show_unofficial,
        )

    async def _send_prediction_embed(self, ctx: commands.Context, result: ContestPredictionResult, *, title: str) -> None:
        rows = result.predictions[:MAX_PREDICTION_ROWS]
        if not rows:
            await ctx.send("No matching participants were found for this prediction query.")
            return

        lines = ["rank handle rating delta new perf pts pen type"]
        for row in rows:
            lines.append(
                f"{row.rank:<4} {row.handle:<16} {row.current_rating:<5} {_format_delta(row.delta):<5} "
                f"{row.new_rating:<5} {row.performance:<5} {row.points:<4.1f} {row.penalty:<4} {row.participant_type}"
            )

        embed = discord.Embed(
            title=title,
            description=f"Contest: **{result.contest_name}** (`{result.contest_id}`)\nPhase: `{result.phase}`",
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="Predictions", value=f"```text\n" + "\n".join(lines) + "\n```", inline=False)
        if len(result.predictions) > len(rows):
            embed.set_footer(text=f"Showing first {len(rows)} of {len(result.predictions)} rows")
        else:
            embed.set_footer(text="Approximate deltas; official Codeforces changes can differ")
        await ctx.send(embed=embed)

    def _cf_failure_message(self, fallback: str) -> str:
        err = self._cf.last_error
        if err is None:
            return fallback
        return (
            f"{fallback}\n"
            f"`urlrequested: {err.requested_url}`\n"
            f"`detail: {err.detail}`"
        )

    def _start_watch_task(self, job: WatchJob) -> None:
        key = (job.guild_id, job.channel_id, job.contest_id)
        existing = self._watch_tasks.get(key)
        if existing is not None:
            existing.cancel()

        self._watch_tasks[key] = asyncio.create_task(self._watch_loop(job), name=f"cf-watch-{job.contest_id}-{job.channel_id}")

    async def _watch_loop(self, initial_job: WatchJob) -> None:
        key = (initial_job.guild_id, initial_job.channel_id, initial_job.contest_id)
        try:
            while True:
                job = await self._repo.get_watch_job(initial_job.guild_id, initial_job.channel_id, initial_job.contest_id)
                if job is None or not job.enabled:
                    break

                channel = self.bot.get_channel(job.channel_id)
                if not isinstance(channel, discord.TextChannel):
                    await asyncio.sleep(job.interval_minutes * 60)
                    continue

                result = await self._build_prediction_result(
                    guild_id=job.guild_id,
                    contest_id=job.contest_id,
                    server_only=job.server_only,
                    show_unofficial=job.show_unofficial,
                    handles_filter=None,
                )
                if result is not None and result.predictions:
                    content_lines = [
                        f"CF watch update for **{result.contest_name}** (`{job.contest_id}`)",
                        "```text",
                        "rank handle rating delta new",
                    ]
                    for row in result.predictions[:20]:
                        content_lines.append(
                            f"{row.rank:<4} {row.handle:<16} {row.current_rating:<5} {_format_delta(row.delta):<5} {row.new_rating:<5}"
                        )
                    content_lines.append("```")
                    content = "\n".join(content_lines)

                    message: Optional[discord.Message] = None
                    if job.message_id is not None:
                        try:
                            message = await channel.fetch_message(job.message_id)
                        except discord.HTTPException:
                            message = None

                    if message is None:
                        sent = await channel.send(content)
                        await self._repo.set_watch_job_message_id(job.guild_id, job.channel_id, job.contest_id, sent.id)
                    else:
                        await message.edit(content=content)

                await asyncio.sleep(job.interval_minutes * 60)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("CF watch loop failed for key=%s", key)
        finally:
            self._watch_tasks.pop(key, None)

    @commands.hybrid_command(name="cf-link")
    @commands.guild_only()
    async def cf_link(self, ctx: commands.Context, handle: str) -> None:
        """Use verify flow instead of separate linking for predictions."""
        await ctx.send(
            "Predictions use verified handles from `!verify`.\n"
            "Run `!verify <handle>` then `!confirm`."
        )

    @commands.hybrid_command(name="cf-unlink")
    @commands.guild_only()
    async def cf_unlink(self, ctx: commands.Context) -> None:
        """Link management is handled by verification commands."""
        await ctx.send("Use `!verify` to set or update your handle for predictions.")

    @commands.hybrid_command(name="cf-linked")
    @commands.guild_only()
    async def cf_linked(self, ctx: commands.Context) -> None:
        """Show your verified handle used by prediction commands."""
        assert ctx.guild is not None
        verified = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if verified is None:
            await ctx.send("You are not verified in this server. Use `!verify <handle>` first.")
            return
        await ctx.send(f"Your verified handle is **{verified.cf_handle}**.")

    @commands.hybrid_command(name="cf-predict")
    @commands.guild_only()
    async def cf_predict(
        self,
        ctx: commands.Context,
        contest_id: int,
        server_only: bool = True,
        show_unofficial: bool = False,
    ) -> None:
        """Predict rating changes for an ongoing contest."""
        assert ctx.guild is not None
        result = await self._build_prediction_result(
            guild_id=ctx.guild.id,
            contest_id=contest_id,
            server_only=server_only,
            show_unofficial=show_unofficial,
            handles_filter=None,
        )
        if result is None:
            await ctx.send(self._cf_failure_message("Could not fetch standings for that contest right now."))
            return

        await self._send_prediction_embed(ctx, result, title="Contest Rating Prediction")

    @commands.hybrid_command(name="cf-predict-handles")
    @commands.guild_only()
    async def cf_predict_handles(self, ctx: commands.Context, contest_id: int, *, handles: str) -> None:
        """Predict rating changes for specific handles in a contest."""
        assert ctx.guild is not None
        parsed = _parse_handles(handles)
        if not parsed:
            await ctx.send("Provide at least one handle.")
            return

        result = await self._build_prediction_result(
            guild_id=ctx.guild.id,
            contest_id=contest_id,
            server_only=False,
            show_unofficial=False,
            handles_filter=set(parsed),
        )
        if result is None:
            await ctx.send(self._cf_failure_message("Could not fetch standings for that contest right now."))
            return

        await self._send_prediction_embed(ctx, result, title="Handle Rating Prediction")

    @commands.hybrid_command(name="cf-predict-me")
    @commands.guild_only()
    async def cf_predict_me(self, ctx: commands.Context, contest_id: int) -> None:
        """Predict your rating change using your verified handle."""
        assert ctx.guild is not None
        verified = await self._repo.get_by_discord_id(ctx.author.id, ctx.guild.id)
        if verified is None:
            await ctx.send("Verify first with `!verify <handle>` then `!confirm`.")
            return

        result = await self._build_prediction_result(
            guild_id=ctx.guild.id,
            contest_id=contest_id,
            server_only=False,
            show_unofficial=False,
            handles_filter={verified.cf_handle},
        )
        if result is None or not result.predictions:
            await ctx.send(self._cf_failure_message("Could not build prediction for your handle in that contest."))
            return

        row = result.predictions[0]
        embed = discord.Embed(
            title="Your Contest Prediction",
            description=f"Contest: **{result.contest_name}** (`{result.contest_id}`)",
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="Handle", value=row.handle, inline=True)
        embed.add_field(name="Rank", value=str(row.rank), inline=True)
        embed.add_field(name="Current", value=str(row.current_rating), inline=True)
        embed.add_field(name="Delta", value=_format_delta(row.delta), inline=True)
        embed.add_field(name="New Rating", value=str(row.new_rating), inline=True)
        embed.add_field(name="Performance", value=str(row.performance), inline=True)
        embed.set_footer(text="Approximate; official Codeforces change may differ")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="cf-watch")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def cf_watch(
        self,
        ctx: commands.Context,
        contest_id: int,
        interval_minutes: int = DEFAULT_WATCH_INTERVAL_MINUTES,
        server_only: bool = True,
        show_unofficial: bool = False,
    ) -> None:
        """Start periodic prediction updates in this channel."""
        assert ctx.guild is not None
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command must be used in a text channel.")
            return

        interval = max(1, min(interval_minutes, 60))
        existing = await self._repo.get_watch_job(ctx.guild.id, ctx.channel.id, contest_id)
        message_id = existing.message_id if existing else None
        job = WatchJob(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            contest_id=contest_id,
            interval_minutes=interval,
            message_id=message_id,
            server_only=server_only,
            show_unofficial=show_unofficial,
            enabled=True,
            created_at=existing.created_at if existing else discord.utils.utcnow(),
            updated_at=discord.utils.utcnow(),
        )
        await self._repo.upsert_watch_job(job)
        self._start_watch_task(job)
        await ctx.send(
            f"Started watch for contest `{contest_id}` in {ctx.channel.mention} every {interval} minute(s)."
        )

    @commands.hybrid_command(name="cf-unwatch")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def cf_unwatch(self, ctx: commands.Context, contest_id: int) -> None:
        """Stop periodic prediction updates for a contest in this channel."""
        assert ctx.guild is not None
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command must be used in a text channel.")
            return

        disabled = await self._repo.disable_watch_job(ctx.guild.id, ctx.channel.id, contest_id)
        key = (ctx.guild.id, ctx.channel.id, contest_id)
        task = self._watch_tasks.pop(key, None)
        if task is not None:
            task.cancel()

        if disabled:
            await ctx.send(f"Stopped watch for contest `{contest_id}` in {ctx.channel.mention}.")
        else:
            await ctx.send("No active watch found for that contest in this channel.")

    @commands.hybrid_command(name="cf-verify-finished")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def cf_verify_finished(self, ctx: commands.Context, contest_id: int) -> None:
        """Compare local predictions with official deltas for finished contests."""
        assert ctx.guild is not None

        result = await self._build_prediction_result(
            guild_id=ctx.guild.id,
            contest_id=contest_id,
            server_only=False,
            show_unofficial=False,
            handles_filter=None,
        )
        if result is None:
            await ctx.send(self._cf_failure_message("Could not fetch standings for that contest."))
            return

        metrics = await self._service.compare_with_official(contest_id=contest_id, predictions=result.predictions)
        if metrics["count"] == 0:
            await ctx.send("No official rating changes found yet for comparison.")
            return

        embed = discord.Embed(
            title="Prediction Verification",
            description=f"Contest `{contest_id}` - {result.contest_name}",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Compared", value=str(int(metrics["count"])), inline=True)
        embed.add_field(name="MAE", value=f"{metrics['mae']:.2f}", inline=True)
        embed.add_field(name="Max Error", value=f"{metrics['max_error']:.0f}", inline=True)
        embed.add_field(name="Exact Matches", value=str(int(metrics["exact"])), inline=True)
        embed.add_field(name="Close (<=10)", value=str(int(metrics["close"])), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CFPredictorCog(bot, getattr(bot, "user_repo"), getattr(bot, "cf_client")))
