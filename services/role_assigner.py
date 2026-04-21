# services/role_assigner.py
# Rating → Discord role mapping with OCP-friendly rule list.
# Usage: assigner = RoleAssigner(); role_name, colour = assigner.role_for(1500)

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import discord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoleRule:
    """A single rating-bracket → role mapping.

    *min_rating* is inclusive, *max_rating* is inclusive (use ``None`` for
    unbounded upper end).  To add a new tier, simply append a ``RoleRule``
    to ``DEFAULT_RULES`` — no logic changes required (OCP).
    """

    name: str
    colour: discord.Colour
    min_rating: int
    max_rating: Optional[int]  # None → no upper bound


# Ordered lowest → highest so the first match wins.
DEFAULT_RULES: list[RoleRule] = [
    RoleRule("Newbie",           discord.Colour.greyple(),              0,    1199),
    RoleRule("Pupil",            discord.Colour.green(),             1200,    1399),
    RoleRule("Specialist",       discord.Colour.from_rgb(0, 255, 255), 1400, 1599),
    RoleRule("Expert",           discord.Colour.blue(),              1600,    1899),
    RoleRule("Candidate Master", discord.Colour.purple(),            1900,    2099),
    RoleRule("Master",           discord.Colour.orange(),            2100,    2399),
    RoleRule("Grandmaster",      discord.Colour.red(),               2400,    None),
]


class RoleAssignerBase(abc.ABC):
    """Abstraction for role-assignment logic (DIP)."""

    @abc.abstractmethod
    def role_for(self, rating: int) -> Optional[RoleRule]:
        """Return the matching ``RoleRule`` for *rating*, or ``None``."""

    @abc.abstractmethod
    async def apply(
        self,
        member: discord.Member,
        guild: discord.Guild,
        rating: int,
    ) -> Optional[discord.Role]:
        """Resolve the correct role, create it if missing, swap old CF roles,
        and return the newly-assigned role (or ``None`` on failure)."""


class RoleAssigner(RoleAssignerBase):
    """Concrete role assigner driven by a list of ``RoleRule`` dataclasses."""

    def __init__(self, rules: Sequence[RoleRule] = DEFAULT_RULES) -> None:
        self._rules = list(rules)

    # ── public API ─────────────────────────────────────────────

    @property
    def all_role_names(self) -> set[str]:
        return {r.name for r in self._rules}

    def role_for(self, rating: int) -> Optional[RoleRule]:
        for rule in self._rules:
            upper = rule.max_rating if rule.max_rating is not None else float("inf")
            if rule.min_rating <= rating <= upper:
                return rule
        return None

    async def apply(
        self,
        member: discord.Member,
        guild: discord.Guild,
        rating: int,
    ) -> Optional[discord.Role]:
        rule = self.role_for(rating)
        if rule is None:
            return None

        # Ensure the target role exists in the guild.
        role = discord.utils.get(guild.roles, name=rule.name)
        if role is None:
            try:
                role = await guild.create_role(
                    name=rule.name,
                    colour=rule.colour,
                    reason="Auto-created by CF verification bot",
                )
                logger.info("Created role %s in guild %s", rule.name, guild.name)
            except discord.Forbidden:
                logger.error("Missing permissions to create role %s", rule.name)
                return None

        # Remove any other CF-tier roles the member might have.
        stale = [r for r in member.roles if r.name in self.all_role_names and r.id != role.id]
        if stale:
            try:
                await member.remove_roles(*stale, reason="CF rating role update")
            except discord.Forbidden:
                logger.warning("Could not remove stale roles from %s", member)

        # Assign the new role.
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="CF rating role assignment")
            except discord.Forbidden:
                logger.error("Missing permissions to assign role %s", rule.name)
                return None

        return role
