"""Per-category audit logging.

Every entry is filed under a category (moderation, messages, tickets,
security or general) and posted to that category's own channel. Missing
channels are discovered by name and, when the bot has permission, created
on the fly — so a redeploy on a host with no local state still finds them.
"""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from core.config import FOOTER_TEXT, INFO_COLOR, SUCCESS_COLOR
from core.utils import error_embed, info_embed, success_embed, truncate

log = logging.getLogger(__name__)

# category -> label, channel name used when one has to be created, and the
# names that are accepted when looking for an existing channel.
CATEGORIES: dict[str, dict[str, object]] = {
    "general": {
        "label": "General",
        "channel": "logs",
        "names": ("logs", "bot-logs", "bot-log", "log"),
    },
    "moderation": {
        "label": "Moderation",
        "channel": "mod-logs",
        "names": ("mod-logs", "modlogs", "mod-log", "moderation-logs", "moderation"),
    },
    "messages": {
        "label": "Messages",
        "channel": "message-logs",
        "names": ("message-logs", "messages-logs", "msg-logs", "chat-logs", "message-log"),
    },
    "tickets": {
        "label": "Tickets",
        "channel": "ticket-logs",
        "names": ("ticket-logs", "tickets-logs", "ticket-log", "ticket-transcript-logs"),
    },
    "members": {
        "label": "Members",
        "channel": "member-logs",
        "names": (
            "member-logs",
            "members-logs",
            "member-log",
            "join-logs",
            "join-leave-logs",
            "member-join-leave",
        ),
    },
    "security": {
        "label": "Security",
        "channel": "security-logs",
        "names": ("security-logs", "security-log", "raid-logs", "raid-log"),
    },
}

CATEGORY_ALIASES: dict[str, str] = {
    "general": "general",
    "main": "general",
    "default": "general",
    "bot": "general",
    "mod": "moderation",
    "mods": "moderation",
    "moderation": "moderation",
    "message": "messages",
    "messages": "messages",
    "msg": "messages",
    "chat": "messages",
    "ticket": "tickets",
    "tickets": "tickets",
    "member": "members",
    "members": "members",
    "join": "members",
    "joins": "members",
    "leave": "members",
    "leaves": "members",
    "security": "security",
    "raid": "security",
    "alerts": "security",
}

# Title keywords, checked in order. The first category whose keywords appear
# in an entry's title owns that entry. Anything unmatched is "general".
TITLE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("message deleted", "message edited", "bulk message delete"), "messages"),
    (("member joined", "member left", "member join", "member leave"), "members"),
    (("ticket",), "tickets"),
    (("raid", "nuke", "scam", "honeypot", "security", "account too young"), "security"),
    (
        (
            "bann",
            "kick",
            "timed out",
            "timeout",
            "mute",
            "warn",
            "purge",
            "slowmode",
            "channel locked",
            "channel unlocked",
            "channel access",
            "role added",
            "role removed",
            "nickname",
        ),
        "moderation",
    ),
)


def _clean(name: str) -> str:
    """Reduce a channel name to letters, digits and dashes (drops emoji)."""
    return re.sub(r"[^a-z0-9-]", "", (name or "").lower().replace("_", "-")).strip("-")


def category_for_title(title: str) -> str:
    """Guess which category a log entry belongs to from its title."""
    text = (title or "").lower()
    for keywords, category in TITLE_HINTS:
        if any(word in text for word in keywords):
            return category
    return "general"


def category_names() -> str:
    return ", ".join(f"`{key}`" for key in CATEGORIES)


