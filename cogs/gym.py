from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

from db.repository import GymProblemTag, UserRepositoryBase, VerifiedUser
from services.cf_client import CFSubmission, CodeforcesClientBase
from services.guild_config import GuildConfigService

PARTICIPATION_CACHE_SECONDS = 3600
FORCE_REFRESH_SECONDS = 600
SUBMISSION_CACHE_SECONDS = 3600

COMMON_TAGS: tuple[str, ...] = (
    "implementation",
    "math",
    "greedy",
    "dp",
    "graphs",
    "data structures",
    "binary search",
    "sortings",
    "two pointers",
    "constructive algorithms",
    "strings",
    "number theory",
    "combinatorics",
    "bitmasks",
    "brute force",
    "geometry",
    "dfs and similar",
    "trees",
    "shortest paths",
    "probabilities",
    "interactive",
)


def _normalize_problem_index(raw: str) -> str:
    return raw.strip().upper()


def _normalize_tag(raw: str) -> str:
    return " ".join(raw.strip().lower().split())


def _weight_for_rating(rating: int) -> float:
    if rating < 1200:
        return 1.0
    if rating < 1600:
        return 1.3
    if rating < 1900:
        return 1.7
    if rating < 2100:
        return 2.1
    return 2.6


class ContestIdModal(discord.ui.Modal):
    contest_id = discord.ui.TextInput(label="Contest ID", placeholder="e.g. 2062", required=True, max_length=16)

    def __init__(self, title: str, callback: Callable[[discord.Interaction, int], Awaitable[None]]) -> None:
        super().__init__(title=title)
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            contest_id = int(str(self.contest_id).strip())
        except ValueError:
            await interaction.response.send_message("Contest ID must be an integer.", ephemeral=True)
            return
        await self._callback(interaction, contest_id)


class ContestProblemModal(discord.ui.Modal):
    contest_id = discord.ui.TextInput(label="Contest ID", placeholder="e.g. 2062", required=True, max_length=16)
    problem_index = discord.ui.TextInput(label="Problem Index", placeholder="e.g. A / B1 / C", required=True, max_length=12)

    def __init__(
        self,
        title: str,
        callback: Callable[[discord.Interaction, int, str], Awaitable[None]],
    ) -> None:
        super().__init__(title=title)
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            contest_id = int(str(self.contest_id).strip())
        except ValueError:
            await interaction.response.send_message("Contest ID must be an integer.", ephemeral=True)
            return
        problem_index = _normalize_problem_index(str(self.problem_index))
        if not problem_index:
            await interaction.response.send_message("Problem index cannot be empty.", ephemeral=True)
            return
        await self._callback(interaction, contest_id, problem_index)


class ContestProblemOptionalModal(discord.ui.Modal):
    contest_id = discord.ui.TextInput(label="Contest ID", placeholder="e.g. 2062", required=True, max_length=16)
    problem_index = discord.ui.TextInput(
        label="Problem Index (optional)",
        placeholder="leave empty to show all",
        required=False,
        max_length=12,
    )

    def __init__(
        self,
        title: str,
        callback: Callable[[discord.Interaction, int, Optional[str]], Awaitable[None]],
    ) -> None:
        super().__init__(title=title)
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            contest_id = int(str(self.contest_id).strip())
        except ValueError:
            await interaction.response.send_message("Contest ID must be an integer.", ephemeral=True)
            return

        problem_index_raw = str(self.problem_index).strip()
        problem_index = _normalize_problem_index(problem_index_raw) if problem_index_raw else None
        await self._callback(interaction, contest_id, problem_index)


