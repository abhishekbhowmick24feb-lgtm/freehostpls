"""Store banner images/GIFs locally and show them on panels.

Discord's proxy turns an animated GIF into a still frame when an embed points
at an outside URL, and copied CDN links expire. Uploading the file to us once
and attaching it to the panel message keeps GIFs animated and banners
permanent, so stored banners are kept as ``media:<filename>``.

GIF page links (Klipy, Giphy, or any page with an ``og:image``) are resolved to
the real media file and downloaded too, so pasting a share link behaves like
attaching the GIF.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

MEDIA_DIR = Path(__file__).resolve().parent.parent / "data" / "media"
MEDIA_PREFIX = "media:"
MAX_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CLEAR_WORDS = {"none", "clear", "reset", "remove"}

_MISSING_WARNED: set[str] = set()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
PAGE_LIMIT = 512 * 1024
# Tenor's CDN returns an HTML page when the client prefers text/html, so pages
# and media ask for different things.
PAGE_ACCEPT = "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.7"
IMAGE_ACCEPT = "image/avif,image/webp,image/gif,image/*;q=0.9,*/*;q=0.8"
KLIPY_API = "https://api.klipy.com/api/v1/web/gifs/items?slugs={}"
KLIPY_RE = re.compile(r"klipy\.com/gifs/([A-Za-z0-9_-]+)", re.I)
GIPHY_RE = re.compile(
    r"giphy\.com/(?:gifs|stickers)/(?:.*-)?([A-Za-z0-9]+)(?:[/?#]|$)", re.I
)
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']"
    r"(?:og:image(?::secure_url)?|twitter:image(?::src)?)[\"'][^>]*>",
    re.I,
)
_META_REVERSED_RE = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']"
    r"(?:og:image(?::secure_url)?|twitter:image(?::src)?)[\"']",
    re.I,
)
_CONTENT_RE = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)
_IMAGE_SRC_RE = re.compile(
    r"<link[^>]+rel=[\"']image_src[\"'][^>]+href=[\"']([^\"']+)[\"']", re.I
)
_MEDIA_URL_RE = re.compile(
    r"https://(?:media\d*\.tenor\.com|i\.giphy\.com|media\.giphy\.com|"
    r"static\d*\.klipy\.com)/[^\"'\s\\<>]+?\.(?:gif|webp)",
    re.I,
)


def is_image(attachment: discord.Attachment) -> bool:
    suffix = Path(attachment.filename or "").suffix.lower()
    return suffix in IMAGE_SUFFIXES or (attachment.content_type or "").startswith(
        "image/"
    )


def uploaded_image(
    ctx: commands.Context, attachment: Optional[discord.Attachment] = None
) -> Optional[discord.Attachment]:
    """The attachment option, or the first image/GIF attached to the message."""
    if attachment is not None:
        return attachment if is_image(attachment) else None
    message = getattr(ctx, "message", None)
    for item in getattr(message, "attachments", None) or []:
        if is_image(item):
            return item
    return None


def _store_bytes(data: bytes, suffix: str) -> str:
    name = f"{hashlib.sha256(data).hexdigest()[:20]}{suffix}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    return MEDIA_PREFIX + name


async def store_upload(attachment: discord.Attachment) -> str:
    """Save an uploaded image/GIF and return the value to keep in the database."""
    if not is_image(attachment):
        raise ValueError("That file is not an image or GIF.")
    if attachment.size > MAX_BYTES:
        raise ValueError(
            f"That file is too big ({attachment.size / 1024 / 1024:.1f} MB). "
            "The limit is 8 MB."
        )
    suffix = Path(attachment.filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    data = await attachment.read()
    if len(data) > MAX_BYTES:
        raise ValueError("That file is too big. The limit is 8 MB.")
    return _store_bytes(data, suffix)


def is_direct_image(url: str) -> bool:
    """True when the URL itself points at an image file."""
    return Path(urlparse(url).path).suffix.lower() in IMAGE_SUFFIXES


def _sniff_suffix(data: bytes) -> str | None:
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    limit: int,
    *,
    truncate: bool = False,
    accept: str = IMAGE_ACCEPT,
) -> bytes:
    """Download a URL, giving up as soon as it passes ``limit`` bytes.

    The body is read in chunks instead of all at once, so an oversized file
    is rejected after about one chunk past the limit and never held in memory.
    With ``truncate`` the first ``limit`` bytes are returned instead (pages).
    """
    too_big = ValueError("That file is too big. The limit is 8 MB.")
    chunks: list[bytes] = []
    total = 0
    async with session.get(
        url, allow_redirects=True, headers={"Accept": accept}
    ) as resp:
        if resp.status != 200:
            raise ValueError(
                f"That link answered HTTP {resp.status}. Attach the GIF instead."
            )
        length = resp.content_length
        if not truncate and length is not None and length > limit:
            raise too_big
        async for chunk in resp.content.iter_chunked(64 * 1024):
            if total + len(chunk) > limit:
                if not truncate:
                    raise too_big
                chunks.append(chunk[: limit - total])
                break
            chunks.append(chunk)
            total += len(chunk)
    return b"".join(chunks)


async def _klipy_link(session: aiohttp.ClientSession, slug: str) -> str | None:
    async with session.get(KLIPY_API.format(quote(slug))) as resp:
        if resp.status != 200:
            return None
        payload = await resp.json(content_type=None)
    items = ((payload or {}).get("data") or {}).get("data") or []
    if not items:
        return None
    files = items[0].get("file") or {}
    sizes = [files.get(size) or {} for size in ("hd", "md", "sm", "xs")]
    for entry in sizes:
        gif = entry.get("gif") or {}
        if gif.get("url") and (gif.get("size") or 0) <= MAX_BYTES:
            return gif["url"]
    for entry in sizes:
        link = (entry.get("gif") or {}).get("url")
        if link:
            return link
    return None


def _page_image(page: str) -> str | None:
    for match in _META_RE.finditer(page):
        content = _CONTENT_RE.search(match.group(0))
        if content:
            link = html.unescape(content.group(1))
            if is_direct_image(link):
                return link
    for match in _META_REVERSED_RE.finditer(page):
        link = html.unescape(match.group(1))
        if is_direct_image(link):
            return link
    for match in _IMAGE_SRC_RE.finditer(page):
        link = html.unescape(match.group(1))
        if is_direct_image(link):
            return link
    fallback = _MEDIA_URL_RE.search(page)
    return html.unescape(fallback.group(0)) if fallback else None


async def _image_link(session: aiohttp.ClientSession, url: str) -> str:
    """The direct image file behind a URL, downloading page metadata if needed."""
    if is_direct_image(url):
        return url
    klipy = KLIPY_RE.search(url)
    if klipy:
        link = await _klipy_link(session, klipy.group(1))
        if link:
            return link
        raise ValueError(
            "I could not find that Klipy GIF. Attach the GIF instead, or paste "
            "a direct .gif link."
        )
    giphy = GIPHY_RE.search(url)
    if giphy:
        return f"https://i.giphy.com/{giphy.group(1)}.gif"
    page = await _fetch(
        session, url, PAGE_LIMIT, truncate=True, accept=PAGE_ACCEPT
    )
    link = _page_image(page.decode("utf-8", "ignore"))
    if link:
        return link
    raise ValueError(
        "That page does not link to a GIF I can download. Attach the GIF "
        "instead, or paste a direct .gif link."
    )


async def store_remote_banner(url: str) -> str:
    """Download the GIF behind a share page/direct link and store it locally."""
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as session:
        link = await _image_link(session, url)
        data = await _fetch(session, link, MAX_BYTES)
    suffix = _sniff_suffix(data)
    if suffix is None:
        raise ValueError(
            "That link did not return an image. Attach the GIF instead, or "
            "paste a direct .gif link."
        )
    return _store_bytes(data, suffix)


def stored_name(value: str | None) -> str | None:
    if not value or not value.startswith(MEDIA_PREFIX):
        return None
    name = value[len(MEDIA_PREFIX) :].strip()
    return name or None


def stored_path(value: str | None) -> Path | None:
    """The file behind a stored banner value, or None when it is gone."""
    name = stored_name(value)
    if name is None:
        return None
    path = MEDIA_DIR / name
    if not path.is_file():
        if name not in _MISSING_WARNED:
            _MISSING_WARNED.add(name)
            log.warning("Stored banner %s is missing", name)
        return None
    return path


def banner_url(value: str | None) -> str | None:
    """The URL to hand to ``embed.set_image`` for a stored banner value.

    A stored banner whose file disappeared resolves to None so the embed drops
    the image instead of pointing at an attachment that is not being sent.
    """
    name = stored_name(value)
    if name is not None:
        return f"attachment://{name}" if stored_path(value) is not None else None
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def banner_file(value: str | None) -> discord.File | None:
    """A fresh file to upload alongside the embed, when the banner is stored."""
    path = stored_path(value)
    if path is None:
        return None
    return discord.File(path, filename=path.name)


def banner_files(value: str | None) -> list[discord.File]:
    """The banner upload for a panel message, or an empty list."""
    file = banner_file(value)
    return [file] if file else []


def banner_change(value: str) -> str:
    """One word for what a resolved banner value did: uploaded/updated/removed."""
    if value.startswith(MEDIA_PREFIX):
        return "uploaded"
    return "updated" if value else "removed"


async def banner_from_input(
    ctx: commands.Context,
    attachment: Optional[discord.Attachment] = None,
    url: str | None = None,
    *,
    required: bool = True,
) -> tuple[str, str | None]:
    """Resolve an uploaded file or a typed value into the banner to store.

    Returns ``(value, error)``. ``value`` is the string to save, where ``\"\"``
    means no banner; when ``error`` is set nothing should be changed. With
    ``required=False`` an empty input simply means "no banner". Share-page
    links are downloaded and stored like an attachment.
    """
    upload = uploaded_image(ctx, attachment)
    if upload is not None:
        try:
            return await store_upload(upload), None
        except ValueError as exc:
            return "", str(exc)
    value = (url or "").strip()
    if not value:
        if not required:
            return "", None
        return "", "Attach an image or GIF, or give a direct http(s) image URL."
    if value.lower() in CLEAR_WORDS:
        return "", None
    if not value.startswith(("http://", "https://")):
        return "", "That does not look like an image URL."
    try:
        return await store_remote_banner(value), None
    except aiohttp.ClientError:
        if is_direct_image(value):
            return value, None
        return "", "I could not reach that link. Attach the GIF instead."
    except ValueError as exc:
        if is_direct_image(value):
            return value, None
        return "", str(exc)
