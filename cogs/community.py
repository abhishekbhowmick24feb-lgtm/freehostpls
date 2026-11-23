"""Birthday announcements and AFK status."""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timezone

import discord
from discord.ext import commands, tasks

from core.config import SUCCESS_COLOR
from core.utils import (
    error_embed,
    fmt_duration,
    info_embed,
    success_embed,
    truncate,
)

log = logging.getLogger(__name__)


class Birthdays(commands.Cog):
    """Collect birthdays and announce them in a chosen channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.birthday_task.start()

    def cog_unload(self) -> None:
        self.birthday_task.cancel()

    # --------------------------------------------------------------- helpers

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self._data(guild).get("birthday_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _valid(month: int, day: int) -> bool:
        if not 1 <= month <= 12:
            return False
        max_day = calendar.monthrange(2024, month)[1]
        return 1 <= day <= max_day

    @staticmethod
    def _next_date(month: int, day: int, today: date) -> date:
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                candidate = date(year, month, 28)
            if candidate >= today:
                return candidate
        return date(today.year + 1, month, 1)

    @staticmethod
    def _human_date(month: int, day: int) -> str:
        return f"{calendar.month_name[month]} {day}"

    async def _announce_due(self) -> None:
        today = datetime.now().date()
        key = today.isoformat()
        for guild in self.bot.guilds:
            data = self._data(guild)
            if data.get("birthday_last_announced") == key:
                continue
            channel = self._channel(guild)
            if channel is None:
                continue
            birthdays = data.get("birthdays", {})
            members: list[discord.Member] = []
            for raw_id, record in birthdays.items():
                try:
                    if (
                        int(record.get("month", 0)) != today.month
                        or int(record.get("day", 0)) != today.day
                    ):
                        continue
                    member = guild.get_member(int(raw_id))
                except (TypeError, ValueError):
                    continue
                if member is not None:
                    members.append(member)
            if not members:
                continue

            mentions = ", ".join(member.mention for member in members)
            embed = discord.Embed(
                title="🎂 Happy Birthday!",
                description=f"Join us in wishing {mentions} a happy birthday! 🎉",
                color=0xF1C40F,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"{guild.name} birthdays")
            try:
                await channel.send(
                    content=mentions,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not post birthday announcement in %s: %s", channel.id, exc)
                continue
            data["birthday_last_announced"] = key
            await self.bot.db.save()

    @tasks.loop(minutes=30)
    async def birthday_task(self) -> None:
        await self._announce_due()

    @birthday_task.before_loop
    async def before_birthday_task(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._announce_due()

    async def _status(self, ctx: commands.Context) -> None:
        data = self._data(ctx.guild)
        record = data.get("birthdays", {}).get(str(ctx.author.id))
        channel = self._channel(ctx.guild)
        yours = (
            f"**{self._human_date(int(record['month']), int(record['day']))}**"
            if record
            else "not set"
        )
        await ctx.reply(
            embed=info_embed(
                f"**Your birthday:** {yours}\n"
                f"**Announcement channel:** "
                f"{channel.mention if channel else 'not set'}\n\n"
                f"**Set it:** `/birthday set month:1 day:31`\n"
                f"**Remove it:** `/birthday remove`\n"
                f"**Upcoming:** `/birthday upcoming`",
                title="🎂 Birthdays",
            ),
            mention_author=False,
        )

    @commands.hybrid_group(
        name="birthday",
        aliases=["bday"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def birthday(self, ctx: commands.Context) -> None:
        """Set your birthday and see the announcement setup."""
        await self._status(ctx)

    @birthday.command(name="set", usage="<month> <day>")
    @commands.guild_only()
    @discord.app_commands.describe(month="Birthday month, 1-12", day="Birthday day")
    async def set_birthday(
        self, ctx: commands.Context, month: int, day: int
    ) -> None:
        """Save your birthday."""
        if not self._valid(month, day):
            await ctx.reply(
                embed=error_embed(
                    "That date is not valid. Use a month from **1–12** and a "
                    "real day for that month."
                ),
                mention_author=False,
            )
            return
        data = self._data(ctx.guild)
        data.setdefault("birthdays", {})[str(ctx.author.id)] = {
            "month": month,
            "day": day,
            "name": str(ctx.author),
            "set_at": discord.utils.utcnow().isoformat(),
        }
        await self.bot.db.save()
        channel = self._channel(ctx.guild)
        text = (
            f"Your birthday is saved as **{self._human_date(month, day)}**.\n"
            + (
                f"I'll announce it in {channel.mention}."
                if channel
                else "Ask staff to choose an announcement channel with "
                "`/birthday channel`."
            )
        )
        await ctx.reply(
            embed=success_embed(text, title="🎂 Birthday saved"),
            mention_author=False,
        )

    @birthday.command(name="remove", aliases=["clear", "delete"])
    @commands.guild_only()
    async def remove_birthday(self, ctx: commands.Context) -> None:
        """Remove your saved birthday."""
        data = self._data(ctx.guild)
        if data.get("birthdays", {}).pop(str(ctx.author.id), None) is None:
            await ctx.reply(
                embed=info_embed("You do not have a birthday saved."),
                mention_author=False,
            )
            return
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed("Your birthday was removed."),
            mention_author=False,
        )

    @birthday.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def birthday_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Choose where birthday announcements are posted."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return
        self._data(ctx.guild)["birthday_channel_id"] = channel.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🎂 Birthday channel set",
            description=f"Birthday announcements will be posted in {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Birthday announcements will be posted in {channel.mention}.",
                title="🎂 Birthday channel set",
            ),
            mention_author=False,
        )

    @birthday.command(name="upcoming", aliases=["list", "next"])
    @commands.guild_only()
    async def upcoming(self, ctx: commands.Context) -> None:
        """Show the next saved birthdays."""
        data = self._data(ctx.guild)
        today = datetime.now().date()
        entries: list[tuple[date, discord.Member]] = []
        for raw_id, record in data.get("birthdays", {}).items():
            try:
                member = ctx.guild.get_member(int(raw_id))
                if member is None:
                    continue
                next_date = self._next_date(
                    int(record["month"]), int(record["day"]), today
                )
            except (KeyError, TypeError, ValueError):
                continue
            entries.append((next_date, member))
        if not entries:
            await ctx.reply(
                embed=info_embed(
                    "No birthdays are saved yet.\n"
                    "Set yours with `/birthday set month:1 day:31`."
                ),
                mention_author=False,
            )
            return
        entries.sort(key=lambda item: item[0])
        lines = []
        for next_date, member in entries[:10]:
            days = (next_date - today).days
            when = "Today" if days == 0 else f"in {days} day(s)"
            lines.append(
                f"• **{member.display_name}** — {next_date.strftime('%b %d')} ({when})"
            )
        await ctx.reply(
            embed=info_embed("\n".join(lines), title="🎂 Upcoming birthdays"),
            mention_author=False,
        )

    @birthday.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show your birthday and the announcement channel."""
        await self._status(ctx)


