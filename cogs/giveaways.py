"""Giveaways with requirements, persistent entry buttons and automatic endings."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from cogs.moderation import is_moderator
from core.agecheck import account_age_days
from core.config import BRAND_COLOR, ERROR_COLOR, SUCCESS_COLOR
from core.media import banner_files, banner_from_input, banner_url
from core.utils import (
    ConfirmView,
    error_embed,
    info_embed,
    parse_duration,
    success_embed,
    truncate,
)

log = logging.getLogger(__name__)

MIN_DURATION = 10
MAX_DURATION = 30 * 86400
MAX_WINNERS = 20
MAX_ACCOUNT_AGE = 3650
MAX_BONUS_ENTRIES = 10

GIVEAWAY_COLOR = 0xF1C40F


def _expand(text: str) -> str:
    return (text or "").replace("\\n", "\n")


class GiveawayEntryView(discord.ui.View):
    """Persistent Enter / Leave buttons shared by every giveaway message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        enter = discord.ui.Button(
            label="Enter",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id="giveaway:enter",
        )
        leave = discord.ui.Button(
            label="Leave",
            emoji="🚪",
            style=discord.ButtonStyle.secondary,
            custom_id="giveaway:leave",
        )
        enter.callback = self._enter
        leave.callback = self._leave
        self.add_item(enter)
        self.add_item(leave)

    @staticmethod
    async def _cog(interaction: discord.Interaction):
        cog = interaction.client.get_cog("Giveaways")
        if cog is None:
            await interaction.response.send_message(
                embed=error_embed("The giveaway system is not loaded right now."),
                ephemeral=True,
            )
            return None
        return cog

    async def _enter(self, interaction: discord.Interaction) -> None:
        cog = await self._cog(interaction)
        if cog is not None:
            await cog.handle_entry(interaction)

    async def _leave(self, interaction: discord.Interaction) -> None:
        cog = await self._cog(interaction)
        if cog is not None:
            await cog.handle_leave(interaction)


