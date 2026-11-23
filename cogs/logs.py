"""Configurable audit log channel for bot activity."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import FOOTER_TEXT, INFO_COLOR, SUCCESS_COLOR
from core.utils import error_embed, info_embed, success_embed, truncate

log = logging.getLogger(__name__)


class Logs(commands.Cog):
    """Choose a channel where the bot records what it does."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def channel_for(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self.bot.db.guild(guild.id).get("log_channel_id")
        channel = guild.get_channel(channel_id or 0)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def audit(
        self,
        guild: discord.Guild,
        *,
        title: str,
        description: str | None = None,
        color: int = INFO_COLOR,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> bool:
        """Send one audit entry. Returns False when no log channel is set."""
        channel = self.channel_for(guild)
        if channel is None:
            return False

        embed = discord.Embed(
            title=truncate(title, 256),
            description=truncate(description or "", 4000) or None,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        for name, value, inline in fields or []:
            embed.add_field(
                name=truncate(str(name), 256),
                value=truncate(str(value), 1024) or "—",
                inline=inline,
            )
        embed.set_footer(text=FOOTER_TEXT)

        try:
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            return True
        except discord.Forbidden:
            log.warning("Missing permission to send logs in guild %s", guild.id)
            return False
        except discord.HTTPException as exc:
            log.warning("Could not send log entry: %s", exc)
            return False

    # ------------------------------------------------------- message events

    @staticmethod
    def _content_block(text: str, limit: int = 900) -> str:
        """Quote a message body so it reads safely inside an embed."""
        cleaned = (text or "").strip()
        if not cleaned:
            return "*No text — embed, sticker or attachment only, or the message was too old to be cached.*"
        lines = cleaned.splitlines()[:12]
        return truncate("\n".join(f"> {line}" for line in lines), limit)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Record who deleted what, including the message text."""
        guild = message.guild
        if guild is None or message.author.bot:
            return
        channel = self.channel_for(guild)
        if channel is None or channel.id == message.channel.id:
            return

        author = message.author
        description = (
            f"**Author:** {author.mention} (`{author.id}`)\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Sent:** {discord.utils.format_dt(message.created_at, 'R')}\n\n"
            f"**Message:**\n{self._content_block(message.content)}"
        )
        fields: list[tuple[str, str, bool]] = [
            ("Message ID", f"`{message.id}`", True)
        ]
        if message.attachments:
            files = ", ".join(
                f"[{truncate(item.filename, 60)}]({item.url})"
                for item in message.attachments[:5]
            )
            fields.append(("Attachments", files, False))

        await self.audit(
            guild,
            title="🗑️ Message deleted",
            description=description,
            color=0xED4245,
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(
        self, messages: list[discord.Message]
    ) -> None:
        """Record a purge, with a preview of what was removed."""
        if not messages:
            return
        first = messages[0]
        guild = first.guild
        if guild is None:
            return
        channel = self.channel_for(guild)
        if channel is None or channel.id == first.channel.id:
            return

        preview = "\n".join(
            f"**{truncate(str(item.author), 60)}:** "
            f"{truncate(item.content or '*no text*', 120)}"
            for item in messages[:10]
        )
        if len(messages) > 10:
            preview += f"\n*…and {len(messages) - 10} more*"

        await self.audit(
            guild,
            title="🧹 Bulk message delete",
            description=(
                f"**Channel:** {first.channel.mention}\n"
                f"**Messages:** {len(messages)}\n\n{preview}"
            ),
            color=0xED4245,
        )

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Record edits so moderators can see what a message used to say."""
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return
        channel = self.channel_for(before.guild)
        if channel is None or channel.id == after.channel.id:
            return

        await self.audit(
            before.guild,
            title="✏️ Message edited",
            description=(
                f"**Author:** {after.author.mention} (`{after.author.id}`)\n"
                f"**Channel:** {after.channel.mention}\n"
                f"**Jump:** {after.jump_url}\n\n"
                f"**Before:**\n{self._content_block(before.content)}\n\n"
                f"**After:**\n{self._content_block(after.content)}"
            ),
            color=0xFEE75C,
        )

    # ---------------------------------------------------------------- group

    @commands.hybrid_group(
        name="logs",
        aliases=["log", "logging"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def logs(self, ctx: commands.Context) -> None:
        """Set and inspect the channel where bot activity is logged."""
        channel = self.channel_for(ctx.guild)
        embed = info_embed(
            f"**Log channel:** {channel.mention if channel else 'not set'}\n\n"
            f"Set it with `{ctx.prefix}logs channel #bot-logs`.\n"
            f"Turn it off with `{ctx.prefix}logs disable`.",
            title="📋 Bot logs",
        )
        await ctx.reply(embed=embed, mention_author=False)

    @logs.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Choose the channel that receives bot logs."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return

        self.bot.db.guild(ctx.guild.id)["log_channel_id"] = channel.id
        await self.bot.db.save()
        await self.audit(
            ctx.guild,
            title="✅ Log channel set",
            description=f"Bot activity will now be recorded in {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("Set by", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Bot logs will be sent to {channel.mention}.\n"
                f"Send a test entry with `{ctx.prefix}logs test`."
            ),
            mention_author=False,
        )

    @logs.command(name="disable", aliases=["off", "remove"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        """Stop sending bot logs."""
        channel = self.channel_for(ctx.guild)
        self.bot.db.guild(ctx.guild.id)["log_channel_id"] = None
        await self.bot.db.save()
        # Report after clearing, so this notice cannot land in the channel itself.
        await ctx.reply(
            embed=success_embed(
                f"Bot logging disabled"
                + (f" — {channel.mention} will no longer receive entries." if channel else ".")
            ),
            mention_author=False,
        )

    @logs.command(name="test")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def test(self, ctx: commands.Context) -> None:
        """Send a test entry to the configured log channel."""
        if self.channel_for(ctx.guild) is None:
            await ctx.reply(
                embed=error_embed(
                    f"No log channel is set. Run `{ctx.prefix}logs channel #channel`."
                ),
                mention_author=False,
            )
            return
        await self.audit(
            ctx.guild,
            title="🧪 Test log entry",
            description="Logging works. Moderation, ticket, shop and verification "
            "activity will appear here.",
            color=SUCCESS_COLOR,
            fields=[
                ("Requested by", ctx.author.mention, True),
                ("Channel", ctx.channel.mention, True),
            ],
        )
        await ctx.reply(
            embed=success_embed("Test entry sent to the log channel."),
            mention_author=False,
        )

    @logs.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current log channel."""
        await self.logs(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(bot))
