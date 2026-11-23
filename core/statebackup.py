"""Keep the JSON database alive on hosts that have no persistent disk.

Free hosts rebuild the container on every deploy or restart, which wipes
``data/settings.json``. That file is where the bot remembers everything a
server configured — the log channel, temp-voice setup, prefixes, tickets,
vouches, warnings — so after such a restart logs stop arriving and temporary
rooms stop being created, even though the bot itself is online.

This module mirrors the file into a private staff channel named
``#settings-backup``: one message holding ``settings.json``, replaced shortly
after every save and downloaded again on startup when the local file is
missing or empty. Nothing is sent to anybody's DMs.

The channel is found by name (case-insensitive) so no state has to survive the
wipe; set ``STATE_BACKUP_CHANNEL_ID`` to pin a specific channel instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path

import discord

from core.config import STATE_BACKUP_CHANNEL_ID

log = logging.getLogger(__name__)

FILENAME = "settings.json"
CHANNEL_NAME = "settings-backup"
DEBOUNCE_SECONDS = 8.0
HEADER = (
    "**Marketplace Guard state backup — keep this channel private.**\n"
    "The bot uploads `settings.json` here after every change and restores the "
    "newest copy on startup, so the log channel, temp-voice setup and every "
    "other setting survive a redeploy. Deleting this message deletes the saved "
    "settings."
)


def _parse_state(raw: bytes) -> dict | None:
    """The decoded database, or None when the bytes are not one."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("guilds"), dict):
        return None
    return data


def has_local_state(path: Path) -> bool:
    """True when the file on disk already holds at least one guild."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return False
    data = _parse_state(raw)
    return bool(data and data["guilds"])


class StateBackup:
    """Mirror ``settings.json`` between the disk and a private staff channel."""

    def __init__(self, bot: discord.Client, path: Path) -> None:
        self.bot = bot
        self.path = Path(path)
        self._channel: discord.TextChannel | None = None
        self._digest: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _is_backup_channel(channel: object) -> bool:
        return (
            isinstance(channel, discord.TextChannel)
            and channel.name.strip().lower() == CHANNEL_NAME
        )

    async def _candidate_channels(self) -> list[discord.TextChannel]:
        """Every text channel the bot can see, ready or not."""
        found: list[discord.TextChannel] = []
        seen: set[int] = set()

        def collect(items) -> None:
            for channel in items:
                if isinstance(channel, discord.TextChannel) and channel.id not in seen:
                    seen.add(channel.id)
                    found.append(channel)

        for guild in self.bot.guilds:
            collect(guild.text_channels)
        if found:
            return found

        # Called before the gateway is ready, so ask the REST API instead.
        try:
            async for guild in self.bot.fetch_guilds(limit=200):
                try:
                    collect(await guild.fetch_channels())
                except discord.HTTPException as exc:
                    log.debug(
                        "State backup: cannot list channels of %s: %s", guild.id, exc
                    )
        except discord.HTTPException as exc:
            log.warning("State backup: cannot list servers: %s", exc)
        return found

    async def _find_channel(self) -> discord.TextChannel | None:
        """The backup channel, by ID when pinned and otherwise by name."""
        if self._channel is not None:
            return self._channel

        if STATE_BACKUP_CHANNEL_ID:
            channel = self.bot.get_channel(STATE_BACKUP_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(STATE_BACKUP_CHANNEL_ID)
                except discord.HTTPException as exc:
                    log.warning(
                        "State backup: cannot open channel %s: %s",
                        STATE_BACKUP_CHANNEL_ID,
                        exc,
                    )
                    channel = None
            if isinstance(channel, discord.TextChannel):
                self._channel = channel
                return channel
            log.warning(
                "State backup: STATE_BACKUP_CHANNEL_ID=%s is not a text channel",
                STATE_BACKUP_CHANNEL_ID,
            )

        for channel in await self._candidate_channels():
            if self._is_backup_channel(channel):
                self._channel = channel
                return channel

        log.warning(
            "State backup: no #%s channel found — create a private channel with "
            "that exact name (View Channel, Send Messages, Attach Files, Read "
            "Message History for the bot) and restart to start saving settings.",
            CHANNEL_NAME,
        )
        return None

    @staticmethod
    def _is_public(channel: discord.TextChannel) -> bool:
        """True when @everyone can read the channel, i.e. it is not private."""
        guild = getattr(channel, "guild", None)
        default_role = getattr(guild, "default_role", None)
        if default_role is None:
            return False
        try:
            return bool(channel.permissions_for(default_role).read_messages)
        except Exception:  # permission maths must never break a backup
            return False

    async def _delete_old_copies(
        self, channel: discord.TextChannel, keep_id: int
    ) -> None:
        """Leave one backup message behind, even across restarts."""
        me = self.bot.user.id if self.bot.user else 0
        try:
            async for message in channel.history(limit=50):
                if message.id == keep_id or message.author.id != me:
                    continue
                if not any(
                    item.filename == FILENAME for item in message.attachments
                ):
                    continue
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
        except discord.HTTPException as exc:
            log.debug("State backup: could not tidy old copies: %s", exc)

    # ------------------------------------------------------------- restoring

    async def restore(self) -> bool:
        """Replace the empty local file with the newest copy from the channel."""
        channel = await self._find_channel()
        if channel is None:
            return False
        me = self.bot.user.id if self.bot.user else 0
        try:
            async for message in channel.history(limit=50):
                if message.author.id != me or not message.attachments:
                    continue
                for attachment in message.attachments:
                    if attachment.filename != FILENAME:
                        continue
                    raw = await attachment.read()
                    data = _parse_state(raw)
                    if data is None:
                        log.warning(
                            "State backup: attachment %s is not a valid database",
                            attachment.id,
                        )
                        continue
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_bytes(raw)
                    self._digest = hashlib.sha256(raw).hexdigest()
                    log.info(
                        "Restored state from #%s (%d server(s))",
                        channel.name,
                        len(data["guilds"]),
                    )
                    return True
        except discord.HTTPException as exc:
            log.warning("State backup: could not read #%s: %s", channel.name, exc)
        return False

    # --------------------------------------------------------------- pushing

    async def push(self) -> None:
        """Upload the current file unless the channel already holds it."""
        async with self._lock:
            try:
                raw = self.path.read_bytes()
            except OSError:
                return
            if _parse_state(raw) is None:
                return
            digest = hashlib.sha256(raw).hexdigest()
            if digest == self._digest:
                return
            channel = await self._find_channel()
            if channel is None:
                return
            if self._is_public(channel):
                log.warning(
                    "State backup: #%s is readable by @everyone — remove that "
                    "permission so only staff can see the saved settings.",
                    channel.name,
                )

            try:
                sent = await channel.send(
                    content=HEADER,
                    file=discord.File(io.BytesIO(raw), filename=FILENAME),
                )
            except discord.HTTPException as exc:
                log.warning("State backup: could not upload the state: %s", exc)
                return
            self._digest = digest
            log.info("State backed up to #%s (%d bytes)", channel.name, len(raw))
            # Old copies can exist after a restart; keep only the newest.
            await self._delete_old_copies(channel, sent.id)

    def schedule(self) -> None:
        """Ask for a push soon; coalesces bursts of saves into one upload."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._delayed_push())

    async def _delayed_push(self) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self.push()
        except asyncio.CancelledError:
            raise
        except Exception:  # a backup must never break the bot
            log.exception("State backup failed")