class Logs(commands.Cog):
    """Record what the bot does, one channel per kind of event."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------ channels

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    @staticmethod
    def _by_id(guild: discord.Guild, channel_id: object) -> discord.TextChannel | None:
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _discover(
        self, guild: discord.Guild, key: str
    ) -> discord.TextChannel | None:
        wanted = {_clean(str(name)) for name in CATEGORIES[key]["names"]}  # type: ignore[union-attr]
        for channel in guild.text_channels:
            if _clean(channel.name) in wanted:
                return channel
        return None

    def _direct(
        self, guild: discord.Guild, key: str
    ) -> discord.TextChannel | None:
        """The channel configured or found for one category, no fallback."""
        data = self._data(guild)
        overrides = data.get("log_channels")
        if not isinstance(overrides, dict):
            overrides = {}
            data["log_channels"] = overrides

        channel = self._by_id(guild, overrides.get(key))
        if channel is not None:
            return channel
        if overrides.pop(key, None) is not None:
            log.info("Log channel for %s is gone, looking again", key)

        # Legacy single-channel settings still count as their own category.
        if key == "tickets":
            channel = self._by_id(
                guild, (data.get("tickets") or {}).get("ticket_log_channel_id")
            )
        elif key == "general":
            channel = self._by_id(guild, data.get("log_channel_id"))
        if channel is not None:
            return channel

        channel = self._discover(guild, key)
        if channel is not None:
            overrides[key] = channel.id
        return channel

    @staticmethod
    def _enabled(data: dict) -> bool:
        return bool(data.get("log_enabled", True))

    def channel_for(
        self, guild: discord.Guild, category: str = "general"
    ) -> discord.TextChannel | None:
        """Where entries of this category go, falling back to the general one."""
        data = self._data(guild)
        if not self._enabled(data):
            return None
        key = CATEGORY_ALIASES.get((category or "general").lower(), "general")
        channel = self._direct(guild, key)
        if channel is None and key != "general":
            channel = self._direct(guild, "general")
        return channel

    async def resolve(
        self, guild: discord.Guild, category: str = "general"
    ) -> discord.TextChannel | None:
        """Like channel_for, but creates a missing category channel."""
        data = self._data(guild)
        if not self._enabled(data):
            return None
        key = CATEGORY_ALIASES.get((category or "general").lower(), "general")
        channel = self._direct(guild, key)
        if channel is None:
            channel = await self._create_channel(guild, key)
        if channel is None and key != "general":
            channel = self._direct(guild, "general")
        return channel

    async def _create_channel(
        self, guild: discord.Guild, key: str
    ) -> discord.TextChannel | None:
        template = self._direct(guild, "general") or self._direct(guild, "tickets")
        overwrites = dict(template.overwrites) if template is not None else {}
        if not overwrites:
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            if guild.me is not None:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                    read_message_history=True,
                )

        name = str(CATEGORIES[key]["channel"])
        label = str(CATEGORIES[key]["label"])
        try:
            channel = await guild.create_text_channel(
                name,
                overwrites=overwrites,
                category=template.category if template is not None else None,
                topic=f"{label} log — written by Marketplace Guard",
                reason="Marketplace Guard: create log channel",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not create the #%s log channel: %s", name, exc)
            return None

        overrides = self._data(guild).setdefault("log_channels", {})
        overrides[key] = channel.id
        await self.bot.db.save()
        log.info("Created #%s (%s log) in guild %s", channel.name, key, guild.id)
        return channel

    # -------------------------------------------------------------- entries

    async def audit(
        self,
        guild: discord.Guild,
        *,
        title: str,
        description: str | None = None,
        color: int = INFO_COLOR,
        fields: list[tuple[str, str, bool]] | None = None,
        category: str | None = None,
    ) -> bool:
        """Send one audit entry to its category channel."""
        channel = await self.resolve(guild, category or category_for_title(title))
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
            log.warning("Missing permission to send logs in %s", channel.id)
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
        channel = self.channel_for(guild, "messages")
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
            category="messages",
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
        channel = self.channel_for(guild, "messages")
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
            category="messages",
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
        channel = self.channel_for(before.guild, "messages")
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
            category="messages",
        )

    # -------------------------------------------------------- member events

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Record joins, flagging brand-new accounts."""
        guild = member.guild
        age = discord.utils.utcnow() - member.created_at
        fields = [
            (
                "Account created",
                f"{discord.utils.format_dt(member.created_at, 'R')} ({age.days}d old)",
                True,
            ),
            ("Members now", str(guild.member_count or "—"), True),
        ]
        if age.days < 7:
            fields.append(("Heads up", "🆕 Account is less than a week old", False))

        await self.audit(
            guild,
            title="📥 Member joined",
            description=f"{member.mention} (`{member.id}`)\n**Username:** {member}",
            color=SUCCESS_COLOR,
            fields=fields,
            category="members",
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Record leaves (kicks and bans also surface here; moderation logs hold the reason)."""
        guild = member.guild
        joined = (
            discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "unknown"
        )
        roles = [role.mention for role in member.roles if not role.is_default()][:10]
        fields = [
            ("Joined", joined, True),
            ("Members now", str(guild.member_count or "—"), True),
        ]
        if roles:
            value = ", ".join(roles)
            if len(member.roles) - 1 > len(roles):
                value += f" *(+{len(member.roles) - 1 - len(roles)} more)*"
            fields.append(("Roles", truncate(value, 1024), False))

        await self.audit(
            guild,
            title="📤 Member left",
            description=f"{member.mention} (`{member.id}`)\n**Username:** {member}",
            color=0x95A5A6,
            fields=fields,
            category="members",
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
        """Show where each kind of log entry is sent."""
        lines = []
        for key, info in CATEGORIES.items():
            channel = self.channel_for(ctx.guild, key)
            created_name = str(info["channel"])
            lines.append(
                f"**{info['label']}:** "
                + (channel.mention if channel else f"auto — will create #{created_name}")
            )
        embed = info_embed(
            "\n".join(lines)
            + f"\n\nPoint one at another channel with "
            f"`{ctx.prefix}logs channel #channel <category>`.\n"
            f"Categories: {category_names()}.\n"
            f"Turn everything off with `{ctx.prefix}logs disable`.",
            title="📋 Bot logs",
        )
        await ctx.reply(embed=embed, mention_author=False)

    @logs.command(name="channel", usage="<#channel> [category]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel, category: str = "general"
    ) -> None:
        """Route one category (or everything) into a channel."""
        key = category.lower().lstrip("#")
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return

        data = self.bot.db.guild(ctx.guild.id)
        data["log_enabled"] = True

        if key in {"all", "everything", "every"}:
            data["log_channel_id"] = channel.id
            data["log_channels"] = {}
            await self.bot.db.save()
            await self.audit(
                ctx.guild,
                title="✅ Log channel set",
                description=f"Every log entry will now be recorded in {channel.mention}.",
                color=SUCCESS_COLOR,
                fields=[("Set by", ctx.author.mention, True)],
                category="general",
            )
            await ctx.reply(
                embed=success_embed(
                    f"All bot logs will be sent to {channel.mention}.\n"
                    f"Send a test entry with `{ctx.prefix}logs test`."
                ),
                mention_author=False,
            )
            return

        resolved = CATEGORY_ALIASES.get(key)
        if resolved is None:
            await ctx.reply(
                embed=error_embed(
                    f"Unknown category `{category}`. Pick one of {category_names()}, or `all`."
                ),
                mention_author=False,
            )
            return

        overrides = data.setdefault("log_channels", {})
        overrides[resolved] = channel.id
        await self.bot.db.save()
        await self.audit(
            ctx.guild,
            title="✅ Log channel set",
            description=(
                f"**{CATEGORIES[resolved]['label']}** entries will now be recorded "
                f"in {channel.mention}."
            ),
            color=SUCCESS_COLOR,
            fields=[("Set by", ctx.author.mention, True)],
            category="general",
        )
        await ctx.reply(
            embed=success_embed(
                f"**{CATEGORIES[resolved]['label']}** logs will be sent to {channel.mention}."
            ),
            mention_author=False,
        )

    @logs.command(name="reset", usage="[category|all]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reset(self, ctx: commands.Context, category: str = "all") -> None:
        """Forget a manual channel choice and go back to auto-discovery."""
        key = category.lower()
        data = self.bot.db.guild(ctx.guild.id)
        if key in {"all", "everything", "every"}:
            data["log_channels"] = {}
            data["log_channel_id"] = None
            await self.bot.db.save()
            await ctx.reply(
                embed=success_embed("Log channels reset — they will be found or created again."),
                mention_author=False,
            )
            return

        resolved = CATEGORY_ALIASES.get(key)
        if resolved is None:
            await ctx.reply(
                embed=error_embed(
                    f"Unknown category `{category}`. Pick one of {category_names()}, or `all`."
                ),
                mention_author=False,
            )
            return
        (data.get("log_channels") or {}).pop(resolved, None)
        await self.bot.db.save()
        channel = self.channel_for(ctx.guild, resolved)
        await ctx.reply(
            embed=success_embed(
                f"**{CATEGORIES[resolved]['label']}** logs reset to "
                + (channel.mention if channel else "auto-discovery.")
            ),
            mention_author=False,
        )

    @logs.command(name="disable", aliases=["off", "remove"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        """Stop sending bot logs."""
        data = self.bot.db.guild(ctx.guild.id)
        data["log_enabled"] = False
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                "Bot logging disabled. Re-enable it with "
                f"`{ctx.prefix}logs channel #channel all`."
            ),
            mention_author=False,
        )

    @logs.command(name="test", usage="[category]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def test(self, ctx: commands.Context, category: str = "all") -> None:
        """Send a test entry to one category, or to every category."""
        key = category.lower()
        if key in {"all", "everything", "every"}:
            keys = list(CATEGORIES)
        else:
            resolved = CATEGORY_ALIASES.get(key)
            if resolved is None:
                await ctx.reply(
                    embed=error_embed(
                        f"Unknown category `{category}`. Pick one of {category_names()}, or `all`."
                    ),
                    mention_author=False,
                )
                return
            keys = [resolved]

        sent = []
        for key in keys:
            channel = await self.resolve(ctx.guild, key)
            if channel is None:
                continue
            ok = await self.audit(
                ctx.guild,
                title=f"🧪 Test log entry — {CATEGORIES[key]['label']}",
                description=f"Entries for **{CATEGORIES[key]['label'].lower()}** land here.",
                color=SUCCESS_COLOR,
                fields=[
                    ("Requested by", ctx.author.mention, True),
                    ("Channel", ctx.channel.mention, True),
                ],
                category=key,
            )
            if ok:
                sent.append(channel.mention)

        if not sent:
            await ctx.reply(
                embed=error_embed(
                    "Nothing could be sent. Give me **Manage Channels** to create the "
                    "log channels, or pick them with `logs channel`."
                ),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed("Test entries sent to " + ", ".join(sent) + "."),
            mention_author=False,
        )

    @logs.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current log channels."""
        await self.logs(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(bot))
