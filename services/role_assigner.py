from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import discord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoleRule:
    """A single rating bracket to role mapping."""

    name: str
    colour: discord.Colour
    min_rating: int
    max_rating: Optional[int]


DEFAULT_RULES: list[RoleRule] = [
    RoleRule("Newbie", discord.Colour.greyple(), 0, 1199),
    RoleRule("Pupil", discord.Colour.green(), 1200, 1399),
    RoleRule("Specialist", discord.Colour.from_rgb(0, 255, 255), 1400, 1599),
    RoleRule("Expert", discord.Colour.blue(), 1600, 1899),
    RoleRule("Candidate Master", discord.Colour.purple(), 1900, 2099),
    RoleRule("Master", discord.Colour.orange(), 2100, 2399),
    RoleRule("Grandmaster", discord.Colour.red(), 2400, None),
]


class RoleAssignerBase(abc.ABC):
    """Abstraction for role assignment logic."""

    @abc.abstractmethod
    def role_for(self, rating: int) -> Optional[RoleRule]:
        """Return the matching role rule for ``rating``."""

    @abc.abstractmethod
    async def apply(
        self,
        member: discord.Member,
        guild: discord.Guild,
        rating: int,
    ) -> Optional[discord.Role]:
        """Ensure the correct rating role is applied to the member."""


class RoleAssigner(RoleAssignerBase):
    """Concrete role assigner driven by ``RoleRule`` definitions."""

    def __init__(self, rules: Sequence[RoleRule] = DEFAULT_RULES) -> None:
        self._rules = list(rules)

    @property
    def all_role_names(self) -> set[str]:
        return {rule.name for rule in self._rules}

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
            except discord.HTTPException as exc:
                logger.error("Discord error creating role %s: %s", rule.name, exc)
                return None

        stale_roles = [role_ for role_ in member.roles if role_.name in self.all_role_names and role_.id != role.id]
        if stale_roles:
            try:
                await member.remove_roles(*stale_roles, reason="CF rating role update")
            except discord.Forbidden:
                logger.warning("Could not remove stale roles from %s", member)
            except discord.HTTPException as exc:
                logger.warning("Discord error removing stale roles from %s: %s", member, exc)

        if role not in member.roles:
            try:
                await member.add_roles(role, reason="CF rating role assignment")
            except discord.Forbidden:
                logger.error("Missing permissions to assign role %s", rule.name)
                return None
            except discord.HTTPException as exc:
                logger.error("Discord error assigning role %s: %s", rule.name, exc)
                return None

        return role
