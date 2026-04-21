"""Coach Secretary service — manages coach routing with approval flow.

Responsibilities (SRP):
- Send approval requests to the coach (with Accept/Reject buttons)
- Move members on approval
- Deduplicate notifications
"""

from __future__ import annotations

import abc
import logging
from typing import Optional

import discord

from db.repository import CoachConfig, UserRepositoryBase

logger = logging.getLogger(__name__)


class CoachSecretaryBase(abc.ABC):
    """Abstraction for coach-routing logic (DIP)."""

    @abc.abstractmethod
    async def handle_waiting_member(
        self,
        member: discord.Member,
        guild: discord.Guild,
    ) -> None:
        """Process a member who just joined the waiting room."""

    @abc.abstractmethod
    async def get_config(self, guild_id: int) -> Optional[CoachConfig]:
        """Return the coach config for a guild."""

    @abc.abstractmethod
    async def save_config(self, config: CoachConfig) -> None:
        """Save coach config for a guild."""

    @abc.abstractmethod
    async def remove_config(self, guild_id: int) -> bool:
        """Remove coach config for a guild."""


class ApprovalView(discord.ui.View):
    """Discord button view sent to the coach for approval."""

    def __init__(
        self,
        secretary: "CoachSecretary",
        member: discord.Member,
        guild: discord.Guild,
        config: CoachConfig,
    ) -> None:
        super().__init__(timeout=300)  # 5 minute timeout
        self._secretary = secretary
        self._member = member
        self._guild = guild
        self._config = config
        self._responded = False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._responded:
            await interaction.response.send_message("You already responded.", ephemeral=True)
            return
        self._responded = True

        # Check if member is still in the waiting room.
        member = self._guild.get_member(self._member.id)
        if member is None or member.voice is None or member.voice.channel is None:
            await interaction.response.edit_message(
                content=f"⚠️ **{self._member.display_name}** has already disconnected.",
                view=None,
            )
            return

        # Move to coach's office.
        coach_channel = self._guild.get_channel(self._config.coach_channel_id)
        if not isinstance(coach_channel, discord.VoiceChannel):
            await interaction.response.edit_message(
                content="❌ Coach office channel not found.",
                view=None,
            )
            return

        try:
            await member.move_to(coach_channel, reason="Coach accepted the request")
            await interaction.response.edit_message(
                content=f"✅ **{self._member.display_name}** has been moved to your office.",
                view=None,
            )
            logger.info("Coach accepted %s — moved to office", self._member)
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="❌ Missing permissions to move the member.",
                view=None,
            )
        except discord.HTTPException as exc:
            await interaction.response.edit_message(
                content=f"❌ Could not move member: {exc}",
                view=None,
            )

        # Clean up deduplication.
        self._secretary.clear_notification(self._guild.id, self._member.id)
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._responded:
            await interaction.response.send_message("You already responded.", ephemeral=True)
            return
        self._responded = True

        await interaction.response.edit_message(
            content=f"❌ You rejected **{self._member.display_name}**'s request.",
            view=None,
        )

        # Notify the waiting member.
        try:
            await self._member.send("❌ The coach is currently busy. Please try again later.")
        except discord.Forbidden:
            logger.warning("Cannot DM %s", self._member)

        self._secretary.clear_notification(self._guild.id, self._member.id)
        self.stop()

    async def on_timeout(self) -> None:
        """Called when the coach doesn't respond in time."""
        self._secretary.clear_notification(self._guild.id, self._member.id)


class CoachSecretary(CoachSecretaryBase):
    """Concrete coach routing service with approval flow."""

    def __init__(self, repo: UserRepositoryBase) -> None:
        self._repo = repo

        # Per-guild deduplication: guild_id → set of notified member IDs.
        self._notified: dict[int, set[int]] = {}

        # Cache of configs loaded from DB.
        self._configs: dict[int, CoachConfig] = {}

    # ── config management ──────────────────────────────────────

    async def get_config(self, guild_id: int) -> Optional[CoachConfig]:
        if guild_id in self._configs:
            return self._configs[guild_id]
        config = await self._repo.get_coach_config(guild_id)
        if config is not None:
            self._configs[guild_id] = config
        return config

    async def save_config(self, config: CoachConfig) -> None:
        await self._repo.upsert_coach_config(config)
        self._configs[config.guild_id] = config
        logger.info("Coach config saved for guild %s", config.guild_id)

    async def remove_config(self, guild_id: int) -> bool:
        removed = await self._repo.delete_coach_config(guild_id)
        self._configs.pop(guild_id, None)
        self._notified.pop(guild_id, None)
        return removed

    # ── routing ────────────────────────────────────────────────

    async def handle_waiting_member(
        self,
        member: discord.Member,
        guild: discord.Guild,
    ) -> None:
        config = await self.get_config(guild.id)
        if config is None:
            return

        # Deduplicate: don't spam the coach for the same member.
        notified = self._notified.setdefault(guild.id, set())
        if member.id in notified:
            return
        notified.add(member.id)

        # Find the coach in the guild.
        coach = guild.get_member(config.coach_id)
        if coach is None:
            logger.warning("Coach (ID %s) not found in guild %s", config.coach_id, guild.id)
            return

        # Send approval request to the coach.
        view = ApprovalView(self, member, guild, config)
        try:
            await coach.send(
                f"📣 **{member.display_name}** is waiting in the Secretary. Accept?",
                view=view,
            )
            logger.info("Sent approval request to coach for %s", member)
        except discord.Forbidden:
            logger.warning("Cannot DM coach — DMs may be disabled")
            notified.discard(member.id)
            return

        # Notify the waiting member.
        try:
            await member.send("⏳ The coach has been notified — please wait for approval.")
        except discord.Forbidden:
            logger.warning("Cannot DM %s", member)

    def clear_notification(self, guild_id: int, member_id: int) -> None:
        """Remove a member from the notified set."""
        notified = self._notified.get(guild_id)
        if notified:
            notified.discard(member_id)
