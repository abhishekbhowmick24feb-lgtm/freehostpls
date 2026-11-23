"""Shared account-age and alt-risk helpers."""

from __future__ import annotations

import math
import re
from datetime import timezone

import discord
from discord.ext import commands

AUTO_NAME_RE = re.compile(r"^[a-z]{4,}\d{4,}$", re.IGNORECASE)


def account_age_days(user: discord.abc.User) -> float:
    """Return the account age in days, rounded to one decimal place."""
    created = user.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    seconds = (discord.utils.utcnow() - created).total_seconds()
    return max(0.0, round(seconds / 86400, 1))


def min_age_days(bot: commands.Bot, guild: discord.Guild) -> int:
    """Read the guild's minimum account age (0 means disabled)."""
    try:
        return max(0, int(bot.db.guild(guild.id).get("min_account_age_days") or 0))
    except (TypeError, ValueError):
        return 0


def age_problem(member: discord.abc.User, minimum: int) -> str | None:
    """Return a user-facing reason when the account is too young."""
    if minimum <= 0:
        return None
    age = account_age_days(member)
    if age >= minimum:
        return None
    days_left = max(1, math.ceil(minimum - age))
    return (
        f"Your account must be at least **{minimum} days** old.\n"
        f"It is currently **{age:g} days** old — try again in about "
        f"**{days_left} day(s)**."
    )


def join_age_days(member: discord.Member) -> float | None:
    """How long ago the member joined this server, in days."""
    if member.joined_at is None:
        return None
    joined = member.joined_at
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    seconds = (discord.utils.utcnow() - joined).total_seconds()
    return max(0.0, round(seconds / 86400, 1))


def alt_flags(member: discord.Member, minimum: int) -> list[str]:
    """Heuristic risk indicators. These are clues, not proof of an alt."""
    flags: list[str] = []
    age = account_age_days(member)
    if minimum and age < minimum:
        flags.append(f"Account is only **{age:g} days** old (minimum {minimum}).")

    joined = join_age_days(member)
    if joined is not None and joined < 1:
        flags.append("Joined the server less than **24 hours** ago.")

    if member.avatar is None:
        flags.append("Has the default avatar.")

    if AUTO_NAME_RE.fullmatch(member.name) or (
        len(member.name) >= 8
        and sum(character.isdigit() for character in member.name) >= 4
    ):
        flags.append("Username looks auto-generated.")

    return flags
