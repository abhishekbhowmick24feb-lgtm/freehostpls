"""Moderation commands: bans, kicks, timeouts, warnings and channel controls."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

from core.utils import (
    ConfirmView,
    error_embed,
    fmt_duration,
    hierarchy_problem,
    info_embed,
    parse_duration,
    success_embed,
    truncate,
    warning_embed,
)

log = logging.getLogger(__name__)

MAX_TIMEOUT = 28 * 86400
DEFAULT_MUTE_SECONDS = 3600
MUTE_ROLE_NAME = "Muted"


def _aware(value: str | None) -> dt.datetime | None:
    """Read a stored ISO timestamp as a timezone-aware datetime."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


class ModeratorOnly(commands.CheckFailure):
    """Raised when a member is neither a moderator nor permitted by Discord."""


def is_moderator(permission: str):
    """Allow the configured moderator role, administrators, or the permission."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False

        member = ctx.author
        if member.id == ctx.guild.owner_id or member.guild_permissions.administrator:
            return True

        role_id = ctx.bot.db.guild(ctx.guild.id).get("mod_role_id")
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is not None and role in member.roles:
            return True
        if getattr(member.guild_permissions, permission, False):
            return True

        pretty = permission.replace("_", " ")
        if role is not None:
            raise ModeratorOnly(
                f"You need the {role.mention} role or the **{pretty}** permission."
            )
        raise ModeratorOnly(
            f"You need the **{pretty}** permission.\n"
            f"An administrator can set a moderator role with `{ctx.prefix}modrole @Role`."
        )

    return commands.check(predicate)


class Moderation(commands.Cog):
    """Keep the server clean with bans, kicks, timeouts and warnings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._expire_mutes.start()

    def cog_unload(self) -> None:
        self._expire_mutes.cancel()

    # --------------------------------------------------------------- helpers

    async def _notify(
        self,
        user: discord.abc.User,
        guild: discord.Guild,
        action: str,
        reason: str,
        extra: str = "",
    ) -> None:
        embed = discord.Embed(
            title=f"You were {action}",
            description=(
                f"**Server:** {guild.name}\n"
                f"**Reason:** {reason}"
                + (f"\n{extra}" if extra else "")
            ),
            color=0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            log.info("Could not DM %s about '%s' (DMs closed)", user.id, action)
        except discord.HTTPException as exc:
            log.warning("Could not DM %s: %s", user.id, exc)

    @staticmethod
    def _reason(ctx: commands.Context, reason: str) -> str:
        return f"{ctx.author} ({ctx.author.id}): {reason}"

    async def _audit(
        self,
        ctx: commands.Context,
        action: str,
        target: str,
        reason: str = "",
        *,
        color: int = 0x5865F2,
    ) -> None:
        description = (
            f"**Target:** {target}\n"
            f"**Moderator:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Channel:** {ctx.channel.mention}"
        )
        if reason:
            description += f"\n**Reason:** {truncate(reason, 800)}"
        await self.bot.audit(
            ctx.guild, title=action, description=description, color=color
        )

    @commands.hybrid_command(
        name="modrole", aliases=["moderator", "staffrole"], usage="<@role|none>"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def modrole(self, ctx: commands.Context, *, role: str) -> None:
        """Choose a staff role that can use every moderation command."""
        data = self.bot.db.guild(ctx.guild.id)

        if role.lower() in {"none", "clear", "reset", "off"}:
            data["mod_role_id"] = None
            await self.bot.db.save()
            await ctx.reply(
                embed=success_embed(
                    "Moderator role cleared.\n"
                    "Moderation commands now require Discord permissions again."
                ),
                mention_author=False,
            )
            return

        try:
            target_role = await commands.RoleConverter().convert(ctx, role)
        except commands.BadArgument as exc:
            raise commands.BadArgument(
                "Mention a role, e.g. `modrole @Moderator`."
            ) from exc
        if target_role.is_default() or target_role.managed:
            raise commands.BadArgument("That role cannot be used as a moderator role.")

        data["mod_role_id"] = target_role.id
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"**{target_role.mention}** can now use every moderation command.\n"
                "Administrators and members with the matching Discord permissions keep access.",
                title="Moderator role set",
            ),
            mention_author=False,
        )

    # ------------------------------------------------------------------ ban

    @commands.hybrid_command(name="ban", usage="<user> [reason]")
    @commands.guild_only()
    @is_moderator("ban_members")
    @commands.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: commands.Context,
        user: discord.User,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Ban a user and delete nothing else."""
        if user.id == ctx.author.id:
            await ctx.reply(embed=error_embed("You cannot ban yourself."), mention_author=False)
            return

        member = ctx.guild.get_member(user.id)
        if member is not None:
            problem = hierarchy_problem(ctx, member)
            if problem:
                await ctx.reply(embed=error_embed(problem), mention_author=False)
                return

        await self._notify(user, ctx.guild, "banned", reason)
        try:
            await ctx.guild.ban(user, reason=self._reason(ctx, reason))
        except discord.Forbidden:
            await ctx.reply(
                embed=error_embed("I do not have permission to ban that user."),
                mention_author=False,
            )
            return

        await self._audit(
            ctx, "🔨 Member banned", f"{user} (`{user.id}`)", reason, color=0xED4245
        )
        await ctx.reply(
            embed=success_embed(
                f"**{user}** (`{user.id}`) was banned.\n**Reason:** {reason}",
                title="🔨 Banned",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="unban", usage="<user id> [reason]")
    @commands.guild_only()
    @is_moderator("ban_members")
    @commands.bot_has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: commands.Context,
        user_id: int,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Unban a user by their ID."""
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user, reason=self._reason(ctx, reason))
        except discord.NotFound:
            await ctx.reply(
                embed=error_embed("That user is not banned (or does not exist)."),
                mention_author=False,
            )
            return

        await self._audit(
            ctx, "🔓 Member unbanned", f"{user} (`{user.id}`)", reason, color=0x2ECC71
        )
        await ctx.reply(
            embed=success_embed(
                f"**{user}** (`{user.id}`) was unbanned.\n**Reason:** {reason}",
                title="🔓 Unbanned",
            ),
            mention_author=False,
        )

    # ----------------------------------------------------------------- kick

    @commands.hybrid_command(name="kick", usage="<member> [reason]")
    @commands.guild_only()
    @is_moderator("kick_members")
    @commands.bot_has_permissions(kick_members=True)
    async def kick(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Kick a member from the server."""
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        await self._notify(member, ctx.guild, "kicked", reason)
        await member.kick(reason=self._reason(ctx, reason))
        await self._audit(
            ctx, "👢 Member kicked", f"{member} (`{member.id}`)", reason, color=0xED4245
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** (`{member.id}`) was kicked.\n**Reason:** {reason}",
                title="👢 Kicked",
            ),
            mention_author=False,
        )

    # -------------------------------------------------------------- timeout

    @commands.hybrid_command(name="timeout", usage="<member> <duration> [reason]")
    @commands.guild_only()
    @is_moderator("moderate_members")
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Timeout a member. Duration examples: 10m, 1h30m, 2d (max 28d)."""
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        seconds = parse_duration(duration, default_unit="m")
        if seconds is None:
            await ctx.reply(
                embed=error_embed(
                    "Invalid duration. Use something like `10m`, `1h30m` or `2d`."
                ),
                mention_author=False,
            )
            return

        seconds = min(seconds, MAX_TIMEOUT)
        until = discord.utils.utcnow() + dt.timedelta(seconds=seconds)
        await member.timeout(until, reason=self._reason(ctx, reason))
        await self._notify(
            member,
            ctx.guild,
            "timed out",
            reason,
            extra=f"**Duration:** {fmt_duration(seconds)}",
        )
        await self._audit(
            ctx,
            "🔇 Member timed out",
            f"{member} (`{member.id}`) for {fmt_duration(seconds)}",
            reason,
            color=0xFEE75C,
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** is timed out for **{fmt_duration(seconds)}**.\n"
                f"**Reason:** {reason}",
                title="🔇 Timed out",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="untimeout", usage="<member> [reason]")
    @commands.guild_only()
    @is_moderator("moderate_members")
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Remove a member's timeout."""
        if not member.is_timed_out():
            await ctx.reply(
                embed=info_embed(f"**{member}** is not timed out."), mention_author=False
            )
            return
        await member.timeout(None, reason=self._reason(ctx, reason))
        await self._audit(
            ctx,
            "🔊 Timeout removed",
            f"{member} (`{member.id}`)",
            reason,
            color=0x2ECC71,
        )
        await ctx.reply(
            embed=success_embed(f"**{member}** can talk again.", title="🔊 Timeout removed"),
            mention_author=False,
        )

    # ----------------------------------------------------------------- mute

    async def _ensure_mute_role(
        self, guild: discord.Guild, role: discord.Role | None = None
    ) -> tuple[discord.Role | None, str | None]:
        """Return a usable mute role, creating one when the server has none."""
        data = self.bot.db.guild(guild.id)
        if role is None:
            role_id = data.get("mute_role_id")
            role = guild.get_role(role_id) if role_id else None

        if role is None:
            if guild.me is None or not guild.me.guild_permissions.manage_roles:
                return None, "I need the **Manage Roles** permission to mute members."
            role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
            if role is None:
                try:
                    role = await guild.create_role(
                        name=MUTE_ROLE_NAME,
                        permissions=discord.Permissions.none(),
                        colour=discord.Colour.dark_gray(),
                        hoist=False,
                        mentionable=False,
                        reason="Mute role for moderation",
                    )
                except discord.HTTPException as exc:
                    return None, f"I could not create the mute role: {exc}"

        if guild.me is None or role >= guild.me.top_role:
            return None, (
                f"The mute role **{role.name}** is not below my highest role. "
                "Move my role above it and try again."
            )

        data["mute_role_id"] = role.id
        await self.bot.db.save()
        return role, None

    @staticmethod
    def _role_has_overwrites(guild: discord.Guild, role: discord.Role) -> bool:
        """True when the role is already blocked somewhere in this server."""
        for channel in guild.channels:
            overwrites = getattr(channel, "overwrites", None)
            if isinstance(overwrites, dict) and role in overwrites:
                return True
        return False

    async def _apply_mute_overwrites(
        self, guild: discord.Guild, role: discord.Role
    ) -> int:
        """Stop the role from talking or sending in every channel."""
        updated = 0
        for channel in guild.channels:
            if not isinstance(
                channel,
                (
                    discord.TextChannel,
                    discord.VoiceChannel,
                    discord.StageChannel,
                    discord.ForumChannel,
                ),
            ):
                continue
            try:
                await channel.set_permissions(
                    role,
                    send_messages=False,
                    send_messages_in_threads=False,
                    add_reactions=False,
                    speak=False,
                    create_public_threads=False,
                    create_private_threads=False,
                    reason="Mute role permissions",
                )
                updated += 1
            except discord.HTTPException:
                continue
        return updated

    @commands.hybrid_command(
        name="mute", usage="<member> [duration] [reason]"
    )
    @commands.guild_only()
    @is_moderator("moderate_members")
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        member="Member to mute",
        duration="How long: 10m, 1h, 2d … (default 1h)",
        reason="Why they are muted",
    )
    async def mute(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str = "",
        *,
        reason: str = "",
    ) -> None:
        """Mute a member with the Muted role so they cannot send messages or talk."""
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        seconds = parse_duration(duration, default_unit="m") if duration else None
        if seconds is None:
            # The second argument was not a duration, so treat it as the reason.
            reason = " ".join(part for part in (duration, reason) if part.strip())
            seconds = DEFAULT_MUTE_SECONDS
        reason = reason.strip() or "No reason provided"

        role, problem_text = await self._ensure_mute_role(ctx.guild)
        if role is None:
            await ctx.reply(
                embed=error_embed(problem_text or "No mute role available."),
                mention_author=False,
            )
            return
        if not self._role_has_overwrites(ctx.guild, role):
            # First use: block the role in every channel without a manual setup.
            await self._apply_mute_overwrites(ctx.guild, role)

        until = discord.utils.utcnow() + dt.timedelta(seconds=seconds)
        data = self.bot.db.guild(ctx.guild.id)
        data["mutes"][str(member.id)] = {
            "until": until.isoformat(timespec="seconds"),
            "reason": reason,
            "moderator_id": ctx.author.id,
            "moderator": str(ctx.author),
            "at": discord.utils.utcnow().isoformat(timespec="seconds"),
        }
        await self.bot.db.save()

        try:
            if role not in member.roles:
                await member.add_roles(
                    role, reason=self._reason(ctx, f"mute {fmt_duration(seconds)}")
                )
        except discord.Forbidden:
            data["mutes"].pop(str(member.id), None)
            await self.bot.db.save()
            await ctx.reply(
                embed=error_embed(
                    "Discord blocked me from adding the mute role — check my role "
                    "position and the Manage Roles permission."
                ),
                mention_author=False,
            )
            return

        await self._notify(
            member,
            ctx.guild,
            "muted",
            reason,
            extra=(
                f"**Duration:** {fmt_duration(seconds)}\n"
                f"**Ends:** {discord.utils.format_dt(until, 'R')}"
            ),
        )
        await self._audit(
            ctx,
            "🔇 Member muted",
            f"{member} (`{member.id}`) for {fmt_duration(seconds)}",
            reason,
            color=0xFEE75C,
        )
        await ctx.reply(
            embed=success_embed(
                f"**{member}** is muted for **{fmt_duration(seconds)}** and cannot "
                f"send messages or talk.\n"
                f"**Reason:** {reason}\n"
                f"Unmute early with `{ctx.prefix}unmute {member.display_name}`.",
                title="🔇 Muted",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="unmute", usage="<member> [reason]")
    @commands.guild_only()
    @is_moderator("moderate_members")
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Remove a mute, the Muted role and any timeout from a member."""
        data = self.bot.db.guild(ctx.guild.id)
        removed: list[str] = []

        if data["mutes"].pop(str(member.id), None) is not None:
            await self.bot.db.save()
            removed.append("mute")

        role_id = data.get("mute_role_id")
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason=self._reason(ctx, "unmute"))
                removed.append("mute role")
            except discord.Forbidden:
                await ctx.reply(
                    embed=error_embed(
                        "Discord blocked me from removing the mute role — check "
                        "my role position."
                    ),
                    mention_author=False,
                )
                return

        if member.is_timed_out():
            try:
                await member.timeout(None, reason=self._reason(ctx, "unmute"))
                removed.append("timeout")
            except discord.HTTPException as exc:
                log.warning("Could not clear timeout for %s: %s", member.id, exc)

        if not removed:
            await ctx.reply(
                embed=info_embed(f"**{member}** is not muted."), mention_author=False
            )
            return

        await self._notify(member, ctx.guild, "unmuted", reason)
        await self._audit(
            ctx,
            "🔊 Member unmuted",
            f"{member} (`{member.id}`)",
            reason,
            color=0x2ECC71,
        )
        await ctx.reply(
            embed=success_embed(f"**{member}** can talk again.", title="🔊 Unmuted"),
            mention_author=False,
        )

    @commands.hybrid_command(name="mutes", aliases=["mutelist", "muted"])
    @commands.guild_only()
    async def mutes(self, ctx: commands.Context) -> None:
        """List everyone who is currently muted with the Muted role."""
        data = self.bot.db.guild(ctx.guild.id)
        records = data.get("mutes") or {}
        if not records:
            await ctx.reply(
                embed=info_embed(
                    f"Nobody is muted.\nUse `{ctx.prefix}mute @member 1h reason` to mute someone."
                ),
                mention_author=False,
            )
            return

        lines: list[str] = []
        for user_id, record in list(records.items())[:15]:
            member = (
                ctx.guild.get_member(int(user_id))
                if str(user_id).isdigit()
                else None
            )
            who = member.mention if member else f"`{user_id}` (left the server)"
            until = _aware(record.get("until"))
            when = discord.utils.format_dt(until, "R") if until else "no expiry"
            lines.append(
                f"• {who} — {when}\n"
                f"  {truncate(record.get('reason') or 'No reason provided', 120)}"
                f" *(by {record.get('moderator', 'unknown')})*"
            )
        if len(records) > 15:
            lines.append(f"*…and {len(records) - 15} more*")

        await ctx.reply(
            embed=warning_embed(
                f"**{len(records)}** muted member(s).\n\n" + "\n".join(lines),
                title="🔇 Active mutes",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="mutesetup", aliases=["muterole"], usage="[role]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mutesetup(
        self, ctx: commands.Context, role: discord.Role | None = None
    ) -> None:
        """Create (or repair) the Muted role and its channel permissions."""
        if role is not None:
            if role.is_default() or role.managed:
                await ctx.reply(
                    embed=error_embed("That role cannot be used for mutes."),
                    mention_author=False,
                )
                return
            self.bot.db.guild(ctx.guild.id)["mute_role_id"] = role.id
            await self.bot.db.save()

        muted_role, problem_text = await self._ensure_mute_role(ctx.guild, role)
        if muted_role is None:
            await ctx.reply(
                embed=error_embed(problem_text or "No mute role available."),
                mention_author=False,
            )
            return

        updated = await self._apply_mute_overwrites(ctx.guild, muted_role)
        await self._audit(
            ctx,
            "🔧 Mute role configured",
            f"{muted_role.mention} — {updated} channel(s) updated",
            color=0x2ECC71,
        )
        await ctx.reply(
            embed=success_embed(
                f"**{muted_role.mention}** is ready and blocked in {updated} "
                f"channel(s).\n\nUse `{ctx.prefix}mute @member 1h reason` to mute "
                f"someone and `{ctx.prefix}unmute @member` to undo it.\n"
                "Mutes expire on their own, so nobody stays muted by accident.",
                title="🔧 Mute role ready",
            ),
            mention_author=False,
        )

    @tasks.loop(seconds=45)
    async def _expire_mutes(self) -> None:
        """Lift mutes whose time is up, even after a restart."""
        for guild in list(self.bot.guilds):
            data = self.bot.db.guild(guild.id)
            records = data.get("mutes") or {}
            if not records:
                continue
            now = discord.utils.utcnow()
            expired = [
                user_id
                for user_id, record in records.items()
                if (until := _aware(record.get("until")))
                and until <= now
                and str(user_id).isdigit()
            ]
            if not expired:
                continue

            role_id = data.get("mute_role_id")
            role = guild.get_role(role_id) if role_id else None
            for user_id in expired:
                records.pop(user_id, None)
                member = guild.get_member(int(user_id))
                if member is None or role is None or role not in member.roles:
                    continue
                try:
                    await member.remove_roles(role, reason="Mute expired")
                except discord.HTTPException as exc:
                    log.warning("Could not lift expired mute for %s: %s", user_id, exc)
            await self.bot.db.save()
            await self.bot.audit(
                guild,
                title="🔊 Mute expired",
                description=f"**{len(expired)}** member(s) can talk again.",
                color=0x2ECC71,
            )

    @_expire_mutes.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- warnings

    @commands.hybrid_command(name="warn", usage="<member> [reason]")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def warn(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        """Give a member a warning (stored in the database)."""
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return

        data = self.bot.db.guild(ctx.guild.id)
        data["warning_seq"] = int(data.get("warning_seq", 0)) + 1
        warning_id = data["warning_seq"]
        record = {
            "id": warning_id,
            "reason": reason,
            "moderator_id": ctx.author.id,
            "moderator": str(ctx.author),
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        bucket = data["warnings"].setdefault(str(member.id), [])
        bucket.append(record)
        await self.bot.db.save()

        await self._audit(
            ctx,
            f"⚠️ Warning #{warning_id}",
            f"{member} (`{member.id}`)",
            reason,
            color=0xFEE75C,
        )
        await self._notify(member, ctx.guild, "warned", reason)
        await ctx.reply(
            embed=success_embed(
                f"**{member}** received warning **#{warning_id}** "
                f"(they now have **{len(bucket)}**).\n**Reason:** {reason}",
                title="⚠️ Warning added",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="warnings", aliases=["warns", "infractions"], usage="[member]")
    @commands.guild_only()
    async def warnings(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """List warnings for a member (defaults to you)."""
        member = member or ctx.author
        data = self.bot.db.guild(ctx.guild.id)
        records = data["warnings"].get(str(member.id), [])
        if not records:
            await ctx.reply(
                embed=info_embed(f"**{member}** has no warnings."), mention_author=False
            )
            return

        embed = warning_embed(
            f"**{member}** has **{len(records)}** warning(s).", title="Warning history"
        )
        for record in records[:25]:
            try:
                when = discord.utils.format_dt(
                    dt.datetime.fromisoformat(record["at"]), "R"
                )
            except (ValueError, KeyError):
                when = "unknown time"
            embed.add_field(
                name=f"#{record['id']} • {when}",
                value=truncate(
                    f"**Reason:** {record['reason']}\n**By:** {record['moderator']}", 1024
                ),
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="delwarn", usage="<member> <warning id>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def delwarn(
        self, ctx: commands.Context, member: discord.Member, warning_id: int
    ) -> None:
        """Delete a single warning from a member."""
        data = self.bot.db.guild(ctx.guild.id)
        records = data["warnings"].get(str(member.id), [])
        remaining = [r for r in records if int(r["id"]) != warning_id]
        if len(remaining) == len(records):
            await ctx.reply(
                embed=error_embed(f"Warning **#{warning_id}** was not found for {member}."),
                mention_author=False,
            )
            return
        data["warnings"][str(member.id)] = remaining
        await self.bot.db.save()
        await self._audit(
            ctx, "🗑️ Warning removed", f"{member} (`{member.id}`) — #{warning_id}"
        )
        await ctx.reply(
            embed=success_embed(
                f"Warning **#{warning_id}** removed. {member.mention} has **{len(remaining)}** left."
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="clearwarns", usage="<member>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def clearwarns(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Clear every warning from a member."""
        data = self.bot.db.guild(ctx.guild.id)
        count = len(data["warnings"].get(str(member.id), []))
        if not count:
            await ctx.reply(
                embed=info_embed(f"**{member}** has no warnings."), mention_author=False
            )
            return
        if not await ConfirmView.prompt(
            ctx, f"Clear all **{count}** warnings from {member.mention}?"
        ):
            await ctx.reply(embed=info_embed("Cancelled."), mention_author=False)
            return
        data["warnings"].pop(str(member.id), None)
        await self.bot.db.save()
        await self._audit(
            ctx,
            "🧹 Warnings cleared",
            f"{member} (`{member.id}`) — {count} warning(s)",
            color=0x2ECC71,
        )
        await ctx.reply(
            embed=success_embed(f"Cleared **{count}** warnings from {member.mention}."),
            mention_author=False,
        )

    # ---------------------------------------------------------------- purge

    @commands.hybrid_command(
        name="purge", aliases=["clear"], usage="<amount|all> [bots|humans|@member]"
    )
    @commands.guild_only()
    @is_moderator("manage_messages")
    @commands.bot_has_permissions(manage_messages=True)
    @discord.app_commands.describe(
        amount="A number of messages, or 'all' to clear the whole channel",
        target="Optional filter: bots, humans, or a member mention/ID/name",
    )
    async def purge(
        self,
        ctx: commands.Context,
        amount: str,
        *,
        target: str | None = None,
    ) -> None:
        """Delete messages — all or a number, filtered to bots, humans or one member."""
        value = amount.strip().lower()
        limit: int | None

        member: discord.Member | None = None
        only_bots = False
        only_humans = False
        scope = ""

        if target and target.strip():
            requested = target.strip()
            lowered = requested.lower()
            if lowered in {"bots", "bot"}:
                only_bots = True
                scope = " from bots"
            elif lowered in {"humans", "human", "users", "members"}:
                only_humans = True
                scope = " from humans"
            else:
                member_id: int | None = None
                if requested.startswith("<@") and requested.endswith(">"):
                    digits = requested.strip("<@!>")
                    member_id = int(digits) if digits.isdigit() else None
                elif requested.isdigit():
                    member_id = int(requested)

                member = (
                    ctx.guild.get_member(member_id) if member_id is not None else None
                )
                if member is None:
                    member = ctx.guild.get_member_named(requested)
                if member is None:
                    raise commands.BadArgument(
                        "Target must be `bots`, `humans`, or a member mention/ID/name."
                    )
                scope = f" from {member.mention}"

        if value in {"all", "everything", "channel", "full"}:
            if not await ConfirmView.prompt(
                ctx,
                f"Delete **every** message{scope} in {ctx.channel.mention}? "
                "Pinned messages are kept. This cannot be undone.",
                confirm_label="Delete everything",
            ):
                await ctx.reply(
                    embed=info_embed("Cancelled."), mention_author=False
                )
                return
            limit = None
        else:
            try:
                limit = int(value)
            except ValueError as exc:
                raise commands.BadArgument(
                    "Amount must be a number or `all` — for example `purge 50` or `purge all`."
                ) from exc
            if limit < 1:
                raise commands.BadArgument("Amount must be at least `1`.")
            limit = min(limit, 10000)

        def check(message: discord.Message) -> bool:
            if message.pinned:
                return False
            if member is not None:
                return message.author.id == member.id
            if only_bots:
                return message.author.bot
            if only_humans:
                return not message.author.bot
            return True

        try:
            if ctx.message is not None:
                await ctx.message.delete()
        except discord.HTTPException:
            pass

        deleted = await ctx.channel.purge(limit=limit, check=check)
        await self._audit(
            ctx,
            "🧹 Messages purged",
            f"{len(deleted)} message(s) in {ctx.channel.mention}"
            + (" (whole channel)" if limit is None else "")
            + (f" — filter:{scope}" if scope else ""),
        )
        await ctx.send(
            embed=success_embed(
                f"Deleted **{len(deleted)}** message(s){scope}"
                + (" — the channel is clear." if limit is None else "."),
                title="🧹 Purged",
            ),
            delete_after=6,
        )

    # -------------------------------------------------------- channel tools

    @commands.hybrid_command(name="slowmode", usage="<seconds|off>")
    @commands.guild_only()
    @is_moderator("manage_channels")
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: commands.Context, value: str) -> None:
        """Set slowmode. Use a duration like 10s / 1m, or `off`."""
        if value.lower() in {"off", "0", "none"}:
            seconds = 0
        else:
            seconds = parse_duration(value, default_unit="s")
            if seconds is None:
                await ctx.reply(
                    embed=error_embed("Use `off`, `10s`, `30s`, `1m` …"),
                    mention_author=False,
                )
                return
            seconds = min(seconds, 21600)
        await ctx.channel.edit(slowmode_delay=seconds, reason=self._reason(ctx, "slowmode"))
        await self._audit(
            ctx,
            "🐌 Slowmode updated",
            f"{ctx.channel.mention} — {fmt_duration(seconds) if seconds else 'disabled'}",
        )
        text = "Slowmode disabled." if not seconds else f"Slowmode set to **{fmt_duration(seconds)}**."
        await ctx.reply(embed=success_embed(text), mention_author=False)

    async def _apply_whitelist_overwrites(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        allow: bool | None,
    ) -> None:
        """Let whitelisted roles/members talk while a channel is locked."""
        data = self.bot.db.guild(guild.id)
        whitelist = data.get("whitelist") or {}
        reason = "Whitelist channel access"

        role_ids = list(whitelist.get("role_ids") or [])
        permanent_role_id = whitelist.get("role_id")
        if permanent_role_id and permanent_role_id not in role_ids:
            role_ids.append(permanent_role_id)

        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                continue
            overwrite = channel.overwrites_for(role)
            if overwrite.send_messages != allow:
                overwrite.send_messages = allow
                await channel.set_permissions(role, overwrite=overwrite, reason=reason)

        for user_id in whitelist.get("user_ids") or []:
            member = guild.get_member(user_id)
            if member is None:
                continue
            overwrite = channel.overwrites_for(member)
            if overwrite.send_messages != allow:
                overwrite.send_messages = allow
                await channel.set_permissions(member, overwrite=overwrite, reason=reason)

    @commands.hybrid_command(name="lock", usage="[channel]")
    @commands.guild_only()
    @is_moderator("manage_channels")
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Stop members from sending messages in a channel."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=self._reason(ctx, "lock")
        )
        await self._apply_whitelist_overwrites(channel, ctx.guild, True)
        await self._audit(
            ctx, "🔒 Channel locked", channel.mention, color=0xFEE75C
        )
        await ctx.reply(
            embed=success_embed(f"🔒 {channel.mention} is now locked."), mention_author=False
        )

    @commands.hybrid_command(name="unlock", usage="[channel]")
    @commands.guild_only()
    @is_moderator("manage_channels")
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Allow members to send messages in a channel again."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=self._reason(ctx, "unlock")
        )
        await self._apply_whitelist_overwrites(channel, ctx.guild, None)
        await self._audit(
            ctx, "🔓 Channel unlocked", channel.mention, color=0x2ECC71
        )
        await ctx.reply(
            embed=success_embed(f"🔓 {channel.mention} is now unlocked."),
            mention_author=False,
        )

    @commands.hybrid_command(name="addrole", usage="<member> <role>")
    @commands.guild_only()
    @is_moderator("manage_roles")
    @commands.bot_has_permissions(manage_roles=True)
    async def addrole(
        self, ctx: commands.Context, member: discord.Member, *, role: discord.Role
    ) -> None:
        """Give a role to a member."""
        if role.is_default():
            await ctx.reply(
                embed=error_embed("You cannot assign the @everyone role."), mention_author=False
            )
            return
        if role.managed:
            await ctx.reply(
                embed=error_embed("That role is managed by an integration."), mention_author=False
            )
            return
        if role >= ctx.guild.me.top_role:
            await ctx.reply(
                embed=error_embed("That role is higher than my highest role."),
                mention_author=False,
            )
            return
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            await ctx.reply(
                embed=error_embed("That role is higher than your highest role."),
                mention_author=False,
            )
            return
        if role in member.roles:
            await ctx.reply(
                embed=info_embed(f"**{member}** already has {role.mention}."),
                mention_author=False,
            )
            return

        await member.add_roles(role, reason=self._reason(ctx, "addrole"))
        await self._audit(
            ctx,
            "➕ Role added",
            f"{member} (`{member.id}`) — {role.name}",
            color=0x2ECC71,
        )
        await ctx.reply(
            embed=success_embed(f"Added {role.mention} to **{member}**."),
            mention_author=False,
        )

    @commands.hybrid_command(name="removerole", usage="<member> <role>")
    @commands.guild_only()
    @is_moderator("manage_roles")
    @commands.bot_has_permissions(manage_roles=True)
    async def removerole(
        self, ctx: commands.Context, member: discord.Member, *, role: discord.Role
    ) -> None:
        """Remove a role from a member."""
        if role.is_default() or role.managed or role >= ctx.guild.me.top_role:
            await ctx.reply(
                embed=error_embed("I cannot remove that role."), mention_author=False
            )
            return
        if role not in member.roles:
            await ctx.reply(
                embed=info_embed(f"**{member}** does not have {role.mention}."),
                mention_author=False,
            )
            return
        await member.remove_roles(role, reason=self._reason(ctx, "removerole"))
        await self._audit(
            ctx,
            "➖ Role removed",
            f"{member} (`{member.id}`) — {role.name}",
            color=0xED4245,
        )
        await ctx.reply(
            embed=success_embed(f"Removed {role.mention} from **{member}**."),
            mention_author=False,
        )

    @commands.hybrid_command(name="nick", usage="<member> [nickname]")
    @commands.guild_only()
    @is_moderator("manage_nicknames")
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        nickname: str | None = None,
    ) -> None:
        """Change a member's nickname, or reset it when empty."""
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.reply(embed=error_embed(problem), mention_author=False)
            return
        await member.edit(
            nick=nickname, reason=self._reason(ctx, "nickname change")
        )
        await self._audit(
            ctx,
            "📝 Nickname changed",
            f"{member} (`{member.id}`)",
            f"New nickname: {nickname}" if nickname else "Nickname reset",
        )
        if nickname:
            await ctx.reply(
                embed=success_embed(f"**{member}** is now called **{nickname}**."),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=success_embed(f"Reset **{member}**'s nickname."), mention_author=False
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
