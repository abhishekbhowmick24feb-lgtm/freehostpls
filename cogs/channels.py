"""Give or remove a member's access to a private channel."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.config import SUCCESS_COLOR, WARNING_COLOR
from core.utils import error_embed, info_embed, success_embed

log = logging.getLogger(__name__)

ACCESS_PERMISSIONS = (
    "view_channel",
    "send_messages",
    "read_message_history",
    "attach_files",
    "embed_links",
)


class Channels(commands.Cog):
    """Let staff add a member to any private text channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _resolve_target(
        ctx: commands.Context, channel: discord.TextChannel | None
    ) -> tuple[discord.TextChannel | None, str | None]:
        target = channel or ctx.channel
        if isinstance(target, discord.Thread):
            return None, "Threads are not supported. Choose a normal text channel."
        if not isinstance(target, discord.TextChannel):
            return None, "Choose a normal text channel."
        return target, None

    @staticmethod
    def _manage_problem(
        ctx: commands.Context, target: discord.TextChannel
    ) -> str | None:
        me = ctx.guild.me
        if me is None:
            return "I could not find my server member."
        if not target.permissions_for(me).manage_channels:
            return f"I need **Manage Channels** in {target.mention}."
        return None

    async def _grant(
        self,
        ctx: commands.Context,
        member: discord.Member,
        target: discord.TextChannel,
    ) -> None:
        problem = self._manage_problem(ctx, target)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        overwrite = target.overwrites_for(member)
        changed: list[str] = []
        for permission in ACCESS_PERMISSIONS:
            if getattr(overwrite, permission) is not True:
                setattr(overwrite, permission, True)
                changed.append(permission)

        if not changed:
            await ctx.reply(
                embed=info_embed(
                    f"**{member}** already has explicit access to {target.mention}."
                ),
                mention_author=False,
            )
            return

        try:
            await target.set_permissions(
                member,
                overwrite=overwrite,
                reason=f"{ctx.author} ({ctx.author.id}): private channel access",
            )
        except discord.Forbidden:
            await ctx.reply(
                embed=error_embed(
                    f"I do not have permission to change access in {target.mention}."
                ),
                mention_author=False,
            )
            return
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected the permission change: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="🔐 Channel access granted",
            description=f"Added {member.mention} to {target.mention}.",
            color=SUCCESS_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("Channel", target.mention, True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** can now see and talk in {target.mention}.",
                title="🔐 Channel access added",
            ),
            mention_author=False,
        )

    async def _revoke(
        self,
        ctx: commands.Context,
        member: discord.Member,
        target: discord.TextChannel,
    ) -> None:
        problem = self._manage_problem(ctx, target)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        overwrite = target.overwrites_for(member)
        changed: list[str] = []
        for permission in ACCESS_PERMISSIONS:
            if getattr(overwrite, permission) is not None:
                setattr(overwrite, permission, None)
                changed.append(permission)

        if not changed:
            await ctx.reply(
                embed=info_embed(
                    f"**{member}** has no explicit access overwrite in {target.mention}."
                ),
                mention_author=False,
            )
            return

        try:
            await target.set_permissions(
                member,
                overwrite=overwrite,
                reason=f"{ctx.author} ({ctx.author.id}): private channel access removed",
            )
        except discord.Forbidden:
            await ctx.reply(
                embed=error_embed(
                    f"I do not have permission to change access in {target.mention}."
                ),
                mention_author=False,
            )
            return
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected the permission change: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="🔐 Channel access removed",
            description=f"Removed the explicit access overwrite for {member.mention} in {target.mention}.",
            color=WARNING_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("Channel", target.mention, True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"Removed **{member}**'s explicit access to {target.mention}.\n"
                "Role permissions may still give them access.",
                title="🔐 Channel access removed",
            ),
            mention_author=False,
        )

    # -------------------------------------------------------------- commands

    @commands.hybrid_group(
        name="channel",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def channel(self, ctx: commands.Context) -> None:
        """Add or remove a member's access to a private channel."""
        await ctx.reply(
            embed=info_embed(
                f"`{ctx.prefix}channel add @Member [#channel]`\n"
                f"`{ctx.prefix}channel remove @Member [#channel]`\n\n"
                "The channel defaults to the one you run the command in.",
                title="🔐 Private channel access",
            ),
            mention_author=False,
        )

    @channel.command(
        name="add",
        aliases=["allow", "grant"],
        usage="<member> [channel]",
    )
    @commands.guild_only()
    @is_moderator("manage_channels")
    @discord.app_commands.describe(
        member="Member to add to the private channel",
        channel="Private text channel; defaults to the current channel",
    )
    async def add(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Give a member access to a private channel."""
        target, problem = self._resolve_target(ctx, channel)
        if target is None:
            await ctx.reply(
                embed=error_embed(problem or "Choose a normal text channel."),
                mention_author=False,
            )
            return
        await self._grant(ctx, member, target)

    @channel.command(
        name="remove",
        aliases=["deny", "revoke"],
        usage="<member> [channel]",
    )
    @commands.guild_only()
    @is_moderator("manage_channels")
    @discord.app_commands.describe(
        member="Member to remove from the private channel",
        channel="Private text channel; defaults to the current channel",
    )
    async def remove(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Remove a member's explicit access to a private channel."""
        target, problem = self._resolve_target(ctx, channel)
        if target is None:
            await ctx.reply(
                embed=error_embed(problem or "Choose a normal text channel."),
                mention_author=False,
            )
            return
        await self._revoke(ctx, member, target)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Channels(bot))
