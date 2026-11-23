"""Marketplace Guard — a professional moderation, verification and shop bot."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from core.config import (
    BASE_DIR,
    DATA_DIR,
    DB_PATH,
    EXTENSIONS,
    LOG_DIR,
    STATE_BACKUP,
    TOKEN,
)
from core.database import Database
from core.statebackup import StateBackup, has_local_state
from core.utils import error_embed

log = logging.getLogger("bot")


def setup_logging() -> None:
    # Windows consoles default to cp1252, which cannot print emoji or fancy names.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


async def get_prefix(bot: "MarketplaceBot", message: discord.Message):
    if message.guild is None:
        return commands.when_mentioned(bot, message)
    prefix = await bot.db.get_prefix(message.guild.id)
    return commands.when_mentioned_or(prefix)(bot, message)


class MarketplaceBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the marketplace"
            ),
            status=discord.Status.online,
        )
        self.db = Database(DB_PATH)
        self.state = StateBackup(self, DB_PATH)
        self.launch_time = datetime.now(timezone.utc)
        self._telemetry_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        # A free host wipes the filesystem on every restart, so pull the last
        # saved settings back before any cog can read an empty database.
        if STATE_BACKUP:
            if not has_local_state(DB_PATH) and await self.state.restore():
                log.info("Settings restored from the owner's DMs")
            self.db.on_save = self.state.schedule
        await self.db.load()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        log.info("Loaded %d extensions", len(EXTENSIONS))

        # Small status file so the local Bot Control panel can show ping/uptime.
        self._telemetry_task = asyncio.create_task(self._write_telemetry())

        from cogs.giveaways import GiveawayEntryView
        from cogs.tempvoice import TempRoomView
        from cogs.tickets import TicketActionsView, TicketPanelView
        from cogs.verification import VerifyView

        self.add_view(VerifyView())
        self.add_view(TicketPanelView())
        self.add_view(TicketActionsView())
        self.add_view(GiveawayEntryView())
        self.add_view(TempRoomView())
        log.info(
            "Persistent views registered: verification, tickets, giveaways, "
            "tempvoice"
        )

        # Slash commands are registered per guild for instant updates. Clear any
        # previously registered global copies so Discord does not list duplicates.
        if self.application_id is None:
            log.info("Skipping application command sync (not logged in)")
        else:
            try:
                await self.http.bulk_upsert_global_commands(
                    self.application_id, []
                )
                log.info(
                    "Using guild-scoped slash commands only (global commands cleared)"
                )
            except discord.HTTPException as exc:
                log.warning("Could not clear global application commands: %s", exc)

    async def _write_telemetry(self) -> None:
        """Keep DATA_DIR/status.json fresh for the Windows control panel."""
        path = DATA_DIR / "status.json"
        tmp = path.with_name("status.json.tmp")
        while True:
            latency = self.latency
            online = self.is_ready() and not (
                isinstance(latency, float) and math.isnan(latency)
            )
            payload = {
                "ping_ms": round(latency * 1000) if online else None,
                "uptime": int(
                    (datetime.now(timezone.utc) - self.launch_time).total_seconds()
                ),
                "guilds": len(self.guilds),
                "commands": len(self.commands),
                "updated": time.time(),
            }
            try:
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                log.debug("Could not write the status file: %s", exc)
            await asyncio.sleep(10)

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (%s) • %d guild(s) • %d commands",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
            len(self.commands),
        )
        # Guild-scoped copies appear instantly instead of waiting for global propagation.
        for guild in self.guilds:
            await self.sync_guild_commands(guild)

    async def sync_guild_commands(self, guild: discord.Guild) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d slash commands to guild %s (%s)",
                len(synced),
                guild.name,
                guild.id,
            )
        except discord.HTTPException as exc:
            log.warning("Could not sync slash commands to guild %s: %s", guild.id, exc)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild_commands(guild)
        me = guild.me
        permissions = (
            me.guild_permissions if me is not None else discord.Permissions.none()
        )
        required = {
            "Manage Roles": permissions.manage_roles,
            "Manage Channels": permissions.manage_channels,
            "Manage Messages": permissions.manage_messages,
            "Kick Members": permissions.kick_members,
            "Ban Members": permissions.ban_members,
            "Moderate Members": permissions.moderate_members,
            "Embed Links": permissions.embed_links,
            "Attach Files": permissions.attach_files,
        }
        missing = [name for name, ok in required.items() if not ok]
        log.info(
            "Joined guild %s (%s) • %s members • missing permissions: %s",
            guild.name,
            guild.id,
            guild.member_count,
            ", ".join(missing) or "none",
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removed from guild %s (%s)", guild.name, guild.id)

    async def on_disconnect(self) -> None:
        log.warning("Disconnected from Discord, reconnecting…")

    async def on_resumed(self) -> None:
        log.info("Connection resumed")

    async def audit(self, guild: discord.Guild | None, **kwargs) -> bool:
        """Send an entry to the guild's configured log channel, if any."""
        if guild is None:
            return False
        cog = self.get_cog("Logs")
        if cog is None:
            return False
        return await cog.audit(guild, **kwargs)

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            return

        log.warning(
            "Command %s failed in %s: %s",
            ctx.command.qualified_name if ctx.command else "?",
            ctx.guild.id if ctx.guild else "DM",
            error,
        )
        if isinstance(error, commands.CommandOnCooldown):
            await self._reply_error(
                ctx, f"Slow down — try again in **{error.retry_after:.1f}s**."
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await self._reply_error(ctx, "This command only works inside a server.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            await self._reply_error(
                ctx, f"Missing **{error.param.name}**.\n**Usage:** `{usage}`"
            )
            return
        if isinstance(error, commands.BadArgument):
            await self._reply_error(ctx, str(error) or "That argument is invalid.")
            return
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            await self._reply_error(ctx, f"You need the **{perms}** permission.")
            return
        if isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            await self._reply_error(ctx, f"I need the **{perms}** permission to do that.")
            return
        if isinstance(error, commands.CheckFailure):
            await self._reply_error(
                ctx, str(error) or "You do not have permission to use this."
            )
            return
        if isinstance(error, discord.Forbidden):
            await self._reply_error(
                ctx, "Discord blocked that action — check my role position and permissions."
            )
            return
        if isinstance(error, discord.HTTPException):
            log.warning("Discord HTTP error: %s", error)
            await self._reply_error(ctx, f"Discord rejected the request: {error}")
            return

        log.error(
            "Unhandled error in command %s",
            ctx.command.qualified_name if ctx.command else "?",
            exc_info=error,
        )
        await self._reply_error(
            ctx, "An unexpected error occurred. The details are in the console log."
        )

    @staticmethod
    async def _reply_error(ctx: commands.Context, message: str) -> None:
        try:
            await ctx.reply(
                embed=error_embed(message), mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass


async def main() -> None:
    setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        # Never log the value, only where we looked and what we found.
        raw = os.environ.get("DISCORD_TOKEN")
        token_like = sorted(
            key
            for key in os.environ
            if "TOKEN" in key.upper() or key.upper().startswith("DISCORD")
        )
        log.error(
            "DISCORD_TOKEN is empty (cwd=%s, .env file present=%s, "
            "DISCORD_TOKEN set=%s with %d characters, token-like env keys: %s)",
            Path.cwd(),
            (BASE_DIR / ".env").is_file(),
            raw is not None,
            len(raw or ""),
            ", ".join(token_like) or "none",
        )
        log.error(
            "Locally: copy .env.example to .env and paste your token. "
            "On a host: add DISCORD_TOKEN to the service's environment variables "
            "(no quotes, no spaces) and redeploy."
        )
        return

    bot = MarketplaceBot()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN in your .env file.")
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")
