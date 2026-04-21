"""Coach secretary service for waiting-room approval workflow."""

from __future__ import annotations

import abc
import logging
from typing import Optional

import discord

from db.repository import CoachConfig, UserRepositoryBase

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 300


class CoachSecretaryBase(abc.ABC):
    """Abstraction for coach-routing logic."""

    @abc.abstractmethod
    async def handle_waiting_member(self, member: discord.Member, guild: discord.Guild) -> None:
        """Process a member who joined the waiting room."""

    @abc.abstractmethod
    async def get_config(self, guild_id: int) -> Optional[CoachConfig]:
        """Return coach config for a guild."""

    @abc.abstractmethod
    async def save_config(self, config: CoachConfig) -> None:
        """Persist coach config for a guild."""

    @abc.abstractmethod
    async def remove_config(self, guild_id: int) -> bool:
        """Delete coach config for a guild."""

    @abc.abstractmethod
    def clear_notification(self, guild_id: int, member_id: int) -> None:
        """Clear dedupe state for a pending member request."""


class ApprovalView(discord.ui.View):
    """DM button view sent to the coach for approval."""

    def __init__(
        self,
        secretary: "CoachSecretary",
        member: discord.Member,
        guild: discord.Guild,
        config: CoachConfig,
    ) -> None:
        super().__init__(timeout=REQUEST_TIMEOUT_SECONDS)
        self._secretary = secretary
        self._member = member
        self._guild = guild
        self._config = config
        self._responded = False

    def _finalize(self) -> None:
        self._secretary.clear_notification(self._guild.id, self._member.id)
        self.stop()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self._responded:
            await interaction.response.send_message("You already responded.", ephemeral=True)
            return
        self._responded = True

        member = self._guild.get_member(self._member.id)
        if member is None or member.voice is None or member.voice.channel is None:
            await interaction.response.edit_message(
                content=f"{self._member.display_name} is no longer in voice.",
                view=None,
            )
            self._finalize()
            return

        if member.voice.channel.id != self._config.waiting_room_id:
            await interaction.response.edit_message(
                content=f"{self._member.display_name} is no longer in the waiting room.",
                view=None,
            )
            self._finalize()
            return

        coach_channel = self._guild.get_channel(self._config.coach_channel_id)
        if not isinstance(coach_channel, discord.VoiceChannel):
            await interaction.response.edit_message(content="Coach room channel was not found.", view=None)
            self._finalize()
            return

        try:
            await member.move_to(coach_channel, reason="Coach accepted waiting-room request")
        except discord.Forbidden:
            await interaction.response.edit_message(content="Missing permissions to move that member.", view=None)
            self._finalize()
            return
        except discord.HTTPException as exc:
            await interaction.response.edit_message(content=f"Could not move member: {exc}", view=None)
            self._finalize()
            return

        await interaction.response.edit_message(
            content=f"Accepted. Moved {self._member.display_name} to {coach_channel.name}.",
            view=None,
        )
        logger.info("Coach accepted %s in guild %s", self._member.id, self._guild.id)
        self._finalize()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self._responded:
            await interaction.response.send_message("You already responded.", ephemeral=True)
            return
        self._responded = True

        await interaction.response.edit_message(
            content=f"Rejected {self._member.display_name}'s request.",
            view=None,
        )
        try:
            await self._member.send("The coach declined your request. Please try again later.")
        except discord.Forbidden:
            logger.warning("Cannot DM member %s after rejection", self._member.id)

        self._finalize()

    async def on_timeout(self) -> None:
        self._finalize()


class CoachSecretary(CoachSecretaryBase):
    """Concrete coach routing service with approval workflow."""

    def __init__(self, repo: UserRepositoryBase) -> None:
        self._repo = repo
        self._notified: dict[int, set[int]] = {}
        self._configs: dict[int, CoachConfig] = {}

    async def get_config(self, guild_id: int) -> Optional[CoachConfig]:
        cached = self._configs.get(guild_id)
        if cached is not None:
            return cached

        config = await self._repo.get_coach_config(guild_id)
        if config is not None:
            self._configs[guild_id] = config
        return config

    async def save_config(self, config: CoachConfig) -> None:
        await self._repo.upsert_coach_config(config)
        self._configs[config.guild_id] = config
        logger.info("Saved coach config for guild %s", config.guild_id)

    async def remove_config(self, guild_id: int) -> bool:
        removed = await self._repo.delete_coach_config(guild_id)
        self._configs.pop(guild_id, None)
        self._notified.pop(guild_id, None)
        return removed

    async def handle_waiting_member(self, member: discord.Member, guild: discord.Guild) -> None:
        config = await self.get_config(guild.id)
        if config is None:
            return

        notified = self._notified.setdefault(guild.id, set())
        if member.id in notified:
            return

        coach = guild.get_member(config.coach_id)
        if coach is None:
            logger.warning("Configured coach %s is not in guild %s", config.coach_id, guild.id)
            return

        notified.add(member.id)

        try:
            await coach.send(
                f"{member.display_name} is in the waiting room. Accept request?",
                view=ApprovalView(self, member, guild, config),
            )
        except discord.Forbidden:
            self.clear_notification(guild.id, member.id)
            logger.warning("Cannot DM coach %s in guild %s", coach.id, guild.id)
            return

        try:
            await member.send("The coach has been notified. Please wait for a response.")
        except discord.Forbidden:
            logger.info("Cannot DM waiting member %s in guild %s", member.id, guild.id)

    def clear_notification(self, guild_id: int, member_id: int) -> None:
        notified = self._notified.get(guild_id)
        if notified is None:
            return
        notified.discard(member_id)
        if not notified:
            self._notified.pop(guild_id, None)