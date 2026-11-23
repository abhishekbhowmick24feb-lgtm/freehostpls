"""Vouch and seller reputation system for the marketplace."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.config import SUCCESS_COLOR, WARNING_COLOR
from core.utils import error_embed, info_embed, success_embed, truncate

log = logging.getLogger(__name__)

VOUCH_COLOR = 0xF1C40F
VOUCH_COOLDOWN = 24 * 3600


class Vouches(commands.Cog):
    """Let verified buyers vouch for sellers and build reputation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _buyer_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = self._config(guild).get("buyer_role_id")
        return guild.get_role(role_id) if role_id else None

    def _vouch_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self._config(guild).get("vouch_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _records_for(config: dict, seller_id: int) -> list[dict]:
        return [
            record
            for record in config.get("vouches", [])
            if int(record.get("seller_id", 0)) == seller_id
        ]

    @staticmethod
    def _stats(records: list[dict]) -> dict:
        count = len(records)
        if not count:
            return {"count": 0, "avg": 0.0, "unique": 0, "five": 0}
        total = sum(int(record.get("stars", 0)) for record in records)
        buyers = {int(record.get("buyer_id", 0)) for record in records}
        five = sum(1 for record in records if int(record.get("stars", 0)) == 5)
        return {
            "count": count,
            "avg": total / count,
            "unique": len(buyers),
            "five": five,
        }

    @staticmethod
    def _stars_text(stars: int) -> str:
        stars = max(1, min(5, int(stars)))
        return f"{'⭐' * stars}{'☆' * (5 - stars)} (`{stars}/5`)"

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

    async def _show_setup(self, ctx: commands.Context) -> None:
        config = self._config(ctx.guild)
        role = self._buyer_role(ctx.guild)
        channel = self._vouch_channel(ctx.guild)
        await ctx.reply(
            embed=info_embed(
                f"**Buyer role:** {role.mention if role else 'not set'}\n"
                f"**Vouch channel:** {channel.mention if channel else 'not set'}\n"
                f"**Vouches recorded:** `{len(config.get('vouches', []))}`\n\n"
                f"Set the buyer role: `{ctx.prefix}vouchsetup role @Role`\n"
                f"Set the vouch channel: `{ctx.prefix}vouchsetup channel #channel`\n"
                f"Clear both: `{ctx.prefix}vouchsetup clear`",
                title="⭐ Vouch setup",
            ),
            mention_author=False,
        )

    # ---------------------------------------------------------------- vouch

    @commands.hybrid_command(
        name="vouch",
        usage="<seller> [stars] [comment]",
    )
    @commands.guild_only()
    @discord.app_commands.describe(
        seller="Member you bought from",
        stars="Rating from 1 to 5 (default 5)",
        comment="Optional short comment about the trade",
    )
    async def vouch(
        self,
        ctx: commands.Context,
        seller: discord.Member,
        stars: int = 5,
        *,
        comment: str | None = None,
    ) -> None:
        """Vouch for a seller after a completed purchase."""
        if seller.id == ctx.author.id:
            await ctx.reply(
                embed=error_embed("You cannot vouch for yourself."),
                mention_author=False,
            )
            return
        if seller.bot:
            await ctx.reply(
                embed=error_embed("You cannot vouch for a bot."),
                mention_author=False,
            )
            return
        if not 1 <= int(stars) <= 5:
            await ctx.reply(
                embed=error_embed("Stars must be between **1** and **5**."),
                mention_author=False,
            )
            return

        config = self._config(ctx.guild)
        buyer_role = self._buyer_role(ctx.guild)
        if buyer_role is None:
            await ctx.reply(
                embed=error_embed(
                    "The buyer role is not set yet.\n"
                    "Staff can set it with `/vouchsetup role`."
                ),
                mention_author=False,
            )
            return
        if buyer_role not in ctx.author.roles:
            await ctx.reply(
                embed=error_embed(
                    f"You need the {buyer_role.mention} role to vouch.\n"
                    "Ask staff to give it to you after a completed purchase."
                ),
                mention_author=False,
            )
            return

        vouch_channel = self._vouch_channel(ctx.guild)
        if vouch_channel is None:
            await ctx.reply(
                embed=error_embed(
                    "The vouch channel is not set yet.\n"
                    "Staff can set it with `/vouchsetup channel`."
                ),
                mention_author=False,
            )
            return
        if ctx.channel.id != vouch_channel.id:
            await ctx.reply(
                embed=error_embed(
                    f"Please use `/vouch` in {vouch_channel.mention}."
                ),
                mention_author=False,
            )
            return

        permissions = vouch_channel.permissions_for(ctx.guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            await ctx.reply(
                embed=error_embed(
                    f"I need **View Channel**, **Send Messages** and "
                    f"**Embed Links** in {vouch_channel.mention}."
                ),
                mention_author=False,
            )
            return

        now = discord.utils.utcnow().timestamp()
        for record in config.get("vouches", []):
            if (
                int(record.get("buyer_id", 0)) == ctx.author.id
                and int(record.get("seller_id", 0)) == seller.id
                and now - float(record.get("at", 0)) < VOUCH_COOLDOWN
            ):
                await ctx.reply(
                    embed=info_embed(
                        f"You already vouched for **{seller}** in the last 24 hours."
                    ),
                    mention_author=False,
                )
                return

        safe_comment = discord.utils.escape_mentions((comment or "").strip())
        record = {
            "buyer_id": ctx.author.id,
            "buyer": str(ctx.author),
            "seller_id": seller.id,
            "seller": str(seller),
            "stars": int(stars),
            "comment": truncate(safe_comment, 1000),
            "at": now,
        }
        records = self._records_for(config, seller.id) + [record]
        stats = self._stats(records)

        embed = discord.Embed(
            title=f"⭐ New vouch for {seller.display_name}",
            description=record["comment"] or "No comment provided.",
            color=VOUCH_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Rating", value=self._stars_text(int(stars)), inline=True
        )
        embed.add_field(name="Buyer", value=ctx.author.display_name, inline=True)
        embed.add_field(name="Seller", value=seller.mention, inline=True)
        embed.add_field(
            name="Total vouches", value=f"`{stats['count']}`", inline=True
        )
        embed.add_field(
            name="Average rating", value=f"`{stats['avg']:.2f}/5`", inline=True
        )
        embed.set_thumbnail(url=seller.display_avatar.url)

        config["vouch_seq"] = int(config.get("vouch_seq", 0)) + 1
        record["id"] = config["vouch_seq"]
        embed.set_footer(text=f"Vouch #{record['id']} • {ctx.guild.name}")

        try:
            await ctx.reply(embed=embed, mention_author=False)
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not post the vouch: {exc}"),
                mention_author=False,
            )
            return

        config.setdefault("vouches", []).append(record)
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="⭐ Vouch submitted",
            description=(
                f"{ctx.author.mention} vouched for {seller.mention} "
                f"with {int(stars)}/5."
            ),
            color=VOUCH_COLOR,
            fields=[
                ("Buyer", f"{ctx.author} (`{ctx.author.id}`)", True),
                ("Seller", f"{seller} (`{seller.id}`)", True),
                ("Vouch ID", f"#{record['id']}", True),
            ],
        )

    # -------------------------------------------------------------- profile

    @commands.hybrid_command(
        name="vouchprofile",
        aliases=["reputation", "rep"],
        usage="[member]",
    )
    @commands.guild_only()
    @discord.app_commands.describe(member="Seller to show; defaults to you")
    async def vouchprofile(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show a seller's vouches, average rating and recent feedback."""
        member = member or ctx.author
        config = self._config(ctx.guild)
        records = self._records_for(config, member.id)
        if not records:
            await ctx.reply(
                embed=info_embed(f"**{member}** has no vouches yet."),
                mention_author=False,
            )
            return

        stats = self._stats(records)
        embed = discord.Embed(
            title=f"⭐ {member.display_name}'s reputation",
            color=VOUCH_COLOR,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Vouches", value=f"`{stats['count']}`", inline=True)
        embed.add_field(
            name="Average rating", value=f"`{stats['avg']:.2f}/5`", inline=True
        )
        embed.add_field(
            name="Unique buyers", value=f"`{stats['unique']}`", inline=True
        )
        embed.add_field(
            name="5-star vouches", value=f"`{stats['five']}`", inline=True
        )

        recent = sorted(
            records, key=lambda item: float(item.get("at", 0)), reverse=True
        )[:5]
        lines: list[str] = []
        for record in recent:
            buyer_id = int(record.get("buyer_id", 0))
            buyer = ctx.guild.get_member(buyer_id)
            buyer_text = buyer.display_name if buyer else record.get("buyer", "Unknown")
            when = f"<t:{int(float(record.get('at', 0)))}:R>"
            text = record.get("comment") or "No comment."
            lines.append(
                f"{self._stars_text(int(record.get('stars', 0)))} — "
                f"**{buyer_text}** {when}\n{truncate(text, 180)}"
            )
        embed.add_field(
            name="Recent vouches",
            value=truncate("\n\n".join(lines), 1024) or "None",
            inline=False,
        )
        embed.set_footer(text=f"{stats['count']} vouch(es) • {ctx.guild.name}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        name="vouchleaderboard",
        aliases=["vouchtop", "topvouches"],
    )
    @commands.guild_only()
    async def vouchleaderboard(self, ctx: commands.Context) -> None:
        """Show the top sellers by vouch count."""
        config = self._config(ctx.guild)
        records = config.get("vouches", [])
        if not records:
            await ctx.reply(
                embed=info_embed("No vouches have been recorded yet."),
                mention_author=False,
            )
            return

        grouped: dict[int, list[dict]] = {}
        for record in records:
            seller_id = int(record.get("seller_id", 0))
            grouped.setdefault(seller_id, []).append(record)

        ranked = sorted(
            grouped.items(),
            key=lambda item: (len(item[1]), self._stats(item[1])["avg"]),
            reverse=True,
        )[:10]

        lines: list[str] = []
        for index, (seller_id, seller_records) in enumerate(ranked, start=1):
            stats = self._stats(seller_records)
            member = ctx.guild.get_member(seller_id)
            name = (
                member.display_name
                if member
                else seller_records[0].get("seller", "Unknown")
            )
            lines.append(
                f"**#{index}** **{name}** — `{stats['count']}` vouches • "
                f"`{stats['avg']:.2f}/5`"
            )

        embed = discord.Embed(
            title="⭐ Vouch leaderboard",
            description="\n".join(lines) or "No vouches yet.",
            color=VOUCH_COLOR,
        )
        embed.set_footer(text=f"Top {len(ranked)} sellers • {ctx.guild.name}")
        await ctx.reply(embed=embed, mention_author=False)

    # --------------------------------------------------------------- setup

    @commands.hybrid_group(
        name="vouchsetup",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def vouchsetup(self, ctx: commands.Context) -> None:
        """Configure the buyer role and vouch channel."""
        await self._show_setup(ctx)

    @vouchsetup.command(name="role", usage="<@role>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def role_cmd(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Set the role required to vouch."""
        problem = self._role_problem(ctx, role)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return
        config = self._config(ctx.guild)
        config["buyer_role_id"] = role.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="⭐ Buyer role set",
            description=f"Buyers with {role.mention} can now vouch.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{role.mention}** is now the buyer role.\n"
                "Give it to buyers with `/buyer add` after a purchase.",
                title="⭐ Buyer role set",
            ),
            mention_author=False,
        )

    @vouchsetup.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the channel where vouches are posted."""
        permissions = channel.permissions_for(ctx.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
            )
            if not ok
        ]
        if missing:
            await ctx.reply(
                embed=error_embed(
                    f"I need **{', '.join(missing)}** in {channel.mention}."
                ),
                mention_author=False,
            )
            return
        config = self._config(ctx.guild)
        config["vouch_channel_id"] = channel.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="⭐ Vouch channel set",
            description=f"Vouches will be posted in {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Vouches will be posted in {channel.mention}.\n"
                "Buyers must use `/vouch` in that channel.",
                title="⭐ Vouch channel set",
            ),
            mention_author=False,
        )

    @vouchsetup.command(name="show", aliases=["status"])
    @commands.guild_only()
    async def show(self, ctx: commands.Context) -> None:
        """Show the vouch settings."""
        await self._show_setup(ctx)

    @vouchsetup.command(name="clear", aliases=["reset"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def clear(self, ctx: commands.Context) -> None:
        """Clear the buyer role and vouch channel settings."""
        config = self._config(ctx.guild)
        config["buyer_role_id"] = None
        config["vouch_channel_id"] = None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="⭐ Vouch settings cleared",
            description="The buyer role and vouch channel were cleared.",
            color=WARNING_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "The buyer role and vouch channel were cleared. "
                "Existing vouches are kept."
            ),
            mention_author=False,
        )

    # --------------------------------------------------------------- buyers

    @commands.hybrid_group(
        name="buyer",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def buyer(self, ctx: commands.Context) -> None:
        """Give or remove the buyer role."""
        role = self._buyer_role(ctx.guild)
        await ctx.reply(
            embed=info_embed(
                f"**Buyer role:** {role.mention if role else 'not set'}\n\n"
                f"Give it: `{ctx.prefix}buyer add @Member`\n"
                f"Remove it: `{ctx.prefix}buyer remove @Member`",
                title="⭐ Buyer role",
            ),
            mention_author=False,
        )

    @buyer.command(name="add", aliases=["give", "grant"], usage="<member>")
    @commands.guild_only()
    @is_moderator("manage_roles")
    async def add(self, ctx: commands.Context, member: discord.Member) -> None:
        """Give the buyer role to a member after a purchase."""
        role = self._buyer_role(ctx.guild)
        if role is None:
            await ctx.reply(
                embed=error_embed(
                    "The buyer role is not set yet.\n"
                    "Set it with `/vouchsetup role` first."
                ),
                mention_author=False,
            )
            return
        if member.bot:
            await ctx.reply(
                embed=error_embed("You cannot give the buyer role to a bot."),
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
                role, reason=f"{ctx.author} ({ctx.author.id}): buyer role"
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
            title="⭐ Buyer role added",
            description=f"Gave {role.mention} to {member.mention}.",
            color=SUCCESS_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** now has {role.mention} and can vouch in the vouch channel.",
                title="⭐ Buyer role added",
            ),
            mention_author=False,
        )

    @buyer.command(name="remove", aliases=["take", "revoke"], usage="<member>")
    @commands.guild_only()
    @is_moderator("manage_roles")
    async def remove(self, ctx: commands.Context, member: discord.Member) -> None:
        """Remove the buyer role from a member."""
        role = self._buyer_role(ctx.guild)
        if role is None:
            await ctx.reply(
                embed=error_embed("The buyer role is not set yet."),
                mention_author=False,
            )
            return
        if role not in member.roles:
            await ctx.reply(
                embed=info_embed(f"**{member}** does not have {role.mention}."),
                mention_author=False,
            )
            return
        try:
            await member.remove_roles(
                role, reason=f"{ctx.author} ({ctx.author.id}): buyer role removed"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not remove the role: {exc}"),
                mention_author=False,
            )
            return
        await self.bot.audit(
            ctx.guild,
            title="⭐ Buyer role removed",
            description=f"Removed {role.mention} from {member.mention}.",
            color=WARNING_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("By", ctx.author.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"Removed {role.mention} from **{member}**.",
                title="⭐ Buyer role removed",
            ),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Vouches(bot))
