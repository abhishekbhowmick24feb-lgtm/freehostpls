"""Static configuration, paths and cosmetic constants."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "settings.json"

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEFAULT_PREFIX = (os.getenv("DEFAULT_PREFIX", "!").strip() or "!")

# Load order for cogs. Used by the owner reload command as well.
EXTENSIONS: tuple[str, ...] = (
    "cogs.logs",
    "cogs.security",
    "cogs.status",
    "cogs.utility",
    "cogs.moderation",
    "cogs.channels",
    "cogs.vouches",
    "cogs.agecheck",
    "cogs.verification",
    "cogs.honeypot",
    "cogs.whitelist",
    "cogs.marketplace",
    "cogs.tickets",
    "cogs.modmail",
    "cogs.tempvoice",
    "cogs.community",
    "cogs.games",
    "cogs.tags",
    "cogs.giveaways",
    "cogs.backup",
    "cogs.owner",
)

# Palette
BRAND_COLOR = 0x5865F2
SUCCESS_COLOR = 0x2ECC71
ERROR_COLOR = 0xED4245
WARNING_COLOR = 0xFEE75C
INFO_COLOR = 0x5865F2
OWNER_COLOR = 0xEB459E

FOOTER_TEXT = "Marketplace Guard"