class Giveaways(commands.Cog):
    """Create, enter, end and reroll giveaways."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.giveaway_loop.start()

    async def cog_unload(self) -> None:
        self.giveaway_loop.cancel()

    # --------------------------------------------------------------- helpers

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _records(self, guild: discord.Guild) -> dict:
        data = self._data(guild)
        records = data.get("giveaways")
        if not isinstance(records, dict):
            records = {}
            data["giveaways"] = records
        return records

    def _find_by_id(
        self, guild: discord.Guild, giveaway_id: int
    ) -> dict | None:
        record = self._records(guild).get(str(giveaway_id))
        return record if isinstance(record, dict) else None

    def _find_by_message(
        self, guild: discord.Guild, message_id: int
    ) -> dict | None:
        for record in self._records(guild).values():
            if not isinstance(record, dict):
                continue
            if record.get("message_id") == message_id:
                return record
        return None

    def _default_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self._data(guild).get("giveaway_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _format_dt(cls, value: str | None, style: str = "R") -> str:
        parsed = cls._parse_dt(value)
        return discord.utils.format_dt(parsed, style) if parsed else "unknown"

    @staticmethod
    def _entry_total(record: dict) -> int:
        entries = record.get("entries") or {}
        total = 0
        for value in entries.values():
            try:
                total += max(0, int(value))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _entry_unique(record: dict) -> int:
        entries = record.get("entries") or {}
        unique = 0
        for value in entries.values():
            try:
                if int(value) > 0:
                    unique += 1
            except (TypeError, ValueError):
                continue
        return unique

    @staticmethod
    def _channel_problem(
        guild: discord.Guild, channel: discord.TextChannel
    ) -> list[str]:
        permissions = channel.permissions_for(guild.me)
        missing = []
        if not permissions.send_messages:
            missing.append("Send Messages")
        if not permissions.embed_links:
            missing.append("Embed Links")
        if not permissions.read_message_history:
            missing.append("Read Message History")
        return missing

    @staticmethod
    async def _defer(ctx: commands.Context) -> None:
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

    def _requirement_labels(
        self, guild: discord.Guild, requirements: dict
    ) -> list[str]:
        labels: list[str] = []
        if requirements.get("verified"):
            role_id = requirements.get("verified_role_id") or 0
            role = guild.get_role(int(role_id))
            labels.append(
                "Be verified"
                + (f" ({role.mention})" if role else " (verification role missing)")
            )
        if requirements.get("role_id"):
            role = guild.get_role(int(requirements["role_id"]))
            labels.append(
                f"Have {role.mention}"
                if role
                else "Have the required role (role missing)"
            )
        if requirements.get("booster"):
            labels.append("Be a server booster")
        try:
            age = int(requirements.get("min_account_age") or 0)
        except (TypeError, ValueError):
            age = 0
        if age > 0:
            labels.append(f"Have an account at least {age} days old")
        return labels

    def _requirement_checks(
        self, member: discord.Member, requirements: dict
    ) -> list[tuple[bool, str]]:
        checks: list[tuple[bool, str]] = []
        if requirements.get("verified"):
            role_id = requirements.get("verified_role_id") or 0
            role = member.guild.get_role(int(role_id))
            if role is None:
                checks.append((False, "The verification role no longer exists."))
            elif role in member.roles:
                checks.append((True, ""))
            else:
                checks.append((False, f"You need {role.mention} (verified)."))
        if requirements.get("role_id"):
            role = member.guild.get_role(int(requirements["role_id"]))
            if role is None:
                checks.append((False, "The required role no longer exists."))
            elif role in member.roles:
                checks.append((True, ""))
            else:
                checks.append((False, f"You need {role.mention}."))
        if requirements.get("booster"):
            is_booster = member.premium_since is not None
            checks.append(
                (
                    is_booster,
                    "" if is_booster else "You need to be a server booster.",
                )
            )
        try:
            age = int(requirements.get("min_account_age") or 0)
        except (TypeError, ValueError):
            age = 0
        if age > 0:
            actual = account_age_days(member)
            passes = actual >= age
            checks.append(
                (
                    passes,
                    ""
                    if passes
                    else (
                        f"Your account is {actual:g} days old; at least "
                        f"{age} days are required."
                    ),
                )
            )
        return checks

    def _check_requirements(
        self, member: discord.Member, requirements: dict
    ) -> tuple[bool, str]:
        checks = self._requirement_checks(member, requirements)
        if not checks:
            return True, ""
        if requirements.get("mode") == "any":
            if any(ok for ok, _ in checks):
                return True, ""
            labels = self._requirement_labels(member.guild, requirements)
            return (
                False,
                "You need at least one of:\n"
                + "\n".join(f"• {label}" for label in labels),
            )
        failed = [reason for ok, reason in checks if not ok]
        if not failed:
            return True, ""
        return (
            False,
            "You do not meet every requirement:\n"
            + "\n".join(f"• {reason}" for reason in failed),
        )

    def _bonus_for(self, member: discord.Member, requirements: dict) -> int:
        try:
            role_id = int(requirements.get("bonus_role_id") or 0)
            amount = int(requirements.get("bonus_entries") or 0)
        except (TypeError, ValueError):
            return 0
        if role_id <= 0 or amount <= 0:
            return 0
        role = member.guild.get_role(role_id)
        if role is not None and role in member.roles:
            return max(0, min(amount, MAX_BONUS_ENTRIES))
        return 0

    def _pick_winners(
        self,
        guild: discord.Guild,
        record: dict,
        count: int,
        *,
        exclude: set[int] | None = None,
    ) -> list[int]:
        excluded = set(exclude or [])
        entries = record.get("entries") or {}
        pool: list[int] = []
        for raw_id, raw_count in entries.items():
            try:
                user_id = int(raw_id)
                amount = max(0, int(raw_count))
            except (TypeError, ValueError):
                continue
            if user_id in excluded or amount <= 0:
                continue
            pool.extend([user_id] * amount)

        random.shuffle(pool)
        requirements = record.get("requirements") or {}
        winners: list[int] = []
        for user_id in pool:
            if user_id in winners:
                continue
            member = guild.get_member(user_id)
            if member is None or member.bot:
                continue
            eligible, _ = self._check_requirements(member, requirements)
            if not eligible:
                continue
            winners.append(user_id)
            if len(winners) >= count:
                break
        return winners

    def _embed(self, guild: discord.Guild, record: dict) -> discord.Embed:
        requirements = record.get("requirements") or {}
        status = record.get("status") or "active"
        prize = truncate(record.get("prize") or "Giveaway", 200)

        title = "GIVEAWAY"
        color = GIVEAWAY_COLOR
        if status == "ended":
            title += " — ENDED"
            color = SUCCESS_COLOR
            call_to_action = "This giveaway has ended."
        elif status == "cancelled":
            title += " — CANCELLED"
            color = ERROR_COLOR
            call_to_action = "This giveaway was cancelled."
        else:
            call_to_action = "Press 🎉 **Enter** below to join!"

        lines = [f"**Prize:** {prize}"]
        extra = _expand(record.get("description") or "")
        if extra:
            lines.append(extra)
        lines.append(call_to_action)
        embed = discord.Embed(
            title=title,
            description=truncate("\n\n".join(lines), 4000),
            color=color,
        )
        image = banner_url(record.get("image"))
        if image:
            # set_image keeps animated GIFs moving instead of showing one frame.
            embed.set_image(url=image)

        if status in {"ended", "cancelled"}:
            winner_ids = record.get("winner_ids") or []
            if status == "cancelled":
                winner_value = "Cancelled before a winner was chosen."
            elif winner_ids:
                winner_value = ", ".join(f"<@{user_id}>" for user_id in winner_ids)
            else:
                winner_value = "No eligible entries."
            embed.add_field(
                name="Winners",
                value=truncate(winner_value, 1024),
                inline=False,
            )

        labels = self._requirement_labels(guild, requirements)
        if not labels:
            must_have = "None — everyone can enter."
        elif requirements.get("mode") == "any":
            must_have = "At least **one** of:\n" + "\n".join(
                f"• {label}" for label in labels
            )
        else:
            must_have = "\n".join(f"• {label}" for label in labels)
        embed.add_field(
            name="Must have",
            value=truncate(must_have, 1024),
            inline=False,
        )

        total = self._entry_total(record)
        word = "entry" if total == 1 else "entries"
        embed.add_field(
            name="Entries",
            value=f"**{self._entry_unique(record)}** member(s) • **{total}** {word}",
            inline=True,
        )

        try:
            bonus_role_id = int(requirements.get("bonus_role_id") or 0)
            bonus_amount = int(requirements.get("bonus_entries") or 0)
        except (TypeError, ValueError):
            bonus_role_id = 0
            bonus_amount = 0
        if bonus_role_id > 0 and bonus_amount > 0:
            role = guild.get_role(bonus_role_id)
            embed.add_field(
                name="Bonus entries",
                value=(
                    f"{role.mention if role else 'Bonus role'} gives "
                    f"**+{bonus_amount}** extra entr(y/ies)."
                ),
                inline=False,
            )

        rerolled = 0
        try:
            rerolled = int(record.get("rerolled") or 0)
        except (TypeError, ValueError):
            pass
        if rerolled > 0:
            embed.add_field(
                name="Rerolled", value=f"**{rerolled}** time(s)", inline=True
            )

        try:
            winner_count = int(record.get("winners", 1))
        except (TypeError, ValueError):
            winner_count = 1
        footer = [
            f"Hosted by {record.get('host_name') or 'unknown-user'}",
            f"Winners {winner_count}",
        ]
        if status == "active":
            footer.append(f"Ends {self._format_dt(record.get('ends_at'), 'R')}")
        elif status == "ended":
            footer.append(f"Ended {self._format_dt(record.get('ended_at'), 'R')}")
        footer.append(f"#{record.get('id')}")
        embed.set_footer(text=truncate(" · ".join(footer), 2048))
        return embed

    async def _refresh_message(
        self, guild: discord.Guild, record: dict
    ) -> None:
        channel = guild.get_channel(record.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel) or not record.get("message_id"):
            return
        try:
            message = channel.get_partial_message(int(record["message_id"]))
            await message.edit(
                embed=self._embed(guild, record),
                attachments=banner_files(record.get("image")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.debug("Could not refresh giveaway message: %s", exc)

    async def _finalize(
        self, guild: discord.Guild, record: dict, content: str
    ) -> None:
        """Remove the entry buttons, refresh the embed and announce a result."""
        channel = guild.get_channel(record.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = self._embed(guild, record)
        mentions = discord.AllowedMentions(
            users="<@" in content, roles=False, everyone=False
        )
        edited = False
        if record.get("message_id"):
            try:
                message = channel.get_partial_message(int(record["message_id"]))
                await message.edit(
                    embed=embed,
                    view=None,
                    attachments=banner_files(record.get("image")),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                edited = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not edit giveaway message: %s", exc)
        try:
            if edited:
                await channel.send(content=content, allowed_mentions=mentions)
            else:
                await channel.send(
                    content=content,
                    embed=embed,
                    files=banner_files(record.get("image")),
                    allowed_mentions=mentions,
                )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not announce giveaway result: %s", exc)

    async def _publish_ended(
        self, guild: discord.Guild, record: dict
    ) -> None:
        winner_ids = record.get("winner_ids") or []
        if winner_ids:
            mentions = ", ".join(f"<@{user_id}>" for user_id in winner_ids)
            content = (
                f"🎉 Congratulations {mentions}! "
                f"You won **{record.get('prize')}**."
            )
        else:
            content = (
                f"😕 The giveaway for **{record.get('prize')}** ended with "
                "no eligible entries."
            )

        await self._finalize(guild, record, content)

        await self.bot.audit(
            guild,
            title="🎉 Giveaway ended",
            description=(
                f"**{record.get('prize')}**\n"
                + (
                    "Winners: "
                    + ", ".join(f"<@{user_id}>" for user_id in winner_ids)
                    if winner_ids
                    else "No eligible entries."
                )
            ),
            color=SUCCESS_COLOR,
            fields=[
                ("Giveaway", f"#{record.get('id')}", True),
                ("Entries", f"{self._entry_unique(record)}", True),
                ("Ended by", record.get("ended_by_name") or "Automatic", True),
            ],
        )

    async def _publish_cancelled(
        self, guild: discord.Guild, record: dict
    ) -> None:
        content = (
            f"🚫 The giveaway for **{record.get('prize')}** was cancelled."
        )
        await self._finalize(guild, record, content)
        await self.bot.audit(
            guild,
            title="🚫 Giveaway cancelled",
            description=f"**{record.get('prize')}**",
            color=ERROR_COLOR,
            fields=[
                ("Giveaway", f"#{record.get('id')}", True),
                ("By", record.get("ended_by_name") or "Staff", True),
            ],
        )

    async def _publish_rerolled(
        self, guild: discord.Guild, record: dict
    ) -> None:
        winner_ids = record.get("winner_ids") or []
        mentions = ", ".join(f"<@{user_id}>" for user_id in winner_ids)
        content = f"🔁 Rerolled winners: {mentions} — **{record.get('prize')}**"
        await self._finalize(guild, record, content)
        await self.bot.audit(
            guild,
            title="🔁 Giveaway rerolled",
            description=(
                f"**{record.get('prize')}**\nWinners: {mentions or 'none'}"
            ),
            color=BRAND_COLOR,
            fields=[
                ("Giveaway", f"#{record.get('id')}", True),
                ("By", record.get("rerolled_by_name") or "Staff", True),
            ],
        )

    async def _end_giveaway(
        self,
        guild: discord.Guild,
        record: dict,
        ended_by: discord.abc.User | None = None,
    ) -> bool:
        async with self._lock:
            if record.get("status") != "active":
                return False
            now = discord.utils.utcnow()
            record["status"] = "ended"
            record["ended_at"] = now.isoformat()
            record["ended_by"] = getattr(ended_by, "id", None)
            record["ended_by_name"] = str(ended_by) if ended_by else "Automatic"
            try:
                count = max(
                    1, min(MAX_WINNERS, int(record.get("winners", 1)))
                )
            except (TypeError, ValueError):
                count = 1
            record["winner_ids"] = self._pick_winners(guild, record, count)
            await self.bot.db.save()
        await self._publish_ended(guild, record)
        return True

    async def _cancel_giveaway(
        self,
        guild: discord.Guild,
        record: dict,
        cancelled_by: discord.abc.User,
    ) -> bool:
        async with self._lock:
            if record.get("status") != "active":
                return False
            record["status"] = "cancelled"
            record["ended_at"] = discord.utils.utcnow().isoformat()
            record["ended_by"] = cancelled_by.id
            record["ended_by_name"] = str(cancelled_by)
            record["winner_ids"] = []
            await self.bot.db.save()
        await self._publish_cancelled(guild, record)
        return True

    async def _reroll_giveaway(
        self,
        guild: discord.Guild,
        record: dict,
        count: int,
        rerolled_by: discord.abc.User,
    ) -> list[int] | None:
        async with self._lock:
            if record.get("status") != "ended":
                return None
            # Exclude everyone who has already won, not just the current
            # winners, so repeated rerolls cannot bring back earlier picks.
            excluded: set[int] = set()
            for raw_id in list(record.get("winner_ids") or []) + list(
                record.get("excluded_ids") or []
            ):
                try:
                    excluded.add(int(raw_id))
                except (TypeError, ValueError):
                    continue
            winners = self._pick_winners(guild, record, count, exclude=excluded)
            if not winners:
                return []
            record["excluded_ids"] = sorted(excluded)
            record["winner_ids"] = winners
            record["rerolled"] = int(record.get("rerolled") or 0) + 1
            record["rerolled_at"] = discord.utils.utcnow().isoformat()
            record["rerolled_by"] = rerolled_by.id
            record["rerolled_by_name"] = str(rerolled_by)
            await self.bot.db.save()
        await self._publish_rerolled(guild, record)
        return winners

    # ---------------------------------------------------------------- entries

    async def handle_entry(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(
                embed=error_embed("This giveaway is not available here."),
                ephemeral=True,
            )
            return
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if member is None:
            await interaction.response.send_message(
                embed=error_embed("I could not find your member profile."),
                ephemeral=True,
            )
            return
        if member.bot:
            await interaction.response.send_message(
                embed=error_embed("Bots cannot enter giveaways."),
                ephemeral=True,
            )
            return

        error: str | None = None
        record: dict | None = None
        already = False
        count = 1
        async with self._lock:
            record = self._find_by_message(guild, interaction.message.id)
            if record is None:
                error = "This giveaway is no longer available."
            elif record.get("status") != "active":
                error = "This giveaway has ended."
            else:
                ends = self._parse_dt(record.get("ends_at"))
                if ends is not None and ends <= discord.utils.utcnow():
                    error = "This giveaway has ended."
                else:
                    eligible, reason = self._check_requirements(
                        member, record.get("requirements") or {}
                    )
                    if not eligible:
                        error = reason
                    else:
                        bonus = self._bonus_for(
                            member, record.get("requirements") or {}
                        )
                        entries = record.get("entries")
                        if not isinstance(entries, dict):
                            entries = {}
                            record["entries"] = entries
                        try:
                            previous = int(entries.get(str(member.id)) or 0)
                        except (TypeError, ValueError):
                            previous = 0
                        already = previous > 0
                        count = max(previous, 1 + bonus)
                        entries[str(member.id)] = count
                        await self.bot.db.save()

        if error is not None:
            await interaction.response.send_message(
                embed=error_embed(error), ephemeral=True
            )
            return
        word = "entry" if count == 1 else "entries"
        if already:
            text = f"Your entry is updated — you have **{count}** {word}."
        else:
            text = f"You're in! You have **{count}** {word}."
        await interaction.response.send_message(
            embed=success_embed(text, title="🎉 Entered"), ephemeral=True
        )
        if record is not None:
            await self._refresh_message(guild, record)

    async def handle_leave(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(
                embed=error_embed("This giveaway is not available here."),
                ephemeral=True,
            )
            return
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if member is None:
            await interaction.response.send_message(
                embed=error_embed("I could not find your member profile."),
                ephemeral=True,
            )
            return

        error: str | None = None
        record: dict | None = None
        async with self._lock:
            record = self._find_by_message(guild, interaction.message.id)
            if record is None:
                error = "This giveaway is no longer available."
            elif record.get("status") != "active":
                error = "This giveaway has ended."
            else:
                ends = self._parse_dt(record.get("ends_at"))
                if ends is not None and ends <= discord.utils.utcnow():
                    error = "This giveaway has ended."
                else:
                    entries = record.get("entries")
                    if not isinstance(entries, dict) or str(member.id) not in entries:
                        error = "You are not entered in this giveaway."
                    else:
                        entries.pop(str(member.id), None)
                        await self.bot.db.save()

        if error is not None:
            await interaction.response.send_message(
                embed=error_embed(error), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=success_embed("You left the giveaway."), ephemeral=True
        )
        if record is not None:
            await self._refresh_message(guild, record)

    # ------------------------------------------------------------------ loop

    async def _end_due(self) -> None:
        now = discord.utils.utcnow()
        for guild in list(self.bot.guilds):
            records = self._records(guild)
            for record in list(records.values()):
                if not isinstance(record, dict):
                    continue
                if record.get("status") != "active":
                    continue
                ends = self._parse_dt(record.get("ends_at"))
                if ends is not None and ends > now:
                    continue
                try:
                    await self._end_giveaway(guild, record)
                except Exception:
                    log.exception(
                        "Could not end giveaway %s in guild %s",
                        record.get("id"),
                        guild.id,
                    )

    @tasks.loop(seconds=30)
    async def giveaway_loop(self) -> None:
        try:
            await self._end_due()
        except Exception:
            log.exception("Giveaway loop failed")

    @giveaway_loop.before_loop
    async def before_giveaway_loop(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        try:
            await self._end_due()
        except Exception:
            log.exception("Giveaway catch-up failed")

    # -------------------------------------------------------------- commands

    @commands.hybrid_group(
        name="giveaway",
        aliases=["gw"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def giveaway(self, ctx: commands.Context) -> None:
        """Create and manage giveaways."""
        default = self._default_channel(ctx.guild)
        active = sum(
            1
            for record in self._records(ctx.guild).values()
            if isinstance(record, dict) and record.get("status") == "active"
        )
        await ctx.reply(
            embed=info_embed(
                f"**Default channel:** "
                f"{default.mention if default else 'current channel'}\n"
                f"**Active giveaways:** `{active}`\n\n"
                "**Start:** `/giveaway start prize:... duration:1h winners:1`\n"
                "**Banner:** `image:` for a link, or upload a picture/GIF with "
                "`banner:`\n"
                "**Must have:** `verified`, `required_role`, `booster`, "
                "`min_account_age`, `require_any`\n"
                "**Set channel:** `/giveaway channel channel:#giveaways`\n"
                "**Manage:** `/giveaway list`, `end`, `cancel`, `reroll`, "
                "`entries`",
                title="🎉 Giveaways",
            ),
            mention_author=False,
        )

    @giveaway.command(name="start", usage="<prize> <duration> [winners] [channel]")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(
        prize="What the winner gets",
        duration="How long it runs, e.g. 1h, 30m, 2d12h",
        winners="How many winners (1-20)",
        channel="Where to post it; defaults to the configured channel or here",
        description="Extra text shown in the embed; use \\\\n for a new line",
        required_role="Members must have this role",
        verified="Members must have the verification role",
        booster="Members must be a server booster",
        min_account_age="Minimum Discord account age in days (0 disables)",
        require_any="If true, satisfying any one requirement is enough",
        bonus_role="Role that grants extra entries",
        bonus_entries="Extra entries for the bonus role (0-10)",
        ping_role="Role to ping when the giveaway starts",
        image="Optional image or animated GIF URL",
        banner="Optional image or animated GIF upload (attachment)",
    )
    async def start(
        self,
        ctx: commands.Context,
        prize: str,
        duration: str,
        winners: int = 1,
        channel: discord.TextChannel | None = None,
        *,
        description: str | None = None,
        required_role: discord.Role | None = None,
        verified: bool = False,
        booster: bool = False,
        min_account_age: int = 0,
        require_any: bool = False,
        bonus_role: discord.Role | None = None,
        bonus_entries: int = 1,
        ping_role: discord.Role | None = None,
        image: str | None = None,
        banner: Optional[discord.Attachment] = None,
    ) -> None:
        """Start a giveaway with optional entry requirements."""
        prize = prize.strip()
        if not prize:
            await ctx.reply(
                embed=error_embed("The prize cannot be empty."),
                mention_author=False,
            )
            return
        seconds = parse_duration(duration, default_unit="m")
        if seconds is None or seconds < MIN_DURATION:
            await ctx.reply(
                embed=error_embed(
                    "Use a duration of at least **10 seconds**, for example "
                    "`30m`, `1h` or `2d12h`."
                ),
                mention_author=False,
            )
            return
        if seconds > MAX_DURATION:
            await ctx.reply(
                embed=error_embed("The maximum duration is **30 days**."),
                mention_author=False,
            )
            return
        if not 1 <= winners <= MAX_WINNERS:
            await ctx.reply(
                embed=error_embed(
                    f"Choose between **1** and **{MAX_WINNERS}** winners."
                ),
                mention_author=False,
            )
            return
        if not 0 <= min_account_age <= MAX_ACCOUNT_AGE:
            await ctx.reply(
                embed=error_embed(
                    f"Account age must be between 0 and {MAX_ACCOUNT_AGE} days."
                ),
                mention_author=False,
            )
            return
        if not 0 <= bonus_entries <= MAX_BONUS_ENTRIES:
            await ctx.reply(
                embed=error_embed(
                    f"Bonus entries must be between 0 and {MAX_BONUS_ENTRIES}."
                ),
                mention_author=False,
            )
            return

        data = self._data(ctx.guild)
        verification = data.get("verification") or {}
        verified_role = (
            ctx.guild.get_role(verification.get("role_id") or 0)
            if verified
            else None
        )
        if verified and verified_role is None:
            await ctx.reply(
                embed=error_embed(
                    "There is no verification role set up, so the verified "
                    "requirement cannot be used. Set up `/verification` first "
                    "or omit `verified`."
                ),
                mention_author=False,
            )
            return
        if required_role is not None and required_role.is_default():
            await ctx.reply(
                embed=error_embed("Pick a real role, not @everyone."),
                mention_author=False,
            )
            return
        if bonus_role is not None and bonus_role.is_default():
            await ctx.reply(
                embed=error_embed("The bonus role cannot be @everyone."),
                mention_author=False,
            )
            return
        if ping_role is not None and ping_role.is_default():
            await ctx.reply(
                embed=error_embed("The ping role cannot be @everyone."),
                mention_author=False,
            )
            return
        image, error = await banner_from_input(
            ctx, banner, image, required=False
        )
        if error is not None:
            await ctx.reply(embed=error_embed(error), mention_author=False)
            return

        target = channel or self._default_channel(ctx.guild)
        if target is None and isinstance(ctx.channel, discord.TextChannel):
            target = ctx.channel
        if target is None:
            await ctx.reply(
                embed=error_embed(
                    "Choose a text channel or set a default with "
                    "`/giveaway channel`."
                ),
                mention_author=False,
            )
            return
        missing = self._channel_problem(ctx.guild, target)
        if missing:
            await ctx.reply(
                embed=error_embed(
                    f"I need **{'**, **'.join(missing)}** in {target.mention}."
                ),
                mention_author=False,
            )
            return

        now = discord.utils.utcnow()
        ends_at = now + timedelta(seconds=seconds)
        requirements = {
            "mode": "any" if require_any else "all",
            "verified": bool(verified),
            "verified_role_id": verified_role.id if verified_role else None,
            "role_id": required_role.id if required_role else None,
            "booster": bool(booster),
            "min_account_age": max(0, min_account_age),
            "bonus_role_id": bonus_role.id if bonus_role else None,
            "bonus_entries": max(0, min(bonus_entries, MAX_BONUS_ENTRIES)),
        }
        record = {
            "id": 0,
            "channel_id": target.id,
            "message_id": None,
            "host_id": ctx.author.id,
            "host_name": str(ctx.author),
            "prize": truncate(prize, 200),
            "description": truncate(_expand(description or ""), 2000),
            "winners": winners,
            "created_at": now.isoformat(),
            "ends_at": ends_at.isoformat(),
            "status": "active",
            "ended_at": None,
            "ended_by": None,
            "ended_by_name": None,
            "winner_ids": [],
            "excluded_ids": [],
            "entries": {},
            "requirements": requirements,
            "image": image,
            "rerolled": 0,
            "ping_role_id": ping_role.id if ping_role else None,
        }

        await self._defer(ctx)
        async with self._start_lock:
            data = self._data(ctx.guild)
            try:
                sequence = int(data.get("giveaway_seq") or 0)
            except (TypeError, ValueError):
                sequence = 0
            sequence += 1
            data["giveaway_seq"] = sequence
            record["id"] = sequence
            records = self._records(ctx.guild)
            records[str(sequence)] = record
            content = (
                f"{ping_role.mention} 🎉 A new giveaway just started!"
                if ping_role
                else None
            )
            try:
                message = await target.send(
                    content=content,
                    embed=self._embed(ctx.guild, record),
                    files=banner_files(record.get("image")),
                    view=GiveawayEntryView(),
                    allowed_mentions=discord.AllowedMentions(
                        users=False,
                        roles=ping_role is not None,
                        everyone=False,
                    ),
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                records.pop(str(sequence), None)
                await self.bot.db.save()
                await ctx.reply(
                    embed=error_embed(f"I could not post the giveaway: {exc}"),
                    mention_author=False,
                )
                return
            record["message_id"] = message.id
            await self.bot.db.save()

        await self.bot.audit(
            ctx.guild,
            title="🎉 Giveaway started",
            description=f"**{record['prize']}**",
            color=BRAND_COLOR,
            fields=[
                ("Giveaway", f"#{record['id']}", True),
                ("Channel", target.mention, True),
                ("Winners", str(winners), True),
                ("Ends", self._format_dt(record["ends_at"], "R"), True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"Giveaway **#{record['id']}** for **{record['prize']}** is "
                f"live in {target.mention}.\n"
                f"It ends {self._format_dt(record['ends_at'], 'R')} with "
                f"**{winners}** winner(s).",
                title="🎉 Giveaway started",
            ),
            mention_author=False,
        )

    @giveaway.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(channel="Default channel for giveaways")
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the default giveaway channel."""
        missing = self._channel_problem(ctx.guild, channel)
        if missing:
            await ctx.reply(
                embed=error_embed(
                    f"I need **{'**, **'.join(missing)}** in {channel.mention}."
                ),
                mention_author=False,
            )
            return
        self._data(ctx.guild)["giveaway_channel_id"] = channel.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🎉 Giveaway channel set",
            description=f"Giveaways will default to {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Giveaways will default to {channel.mention}.\n"
                "You can still override the channel per giveaway.",
                title="🎉 Giveaway channel set",
            ),
            mention_author=False,
        )

    @giveaway.command(name="end", usage="<giveaway_id>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(
        giveaway_id="Giveaway number from /giveaway list"
    )
    async def end_cmd(
        self, ctx: commands.Context, giveaway_id: int
    ) -> None:
        """End an active giveaway now and draw winners."""
        record = self._find_by_id(ctx.guild, giveaway_id)
        if record is None:
            await ctx.reply(
                embed=error_embed("I could not find that giveaway."),
                mention_author=False,
            )
            return
        if record.get("status") != "active":
            await ctx.reply(
                embed=info_embed("That giveaway is not active."),
                mention_author=False,
            )
            return
        await self._defer(ctx)
        ended = await self._end_giveaway(ctx.guild, record, ctx.author)
        if not ended:
            await ctx.reply(
                embed=info_embed("That giveaway has already ended."),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed(
                f"Giveaway **#{giveaway_id}** has been ended and winners "
                "were announced.",
                title="🎉 Giveaway ended",
            ),
            mention_author=False,
        )

    @giveaway.command(name="cancel", usage="<giveaway_id>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(giveaway_id="Giveaway number to cancel")
    async def cancel_cmd(
        self, ctx: commands.Context, giveaway_id: int
    ) -> None:
        """Cancel an active giveaway without choosing a winner."""
        record = self._find_by_id(ctx.guild, giveaway_id)
        if record is None:
            await ctx.reply(
                embed=error_embed("I could not find that giveaway."),
                mention_author=False,
            )
            return
        if record.get("status") != "active":
            await ctx.reply(
                embed=info_embed("That giveaway is not active."),
                mention_author=False,
            )
            return
        confirmed = await ConfirmView.prompt(
            ctx,
            f"Cancel the giveaway for **{record.get('prize')}**? "
            "No winner will be chosen.",
            confirm_label="Cancel giveaway",
        )
        if not confirmed:
            return
        await self._cancel_giveaway(ctx.guild, record, ctx.author)
        await ctx.reply(
            embed=success_embed(
                f"Giveaway **#{giveaway_id}** was cancelled.",
                title="🚫 Giveaway cancelled",
            ),
            mention_author=False,
        )

    @giveaway.command(name="reroll", usage="<giveaway_id> [winners]")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(
        giveaway_id="Giveaway number to reroll",
        winners="How many new winners; defaults to the original count",
    )
    async def reroll_cmd(
        self,
        ctx: commands.Context,
        giveaway_id: int,
        winners: int | None = None,
    ) -> None:
        """Pick new winners for an ended giveaway."""
        record = self._find_by_id(ctx.guild, giveaway_id)
        if record is None:
            await ctx.reply(
                embed=error_embed("I could not find that giveaway."),
                mention_author=False,
            )
            return
        if record.get("status") != "ended":
            await ctx.reply(
                embed=error_embed(
                    "Only an ended giveaway can be rerolled. Use "
                    "`/giveaway end` first if it is still active."
                ),
                mention_author=False,
            )
            return
        try:
            count = (
                winners
                if winners is not None
                else int(record.get("winners", 1))
            )
        except (TypeError, ValueError):
            count = 1
        if not 1 <= count <= MAX_WINNERS:
            await ctx.reply(
                embed=error_embed(
                    f"Choose between **1** and **{MAX_WINNERS}** winners."
                ),
                mention_author=False,
            )
            return
        await self._defer(ctx)
        result = await self._reroll_giveaway(
            ctx.guild, record, count, ctx.author
        )
        if result is None:
            await ctx.reply(
                embed=error_embed("That giveaway is not available to reroll."),
                mention_author=False,
            )
            return
        if not result:
            await ctx.reply(
                embed=info_embed(
                    "No eligible entries are available for a reroll. "
                    "Previous winners are excluded."
                ),
                mention_author=False,
            )
            return
        mentions = ", ".join(f"<@{user_id}>" for user_id in result)
        await ctx.reply(
            embed=success_embed(
                f"Rerolled winners for **#{giveaway_id}**: {mentions}",
                title="🔁 Giveaway rerolled",
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @giveaway.command(name="list", usage="")
    @commands.guild_only()
    async def list_cmd(self, ctx: commands.Context) -> None:
        """List active and recent giveaways."""
        records = [
            record
            for record in self._records(ctx.guild).values()
            if isinstance(record, dict)
        ]
        if not records:
            await ctx.reply(
                embed=info_embed(
                    "No giveaways yet.\n"
                    "Start one with `/giveaway start prize:... duration:1h`."
                ),
                mention_author=False,
            )
            return
        active = [r for r in records if r.get("status") == "active"]
        finished = [r for r in records if r.get("status") != "active"]
        active.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        finished.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        finished = finished[:10]
        lines: list[str] = []
        for record in active[:15]:
            channel = ctx.guild.get_channel(record.get("channel_id") or 0)
            lines.append(
                f"🟢 **#{record.get('id')}** "
                f"{truncate(str(record.get('prize')), 80)} — ends "
                f"{self._format_dt(record.get('ends_at'), 'R')} — "
                f"{self._entry_unique(record)} entries — "
                f"{channel.mention if isinstance(channel, discord.TextChannel) else 'deleted channel'}"
            )
        if len(active) > 15:
            lines.append(f"… and {len(active) - 15} more active giveaways.")
        for record in finished:
            status = record.get("status") or "ended"
            icon = "🚫" if status == "cancelled" else "🏁"
            winners = record.get("winner_ids") or []
            suffix = (
                " — winners "
                + ", ".join(f"<@{user_id}>" for user_id in winners[:3])
                if winners
                else ""
            )
            lines.append(
                f"{icon} **#{record.get('id')}** "
                f"{truncate(str(record.get('prize')), 80)} — {status} — "
                f"{self._entry_unique(record)} entries{suffix}"
            )
        await ctx.reply(
            embed=info_embed(
                truncate("\n".join(lines), 4000), title="🎉 Giveaways"
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @giveaway.command(name="entries", usage="<giveaway_id>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    @discord.app_commands.describe(
        giveaway_id="Giveaway number whose entries you want to view"
    )
    async def entries_cmd(
        self, ctx: commands.Context, giveaway_id: int
    ) -> None:
        """List participants and their entry counts."""
        record = self._find_by_id(ctx.guild, giveaway_id)
        if record is None:
            await ctx.reply(
                embed=error_embed("I could not find that giveaway."),
                mention_author=False,
            )
            return
        entries = record.get("entries") or {}
        items: list[tuple[int, int]] = []
        for raw_id, raw_count in entries.items():
            try:
                user_id = int(raw_id)
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                items.append((user_id, count))
        if not items:
            await ctx.reply(
                embed=info_embed("No one has entered this giveaway yet."),
                mention_author=False,
            )
            return
        items.sort(key=lambda item: (-item[1], item[0]))
        lines = []
        for user_id, count in items[:50]:
            member = ctx.guild.get_member(user_id)
            label = member.mention if member else f"<@{user_id}>"
            lines.append(
                f"• {label} — **{count}** "
                f"{'entry' if count == 1 else 'entries'}"
            )
        if len(items) > 50:
            lines.append(f"… and {len(items) - 50} more.")
        await ctx.reply(
            embed=info_embed(
                f"**Unique:** {len(items)} • "
                f"**Total entries:** {sum(count for _, count in items)}\n\n"
                + "\n".join(lines),
                title=f"🎉 Entries — #{giveaway_id}",
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
