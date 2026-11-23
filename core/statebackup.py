"""Keep the JSON database alive on hosts that have no persistent disk.

Free hosts rebuild the container on every deploy or restart, which wipes
``data/settings.json``. That file is where the bot remembers everything a
server configured — the log channel, temp-voice setup, prefixes, tickets,
vouches, warnings — so after such a restart logs stop arriving and temporary
rooms stop being created, even though the bot itself is online.

This module mirrors the file into a direct message with the bot's owner: one
message holding ``settings.json``, replaced shortly after every save, and
downloaded again on startup when the local file is missing or empty. DMs need
no extra permissions and cost nothing, so the state survives any host.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path

import discord

log = logging.getLogger(__name__)

FILENAME = "settings.json"
DEBOUNCE_SECONDS = 8.0
HEADER = "Marketplace Guard state backup — keep this message, the bot restores from it."


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
    """Mirror ``settings.json`` between the disk and the owner's DMs."""

    def __init__(self, bot: discord.Client, path: Path) -> None:
        self.bot = bot
        self.path = Path(path)
        self._channel: discord.DMChannel | None = None
        self._message_id: int | None = None
        self._digest: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- helpers

    async def _owner_dm(self) -> discord.DMChannel | None:
        if self._channel is not None:
            return self._channel
        try:
            info = await self.bot.application_info()
            owner = info.owner
            if owner is None:
                return None
            self._channel = owner.dm_channel or await owner.create_dm()
        except (discord.HTTPException, AttributeError) as exc:
            log.warning("State backup: cannot reach the owner's DMs: %s", exc)
            return None
        return self._channel

    # ------------------------------------------------------------- restoring

    async def restore(self) -> bool:
        """Replace the empty local file with the newest copy from the DMs."""
        channel = await self._owner_dm()
        if channel is None:
            return False
        me = self.bot.user.id if self.bot.user else 0
        try:
            async for message in channel.history(limit=25):
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
                    self._message_id = message.id
                    self._digest = hashlib.sha256(raw).hexdigest()
                    log.info(
                        "Restored state from the owner DM (%d guild(s))",
                        len(data["guilds"]),
                    )
                    return True
        except discord.HTTPException as exc:
            log.warning("State backup: could not read the owner DM: %s", exc)
        return False

    # --------------------------------------------------------------- pushing

    async def push(self) -> None:
        """Send the current file to the DMs unless it is already up to date."""
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
            channel = await self._owner_dm()
            if channel is None:
                return

            if self._message_id is not None:
                try:
                    old = await channel.fetch_message(self._message_id)
                    await old.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as exc:
                    log.debug("State backup: could not delete the old copy: %s", exc)
                self._message_id = None

            try:
                sent = await channel.send(
                    content=HEADER,
                    file=discord.File(io.BytesIO(raw), filename=FILENAME),
                )
            except discord.HTTPException as exc:
                log.warning("State backup: could not upload the state: %s", exc)
                return
            self._message_id = sent.id
            self._digest = digest
            log.info("State backed up to the owner DM (%d bytes)", len(raw))

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
