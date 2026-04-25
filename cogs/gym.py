from __future__ import annotations

from collections import defaultdict
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

from db.repository import GymFeatureRepository, VerifiedUser
from services.discord_output import send_context_lines_chunks, send_interaction_lines_chunks, send_interaction_text_chunks
from services.cf_client import CFRequestError, CodeforcesClientBase
from services.gym_service import GymService, normalize_problem_index, normalize_tag, problem_ref
from services.guild_config import GuildConfigService

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
        problem_index = normalize_problem_index(str(self.problem_index))
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
        problem_index = normalize_problem_index(problem_index_raw) if problem_index_raw else None
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
        problem_index = normalize_problem_index(str(self.problem_index))
        if not problem_index:
            await interaction.response.send_message("Problem index cannot be empty.", ephemeral=True)
            return
        await self._callback(interaction, contest_id, problem_index, rating)


class RateGymQualityModal(discord.ui.Modal):
    contest_id = discord.ui.TextInput(label="Contest ID", placeholder="e.g. 2062", required=True, max_length=16)
    quality = discord.ui.TextInput(label="Quality (1-5)", placeholder="e.g. 4", required=True, max_length=2)

    def __init__(
        self,
        callback: Callable[[discord.Interaction, int, int], Awaitable[None]],
    ) -> None:
        super().__init__(title="Rate Gym Quality")
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            contest_id = int(str(self.contest_id).strip())
            quality = int(str(self.quality).strip())
        except ValueError:
            await interaction.response.send_message("Contest ID and quality must be integers.", ephemeral=True)
            return
        if quality < 1 or quality > 5:
            await interaction.response.send_message("Quality must be from 1 to 5.", ephemeral=True)
            return
        await self._callback(interaction, contest_id, quality)


