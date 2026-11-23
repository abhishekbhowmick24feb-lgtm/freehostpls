"""Server protection: raid mode, anti-nuke detection and scam filtering."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections import deque
from urllib.parse import urlparse

import discord
from discord.ext import commands

from core.config import ERROR_COLOR, FOOTER_TEXT, SUCCESS_COLOR, WARNING_COLOR
from core.utils import (
    ConfirmView,
    error_embed,
    info_embed,
    is_whitelisted,
    success_embed,
    truncate,
    warning_embed,
)

log = logging.getLogger(__name__)

INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.IGNORECASE
)
GIFT_RE = re.compile(
    r"https?://(?:discord\.gift|(?:www\.)?discord(?:app)?\.com/gifts)/\S+",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)

BUILTIN_BLOCKED_DOMAINS = {
    # Fake Nitro / gift pages
    "discord.gift",
    "discordgift.site",
    "discord-gift.com",
    "discord-nitro.com",
    "free-nitro.com",
    "nitro-gift.com",
    "steamcommunity.gift",
    # IP loggers and grabbers
    "grabify.link",
    "iplogger.org",
    "iplogger.com",
    "iplogger.ru",
    "2no.co",
    "yip.su",
    "ps3cfw.com",
    "blasze.com",
    "whatstheirip.com",
    # Link shorteners hide the destination
    "bit.ly",
    "tinyurl.com",
    "goo.gl",
    "is.gd",
    "cutt.ly",
    "shorturl.at",
    "rebrand.ly",
}

ACTION_LIMITS: dict[discord.AuditLogAction, tuple[int, float, str]] = {
    discord.AuditLogAction.ban: (5, 10.0, "mass bans"),
    discord.AuditLogAction.channel_delete: (3, 10.0, "channel deletions"),
    discord.AuditLogAction.role_delete: (3, 10.0, "role deletions"),
    discord.AuditLogAction.webhook_create: (3, 10.0, "webhook creations"),
}


class Security(commands.Cog):
    """Raid mode, anti-nuke alerts and a scam link/invite filter."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._events: dict[tuple[int, int, object], deque[float]] = {}

    # --------------------------------------------------------------- helpers

    def _security_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self.bot.db.guild(guild.id).get("security_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _alert(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        *,
        color: int = ERROR_COLOR,
        fields: list[tuple[str, str, bool]] | None = None,
        dm_owner: bool = False,
    ) -> bool:
        embed = discord.Embed(
            title=truncate(title, 256),
            description=truncate(description, 4000) or None,
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

        security_channel = self._security_channel(guild)
        if security_channel is not None:
            try:
                await security_channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not send security alert to %s: %s",
                    security_channel.id,
                    exc,
                )

        sent = await self.bot.audit(
            guild,
            title=title,
            description=description,
            color=color,
            fields=fields,
        )
        if sent or not dm_owner:
            return sent

        owner = guild.owner
        if owner is None:
            return False
        try:
            await owner.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    def _record(
        self,
        guild_id: int,
        user_id: int,
        action: discord.AuditLogAction,
        count: int,
        seconds: float,
    ) -> bool:
        key = (guild_id, user_id, action)
        now = time.monotonic()
        bucket = self._events.setdefault(key, deque())
        while bucket and now - bucket[0] > seconds:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) >= count:
            bucket.clear()
            return True
        return False

    @staticmethod
    def _is_security_staff(member: discord.Member, data: dict) -> bool:
        if member.guild_permissions.administrator:
            return True
        if member.guild_permissions.manage_messages:
            return True
        role_id = data.get("mod_role_id")
        role = member.guild.get_role(role_id) if role_id else None
        return bool(role is not None and role in member.roles)

    async def _reply(self, ctx: commands.Context, **kwargs) -> None:
        """Reply normally, or use a followup after a slash confirmation."""
        if ctx.interaction is not None:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(**kwargs)
            else:
                await ctx.interaction.response.send_message(**kwargs)
            return
        await ctx.reply(**kwargs)

    async def _quarantine(
        self, guild: discord.Guild, user: discord.abc.User, label: str
    ) -> None:
        member = guild.get_member(user.id)
        if member is None:
            return
        if member.id in {guild.owner_id, self.bot.user.id} or member.bot:
            return
        me = guild.me
        if me is None or member.top_role >= me.top_role:
            await self._alert(
                guild,
                "🚨 Anti-nuke: quarantine blocked",
                f"I could not quarantine **{member}** because their highest role "
                "is above mine.",
                color=WARNING_COLOR,
                dm_owner=True,
            )
            return

        removable = [
            role
            for role in member.roles
            if role != guild.default_role
            and not role.managed
            and role < me.top_role
        ]
        if not removable:
            return
        remaining = [
            role
            for role in member.roles
            if role != guild.default_role and role not in removable
        ]
        try:
            await member.edit(
                roles=remaining,
                reason=f"Anti-nuke quarantine: {label} detected",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not quarantine %s: %s", member.id, exc)
            await self._alert(
                guild,
                "🚨 Anti-nuke: quarantine failed",
                f"I could not remove roles from **{member}**: {exc}",
                color=WARNING_COLOR,
                dm_owner=True,
            )
            return

        try:
            await member.send(
                embed=error_embed(
                    f"Your roles were removed in **{guild.name}** after "
                    "suspicious audit-log activity. Contact the server owner "
                    "if this was a mistake."
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self._alert(
            guild,
            "🚨 Anti-nuke quarantine",
            f"Removed **{len(removable)}** role(s) from **{member}** after "
            f"detecting **{label}**.",
            color=WARNING_COLOR,
            fields=[
                ("Member", f"{member} (`{member.id}`)", True),
                ("Roles removed", f"`{len(removable)}`", True),
            ],
            dm_owner=True,
        )

    # ---------------------------------------------------------- anti-nuke

    @commands.Cog.listener()
    async def on_audit_log_entry_create(
        self, entry: discord.AuditLogEntry
    ) -> None:
        if entry.guild is None or entry.user is None:
            return
        limit = ACTION_LIMITS.get(entry.action)
        if limit is None:
            return

        guild = entry.guild
        data = self.bot.db.guild(guild.id)
        if not data.get("antinuke_enabled", True):
            return
        if entry.user.id in {self.bot.user.id, guild.owner_id}:
            return

        count, seconds, label = limit
        if not self._record(guild.id, entry.user.id, entry.action, count, seconds):
            return

        target = getattr(entry.target, "name", None) or getattr(
            entry.target, "id", entry.target_id
        )
        fields = [
            ("Executor", f"{entry.user} (`{entry.user.id}`)", True),
            ("Action", label, True),
            ("Threshold", f"{count} in {seconds:g}s", True),
        ]
        if entry.reason:
            fields.append(("Reason", truncate(str(entry.reason), 500), False))
        await self._alert(
            guild,
            "🚨 Anti-nuke alert",
            f"**{entry.user}** triggered the **{label}** threshold.\n"
            f"Last target: **{target}**",
            color=ERROR_COLOR,
            fields=fields,
            dm_owner=True,
        )

        if data.get("antinuke_punish"):
            await self._quarantine(guild, entry.user, label)

    # ------------------------------------------------------------ raidmode

    def _raid_embed(self, guild: discord.Guild) -> discord.Embed:
        data = self.bot.db.guild(guild.id)
        active = bool(data.get("raid_mode"))
        started = data.get("raid_started_at")
        started_text = "—"
        if started:
            try:
                started_text = discord.utils.format_dt(
                    discord.utils.parse_time(started), "R"
                )
            except (TypeError, ValueError):
                started_text = str(started)
        embed = discord.Embed(
            title="🚨 Raid mode",
            color=ERROR_COLOR if active else SUCCESS_COLOR,
        )
        embed.add_field(
            name="Status", value="**ON**" if active else "Off", inline=True
        )
        embed.add_field(name="Started", value=started_text, inline=True)
        embed.add_field(
            name="Reason",
            value=truncate(str(data.get("raid_reason") or "—"), 1024),
            inline=False
        )
        embed.add_field(
            name="Current verification",
            value=f"`{guild.verification_level.name}`",
            inline=True,
        )
        embed.add_field(
            name="Content filter",
            value=f"`{guild.explicit_content_filter.name}`",
            inline=True,
        )
        embed.add_field(
            name="Anti-nuke",
            value=(
                f"`{'on' if data.get('antinuke_enabled', True) else 'off'}`"
                f" • punish `{'on' if data.get('antinuke_punish') else 'off'}`"
            ),
            inline=True,
        )
        embed.set_footer(
            text="Raid mode disables invites, raises verification and filters all members."
        )
        return embed

    @commands.hybrid_group(
        name="raidmode",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def raidmode(self, ctx: commands.Context) -> None:
        """Lock invites, slow joins and alert staff during a raid."""
        await ctx.reply(
            embed=self._raid_embed(ctx.guild), mention_author=False
        )

    @raidmode.command(name="on")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_guild=True)
    @discord.app_commands.describe(reason="Why raid mode is being enabled")
    async def raid_on(
        self, ctx: commands.Context, *, reason: str = "Manual activation"
    ) -> None:
        """Enable raid mode: disable invites and raise join requirements."""
        data = self.bot.db.guild(ctx.guild.id)
        if data.get("raid_mode"):
            await ctx.reply(
                embed=info_embed("Raid mode is already enabled."),
                mention_author=False,
            )
            return

        previous_verification = ctx.guild.verification_level.value
        previous_filter = ctx.guild.explicit_content_filter.value
        try:
            await ctx.guild.edit(
                invites_disabled=True,
                verification_level=discord.VerificationLevel.highest,
                explicit_content_filter=discord.ContentFilter.all_members,
                reason=(
                    f"Raid mode enabled by {ctx.author} "
                    f"({ctx.author.id}): {reason}"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not enable raid mode: {exc}"),
                mention_author=False,
            )
            return

        data["raid_mode"] = True
        data["raid_prev_verification"] = previous_verification
        data["raid_prev_content_filter"] = previous_filter
        data["raid_prev_invites_disabled"] = False
        data["raid_started_at"] = discord.utils.utcnow().isoformat()
        data["raid_reason"] = reason
        await self.bot.db.save()

        await self._alert(
            ctx.guild,
            "🚨 Raid mode enabled",
            "Invites are disabled, verification is set to **highest**, and the "
            "content filter is set to **all members**.",
            color=ERROR_COLOR,
            fields=[
                ("By", ctx.author.mention, True),
                ("Reason", truncate(reason, 500), False),
            ],
            dm_owner=True,
        )
        await ctx.reply(
            embed=success_embed(
                "Raid mode is **on**.\n"
                "• New invites are disabled\n"
                "• Verification level is **highest**\n"
                "• Content filter is **all members**\n\n"
                f"Turn it off with `{ctx.prefix}raidmode off`.",
                title="🚨 Raid mode enabled",
            ),
            mention_author=False,
        )

    @raidmode.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_guild=True)
    async def raid_off(self, ctx: commands.Context) -> None:
        """Disable raid mode and restore the previous join settings."""
        data = self.bot.db.guild(ctx.guild.id)
        if not data.get("raid_mode"):
            await ctx.reply(
                embed=info_embed("Raid mode is not enabled."),
                mention_author=False,
            )
            return

        previous_verification = data.get("raid_prev_verification")
        previous_filter = data.get("raid_prev_content_filter")
        try:
            await ctx.guild.edit(
                invites_disabled=bool(data.get("raid_prev_invites_disabled", False)),
                verification_level=(
                    discord.VerificationLevel(previous_verification)
                    if previous_verification is not None
                    else ctx.guild.verification_level
                ),
                explicit_content_filter=(
                    discord.ContentFilter(previous_filter)
                    if previous_filter is not None
                    else ctx.guild.explicit_content_filter
                ),
                reason=f"Raid mode disabled by {ctx.author} ({ctx.author.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not disable raid mode: {exc}"),
                mention_author=False,
            )
            return

        data["raid_mode"] = False
        data["raid_started_at"] = None
        data["raid_reason"] = None
        await self.bot.db.save()

        await self._alert(
            ctx.guild,
            "✅ Raid mode disabled",
            "Invites and join requirements were restored.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "Raid mode is **off**. Invites and verification settings were restored.",
                title="✅ Raid mode disabled",
            ),
            mention_author=False,
        )

    @raidmode.command(name="status")
    @commands.guild_only()
    async def raid_status(self, ctx: commands.Context) -> None:
        """Show the current raid mode state."""
        await ctx.reply(
            embed=self._raid_embed(ctx.guild), mention_author=False
        )

    # ----------------------------------------------------------- antinuke

    def _antinuke_embed(self, guild: discord.Guild) -> discord.Embed:
        data = self.bot.db.guild(guild.id)
        enabled = data.get("antinuke_enabled", True)
        punish = data.get("antinuke_punish", False)
        me = guild.me
        audit_ok = bool(me and me.guild_permissions.view_audit_log)
        embed = discord.Embed(
            title="🚨 Anti-nuke protection",
            color=ERROR_COLOR if enabled else SUCCESS_COLOR,
        )
        embed.add_field(
            name="Monitoring", value="**ON**" if enabled else "Off", inline=True
        )
        embed.add_field(
            name="Auto-quarantine",
            value="**ON**" if punish else "Off",
            inline=True,
        )
        embed.add_field(
            name="Audit log access",
            value="✅ yes" if audit_ok else "❌ missing **View Audit Log**",
            inline=True,
        )
        embed.add_field(
            name="Thresholds",
            value=(
                "• **5 bans** in 10s\n"
                "• **3 channel deletes** in 10s\n"
                "• **3 role deletes** in 10s\n"
                "• **3 webhook creations** in 10s"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Auto-quarantine removes all removable roles from the executor."
        )
        return embed

    @commands.hybrid_group(
        name="antinuke",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def antinuke(self, ctx: commands.Context) -> None:
        """Detect mass bans, channel/role deletes and webhook creation."""
        await ctx.reply(
            embed=self._antinuke_embed(ctx.guild), mention_author=False
        )

    @antinuke.command(name="on")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def antinuke_on(self, ctx: commands.Context) -> None:
        """Enable anti-nuke monitoring."""
        data = self.bot.db.guild(ctx.guild.id)
        data["antinuke_enabled"] = True
        await self.bot.db.save()
        await self._alert(
            ctx.guild,
            "🚨 Anti-nuke enabled",
            "Monitoring mass bans, channel deletes, role deletes and webhook creation.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "Anti-nuke monitoring is **on**. Alerts go to the bot log channel.",
                title="🚨 Anti-nuke enabled",
            ),
            mention_author=False,
        )

    @antinuke.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def antinuke_off(self, ctx: commands.Context) -> None:
        """Disable anti-nuke monitoring."""
        data = self.bot.db.guild(ctx.guild.id)
        data["antinuke_enabled"] = False
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                "Anti-nuke monitoring is **off**.",
                title="🚨 Anti-nuke disabled",
            ),
            mention_author=False,
        )

    @antinuke.command(name="punish")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @discord.app_commands.describe(
        enabled="Turn auto-quarantine on or off"
    )
    async def antinuke_punish(
        self, ctx: commands.Context, enabled: bool
    ) -> None:
        """Turn automatic role removal on or off when a nuke is detected."""
        if enabled:
            confirmed = await ConfirmView.prompt(
                ctx,
                "Enable auto-quarantine?\n\nWhen the threshold is hit, I will "
                "remove **all removable roles** from the executor and DM them. "
                "This can affect a compromised staff account.",
                confirm_label="Enable quarantine",
            )
            if not confirmed:
                await self._reply(
                    ctx,
                    embed=info_embed("Auto-quarantine was not enabled."),
                    mention_author=False,
                )
                return

        data = self.bot.db.guild(ctx.guild.id)
        data["antinuke_punish"] = bool(enabled)
        await self.bot.db.save()
        await self._alert(
            ctx.guild,
            "🚨 Anti-nuke auto-quarantine changed",
            f"Auto-quarantine is now **{'on' if enabled else 'off'}**.",
            color=WARNING_COLOR if enabled else SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await self._reply(
            ctx,
            embed=success_embed(
                f"Auto-quarantine is now **{'on' if enabled else 'off'}**.",
                title="🚨 Anti-nuke setting saved",
            ),
            mention_author=False,
        )

    @antinuke.command(name="status")
    @commands.guild_only()
    async def antinuke_status(self, ctx: commands.Context) -> None:
        """Show anti-nuke settings and thresholds."""
        await ctx.reply(
            embed=self._antinuke_embed(ctx.guild), mention_author=False
        )

    # ---------------------------------------------------------- scamfilter

    def _scam_embed(self, guild: discord.Guild) -> discord.Embed:
        data = self.bot.db.guild(guild.id)
        enabled = data.get("scam_filter_enabled", True)
        custom = data.get("scam_filter_domains", []) or []
        embed = discord.Embed(
            title="🛡️ Scam link and invite filter",
            color=ERROR_COLOR if enabled else SUCCESS_COLOR,
        )
        embed.add_field(
            name="Status", value="**ON**" if enabled else "Off", inline=True
        )
        embed.add_field(
            name="Custom domains", value=f"`{len(custom)}`", inline=True
        )
        embed.add_field(
            name="Built-in domains", value=f"`{len(BUILTIN_BLOCKED_DOMAINS)}`", inline=True
        )
        embed.add_field(
            name="Also blocks",
            value=(
                "• Discord invites\n"
                "• Discord gift links\n"
                "• IP-logger and shortener domains\n"
                "• Raw IP-address links"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Staff and whitelisted members are exempt; bots and webhooks are ignored."
        )
        return embed

    @commands.hybrid_group(
        name="scamfilter",
        aliases=["scam"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def scamfilter(self, ctx: commands.Context) -> None:
        """Delete suspicious links and invites, then alert staff."""
        await ctx.reply(
            embed=self._scam_embed(ctx.guild), mention_author=False
        )

    @scamfilter.command(name="on")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def scam_on(self, ctx: commands.Context) -> None:
        """Enable the scam link/invite filter."""
        data = self.bot.db.guild(ctx.guild.id)
        data["scam_filter_enabled"] = True
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                "The scam filter is **on**. Suspicious links will be deleted "
                "and staff will get a log entry.",
                title="🛡️ Scam filter enabled",
            ),
            mention_author=False,
        )

    @scamfilter.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def scam_off(self, ctx: commands.Context) -> None:
        """Disable the scam link/invite filter."""
        data = self.bot.db.guild(ctx.guild.id)
        data["scam_filter_enabled"] = False
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                "The scam filter is **off**. Links will no longer be deleted.",
                title="🛡️ Scam filter disabled",
            ),
            mention_author=False,
        )

    @scamfilter.command(name="add", usage="<domain>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(domain="Domain to block, e.g. example-scam.com")
    async def scam_add(self, ctx: commands.Context, domain: str) -> None:
        """Add a custom domain to the blocklist."""
        normalized = self._normalize_domain(domain)
        if normalized is None:
            await ctx.reply(
                embed=error_embed(
                    "Use a domain like `example.com` (no path or spaces)."
                ),
                mention_author=False,
            )
            return
        data = self.bot.db.guild(ctx.guild.id)
        domains = list(data.get("scam_filter_domains", []) or [])
        if normalized in domains:
            await ctx.reply(
                embed=info_embed(f"`{normalized}` is already blocked."),
                mention_author=False,
            )
            return
        if len(domains) >= 100:
            await ctx.reply(
                embed=error_embed("The custom blocklist is limited to 100 domains."),
                mention_author=False,
            )
            return
        domains.append(normalized)
        data["scam_filter_domains"] = domains
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"Added `{normalized}` to the scam blocklist.",
                title="🛡️ Domain blocked",
            ),
            mention_author=False,
        )

    @scamfilter.command(name="remove", usage="<domain>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def scam_remove(self, ctx: commands.Context, domain: str) -> None:
        """Remove a custom domain from the blocklist."""
        normalized = self._normalize_domain(domain)
        data = self.bot.db.guild(ctx.guild.id)
        domains = list(data.get("scam_filter_domains", []) or [])
        if normalized is None or normalized not in domains:
            await ctx.reply(
                embed=info_embed("That domain is not in the custom blocklist."),
                mention_author=False,
            )
            return
        domains.remove(normalized)
        data["scam_filter_domains"] = domains
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"Removed `{normalized}` from the custom blocklist.",
                title="🛡️ Domain unblocked",
            ),
            mention_author=False,
        )

    @scamfilter.command(name="list")
    @commands.guild_only()
    async def scam_list(self, ctx: commands.Context) -> None:
        """Show custom blocked domains."""
        data = self.bot.db.guild(ctx.guild.id)
        custom = list(data.get("scam_filter_domains", []) or [])
        if not custom:
            await ctx.reply(
                embed=info_embed(
                    "No custom domains are blocked yet.\n"
                    f"Add one with `{ctx.prefix}scamfilter add example.com`."
                ),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=info_embed(
                "\n".join(f"• `{domain}`" for domain in custom[:50])
                + (
                    f"\n… and {len(custom) - 50} more."
                    if len(custom) > 50
                    else ""
                ),
                title=f"🛡️ Custom blocked domains ({len(custom)})",
            ),
            mention_author=False,
        )

    @scamfilter.command(name="status")
    @commands.guild_only()
    async def scam_status(self, ctx: commands.Context) -> None:
        """Show the scam filter status."""
        await ctx.reply(
            embed=self._scam_embed(ctx.guild), mention_author=False
        )

    @staticmethod
    def _normalize_domain(value: str) -> str | None:
        raw = value.strip().lower()
        if "://" in raw:
            raw = urlparse(raw).hostname or ""
        raw = raw.split("/")[0].split("?")[0].strip(".")
        if raw.startswith("www."):
            raw = raw[4:]
        return raw if DOMAIN_RE.fullmatch(raw) else None

    @staticmethod
    def _blocked_host(host: str, data: dict) -> bool:
        host = host.lower().strip(".")
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        custom = {str(domain).lower() for domain in data.get("scam_filter_domains", []) or []}
        for domain in BUILTIN_BLOCKED_DOMAINS | custom:
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def _scam_reason(self, content: str, data: dict) -> str | None:
        if INVITE_RE.search(content):
            return "Discord invite"
        if GIFT_RE.search(content):
            return "Discord gift link"
        for url in URL_RE.findall(content):
            host = urlparse(url).hostname
            if host and self._blocked_host(host, data):
                return f"Blocked link: `{host}`"
        return None

    def _security_embed(self, guild: discord.Guild) -> discord.Embed:
        data = self.bot.db.guild(guild.id)
        channel = self._security_channel(guild)
        logs = self.bot.get_cog("Logs")
        fallback = logs.channel_for(guild) if logs is not None else None
        age = data.get("min_account_age_days") or 0
        target = (
            channel.mention
            if channel
            else (fallback.mention if fallback else "not set")
        )
        embed = info_embed(
            f"**Security alerts:** {target}\n"
            f"**Fallback log channel:** "
            f"{fallback.mention if fallback else 'not set'}\n\n"
            "Raid mode, anti-nuke and scam-filter alerts go here. "
            "Normal moderation logs still use `/logs channel`, so you can "
            "point the two at different channels.",
            title="🛡️ Security alert channel",
        )
        embed.add_field(
            name="Raid mode",
            value="on" if data.get("raid_mode") else "off",
            inline=True,
        )
        embed.add_field(
            name="Anti-nuke",
            value=(
                f"{'on' if data.get('antinuke_enabled', True) else 'off'}"
                f" • punish {'on' if data.get('antinuke_punish') else 'off'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Scam filter",
            value="on" if data.get("scam_filter_enabled", True) else "off",
            inline=True,
        )
        embed.add_field(
            name="Account age",
            value=f"{age} days" if age else "off",
            inline=True,
        )
        return embed

    @commands.hybrid_group(
        name="security",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def security(self, ctx: commands.Context) -> None:
        """Choose where security and moderation alerts are posted."""
        await ctx.reply(
            embed=self._security_embed(ctx.guild), mention_author=False
        )

    @security.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def security_channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Send security alerts to a channel separate from the log channel."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return
        self.bot.db.guild(ctx.guild.id)["security_channel_id"] = channel.id
        await self.bot.db.save()
        await self._alert(
            ctx.guild,
            "🛡️ Security alert channel set",
            f"Raid mode, anti-nuke and scam-filter alerts will be sent to {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Security alerts will be sent to {channel.mention}.\n"
                "Normal moderation logs still use `/logs channel`.",
                title="🛡️ Security channel set",
            ),
            mention_author=False,
        )

    @security.command(name="clear", aliases=["disable", "reset"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def security_clear(self, ctx: commands.Context) -> None:
        """Use the bot log channel for security alerts again."""
        self.bot.db.guild(ctx.guild.id)["security_channel_id"] = None
        await self.bot.db.save()
        await self._alert(
            ctx.guild,
            "🛡️ Security channel cleared",
            "Security alerts will use the bot log channel again.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "Security alerts will use the bot log channel again."
            ),
            mention_author=False,
        )

    @security.command(name="status")
    @commands.guild_only()
    async def security_status(self, ctx: commands.Context) -> None:
        """Show security alert routing and protection status."""
        await ctx.reply(
            embed=self._security_embed(ctx.guild), mention_author=False
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        data = self.bot.db.guild(message.guild.id)
        if not data.get("scam_filter_enabled", True):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if self._is_security_staff(member, data) or is_whitelisted(member, data):
            return
        if not message.content:
            return

        reason = self._scam_reason(message.content, data)
        if reason is None:
            return

        try:
            await message.delete(reason=f"Scam filter: {reason}")
        except discord.NotFound:
            return
        except discord.Forbidden:
            await self._alert(
                message.guild,
                "🛡️ Scam link could not be removed",
                (
                    f"I need **Manage Messages** to delete a message from "
                    f"{message.author.mention} in {message.channel.mention}."
                ),
                color=ERROR_COLOR,
            )
            return
        except discord.HTTPException:
            return

        await self._alert(
            message.guild,
            "🛡️ Scam link removed",
            (
                f"**Author:** {message.author.mention} (`{message.author.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Reason:** {reason}"
            ),
            color=ERROR_COLOR,
            fields=[("Message", truncate(message.content, 1000), False)],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Security(bot))
