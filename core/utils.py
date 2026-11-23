"""Embed builders, duration parsing and permission helpers."""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import discord
from discord.ext import commands

from core.config import (
    BRAND_COLOR,
    ERROR_COLOR,
    FOOTER_TEXT,
    INFO_COLOR,
    SUCCESS_COLOR,
    WARNING_COLOR,
)

_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.IGNORECASE)

_UNIT_SECONDS = {
    "w": 604800,
    "d": 86400,
    "h": 3600,
    "m": 60,
    "s": 1,
}


def parse_duration(value: str, *, default_unit: str | None = None) -> int | None:
    """Turn strings like ``1h30m`` or ``10`` into seconds."""
    if not value:
        return None
    text = value.strip()
    matches = _DURATION_RE.findall(text)
    if not matches:
        if default_unit and text.isdigit():
            return int(text) * _UNIT_SECONDS.get(default_unit, 60)
        return None
    total = sum(int(amount) * _UNIT_SECONDS[unit.lower()] for amount, unit in matches)
    return total or None


def fmt_duration(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    parts: list[str] = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{label}")
    return " ".join(parts) or "0s"


def is_whitelisted(member: discord.Member, data: dict) -> bool:
    """True when the member's ID or one of their roles is whitelisted."""
    whitelist = data.get("whitelist") or {}
    if member.id in set(whitelist.get("user_ids") or []):
        return True
    role_ids = set(whitelist.get("role_ids") or [])
    permanent_role_id = whitelist.get("role_id")
    if permanent_role_id:
        role_ids.add(permanent_role_id)
    if not role_ids:
        return False
    return any(role.id in role_ids for role in getattr(member, "roles", []) or [])


def parse_color(value: str | None, default: int = BRAND_COLOR) -> int:
    if not value:
        return default
    cleaned = value.strip().lstrip("#").removeprefix("0x")
    if len(cleaned) != 6:
        return default
    try:
        return int(cleaned, 16)
    except ValueError:
        return default


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def base_embed(
    title: str | None = None,
    description: str | None = None,
    *,
    color: int = BRAND_COLOR,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def success_embed(description: str, title: str = "Success") -> discord.Embed:
    return base_embed(title, description, color=SUCCESS_COLOR)


def error_embed(description: str, title: str = "Something went wrong") -> discord.Embed:
    return base_embed(title, description, color=ERROR_COLOR)


def info_embed(description: str, title: str | None = None) -> discord.Embed:
    return base_embed(title, description, color=INFO_COLOR)


def warning_embed(description: str, title: str = "Heads up") -> discord.Embed:
    return base_embed(title, description, color=WARNING_COLOR)


def hierarchy_problem(ctx: commands.Context, member: discord.Member) -> str | None:
    """Return a human readable reason why the invoker may not act on ``member``."""
    if member.id == ctx.author.id:
        return "You cannot use this on yourself."
    if member.id == ctx.bot.user.id:
        return "You cannot use this on me."
    if member.id == ctx.guild.owner_id:
        return "You cannot use this on the server owner."
    if ctx.author.id != ctx.guild.owner_id and ctx.author.top_role <= member.top_role:
        return "That member has a role equal to or higher than your highest role."
    if member.guild.me.top_role <= member.top_role:
        return "That member has a role equal to or higher than my highest role."
    return None


def chunk(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


class ConfirmView(discord.ui.View):
    """Yes / no prompt that only the invoking member can answer."""

    def __init__(
        self,
        author: discord.abc.User,
        *,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        danger: bool = True,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author = author
        self.value: bool | None = None

        confirm = discord.ui.Button(
            label=confirm_label,
            emoji="✅",
            style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
        )
        cancel = discord.ui.Button(
            label=cancel_label, emoji="✖️", style=discord.ButtonStyle.secondary
        )
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                embed=error_embed("This confirmation is not for you."), ephemeral=True
            )
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()

    @classmethod
    async def prompt(
        cls,
        ctx: commands.Context,
        message: str,
        *,
        confirm_label: str = "Confirm",
        danger: bool = True,
    ) -> bool:
        view = cls(
            ctx.author, confirm_label=confirm_label, danger=danger, timeout=60.0
        )
        sent = await ctx.send(embed=warning_embed(message), view=view)
        await view.wait()
        view.stop()
        try:
            await sent.edit(view=None)
        except discord.HTTPException:
            pass
        return bool(view.value)


def bullet_lines(lines: Iterable[str]) -> str:
    return "\n".join(f"• {line}" for line in lines) or "*Nothing here yet.*"
