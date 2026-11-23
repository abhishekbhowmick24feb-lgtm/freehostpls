"""Small async-safe JSON store for per-guild settings."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from core.config import DEFAULT_PREFIX

DEFAULT_SHOP: dict[str, Any] = {
    "channel_id": None,
    "message_id": None,
    "staff_role_id": None,
    "heading": {
        "title": "🛒 Marketplace",
        "description": (
            "Welcome to the marketplace! Browse the sections below.\n"
            "Open a ticket to purchase anything you see here."
        ),
        "image": "",
        "thumbnail": "",
        "footer": "Prices may change without notice • Open a ticket to purchase",
        "color": "#5865F2",
    },
    "sections": [],
}

DEFAULT_TICKETS: dict[str, Any] = {
    "channel_id": None,
    "message_id": None,
    "category_id": None,
    "staff_role_id": None,
    "ticket_log_channel_id": None,
    "counter": 0,
    "title": "🎟️ Buy something",
    "description": (
        "Click the button below to open a private ticket.\n"
        "Tell us **what you are buying** and **how many**, and our staff will handle the rest."
    ),
    "button_label": "Open a ticket",
    "image": "",
    "thumbnail": "",
    "footer": "Our staff will claim your ticket shortly",
    "color": "#5865F2",
    "open": {},
}

DEFAULT_VERIFICATION: dict[str, Any] = {
    "channel_id": None,
    "role_id": None,
    "message_id": None,
    "title": "✅ Verify to enter",
    "description": (
        "Click the **Verify** button below to unlock the server.\n\n"
        "By verifying you agree to follow the server rules."
    ),
    "button_label": "Verify",
    "image": "",
    "color": "#57F287",
}

DEFAULT_HONEYPOT: dict[str, Any] = {
    "channel_id": None,
    "message_id": None,
    "count": 0,
    "rejoin_invite": None,
}

DEFAULTS: dict[str, Any] = {
    "prefix": None,
    "log_channel_id": None,
    "mod_role_id": None,
    "raid_mode": False,
    "raid_prev_verification": None,
    "raid_prev_content_filter": None,
    "raid_prev_invites_disabled": None,
    "raid_started_at": None,
    "raid_reason": None,
    "antinuke_enabled": True,
    "antinuke_punish": False,
    "scam_filter_enabled": True,
    "scam_filter_domains": [],
    "min_account_age_days": 0,
    "modmail_category_id": None,
    "modmail_staff_role_id": None,
    "modmail_open": {},
    "modmail_counter": 0,
    "tempvoice_creator_id": None,
    "tempvoice_category_id": None,
    "tempvoice_rooms": {},
    "tempvoice_permanent": [],
    "counting_channel_id": None,
    "counting": {"next": 1, "day": None, "users": {}},
    "birthday_channel_id": None,
    "birthdays": {},
    "birthday_last_announced": None,
    "afk": {},
    "tags": {},
    "giveaway_channel_id": None,
    "giveaways": {},
    "giveaway_seq": 0,
    "security_channel_id": None,
    "buyer_role_id": None,
    "vouch_channel_id": None,
    "vouches": [],
    "vouch_seq": 0,
    "status_channel_id": None,
    "status_message_id": None,
    "status_history": [],
    "status_maintenance": None,
    "honeypot": DEFAULT_HONEYPOT,
    "whitelist": {"role_id": None, "role_ids": [], "user_ids": []},
    "verification": DEFAULT_VERIFICATION,
    "tickets": DEFAULT_TICKETS,
    "shop": DEFAULT_SHOP,
    "warnings": {},
    "warning_seq": 0,
}


def _merge(defaults: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Fill in any missing keys without clobbering existing values."""
    for key, value in defaults.items():
        if key not in current:
            current[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(current[key], dict):
            _merge(value, current[key])
    return current


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"guilds": {}}
        self._lock = asyncio.Lock()
        # Called after every write, so a backup can mirror the file off-disk.
        self.on_save: Callable[[], None] | None = None

    async def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
            except (json.JSONDecodeError, OSError):
                backup = self.path.with_suffix(".corrupt.json")
                try:
                    shutil.copy2(self.path, backup)
                except OSError:
                    pass
                self._data = {"guilds": {}}
        self._data.setdefault("guilds", {})

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        notify = self.on_save
        if notify is not None:
            try:
                notify()
            except Exception:  # a backup must never break a save
                pass

    def guild(self, guild_id: int) -> dict[str, Any]:
        store = self._data.setdefault("guilds", {})
        data = store.setdefault(str(guild_id), {})
        return _merge(DEFAULTS, data)

    async def get_prefix(self, guild_id: int) -> str:
        return self.guild(guild_id).get("prefix") or DEFAULT_PREFIX