class OtherTagModal(discord.ui.Modal):
    tag = discord.ui.TextInput(label="Tag", placeholder="type custom tag", required=True, max_length=64)

    def __init__(
        self,
        callback: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        super().__init__(title="Add Custom Tag")
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tag = normalize_tag(str(self.tag))
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

    @discord.ui.button(label="Tag Add", style=discord.ButtonStyle.secondary, row=1)
    async def add_tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Add Gym Tag", self._cog._open_add_tag))

    @discord.ui.button(label="Tag Delete", style=discord.ButtonStyle.secondary, row=1)
    async def delete_tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Delete Gym Tag", self._cog._open_delete_tag))

    @discord.ui.button(label="Tag List", style=discord.ButtonStyle.secondary, row=1)
    async def show_tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemOptionalModal("Show Gym Tags", self._cog._show_tags))

    @discord.ui.button(label="Problem Rate", style=discord.ButtonStyle.primary, row=2)
    async def rate_problem(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RateProblemModal(self._cog._rate_problem))

    @discord.ui.button(label="Problem Show", style=discord.ButtonStyle.primary, row=2)
    async def show_problem(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestProblemModal("Show Gym Problem", self._cog._show_problem))

    @discord.ui.button(label="Quality Rate", style=discord.ButtonStyle.primary, row=2)
    async def rate_gym_quality(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RateGymQualityModal(self._cog._rate_gym_quality))

    @discord.ui.button(label="Quality Show", style=discord.ButtonStyle.primary, row=2)
    async def show_gym_quality(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Show Gym Quality", self._cog._show_gym_quality))

    @discord.ui.button(label="Gym Reset", style=discord.ButtonStyle.danger, row=3)
    async def reset_gym(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Reset Gym Data", self._cog._reset_gym))

    @discord.ui.button(label="Gym Delete", style=discord.ButtonStyle.danger, row=3)
    async def delete_gym(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContestIdModal("Delete Gym", self._cog._delete_gym))


class GymCog(commands.Cog, name="Gyms"):
    def __init__(
        self,
        bot: commands.Bot,
        repo: GymFeatureRepository,
        cf: CodeforcesClientBase,
        config_service: GuildConfigService,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._cf = cf
        self._config = config_service
        self._gym_service = GymService(repo, cf)

    @staticmethod
    def _cf_error_message(error: CFRequestError) -> str:
        status_text = f", status {error.http_status}" if error.http_status is not None else ""
        lines = [f"Request failed: endpoint {error.endpoint}{status_text} ({error.failure_kind})."]
        if error.requested_url and error.failure_kind != "non_ok":
            lines.append(f"URL: `{error.requested_url}`")
        lines.append("Please try again in a minute.")
        return "\n".join(lines)

    @staticmethod
    def _sorted_members(members: list[discord.Member]) -> list[discord.Member]:
        return sorted(members, key=lambda member: member.display_name.casefold())

    async def _send_ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        await send_interaction_text_chunks(interaction, content, ephemeral=True)

    @staticmethod
    def _guild_id_from_interaction(interaction: discord.Interaction) -> Optional[int]:
        if interaction.guild_id is not None:
            return interaction.guild_id
        if interaction.guild is not None:
            return interaction.guild.id
        return None

    @staticmethod
    def _member_from_interaction(interaction: discord.Interaction) -> Optional[discord.Member]:
        if isinstance(interaction.user, discord.Member):
            return interaction.user
        if interaction.guild is None:
            return None
        return interaction.guild.get_member(interaction.user.id)

    async def _ensure_gym_exists(
        self,
        interaction: discord.Interaction,
        contest_id: int,
        *,
        guild_id: Optional[int] = None,
        not_found_message: str = "Gym not found. Add it first.",
    ) -> bool:
        resolved_guild_id = guild_id if guild_id is not None else self._guild_id_from_interaction(interaction)
        if resolved_guild_id is None:
            await self._send_ephemeral(interaction, "This action can only be used in a server.")
            return False
        gym = await self._repo.get_gym_contest(resolved_guild_id, contest_id)
        if gym is not None:
            return True
        await self._send_ephemeral(interaction, not_found_message)
        return False

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
        return await self._gym_service.verified_map(guild_id)

    async def _can_modify_tags(
        self,
        member: discord.abc.User,
        guild_id: int,
        contest_id: int,
        problem_index: str,
    ) -> tuple[bool, str]:
        return await self._gym_service.can_modify_tags(
            member_id=member.id,
            guild_id=guild_id,
            contest_id=contest_id,
            problem_index=problem_index,
        )

    async def _contest_participation(
        self,
        guild_id: int,
        contest_id: int,
        training_members: list[discord.Member],
        verified_by_id: dict[int, VerifiedUser],
        *,
        force: bool,
    ) -> tuple[dict[int, int], set[int]]:
        training_member_ids = [member.id for member in training_members]
        return await self._gym_service.contest_participation(
            guild_id=guild_id,
            contest_id=contest_id,
            training_member_ids=training_member_ids,
            verified_by_id=verified_by_id,
            force=force,
        )

    async def _problem_rating_summary(self, guild_id: int, contest_id: int, problem_index: str) -> dict[str, float]:
        return await self._gym_service.problem_rating_summary(guild_id, contest_id, problem_index)

    async def _gym_quality_summary(self, guild_id: int, contest_id: int) -> dict[str, float]:
        return await self._gym_service.gym_quality_summary(guild_id, contest_id)

    @commands.command(
        name="gym",
        brief="Open gym panel for contest management, tags, and ratings.",
        help=(
            "Open the interactive gym panel.\n\n"
            "Coach-only actions:\n"
            "- Add gym contest (individual/team)\n"
            "- Reset gym data\n"
            "- Delete gym contest\n\n"
            "Verified-user actions:\n"
            "- Rate problem difficulty\n"
            "- Rate overall gym quality (1-5)\n\n"
            "Tag edits require either solving that problem or being Expert+.\n"
            "If buttons expire, run `!gym` again."
        ),
    )
    @commands.guild_only()
    async def gym(self, ctx: commands.Context) -> None:
        """Open gym panel for contest management, tags, and ratings."""
        assert ctx.guild is not None
        view = GymMainView(self, ctx.author.id, ctx.guild.id)
        embed = discord.Embed(
            title="Gym Control Panel",
            description=(
                "Use the buttons below to manage gym contests, tags, and ratings.\n"
                "If this panel times out, run `!gym` again."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Coach Actions",
            value="Add gyms, reset gym data, delete gyms.",
            inline=False,
        )
        embed.add_field(
            name="Verified User Actions",
            value=(
                "Rate problem difficulty and gym quality.\n"
                "Tag edits require solving the problem or being Expert+."
            ),
            inline=False,
        )
        await ctx.send(embed=embed, view=view)

    @commands.command(
        name="gald",
        usage="[contest_id] [teams] [force]",
        brief="List trainees with zero solved gym problems.",
        help=(
            "Check training members who did not solve at least one problem in gyms.\n\n"
            "Usage: `!gald [contest_id] [teams] [force]`\n"
            "- `contest_id`: optional; check one contest only.\n"
            "- `teams`: include team gyms (default is individual gyms only).\n"
            "- `force`: use 10-minute freshness instead of 1-hour cache.\n\n"
            "Examples:\n"
            "- `!gald`\n"
            "- `!gald 2062`\n"
            "- `!gald teams`\n"
            "- `!gald 2062 teams force`"
        ),
    )
    @commands.guild_only()
    async def gald(self, ctx: commands.Context, *args: str) -> None:
        """List trainees with zero solved gym problems."""
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
            try:
                solved_by_discord, unverified_ids = await self._contest_participation(
                    ctx.guild.id,
                    gym.contest_id,
                    training_members,
                    verified_by_id,
                    force=force,
                )
            except CFRequestError as exc:
                await ctx.send(self._cf_error_message(exc))
                return

            not_participated = self._sorted_members(
                [
                    member
                    for member in training_members
                    if member.id in verified_by_id and solved_by_discord.get(member.id, 0) <= 0
                ]
            )
            unverified_members = self._sorted_members(
                [member for member in training_members if member.id in unverified_ids]
            )

            lines = [
                f"**GALD - Contest `{gym.contest_id}` ({gym.gym_type})**",
                f"Trainees tracked: **{len(training_members)}** | cache mode: **{cache_note}**",
                "",
            ]
            if not not_participated and not unverified_members:
                lines.append("All trainees have solved at least one problem.")
            else:
                if not_participated:
                    lines.append(f"Did not solve any problem yet ({len(not_participated)}):")
                    lines.extend(member.mention for member in not_participated)
                    lines.append("")
                if unverified_members:
                    lines.append(f"Unverified trainees (cannot track CF handle) ({len(unverified_members)}):")
                    lines.extend(member.mention for member in unverified_members)

            await send_context_lines_chunks(ctx, lines)

    async def _start_add_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        member = self._member_from_interaction(interaction)
        if member is None:
            await interaction.response.send_message("Could not validate your permissions.", ephemeral=True)
            return
        if not await self._is_coach(member, guild_id):
            await interaction.response.send_message("Only users with a coach role can add gyms.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Choose type for contest `{contest_id}`:",
            view=GymTypeView(self, interaction.user.id, guild_id, contest_id),
            ephemeral=True,
        )

    async def _complete_add_gym(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        contest_id: int,
        gym_type: str,
    ) -> None:
        member = self._member_from_interaction(interaction)
        if member is None:
            await interaction.response.send_message("Could not validate your permissions.", ephemeral=True)
            return
        if not await self._is_coach(member, guild_id):
            await interaction.response.send_message("Only users with a coach role can add gyms.", ephemeral=True)
            return
        await self._repo.upsert_gym_contest(guild_id, contest_id, gym_type, interaction.user.id)
        await interaction.response.send_message(
            f"Gym `{contest_id}` saved as **{gym_type}** (upserted; no duplicates).",
            ephemeral=True,
        )

    async def _show_gyms(self, interaction: discord.Interaction, guild_id: int) -> None:
        gyms = await self._repo.list_gym_contests(guild_id)
        if not gyms:
            await interaction.response.send_message("No gyms configured yet.", ephemeral=True)
            return

        lines = [f"Configured gyms: **{len(gyms)}**", ""]
        guild = interaction.guild
        for gym in gyms:
            owner = guild.get_member(gym.created_by) if guild is not None else None
            owner_text = owner.mention if owner is not None else f"`{gym.created_by}`"
            created_text = gym.created_at.strftime("%Y-%m-%d")
            lines.append(f"- `{gym.contest_id}` | `{gym.gym_type}` | by {owner_text} | {created_text}")
        await send_interaction_lines_chunks(interaction, lines, ephemeral=True)

    async def _open_add_tag(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        await interaction.response.send_message(
            f"Choose a tag for `{problem_ref(contest_id, problem_index)}`:",
            view=TagAddView(self, interaction.user.id, guild_id, contest_id, problem_index),
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
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        try:
            allowed, reason = await self._can_modify_tags(interaction.user, guild_id, contest_id, problem_index)
        except CFRequestError as exc:
            await self._send_ephemeral(interaction, self._cf_error_message(exc))
            return
        if not allowed:
            await self._send_ephemeral(interaction, reason)
            return
        normalized_tag = normalize_tag(tag)
        if not normalized_tag:
            await self._send_ephemeral(interaction, "Tag cannot be empty.")
            return
        inserted = await self._repo.add_gym_problem_tag(
            guild_id,
            contest_id,
            problem_index,
            normalized_tag,
            interaction.user.id,
        )
        if inserted:
            await self._send_ephemeral(
                interaction,
                f"Added tag `{normalized_tag}` to `{problem_ref(contest_id, problem_index)}`.",
            )
            return
        await self._send_ephemeral(
            interaction,
            f"Tag `{normalized_tag}` already exists for `{problem_ref(contest_id, problem_index)}`.",
        )

    async def _open_delete_tag(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        tags = await self._repo.list_gym_problem_tags(guild_id, contest_id)
        candidates = sorted({row.tag for row in tags if row.problem_index == problem_index})
        if not candidates:
            await interaction.response.send_message("No tags found for that problem.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Select tag to remove from `{problem_ref(contest_id, problem_index)}`:",
            view=TagDeleteView(
                self,
                interaction.user.id,
                guild_id,
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
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        try:
            allowed, reason = await self._can_modify_tags(interaction.user, guild_id, contest_id, problem_index)
        except CFRequestError as exc:
            await self._send_ephemeral(interaction, self._cf_error_message(exc))
            return
        if not allowed:
            await self._send_ephemeral(interaction, reason)
            return
        removed = await self._repo.remove_gym_problem_tag(guild_id, contest_id, problem_index, tag)
        if removed:
            await self._send_ephemeral(
                interaction,
                f"Removed tag `{tag}` from `{problem_ref(contest_id, problem_index)}`.",
            )
            return
        await self._send_ephemeral(interaction, "Tag not found.")

    async def _show_tags(self, interaction: discord.Interaction, contest_id: int, problem_index: Optional[str]) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        tags = await self._repo.list_gym_problem_tags(guild_id, contest_id)
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
        await send_interaction_lines_chunks(interaction, lines, ephemeral=True)

    async def _rate_problem(self, interaction: discord.Interaction, contest_id: int, problem_index: str, rating: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        if rating < 300 or rating > 5000:
            await interaction.response.send_message("Estimated rating must be between 300 and 5000.", ephemeral=True)
            return
        verified = await self._repo.get_by_discord_id(interaction.user.id, guild_id)
        if verified is None:
            await interaction.response.send_message("You must be verified to rate problems.", ephemeral=True)
            return

        await self._repo.upsert_gym_problem_rating_vote(
            guild_id,
            contest_id,
            problem_index,
            interaction.user.id,
            rating,
        )
        summary = await self._problem_rating_summary(guild_id, contest_id, problem_index)
        await interaction.response.send_message(
            f"Saved vote for `{problem_ref(contest_id, problem_index)}`.\n"
            f"Votes: **{int(summary['count'])}** | Avg: **{summary['avg']:.1f}** | "
            f"Weighted Avg: **{summary['weighted_avg']:.1f}**",
            ephemeral=True,
        )

    async def _show_problem(self, interaction: discord.Interaction, contest_id: int, problem_index: str) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return
        tags = await self._repo.list_gym_problem_tags(guild_id, contest_id)
        tags_for_problem = sorted({row.tag for row in tags if row.problem_index == problem_index})
        summary = await self._problem_rating_summary(guild_id, contest_id, problem_index)

        tags_text = ", ".join(tags_for_problem) if tags_for_problem else "No tags"
        if summary["count"] <= 0:
            rating_text = "No verified votes yet"
        else:
            rating_text = (
                f"Votes: {int(summary['count'])} | "
                f"Avg: {summary['avg']:.1f} | "
                f"Weighted Avg: {summary['weighted_avg']:.1f}"
            )
        await send_interaction_text_chunks(
            interaction,
            f"**{problem_ref(contest_id, problem_index)}**\nTags: {tags_text}\nRatings: {rating_text}",
            ephemeral=True,
        )

    async def _rate_gym_quality(self, interaction: discord.Interaction, contest_id: int, quality: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if quality < 1 or quality > 5:
            await interaction.response.send_message("Quality must be from 1 to 5.", ephemeral=True)
            return

        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id):
            return

        verified = await self._repo.get_by_discord_id(interaction.user.id, guild_id)
        if verified is None:
            await interaction.response.send_message("You must be verified to rate gym quality.", ephemeral=True)
            return

        await self._repo.upsert_gym_quality_vote(
            guild_id,
            contest_id,
            interaction.user.id,
            quality,
        )
        summary = await self._gym_quality_summary(guild_id, contest_id)
        await interaction.response.send_message(
            f"Saved quality vote for gym `{contest_id}`.\n"
            f"Votes: **{int(summary['count'])}** | Avg: **{summary['avg']:.2f}/5** | "
            f"Weighted Avg: **{summary['weighted_avg']:.2f}/5**",
            ephemeral=True,
        )

    async def _show_gym_quality(self, interaction: discord.Interaction, contest_id: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id, not_found_message="Gym not found."):
            return

        summary = await self._gym_quality_summary(guild_id, contest_id)
        if summary["count"] <= 0:
            await interaction.response.send_message(
                f"No verified quality votes yet for gym `{contest_id}`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Gym `{contest_id}` quality\n"
            f"Votes: **{int(summary['count'])}**\n"
            f"Average: **{summary['avg']:.2f}/5**\n"
            f"Weighted average: **{summary['weighted_avg']:.2f}/5**",
            ephemeral=True,
        )

    async def _reset_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        member = self._member_from_interaction(interaction)
        if member is None:
            await interaction.response.send_message("Could not validate your permissions.", ephemeral=True)
            return
        if not await self._is_coach(member, guild_id):
            await interaction.response.send_message("Only users with a coach role can reset gyms.", ephemeral=True)
            return
        if not await self._ensure_gym_exists(interaction, contest_id, guild_id=guild_id, not_found_message="Gym not found."):
            return
        await self._repo.reset_gym_contest_data(guild_id, contest_id)
        await interaction.response.send_message(
            f"Reset tags, problem ratings, quality votes, and cache for gym `{contest_id}`.",
            ephemeral=True,
        )

    async def _delete_gym(self, interaction: discord.Interaction, contest_id: int) -> None:
        guild_id = self._guild_id_from_interaction(interaction)
        if guild_id is None:
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        member = self._member_from_interaction(interaction)
        if member is None:
            await interaction.response.send_message("Could not validate your permissions.", ephemeral=True)
            return
        if not await self._is_coach(member, guild_id):
            await interaction.response.send_message("Only users with a coach role can delete gyms.", ephemeral=True)
            return
        deleted = await self._repo.delete_gym_contest(guild_id, contest_id)
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
