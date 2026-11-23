"""Account-age and alt-risk checks for verification and trading."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.agecheck import (
    account_age_days,
    age_problem,
    alt_flags,
    join_age_days,
)
from core.config import SUCCESS_COLOR, WARNING_COLOR
from core.utils import error_embed, info_embed, success_embed

log = logging.getLogger(__name__)


class AgeCheck(commands.Cog):
    """Require a minimum account age and inspect likely alt accounts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _minimum(self, guild: discord.Guild) -> int:
        try:
            return max(
                0,
                int(
                    self.bot.db.guild(guild.id).get("min_account_age_days")
                    or 0
                ),
            )
        except (TypeError, ValueError):
            return 0

    async def _status(self, ctx: commands.Context) -> None:
        minimum = self._minimum(ctx.guild)
        if minimum:
            rule = (
                f"Accounts younger than **{minimum} days** are blocked from "
                "verification and purchase tickets."
            )
        else:
            rule = (
                "No minimum is set. Use `/agecheck set 7` to start enforcing "
                "a 7-day minimum."
            )
        embed = info_embed(
            f"**Minimum account age:** "
            f"{f'{minimum} days' if minimum else 'disabled'}\n\n"
            f"{rule}\n\n"
            f"**Change it:** `{ctx.prefix}agecheck set <days>` "
            "(0 turns the check off)\n"
            f"**Inspect a member:** `{ctx.prefix}agecheck member @Member`",
            title="🕒 Account age checks",
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_group(
        name="agecheck",
        aliases=["age", "accountage"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def agecheck(self, ctx: commands.Context) -> None:
        """Set and inspect the minimum account age."""
        await self._status(ctx)

    @agecheck.command(name="set", usage="<days>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(days="Minimum account age in days; 0 disables")
    async def set_age(self, ctx: commands.Context, days: int) -> None:
        """Set the minimum account age required to verify or open a ticket."""
        if days < 0 or days > 3650:
            await ctx.reply(
                embed=error_embed("Use a number between **0** and **3650** days."),
                mention_author=False,
            )
            return

        self.bot.db.guild(ctx.guild.id)["min_account_age_days"] = int(days)
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🕒 Account-age rule updated",
            description=(
                "Account-age enforcement is now **disabled**."
                if days == 0
                else f"Accounts younger than **{days} days** are blocked from "
                "verification and purchase tickets."
            ),
            color=SUCCESS_COLOR if days else WARNING_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )

        if days == 0:
            text = (
                "Account-age checks are **off**.\n"
                "Verification and ticket opening no longer enforce a minimum."
            )
        else:
            text = (
                f"Accounts must now be at least **{days} days** old to verify "
                "or open a purchase ticket.\n"
                "Existing members are checked when they verify or trade."
            )
        await ctx.reply(
            embed=success_embed(text, title="🕒 Account-age rule saved"),
            mention_author=False,
        )

    @agecheck.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current minimum account age."""
        await self._status(ctx)

    @agecheck.command(name="member", aliases=["check"], usage="<member>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(member="Member to inspect for alt indicators")
    async def member(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Inspect an account's age and likely alt indicators."""
        minimum = self._minimum(ctx.guild)
        age = account_age_days(member)
        joined = join_age_days(member)
        problem = age_problem(member, minimum)
        flags = alt_flags(member, minimum)

        embed = discord.Embed(
            title=f"🕒 Account check — {member}",
            color=WARNING_COLOR if problem or flags else SUCCESS_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Account age",
            value=f"`{age:g} days`\n{discord.utils.format_dt(member.created_at, 'R')}",
            inline=True,
        )
        embed.add_field(
            name="Joined server",
            value=(
                f"`{joined:g} days`\n"
                f"{discord.utils.format_dt(member.joined_at, 'R')}"
                if joined is not None
                else "unknown"
            ),
            inline=True,
        )
        embed.add_field(
            name="Minimum required",
            value=f"`{minimum} days`" if minimum else "*disabled*",
            inline=True,
        )
        embed.add_field(
            name="Result",
            value=(
                f"❌ **Too young** — {problem}"
                if problem
                else "✅ **Passes** the account-age rule."
            ),
            inline=False,
        )
        embed.add_field(
            name="Risk indicators",
            value=(
                "\n".join(f"• {flag}" for flag in flags)
                if flags
                else "No obvious alt indicators."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Heuristic only — these clues are not proof of an alt account."
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgeCheck(bot))
