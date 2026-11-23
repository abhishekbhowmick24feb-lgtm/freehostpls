"""Live bot status panel: online/offline, uptime and ping."""

from __future__ import annotations

import logging
import math

import discord
from discord.ext import commands, tasks

from core.config import ERROR_COLOR, SUCCESS_COLOR
from core.utils import (
    ConfirmView,
    error_embed,
    fmt_duration,
    info_embed,
    success_embed,
)

log = logging.getLogger(__name__)

UPDATE_SECONDS = 30


class Status(commands.Cog):
    """Keep a live status panel in a channel chosen by staff."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._disconnect_alerted = False

    async def cog_load(self) -> None:
        self.status_loop.start()

    async def cog_unload(self) -> None:
        self.status_loop.cancel()

    # --------------------------------------------------------------- helpers

    def _config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _maintenance(self, guild: discord.Guild) -> dict | None:
        maintenance = self._config(guild).get("status_maintenance")
        if isinstance(maintenance, dict) and maintenance.get("reason"):
            return maintenance
        return None

    def _is_online(self) -> bool:
        if not self.bot.is_ready():
            return False
        latency = self.bot.latency
        return not (isinstance(latency, float) and math.isnan(latency))

    async def _record_sample(
        self,
        guild: discord.Guild,
        online: bool | None = None,
        ping: float | None = None,
    ) -> None:
        """Record one uptime/ping sample and keep a rolling 24-hour window."""
        config = self._config(guild)
        history = config.setdefault("status_history", [])
        now = discord.utils.utcnow().timestamp()

        if online is None:
            online = False if self._maintenance(guild) else self._is_online()
        if online and ping is None:
            latency = self.bot.latency
            ping = None if math.isnan(latency) else latency * 1000

        if history:
            last_t = history[-1].get("t", 0)
            # If the bot was not sampling for a while, count the gap as offline.
            if now - last_t > UPDATE_SECONDS * 3:
                offline_start = max(last_t + UPDATE_SECONDS, now - 86400)
                history.append(
                    {"t": offline_start, "online": False, "ping": None}
                )

        history.append(
            {
                "t": now,
                "online": bool(online),
                "ping": ping if online else None,
            }
        )
        cutoff = now - 86400
        config["status_history"] = [
            sample for sample in history if sample.get("t", 0) >= cutoff
        ]
        await self.bot.db.save()

    def _stats(self, guild: discord.Guild) -> tuple[float, float | None]:
        """Return time-weighted 24h uptime percentage and average ping."""
        history = self._config(guild).get("status_history") or []
        now = discord.utils.utcnow().timestamp()
        recent = [sample for sample in history if sample.get("t", 0) >= now - 86400]
        if not recent:
            online = self._is_online()
            latency = self.bot.latency * 1000 if online else None
            return (100.0 if online else 0.0, latency)

        start = recent[0].get("t", now)
        total = max(now - start, 0.0)
        online_time = 0.0
        pings: list[float] = []

        for index, sample in enumerate(recent):
            sample_start = sample.get("t", now)
            sample_end = (
                recent[index + 1].get("t", now)
                if index + 1 < len(recent)
                else now
            )
            duration = max(sample_end - sample_start, 0.0)
            if sample.get("online"):
                online_time += duration
                ping = sample.get("ping")
                if isinstance(ping, (int, float)):
                    pings.append(float(ping))

        uptime_pct = (online_time / total * 100.0) if total > 0 else 0.0
        avg_ping = (sum(pings) / len(pings)) if pings else None
        return uptime_pct, avg_ping

    def _status_embed(
        self,
        guild: discord.Guild,
        online: bool | None = None,
        disconnected: bool = False,
        maintenance: dict | None = None,
    ) -> discord.Embed:
        if maintenance is None:
            maintenance = self._maintenance(guild)
        if maintenance:
            online = False
        elif online is None:
            online = self._is_online()
        latency = self.bot.latency * 1000 if online else None
        uptime = discord.utils.utcnow() - self.bot.launch_time
        uptime_pct, avg_ping = self._stats(guild)
        embed = discord.Embed(
            title="🟢 Bot Online" if online else "🔴 Bot Offline",
            color=SUCCESS_COLOR if online else ERROR_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Status",
            value="🟢 Online" if online else "🔴 Offline",
            inline=True,
        )
        embed.add_field(
            name="Uptime",
            value=f"`{fmt_duration(int(uptime.total_seconds()))}`",
            inline=True,
        )
        embed.add_field(
            name="Ping",
            value=f"`{latency:.0f}ms`" if latency is not None else "`—`",
            inline=True,
        )
        embed.add_field(
            name="24h Uptime",
            value=f"`{uptime_pct:.1f}%`",
            inline=True,
        )
        embed.add_field(
            name="24h Avg Ping",
            value=f"`{avg_ping:.0f}ms`" if avg_ping is not None else "`—`",
            inline=True,
        )
        if maintenance:
            value = maintenance.get("reason") or "Maintenance"
            since = maintenance.get("since")
            if since:
                value += f"\nStarted <t:{int(since)}:R>"
            embed.add_field(name="Maintenance", value=value, inline=False)
        elif not online and disconnected:
            embed.add_field(
                name="Connection",
                value="Lost — reconnecting to Discord…",
                inline=False,
            )
        embed.set_footer(text=guild.name)
        return embed

    async def _channel(
        self, guild: discord.Guild, config: dict
    ) -> discord.TextChannel | None:
        channel_id = config.get("status_channel_id")
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _delete_panel(self, guild: discord.Guild, config: dict) -> None:
        channel = await self._channel(guild, config)
        message_id = config.get("status_message_id")
        if channel is None or not message_id:
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _update_panel(
        self,
        guild: discord.Guild,
        online: bool | None = None,
        disconnected: bool = False,
    ) -> bool:
        config = self._config(guild)
        channel = await self._channel(guild, config)
        if channel is None:
            return False

        maintenance = self._maintenance(guild)
        if maintenance:
            online = False
        elif online is None:
            online = self._is_online()

        embed = self._status_embed(
            guild,
            online=online,
            disconnected=disconnected,
            maintenance=maintenance,
        )
        message_id = config.get("status_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                message = None
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not fetch status message in guild %s: %s", guild.id, exc
                )
                return False
            if message is not None:
                try:
                    await message.edit(embed=embed)
                    return True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "Could not edit status message in guild %s: %s", guild.id, exc
                    )
                    return False

        try:
            message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not post status message in guild %s: %s", guild.id, exc)
            return False

        config["status_message_id"] = message.id
        await self.bot.db.save()
        return True

    async def _show(self, ctx: commands.Context) -> None:
        config = self._config(ctx.guild)
        channel = await self._channel(ctx.guild, config)
        maintenance = self._maintenance(ctx.guild)
        embed = info_embed(
            f"**Channel:** {channel.mention if channel else 'not set'}\n"
            f"**Panel:** {'posted' if config.get('status_message_id') else 'not posted'}\n"
            f"**Maintenance:** {maintenance['reason'] if maintenance else 'off'}\n"
            f"**Refresh:** every `{UPDATE_SECONDS}s`\n\n"
            f"Set it with `{ctx.prefix}status channel #channel`.\n"
            "The panel shows 🟢 online / 🔴 offline, uptime and ping.",
            title="📡 Bot status panel",
        )
        await ctx.reply(embed=embed, mention_author=False)

    # -------------------------------------------------------------- updater

    async def _refresh_all(self) -> None:
        for guild in list(self.bot.guilds):
            config = self._config(guild)
            if not config.get("status_channel_id"):
                continue
            try:
                await self._record_sample(guild)
                await self._update_panel(
                    guild, disconnected=self._disconnect_alerted
                )
            except Exception:
                log.exception("Status panel update failed for guild %s", guild.id)

    @tasks.loop(seconds=UPDATE_SECONDS)
    async def status_loop(self) -> None:
        await self._refresh_all()

    @status_loop.before_loop
    async def before_status_loop(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        """Turn the panel red as soon as the gateway connection drops."""
        if self._disconnect_alerted or self.bot.is_closed():
            return
        self._disconnect_alerted = True
        log.warning("Discord connection lost; posting red status alerts")

        for guild in list(self.bot.guilds):
            config = self._config(guild)
            if not config.get("status_channel_id"):
                continue
            try:
                await self._record_sample(guild, online=False)
                await self._update_panel(
                    guild, online=False, disconnected=True
                )
            except Exception:
                log.exception(
                    "Could not post disconnect alert for guild %s", guild.id
                )

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        """Turn the panel green again when the session resumes."""
        self._disconnect_alerted = False
        await self._refresh_all()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Refresh after a fresh login as well as a resume."""
        self._disconnect_alerted = False
        await self._refresh_all()

    # ------------------------------------------------------------- commands

    @commands.hybrid_group(
        name="status",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Live bot status panel: online/offline, uptime and ping."""
        await self._show(ctx)

    @status.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Choose where the live bot status panel is posted."""
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
        old_channel_id = config.get("status_channel_id")
        if old_channel_id and old_channel_id != channel.id:
            await self._delete_panel(ctx.guild, config)
            config["status_message_id"] = None

        config["status_channel_id"] = channel.id
        await self.bot.db.save()

        await self._record_sample(ctx.guild)
        if not await self._update_panel(ctx.guild):
            await ctx.reply(
                embed=error_embed(
                    f"I could not post the status panel in {channel.mention}."
                ),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="📡 Status panel set",
            description=f"The live bot status panel is now in {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"The live bot status panel is now in {channel.mention}.\n"
                f"It updates every `{UPDATE_SECONDS}s` with online state, uptime and ping.",
                title="📡 Status panel ready",
            ),
            mention_author=False,
        )

    @status.command(name="disable", aliases=["off"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        """Remove the status panel and stop updating it."""
        config = self._config(ctx.guild)
        if not config.get("status_channel_id"):
            await ctx.reply(
                embed=info_embed("The status panel is not set up."),
                mention_author=False,
            )
            return
        if not await ConfirmView.prompt(
            ctx, "Remove the bot status panel and stop updating it?"
        ):
            return

        await self._delete_panel(ctx.guild, config)
        config["status_channel_id"] = None
        config["status_message_id"] = None
        config["status_history"] = []
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="📡 Status panel removed",
            description="The live bot status panel was removed.",
            color=ERROR_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed("The status panel was removed."),
            mention_author=False,
        )

    @status.command(name="refresh", aliases=["update"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def refresh(self, ctx: commands.Context) -> None:
        """Update the status panel immediately."""
        config = self._config(ctx.guild)
        if not config.get("status_channel_id"):
            await ctx.reply(
                embed=info_embed(
                    f"The status panel is not set up. Use `{ctx.prefix}status channel #channel`."
                ),
                mention_author=False,
            )
            return
        if await self._update_panel(ctx.guild):
            await ctx.reply(
                embed=success_embed("The status panel was updated."),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=error_embed(
                    "I could not update the panel. Check the channel permissions."
                ),
                mention_author=False,
            )

    @status.command(
        name="offline",
        aliases=["maintenance", "maint"],
        usage="[reason]",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def offline(
        self, ctx: commands.Context, *, reason: str | None = None
    ) -> None:
        """Show the status panel as offline with an optional reason."""
        reason = (reason or "Maintenance").strip()
        config = self._config(ctx.guild)
        config["status_maintenance"] = {
            "reason": reason,
            "since": discord.utils.utcnow().timestamp(),
        }
        await self.bot.db.save()
        await self._record_sample(ctx.guild, online=False)
        updated = await self._update_panel(ctx.guild, online=False)

        await self.bot.audit(
            ctx.guild,
            title="📡 Status set offline",
            description=f"Maintenance mode enabled: {reason}",
            color=ERROR_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )

        text = (
            "The status panel is now **offline**.\n"
            f"**Reason:** {reason}\n"
            "Use `/status online` when maintenance is finished."
        )
        if not updated:
            if not config.get("status_channel_id"):
                text += (
                    "\nNo status channel is set yet; the panel will show this "
                    "state when you set one."
                )
            else:
                text += (
                    "\n⚠️ I saved maintenance mode but could not update the "
                    "panel; check the channel permissions."
                )
        await ctx.reply(
            embed=success_embed(text, title="📡 Status offline"),
            mention_author=False,
        )

    @status.command(name="online", aliases=["resume", "available"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def online(self, ctx: commands.Context) -> None:
        """Clear maintenance mode and show the real bot status."""
        config = self._config(ctx.guild)
        if not config.get("status_maintenance"):
            await ctx.reply(
                embed=info_embed("The status panel is not in maintenance mode."),
                mention_author=False,
            )
            return

        config["status_maintenance"] = None
        await self.bot.db.save()
        await self._record_sample(ctx.guild)
        updated = await self._update_panel(ctx.guild)

        await self.bot.audit(
            ctx.guild,
            title="📡 Status set online",
            description=(
                "Maintenance mode cleared; the panel reflects the real status again."
            ),
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )

        text = (
            "Maintenance mode cleared. The panel is showing the real bot status again."
        )
        if not updated:
            text += (
                "\n⚠️ I could not update the panel; check the channel permissions."
            )
        await ctx.reply(
            embed=success_embed(text, title="📡 Status online"),
            mention_author=False,
        )

    @status.command(name="show", aliases=["info"])
    @commands.guild_only()
    async def show(self, ctx: commands.Context) -> None:
        """Show the current status panel settings."""
        await self._show(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Status(bot))