class RateProblemModal(discord.ui.Modal):
    contest_id = discord.ui.TextInput(label="Contest ID", placeholder="e.g. 2062", required=True, max_length=16)
    problem_index = discord.ui.TextInput(label="Problem Index", placeholder="e.g. A / B1 / C", required=True, max_length=12)
    estimated_rating = discord.ui.TextInput(label="Estimated Rating", placeholder="e.g. 1400", required=True, max_length=6)

    def __init__(
        self,
        callback: Callable[[discord.Interaction, int, str, int], Awaitable[None]],
    ) -> None:
        super().__init__(title="Rate Gym Problem")
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            contest_id = int(str(self.contest_id).strip())
            rating = int(str(self.estimated_rating).strip())
        except ValueError:
            await interaction.response.send_message("Contest ID and rating must be integers.", ephemeral=True)
            return
        if rating < 300 or rating > 5000:
            await interaction.response.send_message("Estimated rating must be between 300 and 5000.", ephemeral=True)
            return
        problem_index = _normalize_problem_index(str(self.problem_index))
        if not problem_index:
            await interaction.response.send_message("Problem index cannot be empty.", ephemeral=True)
            return
        await self._callback(interaction, contest_id, problem_index, rating)


class OtherTagModal(discord.ui.Modal):
    tag = discord.ui.TextInput(label="Tag", placeholder="type custom tag", required=True, max_length=64)

    def __init__(
        self,
        callback: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        super().__init__(title="Add Custom Tag")
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tag = _normalize_tag(str(self.tag))
        if not tag:
            await interaction.response.send_message("Tag cannot be empty.", ephemeral=True)
            return
        await self._callback(interaction, tag)


class GymTypeView(discord.ui.View):
    def __init__(self, cog: "GymCog", actor_id: int, guild_id: int, contest_id: int) -> None:
        super().__init__(timeout=120)
        self._cog = cog
        self._actor_id = actor_id
        self._guild_id = guild_id
        self._contest_id = contest_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._actor_id:
            await interaction.response.send_message("Run `!gym` yourself to use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Individual", style=discord.ButtonStyle.success)
    async def individual(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog._complete_add_gym(interaction, self._guild_id, self._contest_id, "individual")

    @discord.ui.button(label="Team", style=discord.ButtonStyle.primary)
    async def team(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog._complete_add_gym(interaction, self._guild_id, self._contest_id, "team")


class TagAddSelect(discord.ui.Select):
    def __init__(self, parent: "TagAddView") -> None:
        options = [discord.SelectOption(label=tag, value=tag) for tag in COMMON_TAGS[:24]]
        options.append(discord.SelectOption(label="Other (custom)", value="__other__"))
        super().__init__(placeholder="Select a tag", min_values=1, max_values=1, options=options)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        if selected == "__other__":
            await interaction.response.send_modal(OtherTagModal(self._parent.on_custom_tag))
            return
        await self._parent.on_selected_tag(interaction, selected)


class TagAddView(discord.ui.View):
    def __init__(self, cog: "GymCog", actor_id: int, guild_id: int, contest_id: int, problem_index: str) -> None:
        super().__init__(timeout=180)
        self._cog = cog
        self._actor_id = actor_id
        self._guild_id = guild_id
        self._contest_id = contest_id
        self._problem_index = problem_index
        self.add_item(TagAddSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._actor_id:
            await interaction.response.send_message("Run `!gym` yourself to use this selector.", ephemeral=True)
            return False
        return True

    async def on_selected_tag(self, interaction: discord.Interaction, tag: str) -> None:
        await self._cog._perform_add_tag(interaction, self._guild_id, self._contest_id, self._problem_index, tag)

    async def on_custom_tag(self, interaction: discord.Interaction, tag: str) -> None:
        await self._cog._perform_add_tag(interaction, self._guild_id, self._contest_id, self._problem_index, tag)


class TagDeleteSelect(discord.ui.Select):
    def __init__(self, parent: "TagDeleteView", tags: list[str]) -> None:
        options = [discord.SelectOption(label=tag, value=tag) for tag in tags[:25]]
        super().__init__(placeholder="Select tag to delete", min_values=1, max_values=1, options=options)
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._parent.on_selected_tag(interaction, self.values[0])


class TagDeleteView(discord.ui.View):
    def __init__(
        self,
        cog: "GymCog",
        actor_id: int,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tags: list[str],
    ) -> None:
        super().__init__(timeout=180)
        self._cog = cog
        self._actor_id = actor_id
        self._guild_id = guild_id
        self._contest_id = contest_id
        self._problem_index = problem_index
        self.add_item(TagDeleteSelect(self, tags))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._actor_id:
            await interaction.response.send_message("Run `!gym` yourself to use this selector.", ephemeral=True)
            return False
        return True

    async def on_selected_tag(self, interaction: discord.Interaction, tag: str) -> None:
        await self._cog._perform_delete_tag(interaction, self._guild_id, self._contest_id, self._problem_index, tag)


class GymMainView(discord.ui.View):
    def __init__(self, cog: "GymCog", actor_id: int, guild_id: int) -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self._actor_id = actor_id
        self._guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._actor_id:
            await interaction.response.send_message("Run `!gym` yourself to use this panel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add Gym", style=discord.ButtonStyle.success, row=0)
    async def add_gym(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Add Gym", self._cog._start_add_gym))

    @discord.ui.button(label="List Gyms", style=discord.ButtonStyle.primary, row=0)
    async def list_gyms(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog._show_gyms(interaction, self._guild_id)

    @discord.ui.button(label="Add Tag", style=discord.ButtonStyle.secondary, row=1)
    async def add_tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Add Gym Tag", self._cog._open_add_tag))

    @discord.ui.button(label="Delete Tag", style=discord.ButtonStyle.secondary, row=1)
    async def delete_tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Delete Gym Tag", self._cog._open_delete_tag))

    @discord.ui.button(label="Show Tags", style=discord.ButtonStyle.secondary, row=1)
    async def show_tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemOptionalModal("Show Gym Tags", self._cog._show_tags))

    @discord.ui.button(label="Rate Problem", style=discord.ButtonStyle.primary, row=2)
    async def rate_problem(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RateProblemModal(self._cog._rate_problem))

    @discord.ui.button(label="Show Problem", style=discord.ButtonStyle.primary, row=2)
    async def show_problem(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Show Gym Problem", self._cog._show_problem))

    @discord.ui.button(label="Reset Gym", style=discord.ButtonStyle.danger, row=3)
    async def reset_gym(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Reset Gym Data", self._cog._reset_gym))

    @discord.ui.button(label="Delete Gym", style=discord.ButtonStyle.danger, row=3)
    async def delete_gym(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Delete Gym", self._cog._delete_gym))


class GymCog(commands.Cog, name="Gyms"):
    def __init__(
        self,
        bot: commands.Bot,
        repo: UserRepositoryBase,
        cf: CodeforcesClientBase,
        config_service: GuildConfigService,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._cf = cf
        self._config = config_service
        self._submission_cache: dict[str, tuple[float, list[CFSubmission]]] = {}
        self._submission_sem = asyncio.Semaphore(6)

    @staticmethod
    def _chunk_lines(lines: list[str], max_len: int = 1900) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            addition = len(line) + (1 if current else 0)
            if current and size + addition > max_len:
                chunks.append("\n".join(current))
                current = [line]
                size = len(line)
            else:
                current.append(line)
                size += addition
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _is_coach(self, member: discord.Member, guild_id: int) -> bool:
        coach_substring = await self._config.get_text(guild_id, "coach_role_substring")
        needle = coach_substring.lower().strip()
        if not needle:
            return False
        return any(needle in role.name.lower() for role in member.roles)

    async def _training_members(self, guild: discord.Guild) -> list[discord.Member]:
        training_substring = await self._config.get_text(guild.id, "training_role_substring")
        needle = training_substring.lower().strip()
        if not needle:
            return []
        members_by_id: dict[int, discord.Member] = {}
        for role in guild.roles:
            if needle not in role.name.lower():
                continue
            for member in role.members:
                if member.bot:
                    continue
                members_by_id[member.id] = member
        return list(members_by_id.values())

    async def _verified_map(self, guild_id: int) -> dict[int, VerifiedUser]:
        rows = await self._repo.get_all(guild_id)
        return {row.discord_id: row for row in rows}

    async def _get_submissions(self, handle: str, *, refresh_after_seconds: int = SUBMISSION_CACHE_SECONDS) -> list[CFSubmission]:
        cache_key = handle.lower()
        cached = self._submission_cache.get(cache_key)
        now_ts = time.time()
        if cached is not None:
            ts, payload = cached
            if now_ts - ts < refresh_after_seconds:
                return payload
        async with self._submission_sem:
            payload = await self._cf.get_recent_submissions(handle, count=5000)
        self._submission_cache[cache_key] = (time.time(), payload)
        return payload

    async def _solved_count_for_contest(
        self,
        handle: str,
        contest_id: int,
        *,
        refresh_after_seconds: int,
    ) -> int:
        submissions = await self._get_submissions(handle, refresh_after_seconds=refresh_after_seconds)
        solved = {
            submission.problem_index
            for submission in submissions
            if submission.verdict == "OK"
            and submission.contest_id == contest_id
            and submission.problem_index is not None
        }
        return len(solved)

    async def _user_solved_problem(
        self,
        handle: str,
        contest_id: int,
        problem_index: str,
    ) -> bool:
        submissions = await self._get_submissions(handle, refresh_after_seconds=SUBMISSION_CACHE_SECONDS)
        target_index = _normalize_problem_index(problem_index)
        for submission in submissions:
            if submission.verdict != "OK":
                continue
            if submission.contest_id != contest_id:
                continue
            if submission.problem_index != target_index:
                continue
            return True
        return False

    async def _can_modify_tags(
        self,
        member: discord.Member,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> tuple[bool, str]:
        verified = await self._repo.get_by_discord_id(member.id, guild_id)
        if verified is None:
            return False, "You must be verified to modify gym tags."
        if verified.rating >= 1600:
            return True, ""
        solved = await self._user_solved_problem(verified.cf_handle, contest_id, problem_index)
        if solved:
            return True, ""
        return False, "Only solvers of this problem or Expert+ users can modify tags."

    async def _contest_participation(
        self,
        guild_id: int,
        contest_id: int,
        training_members: list[discord.Member],
        verified_by_id: dict[int, VerifiedUser],
        *,
        force: bool,
    ) -> tuple[dict[int, int], set[int]]:
        now = datetime.now(tz=UTC)
        refresh_age_seconds = FORCE_REFRESH_SECONDS if force else PARTICIPATION_CACHE_SECONDS
        submission_refresh_seconds = FORCE_REFRESH_SECONDS if force else SUBMISSION_CACHE_SECONDS

        cache_rows = await self._repo.get_gym_participation_cache(guild_id, contest_id)
        cache_by_discord = {row.discord_id: row for row in cache_rows}

        solved_by_discord: dict[int, int] = {}
        unverified_ids: set[int] = set()
        pending: list[tuple[int, str]] = []

        for member in training_members:
            verified = verified_by_id.get(member.id)
            if verified is None:
                unverified_ids.add(member.id)
                continue
            cached = cache_by_discord.get(member.id)
            if cached is not None:
                age = (now - cached.checked_at).total_seconds()
                if age < refresh_age_seconds:
                    solved_by_discord[member.id] = cached.solved_count
                    continue
            pending.append((member.id, verified.cf_handle))

        async def _refresh_one(discord_id: int, handle: str) -> tuple[int, int]:
            solved_count = await self._solved_count_for_contest(
                handle,
                contest_id,
                refresh_after_seconds=submission_refresh_seconds,
            )
            await self._repo.upsert_gym_participation_cache(
                guild_id,
                contest_id,
                discord_id,
                solved_count,
                now,
            )
            return discord_id, solved_count

        if pending:
            refreshed = await asyncio.gather(*[_refresh_one(discord_id, handle) for discord_id, handle in pending])
            for discord_id, solved_count in refreshed:
                solved_by_discord[discord_id] = solved_count

        return solved_by_discord, unverified_ids

    async def _problem_rating_summary(self, guild_id: int, contest_id: int, problem_index: str) -> dict[str, float]:
        votes = await self._repo.list_gym_problem_rating_votes(guild_id, contest_id, problem_index)
        verified = await self._verified_map(guild_id)

        included = []
        for vote in votes:
            verifier = verified.get(vote.discord_id)
            if verifier is None:
                continue
            included.append((vote.estimated_rating, _weight_for_rating(verifier.rating)))

        if not included:
            return {"count": 0.0, "avg": 0.0, "weighted_avg": 0.0}

        avg = sum(v for v, _ in included) / len(included)
        weight_sum = sum(w for _, w in included)
        weighted_avg = sum(v * w for v, w in included) / weight_sum if weight_sum > 0 else avg
        return {
            "count": float(len(included)),
            "avg": avg,
            "weighted_avg": weighted_avg,
        }

    @commands.command(name="gym")
    @commands.guild_only()
    async def gym(self, ctx: commands.Context) -> None:
        """Open the gym management panel (buttons)."""
        assert ctx.guild is not None
        view = GymMainView(self, ctx.author.id, ctx.guild.id)
        await ctx.send(
            "Gym panel:\n"
            "- Add/List gyms\n"
            "- Add/Delete/List problem tags\n"
            "- Rate/show problem ratings\n"
            "- Reset/Delete gym data",
            view=view,
        )

    @commands.command(name="gald")
    @commands.guild_only()
    async def gald(self, ctx: commands.Context, *args: str) -> None:
        """Check who in Training Arc has not solved at least one gym problem."""
        assert ctx.guild is not None

        contest_id: Optional[int] = None
        include_teams = False
        force = False

        for raw in args:
            token = raw.strip().lower()
            if token.isdigit() and contest_id is None:
                contest_id = int(token)
                continue
            if token in {"team", "teams", "alltypes", "include-teams"}:
                include_teams = True
                continue
            if token in {"force", "refresh"}:
                force = True
                continue
            await ctx.send("Usage: `!gald [contest_id] [teams] [force]`")
            return

        gyms = await self._repo.list_gym_contests(ctx.guild.id)
        if contest_id is not None:
            gyms = [gym for gym in gyms if gym.contest_id == contest_id]

        if not include_teams:
            gyms = [gym for gym in gyms if gym.gym_type == "individual"]

        if not gyms:
            await ctx.send("No matching gyms found.")
            return

        training_members = await self._training_members(ctx.guild)
        if not training_members:
            await ctx.send("No trainees found. Update `training_role_substring` in `!config text` if needed.")
            return

        verified_by_id = await self._verified_map(ctx.guild.id)
        cache_note = "force(10m)" if force else "normal(1h cache)"

        for gym in gyms:
            solved_by_discord, unverified_ids = await self._contest_participation(
                ctx.guild.id,
                gym.contest_id,
                training_members,
                verified_by_id,
                force=force,
            )

            not_participated = [
                member
                for member in training_members
                if member.id in verified_by_id and solved_by_discord.get(member.id, 0) <= 0
            ]
            unverified_members = [member for member in training_members if member.id in unverified_ids]

            lines = [
                f"**GALD - Contest `{gym.contest_id}` ({gym.gym_type})**",
                f"Trainees tracked: **{len(training_members)}** | cache mode: **{cache_note}**",
                "",
            ]
            if not not_participated and not unverified_members:
                lines.append("All trainees have solved at least one problem.")
            else:
                if not_participated:
                    lines.append("Did not solve any problem yet:")
                    lines.extend(member.mention for member in not_participated)
                    lines.append("")
                if unverified_members:
                    lines.append("Unverified trainees (cannot track CF handle):")
                    lines.extend(member.mention for member in unverified_members)

            for chunk in self._chunk_lines(lines):
                await ctx.send(chunk)

    async def _start_add_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        assert interaction.guild is not None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Could not validate your permissions.", ephemeral=True)
            return
        if not await self._is_coach(interaction.user, interaction.guild.id):
            await interaction.response.send_message("Only coach-role users can add gyms.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Choose type for contest `{contest_id}`:",
            view=GymTypeView(self, interaction.user.id, interaction.guild.id, contest_id),
            ephemeral=True,
        )

    async def _complete_add_gym(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        contest_id: int,
        gym_type: str,
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        if not await self._is_coach(interaction.user, guild_id):
            await interaction.response.send_message("Only coach-role users can add gyms.", ephemeral=True)
            return
        await self._repo.upsert_gym_contest(guild_id, contest_id, gym_type, interaction.user.id)
        await interaction.response.send_message(
            f"Gym `{contest_id}` saved as **{gym_type}** (upserted, no duplicates).",
            ephemeral=True,
        )

    async def _show_gyms(self, interaction: discord.Interaction, guild_id: int) -> None:
        gyms = await self._repo.list_gym_contests(guild_id)
        if not gyms:
            await interaction.response.send_message("No gyms configured yet.", ephemeral=True)
            return

        lines = ["contest_id | type | created_by"]
        for gym in gyms:
            lines.append(f"{gym.contest_id:<10} | {gym.gym_type:<10} | {gym.created_by}")
        message = "```text\n" + "\n".join(lines[:60]) + "\n```"
        await interaction.response.send_message(message, ephemeral=True)

    async def _open_add_tag(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        assert interaction.guild is not None
        gym = await self._repo.get_gym_contest(interaction.guild.id, contest_id)
        if gym is None:
            await interaction.response.send_message("Gym not found. Add it first.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Choose a tag for `{contest_id}{problem_index}`:",
            view=TagAddView(self, interaction.user.id, interaction.guild.id, contest_id, problem_index),
            ephemeral=True,
        )

    async def _perform_add_tag(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        allowed, reason = await self._can_modify_tags(interaction.user, guild_id, contest_id, problem_index)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        normalized_tag = _normalize_tag(tag)
        if not normalized_tag:
            await interaction.response.send_message("Tag cannot be empty.", ephemeral=True)
            return
        inserted = await self._repo.add_gym_problem_tag(
            guild_id,
            contest_id,
            problem_index,
            normalized_tag,
            interaction.user.id,
        )
        if inserted:
            await interaction.response.send_message(
                f"Added tag `{normalized_tag}` to `{contest_id}{problem_index}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Tag `{normalized_tag}` already exists for `{contest_id}{problem_index}`.",
            ephemeral=True,
        )

    async def _open_delete_tag(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        assert interaction.guild is not None
        tags = await self._repo.list_gym_problem_tags(interaction.guild.id, contest_id)
        candidates = sorted({row.tag for row in tags if row.problem_index == problem_index})
        if not candidates:
            await interaction.response.send_message("No tags found for that problem.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Select tag to remove from `{contest_id}{problem_index}`:",
            view=TagDeleteView(
                self,
                interaction.user.id,
                interaction.guild.id,
                contest_id,
                problem_index,
                candidates,
            ),
            ephemeral=True,
        )

    async def _perform_delete_tag(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        contest_id: int,
        problem_index: str,
        tag: str,
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        allowed, reason = await self._can_modify_tags(interaction.user, guild_id, contest_id, problem_index)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        removed = await self._repo.remove_gym_problem_tag(guild_id, contest_id, problem_index, tag)
        if removed:
            await interaction.response.send_message(
                f"Removed tag `{tag}` from `{contest_id}{problem_index}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Tag not found.", ephemeral=True)

    async def _show_tags(self, interaction: discord.Interaction, contest_id: int, problem_index: Optional[str]) -> None:
        assert interaction.guild is not None
        tags = await self._repo.list_gym_problem_tags(interaction.guild.id, contest_id)
        if problem_index is not None:
            tags = [row for row in tags if row.problem_index == problem_index]
        if not tags:
            await interaction.response.send_message("No tags found.", ephemeral=True)
            return

        grouped: dict[str, list[str]] = defaultdict(list)
        for row in tags:
            grouped[row.problem_index].append(row.tag)

        lines = [f"Tags for contest `{contest_id}`:"]
        for idx in sorted(grouped):
            lines.append(f"- {idx}: {', '.join(sorted(grouped[idx]))}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _rate_problem(self, interaction: discord.Interaction, contest_id: int, problem_index: str, rating: int) -> None:
        assert interaction.guild is not None
        verified = await self._repo.get_by_discord_id(interaction.user.id, interaction.guild.id)
        if verified is None:
            await interaction.response.send_message("You must be verified to rate problems.", ephemeral=True)
            return

        await self._repo.upsert_gym_problem_rating_vote(
            interaction.guild.id,
            contest_id,
            problem_index,
            interaction.user.id,
            rating,
        )
        summary = await self._problem_rating_summary(interaction.guild.id, contest_id, problem_index)
        await interaction.response.send_message(
            f"Saved vote for `{contest_id}{problem_index}`.\n"
            f"Votes: **{int(summary['count'])}** | Avg: **{summary['avg']:.1f}** | "
            f"Weighted Avg: **{summary['weighted_avg']:.1f}**",
            ephemeral=True,
        )

    async def _show_problem(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        assert interaction.guild is not None
        tags = await self._repo.list_gym_problem_tags(interaction.guild.id, contest_id)
        tags_for_problem = sorted({row.tag for row in tags if row.problem_index == problem_index})
        summary = await self._problem_rating_summary(interaction.guild.id, contest_id, problem_index)

        tags_text = ", ".join(tags_for_problem) if tags_for_problem else "No tags"
        if summary["count"] <= 0:
            rating_text = "No verified votes yet"
        else:
            rating_text = (
                f"Votes: {int(summary['count'])} | "
                f"Avg: {summary['avg']:.1f} | "
                f"Weighted Avg: {summary['weighted_avg']:.1f}"
            )
        await interaction.response.send_message(
            f"**{contest_id}{problem_index}**\nTags: {tags_text}\nRatings: {rating_text}",
            ephemeral=True,
        )

    async def _reset_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        if not await self._is_coach(interaction.user, interaction.guild.id):
            await interaction.response.send_message("Only coach-role users can reset gyms.", ephemeral=True)
            return
        gym = await self._repo.get_gym_contest(interaction.guild.id, contest_id)
        if gym is None:
            await interaction.response.send_message("Gym not found.", ephemeral=True)
            return
        await self._repo.reset_gym_contest_data(interaction.guild.id, contest_id)
        await interaction.response.send_message(
            f"Reset tags, ratings, and cache for gym `{contest_id}`.",
            ephemeral=True,
        )

    async def _delete_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        if not await self._is_coach(interaction.user, interaction.guild.id):
            await interaction.response.send_message("Only coach-role users can delete gyms.", ephemeral=True)
            return
        deleted = await self._repo.delete_gym_contest(interaction.guild.id, contest_id)
        if deleted:
            await interaction.response.send_message(f"Deleted gym `{contest_id}`.", ephemeral=True)
            return
        await interaction.response.send_message("Gym not found.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(
        GymCog(
            bot,
            getattr(bot, "user_repo"),
            getattr(bot, "cf_client"),
            getattr(bot, "guild_config"),
        )
    )
