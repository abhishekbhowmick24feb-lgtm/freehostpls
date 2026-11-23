"""Whitelist role and member management for locked channels and the honeypot."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from core.config import SUCCESS_COLOR
from core.utils import ConfirmView, error_embed, info_embed, success_embed

log = logging.getLogger(__name__)


class Whitelist(commands.Cog):
    """Trusted roles and people allowed to talk in locked channels."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)["whitelist"]

    def _permanent_role(self, guild: discord.Guild) -> discord.Role | None:
        config = self._config(guild)
        role_id = config.get("role_id")
        return guild.get_role(role_id) if role_id else None

    def _extra_roles(self, guild: discord.Guild) -> list[discord.Role]:
        return [
            role
            for role in (
                guild.get_role(role_id) for role_id in self._config(guild)["role_ids"]
            )
            if role is not None
        ]

    def _members(self, guild: discord.Guild) -> list[discord.Member]:
        return [
            member
            for member in (
                guild.get_member(member_id) for member_id in self._config(guild)["user_ids"]
            )
            if member is not None
        ]

    def _status_embed(self, guild: discord.Guild, prefix: str) -> discord.Embed:
        permanent = self._permanent_role(guild)
        extras = self._extra_roles(guild)
        members = self._members(guild)
        return info_embed(
            f"**Permanent role:** {permanent.mention if permanent else 'not set'}\n"
            f"**Extra roles:** {', '.join(role.mention for role in extras) or 'none'}\n"
            f"**Direct members:** {', '.join(member.mention for member in members) or 'none'}\n\n"
            "**Set the permanent role**\n"
            f"`{prefix}whitelist role:@Whitelisted`\n"
            "**Whitelist a member**\n"
            f"`{prefix}whitelist member:@Member`\n"
            "**Remove a member**\n"
            f"`{prefix}unwhitelist member:@Member`\n"
            "**Clear extra roles and direct members**\n"
            f"`{prefix}whitelistclear`\n\n"
            "Members with the permanent role can talk in channels locked with `lock` "
            "and are never softbanned by the honeypot.",
            title="✅ Whitelist",
        )

    @staticmethod
    async def _resolve(
        ctx: commands.Context, value: str
    ) -> tuple[str, discord.Role | discord.Member]:
        """Resolve a role or member by mention, ID or name (used by prefix)."""
        cleaned = value.strip()
        if cleaned.startswith("@") and not cleaned.startswith("<@"):
            cleaned = cleaned[1:].strip()

        target_id: int | None = None
        mention = re.fullmatch(r"<@[&!]?(\d+)>", cleaned)
        if mention:
            target_id = int(mention.group(1))
        elif cleaned.isdigit():
            target_id = int(cleaned)

        if target_id is not None:
            role = ctx.guild.get_role(target_id)
            if role is not None:
                return "role", role
            member = ctx.guild.get_member(target_id)
            if member is not None:
                return "user", member
            raise commands.BadArgument(
                "No role or member with that ID exists in this server."
            )

        try:
            role = await commands.RoleConverter().convert(ctx, cleaned)
            return "role", role
        except commands.BadArgument:
            pass
        try:
            member = await commands.MemberConverter().convert(ctx, cleaned)
            return "user", member
        except commands.BadArgument:
            pass
        raise commands.BadArgument(
            "Mention a role or a member, or use their exact name or ID."
        )

    def _is_manager(self, ctx: commands.Context) -> bool:
        return (
            ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.manage_guild
        )

    async def _require_manager(self, ctx: commands.Context) -> bool:
        if self._is_manager(ctx):
            return True
        await ctx.reply(
            embed=error_embed("You need the **Manage Server** permission."),
            mention_author=False,
        )
        return False

    @staticmethod
    def _role_problem(ctx: commands.Context, role: discord.Role) -> str | None:
        if role.is_default():
            return "You cannot use the @everyone role."
        if role.managed:
            return "That role is managed by an integration."
        if role >= ctx.guild.me.top_role:
            return "That role is higher than my highest role."
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return "That role is higher than your highest role."
        return None

    async def _set_permanent_role_obj(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        if not await self._require_manager(ctx):
            return

        problem = self._role_problem(ctx, role)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        config = self._config(ctx.guild)
        config["role_id"] = role.id
        if role.id in config["role_ids"]:
            config["role_ids"].remove(role.id)
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="✅ Whitelist role set",
            description=f"The permanent whitelist role is now {role.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{role.mention}** is now the permanent whitelist role.\n"
                f"Whitelist any member with `/whitelist member:@Member`.",
                title="✅ Permanent whitelist role",
            ),
            mention_author=False,
        )

    async def _set_permanent_role(self, ctx: commands.Context, value: str) -> None:
        if not await self._require_manager(ctx):
            return

        if value.strip().lower() in {"none", "clear", "off", "reset", "disable"}:
            config = self._config(ctx.guild)
            config["role_id"] = None
            await self.bot.db.save()
            await self.bot.audit(
                ctx.guild,
                title="✅ Whitelist role cleared",
                description="The permanent whitelist role was cleared.",
                color=SUCCESS_COLOR,
                fields=[("By", ctx.author.mention, True)],
            )
            await ctx.reply(
                embed=success_embed("The permanent whitelist role was cleared."),
                mention_author=False,
            )
            return

        kind, target = await self._resolve(ctx, value)
        if kind != "role":
            await ctx.reply(
                embed=error_embed(
                    "That is a member. Point the command at a role, for example "
                    f"`/whitelist role:@Whitelisted`."
                ),
                mention_author=False,
            )
            return
        await self._set_permanent_role_obj(ctx, target)

    async def _grant(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await self._require_manager(ctx):
            return

        role = self._permanent_role(ctx.guild)
        if role is None:
            await ctx.reply(
                embed=error_embed(
                    "No permanent whitelist role is set yet.\n"
                    "Set one with `/whitelist role:@Whitelisted`, then use "
                    "`/whitelist member:@Member`."
                ),
                mention_author=False,
            )
            return

        problem = self._role_problem(ctx, role)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        if role in member.roles:
            await ctx.reply(
                embed=info_embed(f"**{member}** already has {role.mention}."),
                mention_author=False,
            )
            return

        try:
            await member.add_roles(
                role, reason=f"{ctx.author} ({ctx.author.id}): whitelist"
            )
        except discord.Forbidden:
            await ctx.reply(
                embed=error_embed("I do not have permission to give that role."),
                mention_author=False,
            )
            return
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected the role change: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="✅ Member whitelisted",
            description=f"Gave {role.mention} to {member.mention}.",
            color=SUCCESS_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** is now whitelisted with {role.mention}.\n"
                "They can talk in locked channels and are exempt from the honeypot.",
                title="✅ Whitelisted",
            ),
            mention_author=False,
        )

    async def _remove_member(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        if not await self._require_manager(ctx):
            return

        config = self._config(ctx.guild)
        removed_parts: list[str] = []

        role = self._permanent_role(ctx.guild)
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(
                    role, reason=f"{ctx.author} ({ctx.author.id}): unwhitelist"
                )
                removed_parts.append(role.mention)
            except (discord.Forbidden, discord.HTTPException) as exc:
                await ctx.reply(
                    embed=error_embed(f"I could not remove the role: {exc}"),
                    mention_author=False,
                )
                return

        if member.id in config["user_ids"]:
            config["user_ids"].remove(member.id)
            removed_parts.append("direct whitelist")

        if not removed_parts:
            await ctx.reply(
                embed=info_embed(f"**{member}** is not whitelisted."),
                mention_author=False,
            )
            return

        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="✅ Member unwhitelisted",
            description=f"Removed {', '.join(removed_parts)} from {member.mention}.",
            color=SUCCESS_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** is no longer whitelisted.",
                title="Whitelist updated",
            ),
            mention_author=False,
        )

    async def _clear(self, ctx: commands.Context, clear_all: bool = False) -> None:
        if not await self._require_manager(ctx):
            return

        config = self._config(ctx.guild)
        has_extras = bool(config["role_ids"] or config["user_ids"])
        has_permanent = bool(config.get("role_id"))

        if not has_extras and not (clear_all and has_permanent):
            message = "The whitelist is already empty."
            if has_permanent and not clear_all:
                message = (
                    "Only the permanent whitelist role is set. Use "
                    "`/whitelistclear all:true` to clear it."
                )
            await ctx.reply(embed=info_embed(message), mention_author=False)
            return

        message = "Remove every extra role and direct member from the whitelist?"
        if clear_all:
            message = (
                "Remove every extra role, direct member, and the permanent "
                "whitelist role?"
            )
        if not await ConfirmView.prompt(ctx, message):
            return

        config["role_ids"].clear()
        config["user_ids"].clear()
        if clear_all:
            config["role_id"] = None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🧹 Whitelist cleared",
            description=(
                "Everything was removed from the whitelist."
                if clear_all
                else "Extra roles and direct members were removed from the whitelist."
            ),
            color=0xFEE75C,
            fields=[("By", ctx.author.mention, True)],
        )
        text = "The whitelist is now empty."
        if not clear_all and has_permanent:
            role = self._permanent_role(ctx.guild)
            text = (
                "Extra roles and direct members were cleared. "
                f"The permanent role {role.mention if role else ''} is still set."
            )
        await ctx.reply(embed=success_embed(text), mention_author=False)

    async def _show_status(self, ctx: commands.Context) -> None:
        await ctx.reply(
            embed=self._status_embed(ctx.guild, ctx.prefix or "/"),
            mention_author=False,
        )

    # --------------------------------------------------------------- commands

    @commands.hybrid_command(
        name="whitelist",
        aliases=["wl"],
        usage="[member] [role]",
    )
    @commands.guild_only()
    @discord.app_commands.describe(
        member="Member to whitelist with the permanent whitelist role",
        role="Role to use as the permanent whitelist role",
    )
    async def whitelist(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        role: discord.Role | None = None,
    ) -> None:
        """Whitelist a member, or set the permanent whitelist role."""
        if member is not None and role is not None:
            await ctx.reply(
                embed=error_embed("Choose either a member or a role, not both."),
                mention_author=False,
            )
            return
        if role is not None:
            await self._set_permanent_role_obj(ctx, role)
            return
        if member is not None:
            await self._grant(ctx, member)
            return
        await self._show_status(ctx)

    @commands.command(name="whitelistrole", aliases=["wlrole"])
    @commands.guild_only()
    async def whitelistrole(
        self, ctx: commands.Context, *, role: str | None = None
    ) -> None:
        """Prefix command to set or clear the permanent whitelist role."""
        if not role:
            current = self._permanent_role(ctx.guild)
            await ctx.reply(
                embed=info_embed(
                    f"**Permanent whitelist role:** "
                    f"{current.mention if current else 'not set'}\n"
                    f"Use `{ctx.prefix}whitelistrole @Role` or `{ctx.prefix}whitelistrole none`."
                ),
                mention_author=False,
            )
            return
        await self._set_permanent_role(ctx, role)

    @commands.hybrid_command(name="unwhitelist", aliases=["wlremove"], usage="<member>")
    @commands.guild_only()
    @discord.app_commands.describe(member="Member to remove from the whitelist")
    async def unwhitelist(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Remove the permanent whitelist role from a member."""
        await self._remove_member(ctx, member)

    @commands.hybrid_command(name="whitelistclear", aliases=["wlclear"], usage="[all]")
    @commands.guild_only()
    @discord.app_commands.describe(
        all="Also clear the permanent whitelist role, not just extra roles and members"
    )
    async def whitelistclear(
        self, ctx: commands.Context, all: bool = False
    ) -> None:
        """Clear extra whitelist roles and direct members."""
        await self._clear(ctx, all)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Whitelist(bot))
