"""Custom server tags for FAQs, rules and marketplace terms."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.config import INFO_COLOR
from core.utils import (
    error_embed,
    info_embed,
    success_embed,
    truncate,
)

log = logging.getLogger(__name__)


class Tags(commands.Cog):
    """Save short reusable answers and show them with /tag <name>."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    @staticmethod
    def _normalize(name: str) -> str | None:
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
        cleaned = cleaned.strip("-")[:50]
        return cleaned or None

    def _can_manage(self, ctx: commands.Context) -> bool:
        member = ctx.author
        if (
            member.id == ctx.guild.owner_id
            or member.guild_permissions.administrator
            or member.guild_permissions.manage_messages
        ):
            return True
        role_id = self._data(ctx.guild).get("mod_role_id")
        role = ctx.guild.get_role(role_id) if role_id else None
        return bool(role is not None and role in member.roles)

    @staticmethod
    def _tag_embed(
        name: str, record: dict, requested_by: str | None = None
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"📌 {name}",
            description=record.get("content") or "*Empty tag.*",
            color=INFO_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        footer = f"Used {int(record.get('uses', 0))} time(s)"
        if record.get("created_by_name"):
            footer += f" • Added by {record['created_by_name']}"
        if requested_by:
            footer += f" • Requested by {requested_by}"
        embed.set_footer(text=truncate(footer, 2048))
        return embed

    async def _list_tags(self, ctx: commands.Context) -> None:
        tags = self._data(ctx.guild).get("tags", {})
        if not tags:
            await ctx.reply(
                embed=info_embed(
                    "No tags have been created yet.\n"
                    "Staff can add one with `/tagadd name:rules content:...`."
                ),
                mention_author=False,
            )
            return
        names = sorted(tags)
        lines = "\n".join(f"• `{name}`" for name in names[:50])
        if len(names) > 50:
            lines += f"\n… and {len(names) - 50} more."
        await ctx.reply(
            embed=info_embed(
                lines, title=f"📌 Tags ({len(names)})"
            ),
            mention_author=False,
        )

    async def _save_tag(
        self, ctx: commands.Context, name: str, content: str
    ) -> None:
        normalized = self._normalize(name)
        if normalized is None:
            await ctx.reply(
                embed=error_embed(
                    "Tag names need at least one letter or number. "
                    "Use letters, numbers, hyphens or underscores."
                ),
                mention_author=False,
            )
            return
        text = content.strip().replace("\\n", "\n")
        if not text:
            await ctx.reply(
                embed=error_embed("Tag content cannot be empty."),
                mention_author=False,
            )
            return
        text = truncate(text, 2000)
        data = self._data(ctx.guild)
        tags = data.get("tags")
        if not isinstance(tags, dict):
            tags = {}
            data["tags"] = tags

        existing = tags.get(normalized)
        if existing is not None:
            existing["content"] = text
            existing["updated_by"] = ctx.author.id
            existing["updated_by_name"] = str(ctx.author)
            existing["updated_at"] = discord.utils.utcnow().isoformat()
            verb = "updated"
        else:
            tags[normalized] = {
                "content": text,
                "created_by": ctx.author.id,
                "created_by_name": str(ctx.author),
                "created_at": discord.utils.utcnow().isoformat(),
                "uses": 0,
            }
            verb = "created"
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title=f"📌 Tag {verb}",
            description=f"**{normalized}**\n{truncate(text, 500)}",
            color=INFO_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Tag **`{normalized}`** was {verb}.\n"
                f"View it with `/tag name:{normalized}`.",
                title="📌 Tag saved",
            ),
            mention_author=False,
        )

    # -------------------------------------------------------------- commands

    @commands.hybrid_command(
        name="tag",
        usage="<name> [content]",
    )
    @commands.guild_only()
    @discord.app_commands.describe(
        name="Tag to show, or the name to create/update",
        content="Staff only: create or update the tag with this text",
    )
    async def tag(
        self,
        ctx: commands.Context,
        name: str | None = None,
        *,
        content: str | None = None,
    ) -> None:
        """Show a tag, or have staff create/update one."""
        if name is None:
            if content is not None:
                await ctx.reply(
                    embed=error_embed("Provide a tag name first."),
                    mention_author=False,
                )
                return
            await self._list_tags(ctx)
            return

        normalized = self._normalize(name)
        if normalized is None:
            await ctx.reply(
                embed=error_embed(
                    "Tag names need at least one letter or number."
                ),
                mention_author=False,
            )
            return

        if content is not None:
            if not self._can_manage(ctx):
                await ctx.reply(
                    embed=error_embed(
                        "Only staff can create or update tags. "
                        "Use `/tag name:<name>` to read one."
                    ),
                    mention_author=False,
                )
                return
            await self._save_tag(ctx, normalized, content)
            return

        tags = self._data(ctx.guild).get("tags", {})
        record = tags.get(normalized)
        if record is None:
            await ctx.reply(
                embed=info_embed(
                    f"No tag named **`{normalized}`** exists.\n"
                    f"Use `/taglist` to see all tags."
                ),
                mention_author=False,
            )
            return
        record["uses"] = int(record.get("uses", 0)) + 1
        await self.bot.db.save()
        await ctx.reply(
            embed=self._tag_embed(
                normalized, record, requested_by=ctx.author.display_name
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="tagadd",
        aliases=["addtag"],
        usage="<name> <content>",
    )
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(
        name="Tag name, e.g. rules",
        content="What the tag should say; use \\n for a new line",
    )
    async def tagadd(
        self, ctx: commands.Context, name: str, *, content: str
    ) -> None:
        """Create or update a tag (staff only)."""
        await self._save_tag(ctx, name, content)

    @commands.hybrid_command(
        name="tagremove",
        aliases=["deltag", "tagdelete"],
        usage="<name>",
    )
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(name="Tag to remove")
    async def tagremove(self, ctx: commands.Context, name: str) -> None:
        """Remove a tag (staff only)."""
        normalized = self._normalize(name)
        if normalized is None:
            await ctx.reply(
                embed=error_embed("That is not a valid tag name."),
                mention_author=False,
            )
            return
        data = self._data(ctx.guild)
        tags = data.get("tags", {})
        if not isinstance(tags, dict) or tags.pop(normalized, None) is None:
            await ctx.reply(
                embed=info_embed(f"No tag named **`{normalized}`** exists."),
                mention_author=False,
            )
            return
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="📌 Tag removed",
            description=f"**{normalized}**",
            color=INFO_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(f"Removed tag **`{normalized}`**."),
            mention_author=False,
        )

    @commands.hybrid_command(
        name="taglist",
        aliases=["tags"],
        usage="",
    )
    @commands.guild_only()
    async def taglist(self, ctx: commands.Context) -> None:
        """List all custom tags."""
        await self._list_tags(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tags(bot))