class AFK(commands.Cog):
    """Set an AFK reason and tell people who mention you."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    @staticmethod
    def _duration(since: str | None) -> str:
        if not since:
            return "a while"
        try:
            started = datetime.fromisoformat(since)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            seconds = int((discord.utils.utcnow() - started).total_seconds())
            return fmt_duration(max(0, seconds))
        except (TypeError, ValueError):
            return "a while"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if any(
            prefix and message.content.startswith(prefix) for prefix in prefixes
        ):
            return

        data = self._data(message.guild)
        afk = data.get("afk")
        if not isinstance(afk, dict):
            afk = {}
            data["afk"] = afk

        key = str(message.author.id)
        if key in afk:
            record = afk.pop(key)
            await self.bot.db.save()
            duration = self._duration(record.get("since"))
            try:
                await message.reply(
                    embed=success_embed(
                        f"Welcome back! You were AFK for **{duration}**.\n"
                        f"**Reason:** {record.get('reason') or 'AFK'}",
                        title="👋 Welcome back",
                    ),
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

        if not message.mentions:
            return
        lines: list[str] = []
        for user in message.mentions[:3]:
            if user.id == message.author.id:
                continue
            record = afk.get(str(user.id))
            if not record:
                continue
            duration = self._duration(record.get("since"))
            lines.append(
                f"**{user.display_name}** is AFK — "
                f"{record.get('reason') or 'AFK'} ({duration})"
            )
        if lines:
            try:
                await message.reply(
                    embed=info_embed("\n".join(lines), title="💤 AFK"),
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="afk", usage="[reason]")
    @commands.guild_only()
    @discord.app_commands.describe(
        reason="Why you are AFK; use 'off' to clear it"
    )
    async def afk(self, ctx: commands.Context, *, reason: str | None = None) -> None:
        """Set an AFK reason, or clear it with 'off'."""
        data = self._data(ctx.guild)
        afk = data.get("afk")
        if not isinstance(afk, dict):
            afk = {}
            data["afk"] = afk
        key = str(ctx.author.id)
        current = afk.get(key)

        cleaned = (reason or "").strip()
        if cleaned.lower() in {"off", "clear", "remove", "stop"}:
            if current is None:
                await ctx.reply(
                    embed=info_embed("You are not AFK."),
                    mention_author=False,
                )
                return
            afk.pop(key, None)
            await self.bot.db.save()
            await ctx.reply(
                embed=success_embed("Your AFK status was cleared."),
                mention_author=False,
            )
            return

        if not cleaned and current is not None:
            await ctx.reply(
                embed=info_embed(
                    f"**Reason:** {current.get('reason') or 'AFK'}\n"
                    f"**For:** {self._duration(current.get('since'))}\n\n"
                    "Send a message to clear it, or use `/afk reason:off`.",
                    title="💤 You are AFK",
                ),
                mention_author=False,
            )
            return

        reason_text = truncate(cleaned or "AFK", 200)
        afk[key] = {
            "reason": reason_text,
            "since": discord.utils.utcnow().isoformat(),
            "name": str(ctx.author),
        }
        data["afk"] = afk
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"You are now AFK: **{reason_text}**.\n"
                "I'll tell anyone who mentions you. Send a message to clear it.",
                title="💤 AFK set",
            ),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Birthdays(bot))
    await bot.add_cog(AFK(bot))
